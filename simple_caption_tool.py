#!/usr/bin/env python3
"""
Caption Copier — Windows Live Captions companion.

Manual workflow:
1. Open Captions
2. Capture Now / Start Auto Capture  → Live Caption (read-only)
3. Copy/edit into Text to Send (editable; Auto Capture never overwrites it once set unless user copies)
4. Select GPT Target (verified ChatGPT process or browser tab + URL)
5. Send to GPT (paste + Enter only after focus, tab, URL, and composer verification)
6. Close Captions via verified close control only

No OpenAI API, Ollama, Selenium, Playwright, or auto-send.
Local app lock is a lightweight gate, not secure authentication.
"""

from __future__ import annotations

import hashlib
import logging
import math
import queue
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a desktop)
# ---------------------------------------------------------------------------

MIN_INTERVAL_S = 0.2
MAX_INTERVAL_S = 5.0
DEFAULT_INTERVAL_S = 0.5
CAPTIONS_OPEN_TIMEOUT_S = 5.0
CAPTIONS_CLOSE_TIMEOUT_S = 5.0

CHAT_GPT_TITLE_NEEDLES = (
    "chatgpt",
    "chat gpt",
    "chat.openai.com",
    "chatgpt.com",
)

# Verified ChatGPT / OpenAI desktop executables (basename, lower-case).
VERIFIED_CHATGPT_EXES = frozenset(
    {
        "chatgpt.exe",
        "chatgpt desktop.exe",
        "openai chatgpt.exe",
    }
)
# Path fragments that indicate an official ChatGPT / OpenAI package install.
VERIFIED_CHATGPT_PATH_FRAGMENTS = (
    "\\chatgpt\\",
    "/chatgpt/",
    "openai\\chatgpt",
    "openai/chatgpt",
    "apps.openai.com",
)

BROWSER_PROCESS_NAMES = frozenset(
    {
        "chrome.exe",
        "msedge.exe",
        "firefox.exe",
        "brave.exe",
        "opera.exe",
        "vivaldi.exe",
    }
)

ALLOWED_CHATGPT_URL_HOSTS = frozenset(
    {
        "chatgpt.com",
        "www.chatgpt.com",
        "chat.openai.com",
        "www.chat.openai.com",
    }
)

CAPTION_EXCLUDE_LABELS = (
    "settings",
    "microphone",
    "language",
    "help",
    "position",
    "caption style",
    "more options",
    "mute",
    "unmute",
    "stop",
    "start",
)


def normalize_caption_text(text: str) -> str:
    """Normalize whitespace conservatively; preserve useful line breaks."""
    if not text:
        return ""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = " ".join(raw.split())
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines).strip()


def preview_has_sendable_text(text: str) -> bool:
    return bool(text and text.strip())


def validate_capture_interval(raw: str | float | int) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Interval must be a number.") from exc
    if not math.isfinite(value):
        raise ValueError("Interval must be a finite number.")
    if value < MIN_INTERVAL_S or value > MAX_INTERVAL_S:
        raise ValueError(
            f"Interval must be between {MIN_INTERVAL_S} and {MAX_INTERVAL_S} seconds."
        )
    return value


def should_refresh_preview(previous: str, current: str) -> bool:
    return current != previous


def can_enable_send(
    *,
    preview_text: str,
    target_selected: bool,
    target_valid: bool,
    send_in_progress: bool,
) -> bool:
    if send_in_progress:
        return False
    if not preview_has_sendable_text(preview_text):
        return False
    if not target_selected or not target_valid:
        return False
    return True


def is_chatgpt_title(title: str) -> bool:
    lower = (title or "").strip().lower()
    return any(n in lower for n in CHAT_GPT_TITLE_NEEDLES)


def process_basename(process_name: str) -> str:
    name = (process_name or "").strip().lower().replace("/", "\\")
    if "\\" in name:
        name = name.rsplit("\\", 1)[-1]
    return name


def is_verified_chatgpt_process(process_name: str, process_path: str = "") -> bool:
    """
    Desktop ChatGPT must be identified by executable / path — never title alone.
    If the process cannot be identified, return False (exclude the target).
    """
    base = process_basename(process_name)
    path = (process_path or process_name or "").strip().lower().replace("/", "\\")
    if not base and not path:
        return False
    if base in VERIFIED_CHATGPT_EXES:
        return True
    if base and "chatgpt" in base and base.endswith(".exe"):
        # e.g. ChatGPT.exe variants; still require chatgpt in basename
        return True
    if any(frag in path for frag in VERIFIED_CHATGPT_PATH_FRAGMENTS):
        return True
    return False


def is_browser_process(process_name: str) -> bool:
    return process_basename(process_name) in BROWSER_PROCESS_NAMES


def classify_window_kind(
    title: str,
    process_name: str = "",
    process_path: str = "",
) -> str:
    """
    Classify a top-level window for GPT targeting.
    Returns: desktop_app | companion | browser_window | unknown
    Never claims a non-verified process is ChatGPT based on title alone.
    """
    if is_browser_process(process_name):
        return "browser_window"
    if not process_basename(process_name) and not (process_path or "").strip():
        return "unknown"  # cannot identify process → exclude
    if not is_verified_chatgpt_process(process_name, process_path):
        return "unknown"
    title_l = (title or "").lower()
    if "companion" in title_l:
        return "companion"
    return "desktop_app"


def is_allowed_chatgpt_url(url: str) -> bool:
    """Accept only official ChatGPT hosts (https)."""
    if not url or not isinstance(url, str):
        return False
    raw = url.strip()
    if not raw:
        return False
    # Address bars sometimes omit scheme
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if parsed.scheme.lower() not in ("https", "http"):
        # Prefer https; allow http only if host is exact allowed (some address bars)
        return False
    if parsed.scheme.lower() != "https":
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return host in ALLOWED_CHATGPT_URL_HOSTS


def tabs_match_exactly(
    selected_tab_name: str | None,
    active_tab_name: str | None,
    *,
    selected_ordinal: int | None = None,
    active_ordinal: int | None = None,
    selected_runtime_id: str | None = None,
    active_runtime_id: str | None = None,
) -> bool:
    """Require the exact selected tab — never 'any ChatGPT tab'."""
    if not selected_tab_name or not active_tab_name:
        return False
    if selected_runtime_id and active_runtime_id:
        return selected_runtime_id == active_runtime_id
    if selected_tab_name != active_tab_name:
        return False
    if selected_ordinal is not None and active_ordinal is not None:
        return selected_ordinal == active_ordinal
    return True


@dataclass
class GptTarget:
    """Plain-data GPT target. Never stores live UIA elements across threads."""

    kind: str  # desktop_app | companion | browser_tab
    hwnd: int
    title: str
    process_name: str = ""
    process_path: str = ""
    process_id: int = 0
    tab_name: str | None = None
    tab_ordinal: int | None = None
    tab_runtime_id: str | None = None
    display_label: str = ""

    def __post_init__(self) -> None:
        if not self.display_label:
            if self.kind == "browser_tab" and self.tab_name:
                ord_s = (
                    f" #{self.tab_ordinal + 1}"
                    if self.tab_ordinal is not None
                    else ""
                )
                self.display_label = f"Tab{ord_s}: {self.tab_name}"
            else:
                kind_label = {
                    "desktop_app": "ChatGPT app",
                    "companion": "ChatGPT companion",
                    "browser_tab": "ChatGPT tab",
                }.get(self.kind, self.kind)
                self.display_label = f"{kind_label}: {self.title}"


