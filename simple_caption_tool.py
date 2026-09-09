#!/usr/bin/env python3
"""
Simple Caption Copier

- Copy Now / Start Auto with live caption text
- Live editable preview with Clear
- Open/Close Windows Live Captions
- Send new captions to ChatGPT (manual or auto-send)
"""

import hashlib
import secrets
import sys
import ctypes
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import time
import pyperclip

PBKDF2_ITERATIONS = 120_000
# Built-in unlock credentials (password is not stored in plain text).
_AUTH_SALT_HEX = "a3f8c21e9b4d7056e1ac2f83d5b79c04"
_AUTH_HASH_HEX = "1a50743531d1421301ee5768622447c132b130fce747445d074491eec91ef28d"

try:
    import uiautomation as uia
    UIA_AVAILABLE = True
except ImportError:
    UIA_AVAILABLE = False

try:
    import keyboard
    KEYBOARD_AVAILABLE = True
    if hasattr(keyboard, "DEFAULT_KEYBOARD_DELAY"):
        keyboard.DEFAULT_KEYBOARD_DELAY = 0
except ImportError:
    KEYBOARD_AVAILABLE = False

try:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    WIN32_AVAILABLE = True
except Exception:
    WIN32_AVAILABLE = False


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


def _prompt_password_dialog(title: str, prompt: str) -> str | None:
    """Show a modal password dialog. Returns the password or None if cancelled."""
    dialog = tk.Tk()
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.attributes("-topmost", True)

    frame = ttk.Frame(dialog, padding=16)
    frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frame, text=prompt, wraplength=320).pack(anchor=tk.W, pady=(0, 8))

    password_var = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=password_var, show="•", width=32)
    entry.pack(fill=tk.X, pady=(0, 8))
    entry.focus_set()

    result: list[str | None] = [None]

    def submit() -> None:
        pwd = password_var.get()
        if not pwd:
            messagebox.showerror("Password", "Enter your password.", parent=dialog)
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


def require_password() -> bool:
    """Ask for the app password on every launch."""
    for attempt in range(5):
        password = _prompt_password_dialog(
            "Caption Copier - Unlock",
            "Enter your password to use Caption Copier.",
        )
        if password is None:
            return False
        if _password_matches(password):
            return True
        remaining = 4 - attempt
        if remaining:
            messagebox.showerror(
                "Wrong password",
                f"Incorrect password. {remaining} attempt(s) left.",
            )
        else:
            messagebox.showerror("Wrong password", "Too many failed attempts.")
    return False


def _find_captions_hwnd_fast() -> int | None:
    """Find Live Captions window handle via Win32 (much faster than UIA tree scan)."""
    if not WIN32_AVAILABLE:
        return None

    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum_proc(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        class_name = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, class_name, 256)
        if class_name.value == "LiveCaptionsDesktopWindow":
            found.append(int(hwnd))
            return False
        return True

    try:
        _user32.EnumWindows(_enum_proc, 0)
    except Exception:
        return None
    return found[0] if found else None


live_captions_hwnd: int | None = None
captions_text_ctrl = None
_captions_empty_streak = 0


def _invalidate_captions_text_cache() -> None:
    global captions_text_ctrl, _captions_empty_streak
    captions_text_ctrl = None
    _captions_empty_streak = 0


def _refresh_captions_hwnd() -> int | None:
    """Return cached Live Captions HWND, refreshing only when needed."""
    global live_captions_hwnd

    if live_captions_hwnd and WIN32_AVAILABLE:
        try:
            if _user32.IsWindow(live_captions_hwnd) and _user32.IsWindowVisible(live_captions_hwnd):
                return live_captions_hwnd
        except Exception:
            pass

    live_captions_hwnd = _find_captions_hwnd_fast()
    if not live_captions_hwnd:
        _invalidate_captions_text_cache()
    return live_captions_hwnd


def _is_captions_open() -> bool:
    return _refresh_captions_hwnd() is not None


def _toggle_captions_shortcut() -> None:
    """Fire the Live Captions toggle shortcut with no artificial delay."""
    keyboard.press_and_release("ctrl+win+l")


def find_live_captions_root():
    """Find the Windows Live Captions window. Caller must own UIA COM init."""
    if not UIA_AVAILABLE:
        return None

    hwnd = _refresh_captions_hwnd()
    if hwnd:
        try:
            return uia.ControlFromHandle(hwnd)
        except Exception:
            pass

    try:
        root_control = uia.GetRootControl()
        for ctrl in root_control.GetChildren():
            try:
                class_name = ctrl.ClassName
                name = ctrl.Name
                if class_name == "LiveCaptionsDesktopWindow" or (name and "Live Captions" in name):
                    hwnd = getattr(ctrl, "NativeWindowHandle", None)
                    if hwnd:
                        global live_captions_hwnd
                        live_captions_hwnd = int(hwnd)
                    return ctrl
            except Exception:
                continue
    except Exception:
        pass
    return None


