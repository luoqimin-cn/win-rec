from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from . import config, store

_ACTIVE_FILE = config.RUNTIME_DIR / "active.json"


# ─── icon generation ────────────────────────────────────────────────────────

_ICON_SIZE = 64

_COLORS = {
    "idle":      "#888888",
    "recording": "#E53935",
    "paused":    "#FFC107",
}


def _make_icon(state: str) -> Image.Image:
    color = _COLORS.get(state, _COLORS["idle"])
    img = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 6
    draw.ellipse(
        [margin, margin, _ICON_SIZE - margin, _ICON_SIZE - margin],
        fill=color,
    )
    return img


# ─── state polling ──────────────────────────────────────────────────────────

def _read_active() -> dict | None:
    p = _ACTIVE_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _get_state() -> str:
    """Return 'idle', 'recording', or 'paused'."""
    active = _read_active()
    if not active:
        return "idle"
    pid = active.get("pid")
    if not pid or not _alive(pid):
        return "idle"
    session_dir = active.get("session_dir")
    if not session_dir:
        return "recording"
    log_path = Path(session_dir) / "recorder.log"
    state = "recording"
    if log_path.exists():
        try:
            with log_path.open("rb") as f:
                for raw in f:
                    try:
                        evt = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    e = evt.get("event")
                    if e in {"started", "resumed"}:
                        state = "recording"
                    elif e == "paused":
                        state = "paused"
        except OSError:
            pass
    return state


# ─── win-rec.exe path ────────────────────────────────────────────────────────

def _win_rec_exe() -> str:
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).parent / "win-rec.exe"
        if candidate.exists():
            return str(candidate)
        raise RuntimeError(
            "win-rec.exe not found beside tray.exe — "
            "ensure both files are in the same folder"
        )
    import shutil
    found = shutil.which("win-rec")
    if found:
        return found
    raise RuntimeError("win-rec not found in PATH")


def _run(args: list[str]) -> None:
    """Fire a win-rec command in a thread so the menu doesn't block."""
    def _worker():
        try:
            exe = _win_rec_exe()
            subprocess.run([exe] + args, creationflags=subprocess.CREATE_NO_WINDOW
                           if sys.platform == "win32" else 0)
        except Exception as e:
            _show_error(str(e))
    threading.Thread(target=_worker, daemon=True).start()


def _show_error(msg: str) -> None:
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "win-rec 错误", 0x10)
            return
    except Exception:
        pass
    print(f"win-rec error: {msg}", file=sys.stderr)


# ─── menu actions ────────────────────────────────────────────────────────────

def _open_latest_summary() -> None:
    try:
        sessions = store.list_sessions()
    except Exception:
        sessions = []
    for s in sessions:
        if s.summary_md.exists():
            os.startfile(str(s.summary_md))
            return
    _show_error("暂无摘要文件。请先录音并处理（win-rec stop --process）。")


def _open_recordings_folder() -> None:
    config.ensure_dirs()
    os.startfile(str(config.RECORDINGS_DIR))


# ─── tray app ────────────────────────────────────────────────────────────────

class TrayApp:
    def __init__(self) -> None:
        self._state = "idle"
        self._icon = pystray.Icon(
            "win-rec",
            icon=_make_icon("idle"),
            title="win-rec — 空闲",
            menu=self._build_menu(),
        )

    # ── menu builder ─────────────────────────────────────────────────────────

    def _build_menu(self) -> pystray.Menu:
        Item = pystray.MenuItem

        def is_idle(item):
            return self._state == "idle"

        def is_recording(item):
            return self._state == "recording"

        def is_paused(item):
            return self._state == "paused"

        def is_active(item):
            return self._state in {"recording", "paused"}

        recent_menu = pystray.Menu(self._recent_items)

        return pystray.Menu(
            Item(self._status_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            Item("开始录音",       lambda: _run(["start"]),            enabled=is_idle),
            Item("暂停录音",       lambda: _run(["pause"]),            enabled=is_recording),
            Item("恢复录音",       lambda: _run(["resume"]),           enabled=is_paused),
            Item("停止录音",       lambda: _run(["stop"]),             enabled=is_active),
            Item("停止并处理",     lambda: _run(["stop", "--process"]),enabled=is_active),
            pystray.Menu.SEPARATOR,
            Item("最近录音", recent_menu),
            Item("查看最新摘要",   lambda: _open_latest_summary()),
            Item("打开录音文件夹", lambda: _open_recordings_folder()),
            pystray.Menu.SEPARATOR,
            Item("退出",           self._quit),
        )

    def _status_label(self, item) -> str:
        labels = {
            "idle":      "● 空闲",
            "recording": "● 录音中",
            "paused":    "⏸ 已暂停",
        }
        return labels.get(self._state, "● 空闲")

    def _recent_items(self) -> list:
        Item = pystray.MenuItem
        try:
            sessions = store.list_sessions()[:5]
        except Exception:
            sessions = []
        if not sessions:
            return [Item("（无录音）", None, enabled=False)]
        items = []
        for s in sessions:
            meta = s.read_meta()
            label = meta.get("name") or s.session_id
            dur = meta.get("duration")
            if dur:
                label = f"{label}  {int(dur)}s"
            session_dir = str(s.dir)
            items.append(Item(label, lambda _, d=session_dir: os.startfile(d)))
        return items

    # ── state polling loop ───────────────────────────────────────────────────

    def _poll(self) -> None:
        while True:
            try:
                new_state = _get_state()
            except Exception:
                time.sleep(2)
                continue
            if new_state != self._state:
                self._state = new_state
                self._icon.icon = _make_icon(new_state)
                titles = {
                    "idle":      "win-rec — 空闲",
                    "recording": "win-rec — 录音中",
                    "paused":    "win-rec — 已暂停",
                }
                self._icon.title = titles.get(new_state, "win-rec")
                self._icon.update_menu()
            time.sleep(2)

    # ── quit ─────────────────────────────────────────────────────────────────

    def _quit(self) -> None:
        self._icon.stop()

    # ── run ──────────────────────────────────────────────────────────────────

    def run(self) -> None:
        threading.Thread(target=self._poll, daemon=True).start()
        self._icon.run()


def main() -> None:
    TrayApp().run()
