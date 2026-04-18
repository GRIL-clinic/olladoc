"""Streamlit UI for olladoc. Run: `streamlit run app.py`"""

import io
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import requests
import streamlit as st

from translate import Translator, translate_docx, translate_pdf

OLLAMA_URL = "http://localhost:11434"
MENUBAR_LOG = Path.home() / ".ollama" / "logs" / "server.log"
OLLADOC_LOG = Path(tempfile.gettempdir()) / "olladoc_ollama.log"
OLLAMA_PID_FILE = Path(tempfile.gettempdir()) / "olladoc_ollama.pid"


def _active_log_path() -> Path | None:
    """Return whichever log file is currently in use (ours, then menubar)."""
    for p in (OLLADOC_LOG, MENUBAR_LOG):
        if p.exists():
            return p
    return None


st.set_page_config(page_title="olladoc", layout="centered",
                   initial_sidebar_state="collapsed")

# Hide the multipage sidebar nav so users can only reach the logs
# page via the dedicated logs link (which opens in a new tab).
st.markdown(
    """
    <style>
    [data-testid="stSidebarNav"] { display: none; }
    [data-testid="stSidebarCollapseButton"] { display: none; }
    [data-testid="stSidebar"] { display: none; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.dialog("About olladoc")
def _about_dialog():
    st.markdown(
        "**olladoc** translates `.pdf` and `.docx` files locally; no data "
        "leaves your machine. Powered by [Ollama](https://ollama.com) and "
        "Google's [TranslateGemma](https://blog.google/innovation-and-ai/"
        "technology/developers-tools/translategemma/) by default; any "
        "Ollama-compatible model works."
    )
    st.divider()
    st.markdown(
        "**New to Ollama?**\n\n"
        "**One-time setup**\n\n"
        "1. Install Ollama:\n"
        "   ```\n"
        "   brew install ollama\n"
        "   ```\n"
        "   …or download the macOS app from <https://ollama.com/download>.\n\n"
        "2. Pull the default translation model:\n"
        "   ```\n"
        "   ollama pull translategemma\n"
        "   ```\n\n"
        "**Usage**\n\n"
        "1. Click **Start** at the top of the page to launch Ollama "
        "(or launch the Ollama menubar app yourself).\n"
        "2. Upload one or more `.pdf` / `.docx` files.\n"
        "3. Pick the source and target languages, then click **Translate**.\n"
        "4. Translated files are saved to your chosen output folder."
    )


@st.dialog("Start Ollama first")
def _needs_ollama_dialog():
    st.markdown(
        "Ollama isn't running, so translations can't proceed. "
        "Click **Start** at the top of the page to launch it, then try "
        "again."
    )


title_col, about_col = st.columns([10, 1], vertical_alignment="center")
with title_col:
    st.title("olladoc")
with about_col:
    if st.button("✨", type="tertiary", key="about_btn",
                 help="About / setup help"):
        _about_dialog()
st.caption("Local `.pdf` / `.docx` translation via Ollama")

LANGS = list(Translator.GEMMA_LANG_CODES.keys())


def _pick_folder(initial: str | None) -> str | None:
    """Open a native folder picker. Runs the dialog out-of-process so the
    GUI event loop doesn't clash with Streamlit's."""
    initial = initial or str(Path.home())
    if platform.system() == "Darwin":
        script = (
            'tell application "System Events" to activate\n'
            f'set f to choose folder with prompt "Select output folder" '
            f'default location POSIX file "{initial}"\n'
            'POSIX path of f'
        )
        try:
            out = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            st.error(f"Folder picker failed: {e}")
            return None
        if out.returncode != 0:
            # User cancelled — osascript returns non-zero with "User canceled."
            return None
        return out.stdout.strip() or None

    # Non-macOS: run tkinter in a subprocess so it has its own main thread.
    code = (
        "import sys, tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
        f"p = filedialog.askdirectory(initialdir={initial!r}, "
        "title='Select output folder')\n"
        "sys.stdout.write(p or '')\n"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as e:
        st.error(f"Folder picker failed: {e}")
        return None
    return out.stdout.strip() or None


def _read_log_tail(path: Path, n: int = 50) -> str:
    """Return the last `n` lines of a log file, or an empty string."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                read = min(block, size)
                size -= read
                f.seek(size)
                data = f.read(read) + data
        lines = data.decode("utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except FileNotFoundError:
        return ""
    except Exception as e:
        return f"(could not read {path}: {e})"


def _ollama_status() -> tuple[bool, str | None]:
    """Returns (running, version_or_error_message)."""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/version", timeout=1)
        r.raise_for_status()
        return True, r.json().get("version", "?")
    except Exception:
        return False, None


def _pid_is_ollama(pid: int) -> bool:
    """True if `pid` is alive and its command line mentions `ollama`."""
    import os
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return False
    return "ollama" in out.stdout.lower()


def _managed_pid() -> int | None:
    """Read a previously saved PID from disk and validate it's still ollama."""
    try:
        pid = int(OLLAMA_PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    if _pid_is_ollama(pid):
        return pid
    OLLAMA_PID_FILE.unlink(missing_ok=True)
    return None


def _start_ollama() -> str | None:
    if not shutil.which("ollama"):
        return "`ollama` binary not found on PATH."
    try:
        log_fp = OLLADOC_LOG.open("wb")  # truncate so each Start = fresh log
        proc = subprocess.Popen(
            ["ollama", "serve"],
            stdout=log_fp, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        return str(e)
    OLLAMA_PID_FILE.write_text(str(proc.pid))
    # Wait briefly for the server to bind the port.
    import time
    for _ in range(20):
        time.sleep(0.25)
        if _ollama_status()[0]:
            return None
    return "Started, but server didn't respond on :11434 within 5s."


def _stop_ollama() -> str | None:
    pid = _managed_pid()
    if not pid:
        return "No managed Ollama process to stop."
    import os, signal
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception as e:
        return str(e)
    OLLAMA_PID_FILE.unlink(missing_ok=True)
    OLLADOC_LOG.unlink(missing_ok=True)
    return None


running, version = _ollama_status()
managed = _managed_pid() is not None
status_col, action_col = st.columns([3, 2], vertical_alignment="center")
with status_col:
    if running:
        st.markdown(
            "<a href='/Ollama_logs' target='_blank' title='Show logs' "
            "style='text-decoration:none; color:inherit;'>"
            "🔍&nbsp;&nbsp;<strong>Ollama:</strong> "
            "<span style='color:#09ab3b;'>running</span></a>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown("**Ollama:** :red[not running]")
with action_col:
    b1, b2 = st.columns(2)
    with b1:
        if st.button("Start", disabled=running, use_container_width=True,
                     key="start_btn"):
            err = _start_ollama()
            if err:
                st.error(err)
            else:
                st.rerun()
    with b2:
        # Only allow Stop for a process we started ourselves.
        can_stop = running and managed
        stop_help = (None if can_stop
                     else "Disabled: Ollama wasn't started by this app "
                          "(use the menubar app to stop it).")
        if st.button("Stop", disabled=not can_stop, use_container_width=True,
                     key="stop_btn", help=stop_help):
            err = _stop_ollama()
            if err:
                st.error(err)
            else:
                st.rerun()

st.markdown("<hr style='margin: 0.4rem 0;'>", unsafe_allow_html=True)

uploaded = st.file_uploader(
    "Documents",
    type=["pdf", "docx"],
    accept_multiple_files=True,
)

col1, col2 = st.columns(2)
with col1:
    source_lang = st.selectbox("Source language", LANGS,
                               index=LANGS.index("Spanish"))
with col2:
    target_lang = st.selectbox("Target language", LANGS,
                               index=LANGS.index("English"))

def _list_models():
    """Fetch locally installed models from Ollama."""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        r.raise_for_status()
        models = r.json().get("models", [])
        return [
            {
                "name": m["name"],
                "params": m.get("details", {}).get("parameter_size", "?"),
                "size_gb": m["size"] / 1e9,
            }
            for m in models
        ]
    except Exception:
        return []


if running:
    models = _list_models()
    if models:
        # Sort: translategemma first, then alphabetical.
        models.sort(key=lambda m: (0 if "translategemma" in m["name"] else 1,
                                   m["name"]))
        labels = [
            f"{m['name']}  ({m['params']}, {m['size_gb']:.1f} GB)"
            for m in models
        ]
        idx = st.selectbox("Model", range(len(labels)),
                           format_func=lambda i: labels[i])
        model = models[idx]["name"]
    else:
        model = st.text_input("Model", value="translategemma")
else:
    model = st.text_input("Model", value="translategemma")

BLOBS_DIR = Path.home() / ".ollama" / "models" / "blobs"


def _clean_partial_blobs() -> int:
    """Delete *-partial* files from the Ollama blob store. Returns count removed."""
    if not BLOBS_DIR.is_dir():
        return 0
    removed = 0
    for p in BLOBS_DIR.glob("*partial*"):
        try:
            p.unlink()
            removed += 1
        except Exception:
            pass
    return removed


_PULL: dict = {
    "status": "idle",  # idle | pulling | done | error | cancelled
    "model": "",
    "log": "",
    "lock": threading.Lock(),
    "cancel": False,
    "thread": None,
}


def _pull_worker(model_name):
    try:
        with requests.post(f"{OLLAMA_URL}/api/pull",
                           json={"name": model_name}, stream=True,
                           timeout=600) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if _PULL["cancel"]:
                    resp.close()
                    _PULL["status"] = "cancelled"
                    return
                if not line:
                    continue
                import json
                data = json.loads(line)
                status = data.get("status", "")
                total = data.get("total", 0)
                completed = data.get("completed", 0)
                if total:
                    pct = int(completed / total * 100)
                    with _PULL["lock"]:
                        _PULL["log"] = f"{status} — {pct}%"
                else:
                    with _PULL["lock"]:
                        _PULL["log"] = status
        _PULL["status"] = "done"
    except Exception as e:
        with _PULL["lock"]:
            _PULL["log"] = str(e)
        _PULL["status"] = "error"


with st.expander("Pull a new model"):
    st.caption(
        "Browse all models at [ollama.com/library](https://ollama.com/library). "
        "Any general-purpose model can translate, but language support varies — "
        "check the model card for details. Some options:"
    )
    st.markdown(
        "| Model | Notes |\n"
        "| --- | --- |\n"
        "| `translategemma` | Google's translation-specific model (default) |\n"
        "| `gemma3` | Google, strong multilingual |\n"
        "| `qwen2.5` / `qwen3` | Alibaba, strong multilingual |\n"
        "| `llama3.1` / `llama3.2` | Meta, good general-purpose |\n"
        "| `mistral` / `mixtral` | Mistral AI, good for European languages |\n"
        "| `aya` | Cohere, built for multilingual tasks |\n"
        "| `phi4` | Microsoft, lightweight + capable |\n",
        unsafe_allow_html=False,
    )
    pull_col, pull_btn_col = st.columns([3, 1], vertical_alignment="bottom")
    with pull_col:
        pull_name = st.text_input("Model name", placeholder="e.g. translategemma",
                                  label_visibility="collapsed")
    with pull_btn_col:
        is_pulling = _PULL["status"] == "pulling"
        pull_clicked = st.button(
            "Pull", disabled=not pull_name or not running or is_pulling,
            use_container_width=True)
    if pull_clicked and pull_name and not is_pulling:
        installed = {m["name"] for m in _list_models()} if running else set()
        # Check with and without :latest tag
        if (pull_name in installed
                or f"{pull_name}:latest" in installed
                or pull_name.replace(":latest", "") in installed):
            st.info(f"`{pull_name}` is already installed.")
        else:
            _PULL.update({"status": "pulling", "model": pull_name,
                          "log": "", "cancel": False})
            t = threading.Thread(target=_pull_worker, args=(pull_name,),
                                 daemon=True)
            _PULL["thread"] = t
            t.start()

    @st.fragment(run_every="1s")
    def _pull_progress():
        if _PULL["status"] == "idle":
            return
        with _PULL["lock"]:
            log = _PULL["log"]
        status = _PULL["status"]
        if status == "pulling":
            st.code(log or "Starting…", language="text")
            if st.button("Cancel", key="cancel_pull"):
                _PULL["cancel"] = True
        elif status == "done":
            st.success(f"Pulled `{_PULL['model']}` successfully.")
            _PULL["status"] = "idle"
            st.rerun(scope="app")
        elif status == "cancelled":
            # Try to clean up partial download.
            try:
                r = requests.delete(f"{OLLAMA_URL}/api/delete",
                                    json={"model": _PULL["model"]}, timeout=5)
            except Exception:
                pass
            cleaned = _clean_partial_blobs()
            if cleaned:
                st.warning(f"Pull cancelled — cleaned up {cleaned} partial file(s).")
            else:
                st.warning("Pull cancelled.")
            _PULL["status"] = "idle"
        elif status == "error":
            st.error(f"Pull failed: {log}")
            _PULL["status"] = "idle"

    _pull_progress()

# output_mode = st.radio(
#     "Output",
#     ["Download files", "Save to folder"],
#     horizontal=True,
# )
output_mode = "Save to folder"
output_dir = None
if True:  # output_mode == "Save to folder"
    if "output_dir" not in st.session_state:
        st.session_state.output_dir = str(Path.cwd() / "translated")

    path_col, btn_col = st.columns([4, 1])
    with btn_col:
        st.write("")  # vertical alignment with text input
        if st.button("Browse…"):
            picked = _pick_folder(st.session_state.output_dir)
            if picked:
                st.session_state.output_dir = picked
    with path_col:
        output_dir = st.text_input(
            "Output folder",
            key="output_dir",
            help="Absolute or relative path on this machine. Created if missing.",
        )

run = st.button("Translate", type="primary", disabled=not uploaded)


class _LineStream(io.TextIOBase):
    """Captures writes line-by-line into a thread-safe list."""

    def __init__(self, log_list, lock):
        super().__init__()
        self._buf = ""
        self._log = log_list
        self._lock = lock

    def write(self, s):
        if not s:
            return 0
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            with self._lock:
                self._log.append(line)
        return len(s)

    def flush(self):
        if self._buf:
            with self._lock:
                self._log.append(self._buf)
            self._buf = ""


def _collect_outputs(result):
    keys = ["text_output", "tables_output", "footnotes_output", "comments_output"]
    return [Path(result[k]) for k in keys if result.get(k)]


# Module-level job state. Survives Streamlit script reruns (module imported once).
_JOB: dict = {
    "status": "idle",  # idle | running | done
    "log": [],
    "lock": threading.Lock(),
    "thread": None,
    "done_count": 0,
    "total_count": 0,
    "outputs": [],
    "failures": [],
    "totals": {"blocks": 0, "chars": 0},
    "tmp_ctx": None,
    "settings": {},  # output_mode/output_dir captured at submit time
}


def _worker(uploads, source_lang, target_lang, model, tmp_p):
    stream = _LineStream(_JOB["log"], _JOB["lock"])
    for i, (name, data) in enumerate(uploads):
        suffix = Path(name).suffix.lower()
        print(f"\n=== [{i+1}/{len(uploads)}] {name} ===", file=stream)
        file_dir = tmp_p / f"{i:03d}_{Path(name).stem}"
        file_dir.mkdir(parents=True, exist_ok=True)
        in_path = file_dir / name
        in_path.write_bytes(data)
        out_path = file_dir / f"{in_path.stem}_{target_lang.lower()}.docx"
        fn = translate_pdf if suffix == ".pdf" else translate_docx
        try:
            with redirect_stdout(stream):
                result = fn(str(in_path), str(out_path),
                            source_lang=source_lang,
                            target_lang=target_lang, model=model)
            with _JOB["lock"]:
                _JOB["outputs"].extend(_collect_outputs(result))
                _JOB["totals"]["blocks"] += result.get("total_blocks", 0)
                _JOB["totals"]["chars"] += result.get("chars_in", 0)
        except Exception as e:
            print(f"  Error: {e}", file=stream)
            print(traceback.format_exc(), file=stream)
            with _JOB["lock"]:
                _JOB["failures"].append((name, str(e)))
        with _JOB["lock"]:
            _JOB["done_count"] = i + 1
    stream.flush()
    _JOB["status"] = "done"


def _start_job(uploads, source_lang, target_lang, model,
               output_mode, output_dir):
    # Read all upload bytes upfront — UploadedFile objects don't survive reruns.
    payload = [(u.name, u.getvalue()) for u in uploads]
    tmp_ctx = tempfile.TemporaryDirectory()
    _JOB.update({
        "status": "running",
        "log": [],
        "thread": None,
        "done_count": 0,
        "total_count": len(payload),
        "outputs": [],
        "failures": [],
        "totals": {"blocks": 0, "chars": 0},
        "tmp_ctx": tmp_ctx,
        "settings": {"output_mode": output_mode, "output_dir": output_dir},
    })
    t = threading.Thread(
        target=_worker,
        args=(payload, source_lang, target_lang, model, Path(tmp_ctx.name)),
        daemon=True,
    )
    _JOB["thread"] = t
    t.start()


def _reset_job():
    if _JOB.get("tmp_ctx"):
        try:
            _JOB["tmp_ctx"].cleanup()
        except Exception:
            pass
    _JOB.update({
        "status": "idle", "log": [], "thread": None,
        "done_count": 0, "total_count": 0, "outputs": [],
        "failures": [], "totals": {"blocks": 0, "chars": 0},
        "tmp_ctx": None, "settings": {},
    })


if run and uploaded and _JOB["status"] != "running":
    if not running:
        _needs_ollama_dialog()
    else:
        _start_job(uploaded, source_lang, target_lang, model,
                   output_mode, output_dir)


def _render_results():
    """Render completion summary + saved paths + Clear button."""
    outputs = _JOB["outputs"]
    failures = _JOB["failures"]
    totals = _JOB["totals"]
    total = _JOB["total_count"]
    ok = total - len(failures)
    settings = _JOB["settings"]

    if failures:
        st.warning(f"{ok}/{total} succeeded — {len(failures)} failed.")
        for name, err in failures:
            st.write(f"• **{name}** — {err}")
    else:
        st.success(
            f"Done — {ok} file(s), {totals['blocks']} blocks, "
            f"{totals['chars']} chars, {len(outputs)} output file(s)."
        )

    if outputs and settings.get("output_mode") == "Save to folder":
        dest = Path(settings["output_dir"]).expanduser()
        try:
            dest.mkdir(parents=True, exist_ok=True)
            saved = []
            for p in outputs:
                target = dest / p.name
                shutil.copy2(p, target)
                saved.append(target)
            st.write("Saved:")
            for p in saved:
                st.code(str(p), language="text")
        except Exception as e:
            st.error(f"Could not write to {dest}: {e}")


@st.fragment(run_every="1s")
def _progress_panel():
    if _JOB["status"] == "idle":
        return
    st.subheader("Progress")
    with _JOB["lock"]:
        done = _JOB["done_count"]
        total = _JOB["total_count"]
        log_lines = list(_JOB["log"])
    pct = (done / total) if total else 0.0
    st.progress(pct, text=f"{done} / {total}")
    if log_lines:
        st.code("\n".join(log_lines[-200:]), language="text")
    if _JOB["status"] == "done":
        _JOB["status"] = "done_rendered"
    if _JOB["status"] == "done_rendered":
        _render_results()


_progress_panel()