def _read_control_text(ctrl) -> str:
    """Read text from a UIA control via Name and ValuePattern."""
    try:
        txt = (ctrl.Name or "").strip()
        if txt:
            return txt
    except Exception:
        pass
    try:
        value_pattern = ctrl.GetValuePattern()
        if value_pattern:
            txt = (value_pattern.Value or "").strip()
            if txt:
                return txt
    except Exception:
        pass
    return ""


def _is_ui_chrome_text(text: str) -> bool:
    """Filter out Live Captions chrome labels that are not the transcript."""
    lowered = text.strip().lower()
    if not lowered:
        return True
    chrome = (
        "live captions",
        "settings",
        "more options",
        "microphone",
        "position",
        "caption style",
        "language",
        "close",
    )
    return lowered in chrome or (len(lowered) < 3 and not any(c.isalnum() for c in lowered))


def _scan_captions_text_blocks(root) -> tuple[str, object | None]:
    """
    Walk Live Captions UI and pick the best transcript TextBlock.
    Returns (text, control). Prefers the longest non-chrome TextBlock.
    """
    best_text = ""
    best_ctrl = None
    candidates: list[tuple[str, object]] = []

    for child, _depth in uia.WalkControl(root, includeTop=True, maxDepth=10):
        try:
            ctype = child.ControlTypeName
            class_name = (child.ClassName or "")
            if ctype not in ("TextControl", "DocumentControl", "EditControl"):
                continue
            if ctype == "TextControl" and class_name and class_name != "TextBlock":
                # Still allow unnamed/other text controls as fallback
                pass

            txt = _read_control_text(child)
            if not txt or _is_ui_chrome_text(txt):
                continue
            candidates.append((txt, child))
            if len(txt) > len(best_text):
                best_text = txt
                best_ctrl = child
        except Exception:
            continue

    if best_text:
        return best_text, best_ctrl

    # Fallback: any text-like control with content
    for child, _depth in uia.WalkControl(root, includeTop=True, maxDepth=10):
        try:
            if "Text" not in (child.ControlTypeName or ""):
                continue
            txt = _read_control_text(child)
            if txt and not _is_ui_chrome_text(txt) and len(txt) > len(best_text):
                best_text = txt
                best_ctrl = child
        except Exception:
            continue

    return best_text, best_ctrl


def get_captions_text() -> str:
    """
    Get Live Captions transcript text.

    Caller should own UIAutomationInitializerInThread when used from a worker
    thread. Uses a cached TextBlock but re-scans when empty/stale.
    """
    global captions_text_ctrl, _captions_empty_streak

    if not UIA_AVAILABLE:
        return ""

    if not _refresh_captions_hwnd():
        _invalidate_captions_text_cache()
        return ""

    try:
        # Prefer cached control only if it still returns real transcript text.
        if captions_text_ctrl is not None:
            try:
                cached_txt = _read_control_text(captions_text_ctrl)
                if cached_txt and not _is_ui_chrome_text(cached_txt):
                    _captions_empty_streak = 0
                    return cached_txt
                # Empty or chrome → drop cache and re-scan
                captions_text_ctrl = None
            except Exception:
                captions_text_ctrl = None

        root = find_live_captions_root()
        if not root:
            _captions_empty_streak += 1
            if _captions_empty_streak >= 3:
                _invalidate_captions_text_cache()
            return ""

        text, ctrl = _scan_captions_text_blocks(root)
        if text:
            captions_text_ctrl = ctrl
            _captions_empty_streak = 0
            return text

        _captions_empty_streak += 1
        if _captions_empty_streak >= 3:
            _invalidate_captions_text_cache()
            # Force HWND re-discovery next time in case window was recreated
            global live_captions_hwnd
            live_captions_hwnd = None
    except Exception:
        _invalidate_captions_text_cache()
    return ""


def _wait_for_captions_state(opened: bool, attempts: int = 20, interval: float = 0.05) -> bool:
    """Poll until captions reach the expected open/closed state."""
    for _ in range(attempts):
        if _is_captions_open() == opened:
            return True
        time.sleep(interval)
    return _is_captions_open() == opened


