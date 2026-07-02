"""Flask web backend for the olladoc translation UI.
Run: `python app_flask.py` -> open http://localhost:5001
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, render_template, request, send_file

import translate
from translate import translate_document

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024   # 200 MB uploads

# ---- Ollama integration ---------------------------------------------------

OLLAMA_URL = "http://localhost:11434"
MENUBAR_LOG = Path.home() / ".ollama" / "logs" / "server.log"
OLLADOC_LOG = Path(tempfile.gettempdir()) / "olladoc_ollama.log"
OLLAMA_PID_FILE = Path(tempfile.gettempdir()) / "olladoc_ollama.pid"
BLOBS_DIR = Path.home() / ".ollama" / "models" / "blobs"


def _ollama_status_tuple() -> tuple[bool, str | None]:
    """Returns (running, version_or_None)."""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/version", timeout=1)
        r.raise_for_status()
        return True, r.json().get("version", "?")
    except Exception:
        return False, None


def _pid_is_ollama(pid: int) -> bool:
    """True if `pid` is alive and its command line mentions `ollama`."""
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
    for _ in range(20):
        time.sleep(0.25)
        if _ollama_status_tuple()[0]:
            return None
    return "Started, but server didn't respond on :11434 within 5s."


def _stop_ollama() -> str | None:
    pid = _managed_pid()
    if not pid:
        return "No managed Ollama process to stop."
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception as e:
        return str(e)
    OLLAMA_PID_FILE.unlink(missing_ok=True)
    OLLADOC_LOG.unlink(missing_ok=True)
    return None


def _active_log_path() -> Path | None:
    """Return whichever log file is currently in use (ours, then menubar)."""
    for p in (OLLADOC_LOG, MENUBAR_LOG):
        if p.exists():
            return p
    return None


def _read_log_tail(path: Path, n: int = 200) -> str:
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


def _clean_partial_blobs() -> int:
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


# Pull state
_PULL: dict = {
    "status": "idle",   # idle | pulling | done | error | cancelled
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
                    with _PULL["lock"]:
                        _PULL["log"] = "Pull cancelled."
                    _PULL["status"] = "cancelled"
                    return
                if not line:
                    continue
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


# ---- Job state -------------------------------------------------------------

@dataclass
class Payload:
    """Per-file state that persists across phases."""
    name: str
    data: bytes
    in_path: str | None = None
    out_path: str | None = None
    glossary_path: str | None = None


@dataclass
class Job:
    id: str
    payloads: list[Payload]
    settings: dict[str, Any]
    tmp_ctx: tempfile.TemporaryDirectory
    lock: threading.Lock = field(default_factory=threading.Lock)
    log: list[str] = field(default_factory=list)
    status: str = "idle"       # running_phase1 | awaiting_edit | running_phase2 | done | error
    done_count: int = 0
    total_count: int = 0
    outputs: list[str] = field(default_factory=list)   # absolute paths to output files
    failures: list[tuple[str, str]] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=lambda: {"blocks": 0, "chars": 0})
    saved_paths: list[str] = field(default_factory=list)


JOBS: dict[str, Job] = {}


class _LineStream(io.TextIOBase):
    """Captures writes line-by-line into a thread-safe list on the Job."""

    def __init__(self, job: Job):
        super().__init__()
        self._buf = ""
        self._job = job

    def write(self, s):
        if not s:
            return 0
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("utf-8", errors="replace")
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            with self._job.lock:
                self._job.log.append(line)
        return len(s)

    def flush(self):
        if self._buf:
            with self._job.lock:
                self._job.log.append(self._buf)
            self._buf = ""


def _collect_outputs(result: dict) -> list[Path]:
    keys = ["text_output", "tables_output", "footnotes_output", "comments_output"]
    return [Path(result[k]) for k in keys if result.get(k)]


# ---- Worker ----------------------------------------------------------------

def _run_phase(job: Job, phases: tuple[str, ...]):
    """Run a single phase over every payload in the job.

    Inputs live in temp (uploads are ephemeral bytes materialized just so translate_document has a file path to read).
    Outputs go straight to the user's output folder from Phase 1 onwards.
    """
    stream = _LineStream(job)
    s = job.settings
    is_phase1_only = phases == ("build_glossary",)
    out_dir = Path(s.get("output_dir") or "translated").expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(job.payloads):
        print(f"\n=== [{i+1}/{len(job.payloads)}] {p.name} ===", file=stream)
        suffix = Path(p.name).suffix.lower()
        # First time: materialize the upload to temp and pick the output path.
        if not p.in_path:
            tmp_dir = Path(job.tmp_ctx.name) / f"{i:03d}_{Path(p.name).stem}"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            in_path = tmp_dir / p.name
            in_path.write_bytes(p.data)
            p.in_path = str(in_path)
            p.out_path = str(out_dir / f"{in_path.stem}_{s['target_lang'].lower()}.docx")
        # For Phase 2 alone (two-phase workflow) we already have a glossary_path from Phase 1.
        # translate_document derives its glossary path from the output stem, so Phase 2 must reuse that same stem to find the file.
        # Disable the fresh timestamp; the docx shares Phase 1's stamp so both artifacts of the run stay together on disk.
        phase_timestamp = s["timestamp"]
        out_path = p.out_path
        if phases == ("translate",) and p.glossary_path:
            gp = Path(p.glossary_path)
            out_path = str(gp.with_name(gp.name.replace("_glossary.txt", ".docx")))
            phase_timestamp = False
        try:
            with redirect_stdout(stream):
                result = translate_document(
                    p.in_path, out_path,
                    source_lang=s["source_lang"],
                    target_lang=s["target_lang"],
                    model=s["model"],
                    phases=phases,
                    keep_glossary=s["keep_glossary"],
                    timestamp=phase_timestamp,
                )
            # Remember the (possibly timestamped) output path so Phase 2 / results use it.
            p.out_path = result.get("output") or out_path
            if result.get("glossary_path"):
                p.glossary_path = result["glossary_path"]
            if not is_phase1_only:
                with job.lock:
                    job.outputs.extend(str(o) for o in _collect_outputs(result))
                    job.totals["blocks"] += result.get("total_blocks", 0)
                    job.totals["chars"] += result.get("chars_in", 0)
                if suffix == ".docx":
                    try:
                        from sanity_check import write_report
                        # Skip if no warnings log path is set on the translate module.
                        wlog = getattr(translate, "WARNINGS_LOG_PATH", None)
                        if wlog:
                            n = write_report(p.in_path, p.out_path, str(wlog))
                            if n:
                                print(f"  Sanity check: {n} issue(s)", file=stream)
                    except Exception as e:
                        print(f"  Sanity check failed: {e}", file=stream)
        except Exception as e:
            print(f"  Error: {e}", file=stream)
            print(traceback.format_exc(), file=stream)
            with job.lock:
                job.failures.append((p.name, str(e)))
        with job.lock:
            job.done_count = i + 1
    # After the final phase, expose the glossary paths as downloadable outputs alongside the docx files.
    if not is_phase1_only:
        with job.lock:
            for p in job.payloads:
                if (p.glossary_path
                        and Path(p.glossary_path).exists()
                        and p.glossary_path not in job.outputs):
                    job.outputs.append(p.glossary_path)
            job.saved_paths = list(job.outputs)
        # Tear down the temp dir — only the input docx was in it.
        try:
            job.tmp_ctx.cleanup()
            print("  Cleaned up temp directory.", file=stream)
        except Exception as e:
            print(f"  Temp cleanup failed: {e}", file=stream)
    stream.flush()
    job.status = "awaiting_edit" if is_phase1_only else "done"


def _run_phase_in_thread(job: Job, phases: tuple[str, ...]):
    t = threading.Thread(target=_run_phase, args=(job, phases), daemon=True)
    t.start()


# ---- Routes ----------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/translate")
def start_job():
    """Kick off a new translation job. Multipart form with files + settings."""
    uploads = request.files.getlist("files")
    if not uploads:
        return jsonify({"error": "no files uploaded"}), 400
    payloads = [Payload(name=u.filename, data=u.read()) for u in uploads]
    settings = {
        "source_lang": request.form.get("source_lang", "Spanish"),
        "target_lang": request.form.get("target_lang", "English"),
        "model": request.form.get("model", "translategemma"),
        "output_dir": request.form.get("output_dir",
                                       str(Path.cwd() / "translated")),
        "workflow": request.form.get("workflow", "oneshot"),
        "keep_glossary": request.form.get("keep_glossary", "true") == "true",
        "timestamp": request.form.get("timestamp", "false") == "true",
    }
    job = Job(
        id=uuid.uuid4().hex,
        payloads=payloads,
        settings=settings,
        tmp_ctx=tempfile.TemporaryDirectory(),
        total_count=len(payloads),
    )
    JOBS[job.id] = job
    is_twophase = settings["workflow"] == "twophase"
    # "running_phase1" only when we've deliberately stopped after Phase 1 for the edit step.
    job.status = "running_phase1" if is_twophase else "running"
    _run_phase_in_thread(job,
                         ("build_glossary",) if is_twophase
                         else ("build_glossary", "translate"))
    return jsonify({"job_id": job.id})


def _job_or_404(job_id: str) -> Job | tuple[Any, int]:
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "unknown job"}), 404
    return job


@app.get("/api/status/<job_id>")
def status(job_id):
    r = _job_or_404(job_id)
    if isinstance(r, tuple):
        return r
    job = r
    with job.lock:
        log_tail = list(job.log[-200:])
        payloads = [
            {"name": p.name, "glossary_path": p.glossary_path}
            for p in job.payloads
        ]
        outputs = [Path(op).name for op in job.saved_paths] if job.saved_paths \
                  else [Path(op).name for op in job.outputs]
        return jsonify({
            "status": job.status,
            "done_count": job.done_count,
            "total_count": job.total_count,
            "log": log_tail,
            "outputs": outputs,
            "saved_paths": job.saved_paths,
            "failures": job.failures,
            "totals": job.totals,
            "payloads": payloads,
        })


@app.get("/api/glossary/<job_id>/<int:file_idx>")
def get_glossary(job_id, file_idx):
    r = _job_or_404(job_id)
    if isinstance(r, tuple):
        return r
    job = r
    if not (0 <= file_idx < len(job.payloads)):
        return jsonify({"error": "bad file index"}), 400
    p = job.payloads[file_idx]
    if not p.glossary_path or not Path(p.glossary_path).exists():
        return jsonify({"error": "no glossary yet"}), 404
    return jsonify({
        "name": Path(p.glossary_path).name,
        "content": Path(p.glossary_path).read_text(encoding="utf-8"),
    })


@app.post("/api/glossary/<job_id>/<int:file_idx>")
def save_glossary(job_id, file_idx):
    r = _job_or_404(job_id)
    if isinstance(r, tuple):
        return r
    job = r
    if not (0 <= file_idx < len(job.payloads)):
        return jsonify({"error": "bad file index"}), 400
    p = job.payloads[file_idx]
    if not p.glossary_path:
        return jsonify({"error": "no glossary yet"}), 404
    content = request.get_json(silent=True) or {}
    text = content.get("content", "")
    Path(p.glossary_path).write_text(text, encoding="utf-8")
    # Also mirror to the user's output folder so on-disk copy is up to date.
    out_dir = job.settings.get("output_dir")
    if out_dir:
        try:
            dst = Path(out_dir).expanduser() / Path(p.glossary_path).name
            Path(out_dir).expanduser().mkdir(parents=True, exist_ok=True)
            shutil.copy2(p.glossary_path, dst)
        except Exception:
            pass
    return jsonify({"ok": True})


@app.post("/api/continue/<job_id>")
def continue_to_phase2(job_id):
    r = _job_or_404(job_id)
    if isinstance(r, tuple):
        return r
    job = r
    if job.status != "awaiting_edit":
        return jsonify({"error": f"cannot continue from status {job.status}"}), 400
    with job.lock:
        job.log = []
        job.done_count = 0
        job.status = "running_phase2"
    _run_phase_in_thread(job, ("translate",))
    return jsonify({"ok": True})


@app.post("/api/cancel/<job_id>")
def cancel(job_id):
    job = JOBS.pop(job_id, None)
    if job:
        try:
            job.tmp_ctx.cleanup()
        except Exception:
            pass
    return jsonify({"ok": True})


@app.get("/api/download/<job_id>/<path:name>")
def download(job_id, name):
    r = _job_or_404(job_id)
    if isinstance(r, tuple):
        return r
    job = r
    # Prefer the saved copy in the user's output folder; fall back to the in-tmp copy.
    for src in (job.saved_paths + job.outputs):
        if Path(src).name == name and Path(src).exists():
            return send_file(src, as_attachment=True)
    return jsonify({"error": "file not found"}), 404


# ---- Ollama routes --------------------------------------------------------

@app.get("/api/ollama/status")
def ollama_status():
    running, version = _ollama_status_tuple()
    managed = _managed_pid() is not None
    return jsonify({"running": running, "version": version, "managed": managed})


@app.post("/api/ollama/start")
def ollama_start():
    err = _start_ollama()
    return jsonify({"ok": err is None, "error": err})


@app.post("/api/ollama/stop")
def ollama_stop():
    err = _stop_ollama()
    return jsonify({"ok": err is None, "error": err})


@app.get("/api/ollama/log")
def ollama_log():
    path = _active_log_path()
    if not path:
        return jsonify({"path": None, "log": ""})
    n = int(request.args.get("n", 200))
    return jsonify({"path": str(path), "log": _read_log_tail(path, n)})


@app.get("/api/ollama/models")
def ollama_models():
    """Locally-installed models. Sorted so translategemma sorts first."""
    running, _ = _ollama_status_tuple()
    if not running:
        return jsonify({"running": False, "models": []})
    models = _list_models()
    models.sort(key=lambda m: (0 if "translategemma" in m["name"] else 1,
                               m["name"]))
    return jsonify({"running": True, "models": models})


@app.post("/api/ollama/pull")
def ollama_pull():
    data = request.get_json(silent=True) or {}
    name = (data.get("model") or "").strip()
    if not name:
        return jsonify({"error": "no model name"}), 400
    running, _ = _ollama_status_tuple()
    if not running:
        return jsonify({"error": "Ollama is not running"}), 400
    if _PULL["status"] == "pulling":
        return jsonify({"error": "a pull is already in progress",
                        "model": _PULL["model"]}), 409
    # Short-circuit if already installed.
    installed = {m["name"] for m in _list_models()}
    if (name in installed
            or f"{name}:latest" in installed
            or name.replace(":latest", "") in installed):
        return jsonify({"already_installed": True, "model": name})
    _PULL.update({"status": "pulling", "model": name,
                  "log": "", "cancel": False})
    t = threading.Thread(target=_pull_worker, args=(name,), daemon=True)
    _PULL["thread"] = t
    t.start()
    return jsonify({"ok": True, "model": name})


@app.get("/api/ollama/pull/status")
def ollama_pull_status():
    with _PULL["lock"]:
        return jsonify({
            "status": _PULL["status"],
            "model": _PULL["model"],
            "log": _PULL["log"],
        })


@app.post("/api/ollama/pull/cancel")
def ollama_pull_cancel():
    if _PULL["status"] != "pulling":
        return jsonify({"ok": True, "note": "no pull in progress"})
    _PULL["cancel"] = True
    with _PULL["lock"]:
        _PULL["log"] = "Cancelling…"
    # Give the worker a beat to notice, then clean partial blobs.
    time.sleep(0.5)
    cleaned = _clean_partial_blobs()
    return jsonify({"ok": True, "cleaned_partial_blobs": cleaned})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