def validate_target_snapshot(
    *,
    hwnd: int | None,
    is_window: bool,
    is_visible: bool,
    current_title: str,
    current_pid: int,
    expected_pid: int,
    kind: str,
    process_name: str = "",
    process_path: str = "",
    tab_name: str | None = None,
    active_tab_name: str | None = None,
    tab_ordinal: int | None = None,
    active_tab_ordinal: int | None = None,
    tab_runtime_id: str | None = None,
    active_tab_runtime_id: str | None = None,
    page_url: str | None = None,
    require_url: bool = False,
) -> tuple[bool, str]:
    """Validate a cached target using fresh process/window facts (no UIA objects)."""
    if not hwnd:
        return False, "No target selected."
    if not is_window:
        return False, "Target window no longer exists."
    if not is_visible:
        return False, "Target window is not visible."
    if expected_pid and current_pid and expected_pid != current_pid:
        return False, "Target process changed."

    if kind in ("desktop_app", "companion"):
        if not is_verified_chatgpt_process(process_name, process_path):
            return False, "Process is not a verified ChatGPT application."
        if not is_chatgpt_title(current_title):
            return False, "Window title no longer looks like ChatGPT."
        return True, "ok"

    if kind == "browser_tab":
        if not is_browser_process(process_name):
            return False, "Process is not a supported browser."
        if not tab_name:
            return False, "Browser tab information missing."
        if active_tab_name is not None:
            if not tabs_match_exactly(
                tab_name,
                active_tab_name,
                selected_ordinal=tab_ordinal,
                active_ordinal=active_tab_ordinal,
                selected_runtime_id=tab_runtime_id,
                active_runtime_id=active_tab_runtime_id,
            ):
                return False, "Selected ChatGPT tab is not active."
        if require_url or page_url is not None:
            if not is_allowed_chatgpt_url(page_url or ""):
                return (
                    False,
                    "Active page is not https://chatgpt.com/ (or legacy chat.openai.com).",
                )
        return True, "ok"

    return False, "Unknown target kind."


def safe_focus_allows_paste(focus_ok: bool, verification_ok: bool) -> bool:
    return bool(focus_ok and verification_ok)


def close_may_click_button(
    *,
    window_verified_live_captions: bool,
    button_looks_like_close: bool,
) -> bool:
    return window_verified_live_captions and button_looks_like_close


def should_accept_capture_generation(
    message_generation: int, current_generation: int
) -> bool:
    """Ignore UI messages from superseded Auto Capture workers."""
    return message_generation == current_generation


def format_exception_message(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def is_excluded_caption_label(name: str) -> bool:
    lower = (name or "").strip().lower()
    return any(x in lower for x in CAPTION_EXCLUDE_LABELS)


# ---------------------------------------------------------------------------
# Optional desktop dependencies
# ---------------------------------------------------------------------------

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, scrolledtext
except ImportError:  # pragma: no cover
    tk = None  # type: ignore

try:
    import uiautomation as uia

    UIA_AVAILABLE = True
except ImportError:
    uia = None  # type: ignore
    UIA_AVAILABLE = False

try:
    import keyboard

    KEYBOARD_AVAILABLE = True
    if hasattr(keyboard, "DEFAULT_KEYBOARD_DELAY"):
        keyboard.DEFAULT_KEYBOARD_DELAY = 0
except ImportError:
    keyboard = None  # type: ignore
    KEYBOARD_AVAILABLE = False

try:
    import pyperclip

    PYPERCLIP_AVAILABLE = True
except ImportError:
    pyperclip = None  # type: ignore
    PYPERCLIP_AVAILABLE = False

import ctypes
from ctypes import wintypes

WIN32_AVAILABLE = False
_user32 = None
_kernel32 = None

HWND = wintypes.HWND
BOOL = wintypes.BOOL
DWORD = wintypes.DWORD
LPARAM = wintypes.LPARAM

try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.IsWindow.argtypes = [HWND]
    _user32.IsWindow.restype = BOOL
    _user32.IsWindowVisible.argtypes = [HWND]
    _user32.IsWindowVisible.restype = BOOL
    _user32.IsIconic.argtypes = [HWND]
    _user32.IsIconic.restype = BOOL
    _user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
    _user32.ShowWindow.restype = BOOL
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = HWND
    _user32.SetForegroundWindow.argtypes = [HWND]
    _user32.SetForegroundWindow.restype = BOOL
    _user32.BringWindowToTop.argtypes = [HWND]
    _user32.BringWindowToTop.restype = BOOL
    _user32.SetActiveWindow.argtypes = [HWND]
    _user32.SetActiveWindow.restype = HWND
    _user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
    _user32.GetWindowThreadProcessId.restype = DWORD
    _user32.AttachThreadInput.argtypes = [DWORD, DWORD, BOOL]
    _user32.AttachThreadInput.restype = BOOL
    _user32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowTextLengthW.argtypes = [HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM), LPARAM]
    _user32.EnumWindows.restype = BOOL

    _kernel32.GetCurrentThreadId.argtypes = []
    _kernel32.GetCurrentThreadId.restype = DWORD
    _kernel32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = BOOL

    WIN32_AVAILABLE = True
except Exception:
    WIN32_AVAILABLE = False

# Local app lock (not secure authentication)
PBKDF2_ITERATIONS = 120_000
_AUTH_SALT_HEX = "a3f8c21e9b4d7056e1ac2f83d5b79c04"
_AUTH_HASH_HEX = "1a50743531d1421301ee5768622447c132b130fce747445d074491eec91ef28d"

SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

logger = logging.getLogger("caption_copier")