def _warm_captions_text_after_open() -> None:
    """After open, clear stale cache and wait briefly for transcript TextBlocks."""
    _invalidate_captions_text_cache()
    try:
        with uia.UIAutomationInitializerInThread():
            for _ in range(12):
                text = get_captions_text()
                if text:
                    return
                time.sleep(0.08)
    except Exception:
        _invalidate_captions_text_cache()


def _set_captions_status(text: str, color: str) -> None:
    root.after(0, lambda: status_label.config(text=text, foreground=color))


def _open_captions_worker() -> None:
    global live_captions_hwnd

    try:
        if _is_captions_open():
            _warm_captions_text_after_open()
            _set_captions_status("Captions: OPEN", "green")
            root.after(0, _update_captions_indicator)
            return

        _toggle_captions_shortcut()
        if _wait_for_captions_state(opened=True):
            _warm_captions_text_after_open()
            _set_captions_status("Captions: OPEN", "green")
            root.after(0, _update_captions_indicator)
        else:
            _set_captions_status("Captions: Not detected", "orange")
            root.after(0, _update_captions_indicator)
    except Exception as e:
        root.after(0, lambda: messagebox.showerror("Error", f"Failed to open captions: {e}"))


def open_captions():
    """Open Windows Live Captions."""
    if not KEYBOARD_AVAILABLE:
        messagebox.showerror("Error", "Keyboard library not available.\nInstall: pip install keyboard")
        return

    if _is_captions_open():
        status_label.config(text="Captions: OPEN", foreground="green")
        _update_captions_indicator()
        return

    status_label.config(text="Opening...", foreground="blue")
    threading.Thread(target=_open_captions_worker, daemon=True).start()


def _button_right_edge(button) -> float:
    """Horizontal position of a control's right edge (for finding the title-bar X)."""
    try:
        rect = button.BoundingRectangle
        if not rect:
            return 0.0
        if hasattr(rect, "right"):
            return float(rect.right)
        if hasattr(rect, "width"):
            return float(rect.left + rect.width)
        if isinstance(rect, (tuple, list)) and len(rect) >= 4:
            return float(rect[0] + rect[2])
    except Exception:
        pass
    return 0.0


def click_captions_close_button() -> bool:
    """Click the Live Captions window close (X) button via UI Automation."""
    if not UIA_AVAILABLE:
        return False

    try:
        with uia.UIAutomationInitializerInThread():
            root_caption = find_live_captions_root()
            if not root_caption:
                return False

            close_button = None
            rightmost_button = None
            rightmost_x = -1.0

            for child, _depth in uia.WalkControl(root_caption, includeTop=False, maxDepth=4):
                try:
                    if child.ControlTypeName != "ButtonControl":
                        continue

                    name = (child.Name or "").lower()
                    automation_id = (child.AutomationId or "").lower()

                    if (
                        "close" in name
                        or "close" in automation_id
                        or name in ("×", "x", "✕")
                        or automation_id.endswith(("closebutton", "close_button"))
                    ):
                        close_button = child
                        break

                    x = _button_right_edge(child)
                    if x > rightmost_x:
                        rightmost_x = x
                        rightmost_button = child
                except Exception:
                    continue

            close_button = close_button or rightmost_button
            if not close_button:
                return False

            try:
                invoke = close_button.GetInvokePattern()
                if invoke:
                    invoke.Invoke()
                    return True
            except Exception:
                pass

            close_button.Click()
            return True
    except Exception:
        return False


def _close_captions_worker() -> None:
    global live_captions_hwnd

    try:
        if not _is_captions_open():
            _set_captions_status("Captions: CLOSED", "green")
            return

        _toggle_captions_shortcut()
        if _wait_for_captions_state(opened=False):
            live_captions_hwnd = None
            _invalidate_captions_text_cache()
            _set_captions_status("Captions: CLOSED", "green")
            root.after(0, _update_captions_indicator)
            return

        if click_captions_close_button() and _wait_for_captions_state(opened=False, attempts=6):
            live_captions_hwnd = None
            _invalidate_captions_text_cache()
            _set_captions_status("Captions: CLOSED", "green")
            root.after(0, _update_captions_indicator)
            return

        if _is_captions_open():
            _set_captions_status("Captions: Still open", "orange")
        else:
            live_captions_hwnd = None
            _invalidate_captions_text_cache()
            _set_captions_status("Captions: CLOSED", "green")
            root.after(0, _update_captions_indicator)
    except Exception as e:
        root.after(0, lambda: messagebox.showerror("Error", f"Failed to close captions: {e}"))


