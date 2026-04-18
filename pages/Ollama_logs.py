"""Ollama log viewer — lives on its own page so it can run independently
from the main translation UI."""

import tempfile
from datetime import datetime
from pathlib import Path

import streamlit as st

MENUBAR_LOG = Path.home() / ".ollama" / "logs" / "server.log"
OLLADOC_LOG = Path(tempfile.gettempdir()) / "olladoc_ollama.log"


def _active_log_path():
    for p in (OLLADOC_LOG, MENUBAR_LOG):
        if p.exists():
            return p
    return None


def _read_log_tail(path, n=100):
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


st.set_page_config(page_title="Ollama logs", layout="wide",
                   initial_sidebar_state="collapsed")

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

st.title("Ollama logs")


@st.fragment(run_every="2s")
def _panel():
    log_path = _active_log_path()
    prefix = (f"`{log_path}` — last 100 lines" if log_path
              else "_Ollama not started_")
    st.caption(f"{prefix} · updated {datetime.now().strftime('%H:%M:%S')}")
    log_text = _read_log_tail(log_path, n=100) if log_path else ""
    st.code(log_text or " ", language="text")


_panel()