def _setup_logging() -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    try:
        handler = RotatingFileHandler(
            "caption_copier.log",
            maxBytes=1_000_000,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
    except OSError:
        pass


def _hash_password(password: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return digest.hex()


def _password_matches(password: str) -> bool:
    try:
        salt = bytes.fromhex(_AUTH_SALT_HEX)
    except ValueError:
        return False
    return secrets.compare_digest(_hash_password(password, salt), _AUTH_HASH_HEX)


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------


def win_is_window(hwnd: int | None) -> bool:
    if not WIN32_AVAILABLE or not hwnd:
        return False
    try:
        return bool(_user32.IsWindow(HWND(hwnd)))
    except Exception:
        return False


def win_is_minimized(hwnd: int | None) -> bool:
    if not WIN32_AVAILABLE or not hwnd:
        return False
    try:
        return bool(_user32.IsIconic(HWND(hwnd)))
    except Exception:
        return False


def win_is_visible_for_target(hwnd: int | None) -> bool:
    """
    True if the window may be used as a GPT target.
    Minimized windows are restorable (accepted).
    Fully hidden / invisible non-minimized windows are rejected.
    """
    if not WIN32_AVAILABLE or not hwnd:
        return False
    try:
        h = HWND(hwnd)
        if not _user32.IsWindow(h):
            return False
        if _user32.IsIconic(h):
            return True
        return bool(_user32.IsWindowVisible(h))
    except Exception:
        return False


def win_get_title(hwnd: int | None) -> str:
    if not WIN32_AVAILABLE or not hwnd:
        return ""
    try:
        length = _user32.GetWindowTextLengthW(HWND(hwnd))
        buf = ctypes.create_unicode_buffer(length + 2)
        _user32.GetWindowTextW(HWND(hwnd), buf, length + 2)
        return buf.value or ""
    except Exception:
        return ""


def win_get_class(hwnd: int | None) -> str:
    if not WIN32_AVAILABLE or not hwnd:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(HWND(hwnd), buf, 256)
        return buf.value or ""
    except Exception:
        return ""


def win_get_pid(hwnd: int | None) -> int:
    if not WIN32_AVAILABLE or not hwnd:
        return 0
    try:
        pid = DWORD(0)
        _user32.GetWindowThreadProcessId(HWND(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def win_get_process_path(pid: int) -> str:
    """Use QueryFullProcessImageNameW with PROCESS_QUERY_LIMITED_INFORMATION."""
    if not WIN32_AVAILABLE or not pid:
        return ""
    handle = None
    try:
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        size = DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        ok = _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        if not ok:
            return ""
        return buf.value or ""
    except Exception:
        return ""
    finally:
        if handle:
            try:
                _kernel32.CloseHandle(handle)
            except Exception:
                pass


def win_get_process_name(pid: int) -> str:
    path = win_get_process_path(pid)
    if not path:
        return ""
    return path.rsplit("\\", 1)[-1].lower()


def win_force_foreground(hwnd: int | None) -> bool:
    if not WIN32_AVAILABLE or not hwnd:
        return False
    try:
        h = HWND(hwnd)
        if not _user32.IsWindow(h):
            return False
        if int(_user32.GetForegroundWindow() or 0) == int(hwnd):
            return True
        if _user32.IsIconic(h):
            _user32.ShowWindow(h, SW_RESTORE)

        current_thread = _kernel32.GetCurrentThreadId()
        fg = _user32.GetForegroundWindow()
        fg_pid = DWORD(0)
        fg_thread = (
            _user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid)) if fg else 0
        )
        target_pid = DWORD(0)
        target_thread = _user32.GetWindowThreadProcessId(h, ctypes.byref(target_pid))

        attached_fg = False
        attached_target = False
        try:
            if fg_thread and fg_thread != current_thread:
                attached_fg = bool(
                    _user32.AttachThreadInput(current_thread, fg_thread, True)
                )
            if target_thread and target_thread != current_thread:
                attached_target = bool(
                    _user32.AttachThreadInput(current_thread, target_thread, True)
                )
            _user32.BringWindowToTop(h)
            _user32.SetForegroundWindow(h)
            try:
                _user32.SetActiveWindow(h)
            except Exception:
                pass
        finally:
            if attached_target:
                _user32.AttachThreadInput(current_thread, target_thread, False)
            if attached_fg:
                _user32.AttachThreadInput(current_thread, fg_thread, False)

        fg_now = _user32.GetForegroundWindow()
        return int(fg_now or 0) == int(hwnd)
    except Exception as exc:
        logger.info("Foreground activation failed: %s", format_exception_message(exc))
        return False


def find_live_captions_hwnd() -> int | None:
    if not WIN32_AVAILABLE:
        return None
    found: list[int] = []

    @ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)
    def _enum_proc(hwnd, _lparam):
        try:
            class_name = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(hwnd, class_name, 256)
            if class_name.value == "LiveCaptionsDesktopWindow":
                found.append(int(hwnd))
                return False
        except Exception:
            return True
        return True

    try:
        _user32.EnumWindows(_enum_proc, 0)
    except Exception as exc:
        logger.info("EnumWindows for captions failed: %s", format_exception_message(exc))
        return None
    return found[0] if found else None


def list_top_level_windows() -> list[dict[str, Any]]:
    if not WIN32_AVAILABLE:
        return []
    results: list[dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)
    def _enum_proc(hwnd, _lparam):
        try:
            if not _user32.IsWindowVisible(hwnd) and not _user32.IsIconic(hwnd):
                return True
            title = win_get_title(int(hwnd))
            if not title.strip():
                return True
            pid = win_get_pid(int(hwnd))
            path = win_get_process_path(pid)
            results.append(
                {
                    "hwnd": int(hwnd),
                    "title": title,
                    "class_name": win_get_class(int(hwnd)),
                    "pid": pid,
                    "process_name": path.rsplit("\\", 1)[-1].lower() if path else "",
                    "process_path": path,
                }
            )
        except Exception:
            return True
        return True

    try:
        _user32.EnumWindows(_enum_proc, 0)
    except Exception as exc:
        logger.info("EnumWindows failed: %s", format_exception_message(exc))
    return results


# ---------------------------------------------------------------------------
# Dedicated UIA worker
# ---------------------------------------------------------------------------


class UiaWorker:
    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cancel = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="UiaWorker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._cancel.set()
        self._stop.set()
        try:
            self._jobs.put_nowait(("__stop__", {}, queue.Queue(), None))
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def cancel_pending(self) -> None:
        """Signal in-flight / queued work to abort when possible."""
        self._cancel.set()

    def clear_cancel(self) -> None:
        self._cancel.clear()

    def call(
        self,
        op: str,
        timeout: float = 30.0,
        cancel_event: threading.Event | None = None,
        **kwargs: Any,
    ) -> Any:
        if not UIA_AVAILABLE:
            raise RuntimeError("UIAutomation is not installed.")
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Operation cancelled.")
        reply: queue.Queue = queue.Queue(maxsize=1)
        self._jobs.put((op, kwargs, reply, cancel_event))
        deadline = time.monotonic() + timeout
        while True:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Operation cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"UIA op timed out: {op}")
            try:
                result, err = reply.get(timeout=min(0.2, remaining))
                break
            except queue.Empty:
                continue
        if err:
            raise err
        return result

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                op, kwargs, reply, cancel_event = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            if op == "__stop__":
                break
            if cancel_event and cancel_event.is_set():
                reply.put((None, RuntimeError("Operation cancelled.")))
                continue
            try:
                with uia.UIAutomationInitializerInThread():
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("Operation cancelled.")
                    result = self._dispatch(op, kwargs)
                reply.put((result, None))
            except Exception as exc:
                reply.put((None, exc))

    def _dispatch(self, op: str, kwargs: dict[str, Any]) -> Any:
        if op == "get_captions_text":
            return self._get_captions_text()
        if op == "discover_gpt_targets":
            return self._discover_gpt_targets()
        if op == "activate_browser_tab":
            return self._activate_browser_tab(
                kwargs["hwnd"],
                kwargs.get("tab_name") or "",
                kwargs.get("tab_ordinal"),
                kwargs.get("tab_runtime_id"),
            )
        if op == "get_active_tab_info":
            return self._get_active_tab_info(kwargs["hwnd"])
        if op == "get_browser_url":
            return self._get_browser_url(kwargs["hwnd"])
        if op == "find_and_invoke_captions_close":
            return self._find_and_invoke_captions_close(kwargs.get("hwnd"))
        if op == "paste_and_enter":
            return self._paste_and_enter(
                kwargs["hwnd"],
                kind=kwargs.get("kind") or "",
                require_url_check=bool(kwargs.get("require_url_check")),
            )
        raise ValueError(f"Unknown UIA op: {op}")

    def _control_rect(self, ctrl) -> tuple[int, int, int, int]:
        try:
            rect = ctrl.BoundingRectangle
            left = int(getattr(rect, "left", 0) or 0)
            top = int(getattr(rect, "top", 0) or 0)
            right = int(getattr(rect, "right", left) or left)
            bottom = int(getattr(rect, "bottom", top) or top)
            return left, top, right, bottom
        except Exception:
            return 0, 0, 0, 0

    def _get_captions_text(self) -> str:
        """
        Prefer verified AutomationIds; exclude chrome labels; join caption lines
        ordered by screen position. Fallback for Windows version variance.
        """
        hwnd = find_live_captions_hwnd()
        if not hwnd:
            return ""
        root = uia.ControlFromHandle(hwnd)
        by_aid: list[tuple[int, int, str]] = []
        textblocks: list[tuple[int, int, str]] = []
        fallback: list[tuple[int, int, str]] = []

        for child, _depth in uia.WalkControl(root, includeTop=False, maxDepth=10):
            try:
                ctype = child.ControlTypeName
                class_name = child.ClassName or ""
                name = (child.Name or "").strip()
                if not name or len(name) < 2:
                    continue
                if is_excluded_caption_label(name):
                    continue
                aid = (getattr(child, "AutomationId", "") or "").lower()
                left, top, _r, _b = self._control_rect(child)
                item = (top, left, name)
                if "caption" in aid or aid in ("captionscrollviewer", "captiontext"):
                    by_aid.append(item)
                elif ctype == "TextControl" and class_name == "TextBlock":
                    textblocks.append(item)
                elif "Text" in ctype and len(name) > 8:
                    fallback.append(item)
            except Exception:
                continue

        pool = by_aid or textblocks or fallback
        if not pool:
            return ""
        pool.sort(key=lambda t: (t[0], t[1]))
        # Join distinct lines in reading order
        lines: list[str] = []
        seen: set[str] = set()
        for _t, _l, name in pool:
            if name in seen:
                continue
            seen.add(name)
            lines.append(name)
        return "\n".join(lines)

    def _runtime_id_str(self, ctrl) -> str | None:
        try:
            rid = getattr(ctrl, "GetRuntimeId", None)
            if callable(rid):
                value = rid()
                if value is not None:
                    return str(list(value))
        except Exception:
            pass
        try:
            value = getattr(ctrl, "RuntimeId", None)
            if value is not None:
                return str(list(value) if not isinstance(value, str) else value)
        except Exception:
            pass
        return None

    def _walk_tab_items(self, hwnd: int) -> list[dict[str, Any]]:
        tabs: list[dict[str, Any]] = []
        root = uia.ControlFromHandle(hwnd)
        ordinal = 0
        for child, _depth in uia.WalkControl(root, includeTop=False, maxDepth=22):
            try:
                if child.ControlTypeName != "TabItemControl":
                    continue
                name = (child.Name or "").strip()
                if not name:
                    continue
                selected = False
                try:
                    pattern = child.GetSelectionItemPattern()
                    if pattern:
                        selected = bool(pattern.IsSelected)
                except Exception:
                    selected = False
                tabs.append(
                    {
                        "name": name,
                        "selected": selected,
                        "ordinal": ordinal,
                        "runtime_id": self._runtime_id_str(child),
                    }
                )
                ordinal += 1
            except Exception:
                continue
        return tabs

    def _discover_gpt_targets(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for win in list_top_level_windows():
            hwnd = win["hwnd"]
            title = win["title"]
            proc = win["process_name"]
            path = win.get("process_path") or ""
            kind = classify_window_kind(title, proc, path)

            if kind in ("desktop_app", "companion"):
                if not is_chatgpt_title(title):
                    continue
                candidates.append(
                    {
                        "kind": kind,
                        "hwnd": hwnd,
                        "title": title,
                        "process_name": proc,
                        "process_path": path,
                        "process_id": win["pid"],
                        "tab_name": None,
                        "tab_ordinal": None,
                        "tab_runtime_id": None,
                    }
                )
                continue

            if kind != "browser_window":
                continue
            try:
                tabs = self._walk_tab_items(hwnd)
            except Exception:
                tabs = []
            for tab in tabs:
                if not is_chatgpt_title(tab["name"]):
                    continue
                candidates.append(
                    {
                        "kind": "browser_tab",
                        "hwnd": hwnd,
                        "title": title,
                        "process_name": proc,
                        "process_path": path,
                        "process_id": win["pid"],
                        "tab_name": tab["name"],
                        "tab_ordinal": tab["ordinal"],
                        "tab_runtime_id": tab.get("runtime_id"),
                    }
                )
        return candidates

    def _activate_browser_tab(
        self,
        hwnd: int,
        tab_name: str,
        tab_ordinal: int | None,
        tab_runtime_id: str | None,
    ) -> dict[str, Any]:
        err_msg = (
            "Could not activate the selected ChatGPT tab. "
            "Open the tab manually and try again."
        )
        if not tab_name:
            return {"ok": False, "error": "Missing tab name.", "active_tab": None}

        root = uia.ControlFromHandle(hwnd)
        matched = None
        ordinal = 0
        for child, _depth in uia.WalkControl(root, includeTop=False, maxDepth=22):
            try:
                if child.ControlTypeName != "TabItemControl":
                    continue
                name = (child.Name or "").strip()
                if not name:
                    continue
                rid = self._runtime_id_str(child)
                exact = False
                if tab_runtime_id and rid and rid == tab_runtime_id:
                    exact = True
                elif name == tab_name and (
                    tab_ordinal is None or ordinal == tab_ordinal
                ):
                    exact = True
                if exact:
                    matched = child
                    break
                ordinal += 1
            except Exception:
                continue

        if matched is None:
            return {"ok": False, "error": err_msg, "active_tab": None}

        try:
            pattern = matched.GetSelectionItemPattern()
            if pattern:
                pattern.Select()
            else:
                matched.Click()
        except Exception as exc:
            return {
                "ok": False,
                "error": err_msg,
                "active_tab": None,
                "detail": format_exception_message(exc),
            }

        time.sleep(0.2)
        info = self._get_active_tab_info(hwnd)
        active_name = info.get("name")
        if not tabs_match_exactly(
            tab_name,
            active_name,
            selected_ordinal=tab_ordinal,
            active_ordinal=info.get("ordinal"),
            selected_runtime_id=tab_runtime_id,
            active_runtime_id=info.get("runtime_id"),
        ):
            return {
                "ok": False,
                "error": err_msg,
                "active_tab": active_name,
                "active_ordinal": info.get("ordinal"),
                "active_runtime_id": info.get("runtime_id"),
            }

        url = self._get_browser_url(hwnd)
        if not is_allowed_chatgpt_url(url or ""):
            return {
                "ok": False,
                "error": (
                    "Could not verify ChatGPT page URL "
                    "(need https://chatgpt.com/ or https://chat.openai.com/). "
                    "If using Chrome, try launching with --force-renderer-accessibility."
                ),
                "active_tab": active_name,
                "page_url": url,
            }

        return {
            "ok": True,
            "error": None,
            "active_tab": active_name,
            "active_ordinal": info.get("ordinal"),
            "active_runtime_id": info.get("runtime_id"),
            "page_url": url,
        }

    def _get_active_tab_info(self, hwnd: int) -> dict[str, Any]:
        try:
            tabs = self._walk_tab_items(hwnd)
        except Exception:
            return {"name": None, "ordinal": None, "runtime_id": None}
        for tab in tabs:
            if tab.get("selected"):
                return {
                    "name": tab.get("name"),
                    "ordinal": tab.get("ordinal"),
                    "runtime_id": tab.get("runtime_id"),
                }
        return {"name": None, "ordinal": None, "runtime_id": None}

    def _get_browser_url(self, hwnd: int) -> str | None:
        """Read address-bar URL via UI Automation (best-effort)."""
        root = uia.ControlFromHandle(hwnd)
        keywords = (
            "address",
            "location",
            "url",
            "omnibox",
            "search or type",
            "search with google",
            "search with bing",
        )
        candidates: list[tuple[int, str]] = []
        for child, _depth in uia.WalkControl(root, includeTop=False, maxDepth=18):
            try:
                ctype = child.ControlTypeName
                if ctype not in ("EditControl", "ComboBoxControl", "DocumentControl"):
                    continue
                name = (child.Name or "").lower()
                aid = (getattr(child, "AutomationId", "") or "").lower()
                score = 0
                if any(k in name or k in aid for k in keywords):
                    score += 20
                if "address" in aid or "omnibox" in aid:
                    score += 30
                value = ""
                try:
                    vp = child.GetValuePattern()
                    if vp:
                        value = (vp.Value or "").strip()
                except Exception:
                    value = ""
                if not value:
                    value = (child.Name or "").strip()
                if not value:
                    continue
                if "://" in value or value.startswith("chatgpt.com") or value.startswith(
                    "chat.openai.com"
                ):
                    score += 10
                if score > 0:
                    candidates.append((score, value))
            except Exception:
                continue
        if not candidates:
            return None
        candidates.sort(key=lambda t: t[0], reverse=True)
        return candidates[0][1]

    def _find_and_invoke_captions_close(self, hwnd: int | None) -> dict[str, Any]:
        hwnd = hwnd or find_live_captions_hwnd()
        if not hwnd:
            return {"ok": False, "error": "Live Captions window not found."}
        class_name = win_get_class(hwnd)
        verified = class_name == "LiveCaptionsDesktopWindow"
        if not verified:
            return {
                "ok": False,
                "error": "Window is not a verified Live Captions window.",
            }
        root = uia.ControlFromHandle(hwnd)
        close_btn = None
        for child, _depth in uia.WalkControl(root, includeTop=False, maxDepth=6):
            try:
                if child.ControlTypeName != "ButtonControl":
                    continue
                name = (child.Name or "").lower()
                aid = (getattr(child, "AutomationId", "") or "").lower()
                looks_close = (
                    "close" in name
                    or "close" in aid
                    or name in ("×", "x", "✕")
                    or aid.endswith(("closebutton", "close_button"))
                )
                if close_may_click_button(
                    window_verified_live_captions=True,
                    button_looks_like_close=looks_close,
                ):
                    close_btn = child
                    break
            except Exception:
                continue
        if not close_btn:
            return {
                "ok": False,
                "error": "Verified close control not found for Live Captions.",
            }
        try:
            invoke = close_btn.GetInvokePattern()
            if invoke:
                invoke.Invoke()
            else:
                close_btn.Click()
            return {"ok": True, "error": None}
        except Exception as exc:
            return {"ok": False, "error": format_exception_message(exc)}

    def _is_browser_chrome_field(self, name: str, aid: str) -> bool:
        chrome_hints = (
            "address",
            "omnibox",
            "location",
            "url",
            "search or type a tab",
            "find in page",
            "findinpage",
            "toolbar",
        )
        blob = f"{name} {aid}".lower()
        return any(h in blob for h in chrome_hints)

    def _paste_and_enter(
        self, hwnd: int, *, kind: str, require_url_check: bool
    ) -> dict[str, Any]:
        """
        Verify page URL for browser targets, locate ChatGPT composer (not chrome),
        focus it, verify focus, then Ctrl+V / Enter.
        """
        if require_url_check or kind == "browser_tab":
            url = self._get_browser_url(hwnd)
            if not is_allowed_chatgpt_url(url or ""):
                return {
                    "ok": False,
                    "error": (
                        "Composer blocked: page URL is not an official ChatGPT site."
                    ),
                    "page_url": url,
                }

        root = uia.ControlFromHandle(hwnd)
        composer_keywords = (
            "message",
            "ask anything",
            "ask chatgpt",
            "send a message",
            "composer",
            "prompt",
            "chat input",
        )
        candidates: list[tuple[int, int, Any]] = []

        for child, _depth in uia.WalkControl(root, includeTop=True, maxDepth=28):
            try:
                ctype = child.ControlTypeName
                if ctype not in ("EditControl", "DocumentControl"):
                    continue
                try:
                    if getattr(child, "IsOffscreen", False):
                        continue
                except Exception:
                    pass
                name = (child.Name or "").lower()
                aid = (getattr(child, "AutomationId", "") or "").lower()
                if self._is_browser_chrome_field(name, aid):
                    continue
                # Must be enabled / editable when possible
                try:
                    if hasattr(child, "IsEnabled") and not child.IsEnabled:
                        continue
                except Exception:
                    pass
                score = 0
                if any(k in name or k in aid for k in composer_keywords):
                    score += 25
                if "prosemirror" in aid or "prompt-textarea" in aid:
                    score += 40
                if ctype == "EditControl":
                    score += 2
                left, top, right, bottom = self._control_rect(child)
                height = max(0, bottom - top)
                width = max(0, right - left)
                if height < 20 or width < 80:
                    continue
                # Prefer lower page area (composer), not top chrome
                score += min(bottom // 50, 40)
                if score < 5:
                    continue
                candidates.append((score, bottom, child))
            except Exception:
                continue

        if not candidates:
            return {
                "ok": False,
                "error": "Could not find a verified ChatGPT composer field.",
            }

        candidates.sort(key=lambda t: (t[0], t[1]))
        input_ctrl = candidates[-1][2]
        name = (input_ctrl.Name or "").lower()
        aid = (getattr(input_ctrl, "AutomationId", "") or "").lower()
        if self._is_browser_chrome_field(name, aid):
            return {
                "ok": False,
                "error": "Refusing to paste into browser chrome (address/search).",
            }

        try:
            input_ctrl.SetFocus()
        except Exception:
            try:
                input_ctrl.Click()
            except Exception as exc:
                return {
                    "ok": False,
                    "error": f"Could not focus composer: {format_exception_message(exc)}",
                }

        time.sleep(0.05)
        # Best-effort focus verification
        try:
            focused = uia.GetFocusedControl()
            if focused is not None:
                fname = (focused.Name or "").lower()
                faid = (getattr(focused, "AutomationId", "") or "").lower()
                if self._is_browser_chrome_field(fname, faid):
                    return {
                        "ok": False,
                        "error": "Focus landed on browser chrome; send aborted.",
                    }
        except Exception:
            pass

        try:
            input_ctrl.SendKeys("{Ctrl}v", interval=0, waitTime=0)
            input_ctrl.SendKeys("{Enter}", interval=0, waitTime=0)
            return {"ok": True, "error": None}
        except Exception as exc:
            return {"ok": False, "error": format_exception_message(exc)}


# ---------------------------------------------------------------------------
# Clipboard helpers (send path only)
# ---------------------------------------------------------------------------


def clipboard_try_get() -> tuple[bool, str]:
    """Return (ok, text). ok=False means do not restore (unknown / non-text)."""
    if not PYPERCLIP_AVAILABLE:
        return False, ""
    try:
        value = pyperclip.paste()
        if value is None:
            return True, ""
        return True, str(value)
    except Exception:
        return False, ""


def clipboard_set(text: str) -> None:
    if not PYPERCLIP_AVAILABLE:
        raise RuntimeError("pyperclip is required for Send to GPT.")
    pyperclip.copy(text)


# ---------------------------------------------------------------------------
# Local app lock UI
# ---------------------------------------------------------------------------


def require_local_app_lock() -> bool:
    if tk is None:
        return True
    for attempt in range(5):
        password = _prompt_lock_dialog()
        if password is None:
            return False
        if _password_matches(password):
            return True
        remaining = 4 - attempt
        if remaining:
            messagebox.showerror(
                "Local app lock",
                f"Incorrect password. {remaining} attempt(s) left.",
            )
        else:
            messagebox.showerror("Local app lock", "Too many failed attempts.")
    return False


def _prompt_lock_dialog() -> str | None:
    dialog = tk.Tk()
    dialog.title("Caption Copier — Local App Lock")
    dialog.resizable(False, False)
    dialog.attributes("-topmost", True)
    frame = ttk.Frame(dialog, padding=16)
    frame.pack(fill=tk.BOTH, expand=True)
    ttk.Label(
        frame,
        text="Enter the local app lock password to use Caption Copier.\n"
        "(This is a lightweight gate, not secure authentication.)",
        wraplength=340,
    ).pack(anchor=tk.W, pady=(0, 8))
    password_var = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=password_var, show="•", width=32)
    entry.pack(fill=tk.X, pady=(0, 8))
    entry.focus_set()
    result: list[str | None] = [None]

    def submit() -> None:
        pwd = password_var.get()
        if not pwd:
            messagebox.showerror("Local app lock", "Enter your password.", parent=dialog)
            return
        result[0] = pwd
        dialog.destroy()

    def cancel() -> None:
        result[0] = None
        dialog.destroy()

    buttons = ttk.Frame(frame)
    buttons.pack(fill=tk.X, pady=(8, 0))
    ttk.Button(buttons, text="OK", command=submit).pack(side=tk.RIGHT)
    ttk.Button(buttons, text="Cancel", command=cancel).pack(side=tk.RIGHT, padx=(0, 8))
    dialog.bind("<Return>", lambda _e: submit())
    dialog.bind("<Escape>", lambda _e: cancel())
    dialog.protocol("WM_DELETE_WINDOW", cancel)
    dialog.update_idletasks()
    w, h = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
    x = (dialog.winfo_screenwidth() - w) // 2
    y = (dialog.winfo_screenheight() - h) // 2
    dialog.geometry(f"+{x}+{y}")
    dialog.mainloop()
    return result[0]


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------


@dataclass
class AppState:
    captions_status: str = "Closed"
    capture_status: str = "Idle"
    gpt_status: str = "Not selected"
    send_status: str = "Ready"
    target: GptTarget | None = None
    last_live_snapshot: str = ""
    send_in_progress: bool = False
    open_in_progress: bool = False
    close_in_progress: bool = False
    capture_generation: int = 0
    discovery_in_progress: bool = False


class CaptionCopierApp:
    def __init__(self) -> None:
        if tk is None:
            raise RuntimeError("tkinter is required.")
        self.root = tk.Tk()
        self.root.title("Caption Copier")
        self.root.geometry("780x720")
        self.root.minsize(620, 560)

        self.state = AppState()
        self.ui_queue: queue.Queue = queue.Queue()
        self.uia = UiaWorker()
        self.uia.start()

        self._capture_stop: threading.Event | None = None
        self._capture_thread: threading.Thread | None = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._drain_ui_queue)
        self.root.after(1500, self._periodic_target_check)
        self._set_captions_status("Closed")
        self._refresh_send_enabled()

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        ind = ttk.LabelFrame(main, text="Status", padding=8)
        ind.pack(fill=tk.X, pady=(0, 8))
        self.lbl_captions = ttk.Label(ind, text="Live Captions: Closed")
        self.lbl_captions.pack(anchor=tk.W)
        self.lbl_capture = ttk.Label(ind, text="Capture: Idle")
        self.lbl_capture.pack(anchor=tk.W)
        self.lbl_gpt = ttk.Label(ind, text="GPT target: Not selected")
        self.lbl_gpt.pack(anchor=tk.W)
        self.lbl_send = ttk.Label(ind, text="Send: Ready")
        self.lbl_send.pack(anchor=tk.W)

        controls = ttk.Frame(main)
        controls.pack(fill=tk.X, pady=(0, 8))

        self.btn_open = ttk.Button(controls, text="Open Captions", command=self.open_captions)
        self.btn_open.pack(side=tk.LEFT)
        self.btn_capture = ttk.Button(controls, text="Capture Now", command=self.capture_now)
        self.btn_capture.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_auto = ttk.Button(
            controls, text="Start Auto Capture", command=self.toggle_auto_capture
        )
        self.btn_auto.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(controls, text="Interval (s):").pack(side=tk.LEFT, padx=(12, 4))
        self.interval_var = tk.StringVar(value=str(DEFAULT_INTERVAL_S))
        ttk.Entry(controls, textvariable=self.interval_var, width=6).pack(side=tk.LEFT)

        self.btn_close = ttk.Button(controls, text="Close Captions", command=self.close_captions)
        self.btn_close.pack(side=tk.LEFT, padx=(12, 0))

        gpt_row = ttk.Frame(main)
        gpt_row.pack(fill=tk.X, pady=(0, 8))
        self.btn_select = ttk.Button(
            gpt_row, text="Select GPT Target", command=self.select_gpt_target
        )
        self.btn_select.pack(side=tk.LEFT)
        self.btn_send = ttk.Button(gpt_row, text="Send to GPT", command=self.send_to_gpt)
        self.btn_send.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_copy_live = ttk.Button(
            gpt_row, text="Use Live → Send", command=self.copy_live_to_send
        )
        self.btn_copy_live.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_clear = ttk.Button(
            gpt_row, text="Clear Preview", command=self.clear_send_text
        )
        self.btn_clear.pack(side=tk.LEFT, padx=(8, 0))

        self.status_label = ttk.Label(main, text="Ready", foreground="green")
        self.status_label.pack(anchor=tk.W, pady=(0, 6))

        live_frame = ttk.LabelFrame(
            main, text="Live Caption (read-only — Auto Capture updates this)", padding=8
        )
        live_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        self.live_box = scrolledtext.ScrolledText(
            live_frame, wrap=tk.WORD, height=7, font=("Segoe UI", 10)
        )
        self.live_box.pack(fill=tk.BOTH, expand=True)
        self.live_box.configure(state=tk.DISABLED)

        send_frame = ttk.LabelFrame(
            main,
            text="Text to Send (editable — never overwritten by Auto Capture)",
            padding=8,
        )
        send_frame.pack(fill=tk.BOTH, expand=True)
        self.send_box = scrolledtext.ScrolledText(
            send_frame, wrap=tk.WORD, height=8, font=("Segoe UI", 10)
        )
        self.send_box.pack(fill=tk.BOTH, expand=True)
        self.send_box.bind("<<Modified>>", self._on_send_modified)

        warns = []
        if not UIA_AVAILABLE:
            warns.append("uiautomation")
        if not KEYBOARD_AVAILABLE:
            warns.append("keyboard")
        if not PYPERCLIP_AVAILABLE:
            warns.append("pyperclip")
        if warns:
            self.status_label.config(
                text=f"Missing packages: {', '.join(warns)} — pip install {' '.join(warns)}",
                foreground="orange",
            )

    def _on_send_modified(self, _event=None) -> None:
        try:
            self.send_box.edit_modified(False)
        except Exception:
            pass
        self._refresh_send_enabled()

    def _set_status(self, text: str, color: str = "green") -> None:
        self.status_label.config(text=text, foreground=color)

    def _set_captions_status(self, value: str) -> None:
        self.state.captions_status = value
        self.lbl_captions.config(text=f"Live Captions: {value}")

    def _set_capture_status(self, value: str) -> None:
        self.state.capture_status = value
        self.lbl_capture.config(text=f"Capture: {value}")

    def _set_gpt_status(self, value: str) -> None:
        self.state.gpt_status = value
        self.lbl_gpt.config(text=f"GPT target: {value}")

    def _set_send_status(self, value: str) -> None:
        self.state.send_status = value
        self.lbl_send.config(text=f"Send: {value}")

    def _get_send_text(self) -> str:
        return self.send_box.get("1.0", tk.END).rstrip("\n")

    def _set_live_text(self, text: str) -> None:
        self.live_box.configure(state=tk.NORMAL)
        self.live_box.delete("1.0", tk.END)
        self.live_box.insert(tk.END, text)
        self.live_box.see(tk.END)
        self.live_box.configure(state=tk.DISABLED)
        self.state.last_live_snapshot = text

    def _set_send_text(self, text: str) -> None:
        self.send_box.delete("1.0", tk.END)
        self.send_box.insert(tk.END, text)
        self.send_box.see(tk.END)
        self._refresh_send_enabled()

    def copy_live_to_send(self) -> None:
        live = self.state.last_live_snapshot
        if not live.strip():
            # also read widget
            self.live_box.configure(state=tk.NORMAL)
            live = self.live_box.get("1.0", tk.END).rstrip("\n")
            self.live_box.configure(state=tk.DISABLED)
        if not live.strip():
            messagebox.showinfo("Copy", "Live Caption is empty.", parent=self.root)
            return
        self._set_send_text(live)
        self._set_status("Copied Live Caption into Text to Send.", "green")

    def clear_send_text(self) -> None:
        self._set_send_text("")
        self._set_status("Text to Send cleared.", "#106ba3")

    def _refresh_send_enabled(self) -> None:
        target = self.state.target
        target_selected = target is not None
        target_valid = bool(target and self._quick_validate_target(target))
        if target and not target_valid and self.state.gpt_status != "Target lost":
            self._set_gpt_status("Target lost")
        elif target and target_valid and self.state.gpt_status == "Target lost":
            self._set_gpt_status(f"Selected — {target.display_label[:40]}")
        enabled = can_enable_send(
            preview_text=self._get_send_text(),
            target_selected=target_selected,
            target_valid=target_valid,
            send_in_progress=self.state.send_in_progress,
        )
        try:
            self.btn_send.config(state=tk.NORMAL if enabled else tk.DISABLED)
        except Exception:
            pass

    def _quick_validate_target(self, target: GptTarget) -> bool:
        if not win_is_window(target.hwnd):
            return False
        if not win_is_visible_for_target(target.hwnd):
            return False
        path = win_get_process_path(win_get_pid(target.hwnd)) or target.process_path
        name = process_basename(path) or target.process_name
        ok, _ = validate_target_snapshot(
            hwnd=target.hwnd,
            is_window=True,
            is_visible=True,
            current_title=win_get_title(target.hwnd),
            current_pid=win_get_pid(target.hwnd),
            expected_pid=target.process_id,
            kind=target.kind,
            process_name=name,
            process_path=path,
            tab_name=target.tab_name,
            active_tab_name=None,
            tab_ordinal=target.tab_ordinal,
            tab_runtime_id=target.tab_runtime_id,
            page_url=None,
            require_url=False,
        )
        return ok

    def _post(self, kind: str, **payload: Any) -> None:
        self.ui_queue.put({"kind": kind, **payload})

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                self._handle_ui_message(msg)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_ui_queue)

    def _handle_ui_message(self, msg: dict[str, Any]) -> None:
        kind = msg.get("kind")
        gen = msg.get("generation")
        if gen is not None and not should_accept_capture_generation(
            int(gen), self.state.capture_generation
        ):
            return

        if kind == "status":
            self._set_status(msg.get("text", ""), msg.get("color", "green"))
        elif kind == "captions_status":
            self._set_captions_status(msg["value"])
        elif kind == "capture_status":
            self._set_capture_status(msg["value"])
        elif kind == "gpt_status":
            self._set_gpt_status(msg["value"])
        elif kind == "send_status":
            self._set_send_status(msg["value"])
            if "in_progress" in msg:
                self.state.send_in_progress = bool(msg["in_progress"])
                self._refresh_send_enabled()
        elif kind == "live_caption":
            text = msg.get("text", "")
            if should_refresh_preview(self.state.last_live_snapshot, text):
                self._set_live_text(text)
        elif kind == "error":
            err = msg.get("error", "Unknown error")
            self._set_status(err, "red")
            messagebox.showerror(msg.get("title", "Error"), err, parent=self.root)
        elif kind == "open_done":
            self.state.open_in_progress = False
            self.btn_open.config(state=tk.NORMAL)
        elif kind == "close_done":
            self.state.close_in_progress = False
            self.btn_close.config(state=tk.NORMAL)
        elif kind == "send_done":
            self.state.send_in_progress = False
            self._set_send_status(msg.get("send_status", "Ready"))
            self._refresh_send_enabled()
        elif kind == "auto_stopped":
            self.btn_auto.config(text="Start Auto Capture")
            if self._capture_stop is None or self._capture_stop.is_set():
                self._set_capture_status("Idle")
        elif kind == "discovery_result":
            self.state.discovery_in_progress = False
            callback = msg.get("callback")
            if callable(callback):
                callback(msg.get("items") or [], msg.get("error"))
        elif kind == "discovery_busy":
            pass

    def _periodic_target_check(self) -> None:
        self._refresh_send_enabled()
        self.root.after(1500, self._periodic_target_check)

    # ----- Captions open/close -----

    def open_captions(self) -> None:
        if not KEYBOARD_AVAILABLE:
            messagebox.showerror(
                "Error", "keyboard package required.\nInstall: pip install keyboard"
            )
            return
        if self.state.open_in_progress:
            return
        if find_live_captions_hwnd():
            self._set_captions_status("Ready")
            self._set_status("Live Captions already open.", "green")
            return
        self.state.open_in_progress = True
        self.btn_open.config(state=tk.DISABLED)
        self._set_captions_status("Opening")
        self._set_status("Opening Live Captions…", "blue")
        threading.Thread(target=self._open_captions_worker, daemon=True).start()

    def _open_captions_worker(self) -> None:
        try:
            keyboard.press_and_release("ctrl+win+l")
            deadline = time.monotonic() + CAPTIONS_OPEN_TIMEOUT_S
            delay = 0.05
            while time.monotonic() < deadline:
                if find_live_captions_hwnd():
                    self._post("captions_status", value="Ready")
                    self._post("status", text="Live Captions ready.", color="green")
                    self._post("open_done")
                    return
                time.sleep(delay)
                delay = min(delay * 1.5, 0.4)
            self._post("captions_status", value="Error")
            self._post(
                "error",
                title="Live Captions",
                error="Timed out waiting for Live Captions to open (5s).",
            )
        except Exception as exc:
            self._post("captions_status", value="Error")
            self._post("error", title="Live Captions", error=format_exception_message(exc))
        finally:
            self._post("open_done")

    def close_captions(self) -> None:
        if self.state.close_in_progress:
            return
        hwnd = find_live_captions_hwnd()
        if not hwnd:
            self._set_captions_status("Closed")
            self._set_status("Live Captions already closed.", "green")
            return
        self.state.close_in_progress = True
        self.btn_close.config(state=tk.DISABLED)
        self._set_status("Closing Live Captions…", "blue")
        threading.Thread(
            target=self._close_captions_worker, args=(hwnd,), daemon=True
        ).start()

    def _close_captions_worker(self, hwnd: int) -> None:
        try:
            try:
                result = self.uia.call("find_and_invoke_captions_close", hwnd=hwnd)
            except Exception as exc:
                result = {"ok": False, "error": format_exception_message(exc)}

            if not result.get("ok"):
                self._post("captions_status", value="Error")
                self._post(
                    "error",
                    title="Close Captions",
                    error=str(
                        result.get("error")
                        or "Could not close Live Captions via verified close control."
                    ),
                )
                return

            deadline = time.monotonic() + CAPTIONS_CLOSE_TIMEOUT_S
            delay = 0.05
            while time.monotonic() < deadline:
                if not find_live_captions_hwnd():
                    self._post("captions_status", value="Closed")
                    self._post("status", text="Live Captions closed.", color="green")
                    return
                time.sleep(delay)
                delay = min(delay * 1.5, 0.4)

            self._post("captions_status", value="Error")
            self._post(
                "error",
                title="Close Captions",
                error="Close control was invoked but Live Captions is still open.",
            )
        except Exception as exc:
            self._post(
                "error",
                title="Close Captions",
                error=format_exception_message(exc),
            )
            self._post("captions_status", value="Error")
        finally:
            self._post("close_done")

    # ----- Capture -----

    def capture_now(self) -> None:
        if not UIA_AVAILABLE:
            messagebox.showerror(
                "Error", "uiautomation required.\nInstall: pip install uiautomation"
            )
            return
        self._set_capture_status("Capturing")
        threading.Thread(target=self._capture_once_worker, daemon=True).start()

    def _capture_once_worker(self) -> None:
        try:
            text = self.uia.call("get_captions_text")
            clean = normalize_caption_text(text)
            if not clean:
                self._post("capture_status", value="Error")
                self._post(
                    "status",
                    text="Live Captions not found or empty.",
                    color="#C06010",
                )
                return
            self._post("live_caption", text=clean)
            # First capture also seeds Text to Send if empty
            self._post("capture_status", value="Updated")
            self._post(
                "status",
                text=f"Captured ({len(clean)} chars). Use “Use Live → Send” to edit/send.",
                color="green",
            )
        except Exception as exc:
            self._post("capture_status", value="Error")
            self._post("error", title="Capture", error=format_exception_message(exc))

    def toggle_auto_capture(self) -> None:
        if self._capture_thread and self._capture_thread.is_alive():
            self._stop_auto_capture(join=True)
            return
        if not UIA_AVAILABLE:
            messagebox.showerror(
                "Error", "uiautomation required.\nInstall: pip install uiautomation"
            )
            return
        try:
            interval = validate_capture_interval(
                self.interval_var.get().strip() or DEFAULT_INTERVAL_S
            )
        except ValueError as exc:
            messagebox.showerror("Interval", str(exc), parent=self.root)
            return

        # Do not start until previous worker has fully exited
        if self._capture_thread and self._capture_thread.is_alive():
            self._stop_auto_capture(join=True)
            if self._capture_thread and self._capture_thread.is_alive():
                messagebox.showwarning(
                    "Auto Capture",
                    "Previous capture worker is still stopping. Try again in a moment.",
                    parent=self.root,
                )
                return

        self.state.capture_generation += 1
        generation = self.state.capture_generation
        stop_event = threading.Event()
        self._capture_stop = stop_event
        self.btn_auto.config(text="Stop Auto Capture")
        self._set_capture_status("Capturing")
        self._set_status("Auto capture running…", "#106ba3")
        self._capture_thread = threading.Thread(
            target=self._auto_capture_worker,
            args=(stop_event, interval, generation),
            daemon=True,
            name=f"AutoCapture-{generation}",
        )
        self._capture_thread.start()

    def _stop_auto_capture(self, join: bool = False) -> None:
        if self._capture_stop:
            self._capture_stop.set()
        self.uia.cancel_pending()
        if join and self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=8.0)
        if not (self._capture_thread and self._capture_thread.is_alive()):
            self._capture_stop = None
            self._capture_thread = None
            self.btn_auto.config(text="Start Auto Capture")
            self._set_capture_status("Idle")
        self.uia.clear_cancel()

    def _auto_capture_worker(
        self, stop_event: threading.Event, interval: float, generation: int
    ) -> None:
        try:
            while not stop_event.is_set():
                try:
                    text = self.uia.call(
                        "get_captions_text",
                        cancel_event=stop_event,
                        timeout=15.0,
                    )
                    clean = normalize_caption_text(text)
                    if clean:
                        self._post(
                            "live_caption", text=clean, generation=generation
                        )
                        self._post(
                            "capture_status",
                            value="Updated",
                            generation=generation,
                        )
                        self._post(
                            "status",
                            text=f"Auto-captured ({len(clean)} chars).",
                            color="green",
                            generation=generation,
                        )
                except Exception as exc:
                    if stop_event.is_set():
                        break
                    self._post(
                        "capture_status", value="Error", generation=generation
                    )
                    self._post(
                        "status",
                        text=format_exception_message(exc),
                        color="red",
                        generation=generation,
                    )
                    time.sleep(1.0)
                    continue
                stop_event.wait(interval)
        finally:
            self._post("auto_stopped", generation=generation)

    # ----- GPT target selection (async) -----

    def select_gpt_target(self) -> None:
        if not UIA_AVAILABLE:
            messagebox.showerror(
                "Error", "uiautomation required.\nInstall: pip install uiautomation"
            )
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Select GPT Target")
        dialog.geometry("740x440")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text=(
                "Verified ChatGPT desktop process, or browser TabItem "
                "(exact tab). URL checked on send."
            ),
        ).pack(anchor=tk.W, padx=10, pady=(10, 6))

        listbox = tk.Listbox(dialog, height=14)
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        candidates: list[GptTarget] = []

        status = ttk.Label(dialog, text="")
        status.pack(anchor=tk.W, padx=10)

        btn_refresh = ttk.Button(dialog, text="Refresh")
        searching = {"on": False}

        def apply_results(items: list, error: str | None) -> None:
            if not dialog.winfo_exists():
                return
            searching["on"] = False
            btn_refresh.config(state=tk.NORMAL)
            listbox.delete(0, tk.END)
            candidates.clear()
            if error:
                status.config(text=error, foreground="red")
                listbox.insert(tk.END, "(Discovery failed — see status)")
                return
            status.config(text="", foreground="black")
            for item in items:
                target = GptTarget(
                    kind=item["kind"],
                    hwnd=int(item["hwnd"]),
                    title=item["title"],
                    process_name=item.get("process_name") or "",
                    process_path=item.get("process_path") or "",
                    process_id=int(item.get("process_id") or 0),
                    tab_name=item.get("tab_name"),
                    tab_ordinal=item.get("tab_ordinal"),
                    tab_runtime_id=item.get("tab_runtime_id"),
                )
                candidates.append(target)
                listbox.insert(tk.END, target.display_label)
            if not candidates:
                listbox.insert(
                    tk.END,
                    "(No verified targets — open ChatGPT app or a ChatGPT browser tab)",
                )

        def refresh() -> None:
            if searching["on"]:
                return
            searching["on"] = True
            btn_refresh.config(state=tk.DISABLED)
            listbox.delete(0, tk.END)
            listbox.insert(tk.END, "Searching…")
            status.config(text="Searching for GPT targets…", foreground="blue")

            def worker() -> None:
                try:
                    raw = self.uia.call("discover_gpt_targets", timeout=25.0)
                    self._post(
                        "discovery_result",
                        items=raw,
                        error=None,
                        callback=apply_results,
                    )
                except Exception as exc:
                    self._post(
                        "discovery_result",
                        items=[],
                        error=format_exception_message(exc),
                        callback=apply_results,
                    )

            threading.Thread(target=worker, daemon=True).start()

        def on_ok() -> None:
            if not candidates:
                return
            try:
                idx = int(listbox.curselection()[0])
            except Exception:
                messagebox.showwarning("Select", "Select a target.", parent=dialog)
                return
            if idx < 0 or idx >= len(candidates):
                return
            self.state.target = candidates[idx]
            self._set_gpt_status(f"Selected — {self.state.target.display_label[:48]}")
            self._refresh_send_enabled()
            self._set_status("GPT target selected.", "green")
            dialog.destroy()

        btns = ttk.Frame(dialog)
        btns.pack(fill=tk.X, pady=10, padx=10)
        btn_refresh.config(command=refresh)
        btn_refresh.pack(side=tk.LEFT)
        ttk.Button(btns, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="OK", command=on_ok).pack(side=tk.RIGHT, padx=(0, 8))

        refresh()
        dialog.wait_window(dialog)

    # ----- Send to GPT -----

    def send_to_gpt(self) -> None:
        if self.state.send_in_progress:
            return
        text = self._get_send_text()
        if not preview_has_sendable_text(text):
            messagebox.showwarning("Send", "Text to Send is empty.", parent=self.root)
            return
        target = self.state.target
        if not target:
            messagebox.showwarning("Send", "Select a GPT target first.", parent=self.root)
            return
        if not self._quick_validate_target(target):
            self._set_gpt_status("Target lost")
            self._refresh_send_enabled()
            messagebox.showerror(
                "Send",
                "GPT target is no longer valid. Select it again.",
                parent=self.root,
            )
            return
        if not PYPERCLIP_AVAILABLE:
            messagebox.showerror("Send", "pyperclip is required.", parent=self.root)
            return

        self.state.send_in_progress = True
        self._set_send_status("Sending")
        self._refresh_send_enabled()
        threading.Thread(
            target=self._send_worker, args=(text, target), daemon=True, name="SendGPT"
        ).start()

    def _send_worker(self, text: str, target: GptTarget) -> None:
        clip_ok, previous_clip = clipboard_try_get()
        try:
            if not win_is_window(target.hwnd):
                self._fail_send("Target window no longer exists.")
                return
            if not win_is_visible_for_target(target.hwnd):
                self._fail_send("Target window is not visible.")
                return

            path = win_get_process_path(win_get_pid(target.hwnd)) or target.process_path
            name = process_basename(path) or target.process_name
            page_url = None
            active_tab = None
            active_ord = None
            active_rid = None

            if target.kind == "browser_tab":
                try:
                    act = self.uia.call(
                        "activate_browser_tab",
                        hwnd=target.hwnd,
                        tab_name=target.tab_name or "",
                        tab_ordinal=target.tab_ordinal,
                        tab_runtime_id=target.tab_runtime_id,
                    )
                except Exception as exc:
                    self._fail_send(format_exception_message(exc))
                    return
                if not act.get("ok"):
                    self._fail_send(
                        act.get("error")
                        or (
                            "Could not activate the selected ChatGPT tab. "
                            "Open the tab manually and try again."
                        )
                    )
                    return
                active_tab = act.get("active_tab")
                active_ord = act.get("active_ordinal")
                active_rid = act.get("active_runtime_id")
                page_url = act.get("page_url")

            ok, reason = validate_target_snapshot(
                hwnd=target.hwnd,
                is_window=True,
                is_visible=win_is_visible_for_target(target.hwnd),
                current_title=win_get_title(target.hwnd),
                current_pid=win_get_pid(target.hwnd),
                expected_pid=target.process_id,
                kind=target.kind,
                process_name=name,
                process_path=path,
                tab_name=target.tab_name,
                active_tab_name=active_tab,
                tab_ordinal=target.tab_ordinal,
                active_tab_ordinal=active_ord,
                tab_runtime_id=target.tab_runtime_id,
                active_tab_runtime_id=active_rid,
                page_url=page_url,
                require_url=(target.kind == "browser_tab"),
            )
            if not ok:
                self._fail_send(reason)
                return

            focus_ok = win_force_foreground(target.hwnd)
            if not focus_ok:
                self._fail_send(
                    "Could not activate the ChatGPT target. Focus it manually and retry."
                )
                return

            fg = _user32.GetForegroundWindow() if WIN32_AVAILABLE else None
            if not fg or int(fg) != int(target.hwnd):
                self._fail_send(
                    "Foreground window verification failed. Nothing was pasted."
                )
                return

            if target.kind == "browser_tab":
                try:
                    info = self.uia.call("get_active_tab_info", hwnd=target.hwnd)
                    url2 = self.uia.call("get_browser_url", hwnd=target.hwnd)
                except Exception as exc:
                    self._fail_send(format_exception_message(exc))
                    return
                if not tabs_match_exactly(
                    target.tab_name,
                    info.get("name"),
                    selected_ordinal=target.tab_ordinal,
                    active_ordinal=info.get("ordinal"),
                    selected_runtime_id=target.tab_runtime_id,
                    active_runtime_id=info.get("runtime_id"),
                ):
                    self._fail_send(
                        "Could not activate the selected ChatGPT tab. "
                        "Open the tab manually and try again."
                    )
                    return
                if not is_allowed_chatgpt_url(url2 or ""):
                    self._fail_send(
                        "Active page URL is not an official ChatGPT site. Nothing was pasted."
                    )
                    return

            if not safe_focus_allows_paste(True, True):
                self._fail_send("Focus verification failed. Nothing was pasted.")
                return

            try:
                clipboard_set(text)
            except Exception as exc:
                self._fail_send(format_exception_message(exc))
                return

            try:
                paste_result = self.uia.call(
                    "paste_and_enter",
                    hwnd=target.hwnd,
                    kind=target.kind,
                    require_url_check=(target.kind == "browser_tab"),
                )
            except Exception as exc:
                self._fail_send(format_exception_message(exc))
                return

            if not paste_result.get("ok"):
                self._fail_send(paste_result.get("error") or "Paste/Enter failed.")
                return

            self._post("send_status", value="Sent", in_progress=False)
            self._post("status", text="Sent to GPT.", color="green")
            self._post("send_done", send_status="Sent")
        except Exception as exc:
            self._fail_send(format_exception_message(exc))
        finally:
            if clip_ok:
                try:
                    clipboard_set(previous_clip)
                except Exception:
                    pass

    def _fail_send(self, message: str) -> None:
        self._post("send_status", value="Failed", in_progress=False)
        self._post("send_done", send_status="Failed")
        self._post("error", title="Send to GPT", error=message)

    def _on_close(self) -> None:
        self._stop_auto_capture(join=True)
        self.uia.stop(timeout=2.0)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    _setup_logging()
    logger.info("Caption Copier starting")
    if not require_local_app_lock():
        sys.exit(0)
    app = CaptionCopierApp()
    app.run()


if __name__ == "__main__":
    main()