def close_captions():
    """Close Windows Live Captions quickly using the toggle shortcut."""
    if not KEYBOARD_AVAILABLE:
        messagebox.showerror("Error", "Keyboard library not available.\nInstall: pip install keyboard")
        return

    if not _is_captions_open():
        status_label.config(text="Captions: CLOSED", foreground="green")
        _update_captions_indicator()
        return

    status_label.config(text="Closing...", foreground="blue")
    threading.Thread(target=_close_captions_worker, daemon=True).start()


# --- ChatGPT send ---

chatgpt_window_title: str | None = None
chatgpt_hwnd: int | None = None
chatgpt_input_ctrl = None


def _force_foreground(hwnd: int | None) -> bool:
    """Bring a window to the foreground reliably (works even right after a button click)."""
    if not hwnd or not WIN32_AVAILABLE:
        return False
    try:
        if _user32.GetForegroundWindow() == hwnd:
            return True

        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, 9)  # SW_RESTORE

        current_thread = _kernel32.GetCurrentThreadId()
        fg_hwnd = _user32.GetForegroundWindow()
        fg_thread = _user32.GetWindowThreadProcessId(fg_hwnd, None)
        target_thread = _user32.GetWindowThreadProcessId(hwnd, None)

        attached_fg = False
        attached_target = False
        try:
            if fg_thread and fg_thread != current_thread:
                attached_fg = bool(_user32.AttachThreadInput(current_thread, fg_thread, True))
            if target_thread and target_thread != current_thread:
                attached_target = bool(_user32.AttachThreadInput(current_thread, target_thread, True))

            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
            _user32.SetActiveWindow(hwnd)
        finally:
            if attached_target:
                _user32.AttachThreadInput(current_thread, target_thread, False)
            if attached_fg:
                _user32.AttachThreadInput(current_thread, fg_thread, False)

        return _user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _cache_chatgpt_input() -> None:
    """Find and cache the ChatGPT prompt field (slow path, not on every send)."""
    global chatgpt_input_ctrl
    chatgpt_input_ctrl = _find_chatgpt_input_control(chatgpt_hwnd)


def _paste_and_submit_to_input(text: str) -> bool:
    """Paste text into ChatGPT input and press Enter in one shot."""
    global chatgpt_input_ctrl

    if not UIA_AVAILABLE or not chatgpt_hwnd:
        return False

    try:
        with uia.UIAutomationInitializerInThread():
            input_ctrl = chatgpt_input_ctrl or _find_chatgpt_input_control(chatgpt_hwnd)
            if not input_ctrl:
                return False
            chatgpt_input_ctrl = input_ctrl

            try:
                input_ctrl.SetFocus()
            except Exception:
                try:
                    input_ctrl.Click()
                except Exception:
                    return False

            pyperclip.copy(text)
            input_ctrl.SendKeys("{Ctrl}v", interval=0, waitTime=0)
            input_ctrl.SendKeys("{Enter}", interval=0, waitTime=0)
        return True
    except Exception:
        return False


def _warm_chatgpt_cache() -> None:
    """Pre-detect ChatGPT window in the background so the first send works."""
    global chatgpt_window_title, chatgpt_hwnd

    if chatgpt_hwnd:
        return

    matches = _match_chatgpt_windows()
    if len(matches) == 1:
        chatgpt_window_title = matches[0]
        chatgpt_hwnd = _get_hwnd_by_title(matches[0])
        _cache_chatgpt_input()
        root.after(0, _update_chatgpt_indicator)


def _list_top_level_window_titles() -> list[str]:
    """Return titles of all top-level windows."""
    if not UIA_AVAILABLE:
        return []
    titles: list[str] = []
    try:
        with uia.UIAutomationInitializerInThread():
            root_ctrl = uia.GetRootControl()
            for ctrl in root_ctrl.GetChildren():
                try:
                    name = (ctrl.Name or "").strip()
                    if name:
                        titles.append(name)
                except Exception:
                    continue
    except Exception:
        return []
    return titles


def _match_chatgpt_windows() -> list[str]:
    """Best-effort match for ChatGPT browser/desktop windows."""
    needles = ("chatgpt", "chat gpt", "openai", "chat.openai")
    return [t for t in _list_top_level_window_titles() if any(n in t.lower() for n in needles)]


def _pick_window_dialog(titles: list[str], dialog_title: str, prompt: str) -> str | None:
    """Show a list dialog and return the chosen window title."""
    if not titles:
        return None
    if len(titles) == 1:
        return titles[0]

    dialog = tk.Toplevel(root)
    dialog.title(dialog_title)
    dialog.geometry("600x320")
    dialog.transient(root)
    dialog.grab_set()

    ttk.Label(dialog, text=prompt).pack(pady=(10, 8), padx=10, anchor=tk.W)

    listbox = tk.Listbox(dialog, height=min(12, len(titles)))
    for title in titles:
        listbox.insert(tk.END, title)
    listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
    listbox.selection_set(0)

    chosen: dict[str, str | None] = {"title": None}

    def on_ok() -> None:
        try:
            idx = int(listbox.curselection()[0])
            chosen["title"] = titles[idx]
        except Exception:
            chosen["title"] = None
        dialog.destroy()

    btns = ttk.Frame(dialog)
    btns.pack(pady=(0, 10))
    ttk.Button(btns, text="OK", command=on_ok).pack(side=tk.LEFT, padx=6)
    ttk.Button(btns, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT, padx=6)

    dialog.focus_set()
    dialog.wait_window(dialog)
    return chosen["title"]


def _get_hwnd_by_title(window_title: str) -> int | None:
    """Get NativeWindowHandle for a top-level window by exact title match."""
    if not UIA_AVAILABLE:
        return None
    try:
        with uia.UIAutomationInitializerInThread():
            root_ctrl = uia.GetRootControl()
            for ctrl in root_ctrl.GetChildren():
                try:
                    name = (ctrl.Name or "").strip()
                    if name == window_title:
                        hwnd = getattr(ctrl, "NativeWindowHandle", None)
                        return int(hwnd) if hwnd else None
                except Exception:
                    continue
    except Exception:
        return None
    return None


def _focus_window(window_title: str | None, hwnd: int | None) -> bool:
    """Focus a window by handle or title."""
    if hwnd and _force_foreground(hwnd):
        return True
    if not UIA_AVAILABLE:
        return False
    if hwnd:
        try:
            with uia.UIAutomationInitializerInThread():
                ctrl = uia.ControlFromHandle(hwnd)
                try:
                    ctrl.SetFocus()
                except Exception:
                    pass
            return True
        except Exception:
            pass
    if window_title:
        try:
            with uia.UIAutomationInitializerInThread():
                root_ctrl = uia.GetRootControl()
                for ctrl in root_ctrl.GetChildren():
                    try:
                        if (ctrl.Name or "").strip() == window_title:
                            try:
                                ctrl.SetFocus()
                            except Exception:
                                pass
                            return True
                    except Exception:
                        continue
        except Exception:
            return False
    return False


def _control_bottom_y(ctrl) -> int:
    try:
        rect = ctrl.BoundingRectangle
        return int(getattr(rect, "bottom", 0))
    except Exception:
        return 0


def _find_chatgpt_input_control(hwnd: int | None):
    """Find the ChatGPT prompt input (usually the lowest edit/document field)."""
    if not UIA_AVAILABLE or not hwnd:
        return None

    keywords = ("message", "prompt", "ask", "chat", "input", "composer", "send")
    candidates: list = []

    try:
        with uia.UIAutomationInitializerInThread():
            root = uia.ControlFromHandle(hwnd)
            for child, _depth in uia.WalkControl(root, includeTop=True, maxDepth=25):
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
                    if any(k in name or k in aid for k in keywords):
                        candidates.append(child)
                        continue

                    if ctype == "EditControl":
                        candidates.append(child)
                except Exception:
                    continue

            if not candidates:
                return None
            return sorted(candidates, key=_control_bottom_y)[-1]
    except Exception:
        return None


def select_chatgpt_window() -> None:
    """Let the user pick the ChatGPT browser/desktop window."""
    global chatgpt_window_title, chatgpt_hwnd, chatgpt_input_ctrl

    titles = _match_chatgpt_windows() or _list_top_level_window_titles()
    chosen = _pick_window_dialog(
        titles,
        "Select ChatGPT Window",
        "Pick the ChatGPT window or browser tab.",
    )
    if chosen:
        chatgpt_window_title = chosen
        chatgpt_hwnd = _get_hwnd_by_title(chosen)
        chatgpt_input_ctrl = None
        _cache_chatgpt_input()
        status_label.config(text="ChatGPT window selected.", foreground="green")
        _update_chatgpt_indicator()


def _ensure_chatgpt_window() -> bool:
    """Ensure a ChatGPT window is selected."""
    global chatgpt_window_title, chatgpt_hwnd

    if chatgpt_hwnd:
        return True

    if chatgpt_window_title:
        chatgpt_hwnd = _get_hwnd_by_title(chatgpt_window_title)
        if chatgpt_hwnd:
            return True

    matches = _match_chatgpt_windows()
    if len(matches) == 1:
        chatgpt_window_title = matches[0]
        chatgpt_hwnd = _get_hwnd_by_title(matches[0])
        _cache_chatgpt_input()
        return True

    if matches:
        chosen = _pick_window_dialog(
            matches,
            "Select ChatGPT Window",
            "Multiple ChatGPT windows found. Pick one:",
        )
        if chosen:
            chatgpt_window_title = chosen
            chatgpt_hwnd = _get_hwnd_by_title(chosen)
            _cache_chatgpt_input()
            return True

    select_chatgpt_window()
    return bool(chatgpt_hwnd)


def _get_preview_text() -> str:
    """Read all text from the preview area."""
    try:
        return preview_box.get("1.0", tk.END).strip()
    except Exception:
        return last_copied_text.strip()


def _execute_send_to_chatgpt(text: str) -> None:
    """Run the actual paste+enter after the button click finishes."""
    global chatgpt_hwnd, chatgpt_input_ctrl

    try:
        if not chatgpt_hwnd and chatgpt_window_title:
            chatgpt_hwnd = _get_hwnd_by_title(chatgpt_window_title)

        if not _force_foreground(chatgpt_hwnd) and not _focus_window(chatgpt_window_title, chatgpt_hwnd):
            messagebox.showwarning("Window Not Found", "Could not focus the ChatGPT window.")
            return

        if _paste_and_submit_to_input(text):
            status_label.config(text=f"Sent to ChatGPT ({len(text)} chars).", foreground="green")
            _set_last_action(f"Sent to ChatGPT ({len(text)} chars)")
            return

        # Fallback: global keyboard if UIA input was not found.
        if not chatgpt_input_ctrl:
            _cache_chatgpt_input()
        pyperclip.copy(text)
        keyboard.press_and_release("ctrl+v")
        keyboard.press_and_release("enter")
        status_label.config(text=f"Sent to ChatGPT ({len(text)} chars).", foreground="green")
        _set_last_action(f"Sent to ChatGPT ({len(text)} chars)")
    except Exception as e:
        messagebox.showerror("Error", f"Failed to send to ChatGPT: {e}")
        status_label.config(text="Failed to send to ChatGPT.", foreground="red")


def send_to_chatgpt() -> None:
    """Paste preview text into ChatGPT and press Enter on a single click."""
    if not KEYBOARD_AVAILABLE:
        messagebox.showerror("Error", "Keyboard library not available.\nInstall: pip install keyboard")
        return
    if not UIA_AVAILABLE:
        messagebox.showerror("Error", "UIAutomation not available.\nInstall: pip install uiautomation")
        return

    text = _get_preview_text()
    if not text:
        messagebox.showwarning("No Text", "The preview area is empty. Copy captions first.")
        return

    if not _ensure_chatgpt_window():
        return

    # Defer one tick so Windows finishes the button click before we steal focus.
    root.after(1, _execute_send_to_chatgpt, text)


# --- Caption copying (Copy Now + Auto) ---

is_auto_running = False
auto_thread: threading.Thread | None = None
last_copied_text = ""


def _clean_text(text: str) -> str:
    return " ".join(text.split()).strip()


def _capture_if_changed(clean: str) -> str | None:
    """Return caption text when it changes (safe to call from background threads)."""
    global last_copied_text

    if not clean or clean == last_copied_text:
        return None

    last_copied_text = clean
    pyperclip.copy(clean)
    return clean


def _set_preview(text: str) -> None:
    """Show the full current caption text in the preview area."""
    preview_box.delete("1.0", tk.END)
    preview_box.insert(tk.END, text)
    preview_box.see(tk.END)


def _on_caption_update(text: str, *, trigger_auto_send: bool = False) -> None:
    """Update preview and optionally auto-send (must run on the Tk main thread)."""
    _set_preview(text)
    _set_last_action(f"Captured ({len(text)} chars)")

    if trigger_auto_send and auto_send_var.get():
        if _ensure_chatgpt_window():
            root.after(1, _execute_send_to_chatgpt, text)
            _update_chatgpt_indicator()


def clear_preview() -> None:
    """Clear preview and reset caption tracking."""
    global last_copied_text

    last_copied_text = ""
    preview_box.delete("1.0", tk.END)
    status_label.config(text="Preview cleared.", foreground="#106ba3")
    _set_last_action("Cleared preview")


def copy_now():
    """Copy the current captions into the preview."""
    global last_copied_text

    if not UIA_AVAILABLE:
        messagebox.showerror("Error", "UIAutomation not available.\nInstall: pip install uiautomation")
        return

    try:
        # Single UIA init — get_captions_text() no longer nests its own.
        with uia.UIAutomationInitializerInThread():
            text = get_captions_text()
            # One retry if UI tree is still settling after open.
            if not text and _is_captions_open():
                _invalidate_captions_text_cache()
                time.sleep(0.1)
                text = get_captions_text()

        clean = _clean_text(text)
        if not clean:
            status_label.config(
                text="Live Captions open but no text yet. Speak or wait a moment.",
                foreground="#C06010",
            )
            return

        last_copied_text = clean
        pyperclip.copy(clean)
        _set_preview(clean)
        _set_last_action(f"Copied ({len(clean)} chars)")
        status_label.config(text=f"Copied ({len(clean)} chars).", foreground="green")
    except Exception as e:
        messagebox.showerror("Error", f"Failed to copy captions: {e}")
        status_label.config(text=f"Error: {str(e)[:40]}", foreground="red")


def toggle_auto():
    """Start/stop automatic copying."""
    global is_auto_running, auto_thread

    if not UIA_AVAILABLE:
        messagebox.showerror("Error", "UIAutomation not available.\nInstall: pip install uiautomation")
        return

    if is_auto_running:
        is_auto_running = False
        auto_btn.config(text="Start Auto")
        status_label.config(text="Auto stopped.", foreground="#C06010")
        return

    try:
        interval = float(interval_var.get().strip() or "0.3")
    except ValueError:
        status_label.config(text="Invalid interval.", foreground="red")
        return

    if interval < 0.2:
        interval = 0.2

    is_auto_running = True
    auto_btn.config(text="Stop Auto")
    status_label.config(text="Auto running…", foreground="#106ba3")
    _set_last_action("Auto capture started")

    auto_thread = threading.Thread(target=_auto_loop, args=(interval,), daemon=True)
    auto_thread.start()


def _auto_loop(interval: float) -> None:
    """Background loop to poll captions and update when text changes."""
    global is_auto_running, live_captions_hwnd

    consecutive_empty = 0

    try:
        # One long-lived UIA COM apartment for this worker thread.
        with uia.UIAutomationInitializerInThread():
            while is_auto_running:
                try:
                    text = get_captions_text()
                    clean = _clean_text(text)

                    if not clean:
                        consecutive_empty += 1
                        # After a few empties, drop caches — window may have been recreated.
                        if consecutive_empty >= 4:
                            _invalidate_captions_text_cache()
                            live_captions_hwnd = None
                            consecutive_empty = 0
                    else:
                        consecutive_empty = 0
                        updated = _capture_if_changed(clean)
                        if updated:
                            root.after(
                                0,
                                lambda t=updated: _on_caption_update(t, trigger_auto_send=True),
                            )
                            root.after(
                                0,
                                lambda t=updated: status_label.config(
                                    text=f"Auto-captured ({len(t)} chars).", foreground="green"
                                ),
                            )

                    time.sleep(interval)
                except Exception:
                    _invalidate_captions_text_cache()
                    time.sleep(0.5)
    finally:
        if is_auto_running:
            root.after(0, lambda: auto_btn.config(text="Start Auto"))


# --- Status indicators ---

captions_indicator: ttk.Label
chatgpt_indicator: ttk.Label
last_action_indicator: ttk.Label
auto_send_var: tk.BooleanVar


def _set_last_action(message: str) -> None:
    if last_action_indicator:
        last_action_indicator.config(text=f"Last: {message}")


def _update_captions_indicator() -> None:
    if not captions_indicator:
        return
    if _is_captions_open():
        captions_indicator.config(text="Captions: OPEN", foreground="green")
    else:
        captions_indicator.config(text="Captions: CLOSED", foreground="#888888")


def _update_chatgpt_indicator() -> None:
    if not chatgpt_indicator:
        return
    if chatgpt_hwnd or chatgpt_window_title:
        name = chatgpt_window_title or "Selected"
        short = name if len(name) <= 28 else name[:25] + "…"
        chatgpt_indicator.config(text=f"ChatGPT: {short}", foreground="green")
    else:
        chatgpt_indicator.config(text="ChatGPT: Not set", foreground="orange")


def _schedule_status_refresh() -> None:
    _update_captions_indicator()
    _update_chatgpt_indicator()
    root.after(1500, _schedule_status_refresh)

# --- GUI setup ---

root: tk.Tk
status_label: ttk.Label
preview_box: scrolledtext.ScrolledText
auto_btn: ttk.Button
interval_var: tk.StringVar


def build_ui() -> None:
    global root, status_label, preview_box, auto_btn, interval_var
    global captions_indicator, chatgpt_indicator, last_action_indicator, auto_send_var

    root = tk.Tk()
    root.title("Caption Copier")
    root.geometry("700x620")
    root.minsize(560, 480)

    main_frame = ttk.Frame(root, padding=12)
    main_frame.pack(fill=tk.BOTH, expand=True)

    indicators = ttk.Frame(main_frame)
    indicators.pack(fill=tk.X, pady=(0, 8))

    captions_indicator = ttk.Label(indicators, text="Captions: …", font=("Segoe UI", 9))
    captions_indicator.pack(side=tk.LEFT, padx=(0, 16))

    chatgpt_indicator = ttk.Label(indicators, text="ChatGPT: …", font=("Segoe UI", 9))
    chatgpt_indicator.pack(side=tk.LEFT, padx=(0, 16))

    last_action_indicator = ttk.Label(indicators, text="Last: Ready", font=("Segoe UI", 9))
    last_action_indicator.pack(side=tk.LEFT)

    controls = ttk.Frame(main_frame)
    controls.pack(fill=tk.X, pady=(0, 8))

    copy_btn = ttk.Button(controls, text="Copy Now", command=copy_now)
    copy_btn.pack(side=tk.LEFT)

    auto_btn = ttk.Button(controls, text="Start Auto", command=toggle_auto)
    auto_btn.pack(side=tk.LEFT, padx=(8, 0))

    ttk.Label(controls, text="Interval (s):").pack(side=tk.LEFT, padx=(16, 4))
    interval_var = tk.StringVar(value="0.3")
    interval_entry = ttk.Entry(controls, textvariable=interval_var, width=6)
    interval_entry.pack(side=tk.LEFT)

    open_btn = ttk.Button(controls, text="Open Captions", command=open_captions)
    open_btn.pack(side=tk.LEFT, padx=(16, 0))

    close_btn = ttk.Button(controls, text="Close Captions", command=close_captions)
    close_btn.pack(side=tk.LEFT, padx=(8, 0))

    chatgpt_row = ttk.Frame(main_frame)
    chatgpt_row.pack(fill=tk.X, pady=(0, 8))

    select_chatgpt_btn = ttk.Button(chatgpt_row, text="Select ChatGPT", command=select_chatgpt_window)
    select_chatgpt_btn.pack(side=tk.LEFT)

    send_chatgpt_btn = ttk.Button(chatgpt_row, text="Send to ChatGPT", command=send_to_chatgpt)
    send_chatgpt_btn.pack(side=tk.LEFT, padx=(8, 0))

    auto_send_var = tk.BooleanVar(value=True)
    auto_send_chk = ttk.Checkbutton(
        chatgpt_row,
        text="Auto-send new text to ChatGPT",
        variable=auto_send_var,
    )
    auto_send_chk.pack(side=tk.LEFT, padx=(12, 0))

    status_label = ttk.Label(main_frame, text="Ready", foreground="green")
    status_label.pack(anchor=tk.W, pady=(0, 6))

    preview_frame = ttk.LabelFrame(main_frame, text="Caption Text (editable)", padding=8)
    preview_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

    preview_header = ttk.Frame(preview_frame)
    preview_header.pack(fill=tk.X, pady=(0, 6))
    ttk.Label(
        preview_header,
        text="Live captions appear here. Edit before sending to ChatGPT.",
        font=("Segoe UI", 9),
    ).pack(side=tk.LEFT)
    clear_btn = ttk.Button(preview_header, text="Clear", command=clear_preview)
    clear_btn.pack(side=tk.RIGHT)

    preview_box = scrolledtext.ScrolledText(preview_frame, wrap=tk.WORD, height=12, font=("Segoe UI", 10))
    preview_box.pack(fill=tk.BOTH, expand=True)

    warnings = []
    if not UIA_AVAILABLE:
        warnings.append("UIAutomation")
    if not KEYBOARD_AVAILABLE:
        warnings.append("Keyboard")

    if warnings:
        status_label.config(
            text=f"⚠ Missing: {', '.join(warnings)} - Install with: pip install {' '.join(w.lower() for w in warnings)}",
            foreground="orange",
        )

    threading.Thread(target=_warm_chatgpt_cache, daemon=True).start()

    def _after_warm() -> None:
        _update_chatgpt_indicator()

    root.after(500, _after_warm)
    _update_captions_indicator()
    _schedule_status_refresh()


if __name__ == "__main__":
    if not require_password():
        sys.exit(0)
    build_ui()
    root.mainloop()
