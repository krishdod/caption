#!/usr/bin/env python3
"""
Simple Caption Copier

UI similar to the old "Caption Copier":
- Copy Now button
- Start/Stop Auto button with polling interval
- Live preview of last copied text
- Extra buttons to Open/Close the Windows Live Captions window
"""

import hashlib
import secrets
import sys
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
except ImportError:
    KEYBOARD_AVAILABLE = False


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


def find_live_captions_root():
    """Find the Windows Live Captions window."""
    if not UIA_AVAILABLE:
        return None
    try:
        root_control = uia.GetRootControl()
        for ctrl in root_control.GetChildren():
            try:
                class_name = ctrl.ClassName
                name = ctrl.Name
                if class_name == "LiveCaptionsDesktopWindow" or (name and "Live Captions" in name):
                    return ctrl
            except Exception:
                continue
    except Exception:
        pass
    return None


def get_captions_text() -> str:
    """Get the text from Windows Live Captions."""
    if not UIA_AVAILABLE:
        return ""
    try:
        root = find_live_captions_root()
        if not root:
            return ""

        for child, depth in uia.WalkControl(root, includeTop=False, maxDepth=6):
            try:
                if child.ControlTypeName == "TextControl" and child.ClassName == "TextBlock":
                    txt = (child.Name or "").strip()
                    if txt:
                        return txt
            except Exception:
                continue
    except Exception:
        pass
    return ""


def open_captions():
    """Open Windows Live Captions."""
    if not KEYBOARD_AVAILABLE:
        messagebox.showerror("Error", "Keyboard library not available.\nInstall: pip install keyboard")
        return
    
    try:
        keyboard.press_and_release('ctrl+win+l')
        status_label.config(text="Opening captions...", foreground="blue")
        root.after(500, check_captions_opened)
    except Exception as e:
        messagebox.showerror("Error", f"Failed to open captions: {e}")


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
            buttons_with_position: list[tuple] = []

            for child, _depth in uia.WalkControl(root_caption, includeTop=False, maxDepth=8):
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

                    buttons_with_position.append((child, _button_right_edge(child)))
                except Exception:
                    continue

            if not close_button and buttons_with_position:
                buttons_with_position.sort(key=lambda item: item[1], reverse=True)
                close_button = buttons_with_position[0][0]

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


def close_captions():
    """Close Windows Live Captions by clicking the X button (not the keyboard toggle)."""
    if not UIA_AVAILABLE:
        messagebox.showerror("Error", "UIAutomation not available.\nInstall: pip install uiautomation")
        return

    try:
        with uia.UIAutomationInitializerInThread():
            if not find_live_captions_root():
                status_label.config(text="Captions already closed.", foreground="green")
                return

        status_label.config(text="Closing...", foreground="blue")
        if click_captions_close_button():
            root.after(300, check_captions_closed)
        else:
            status_label.config(
                text="Could not find the close (X) button. Close it manually.",
                foreground="orange",
            )
    except Exception as e:
        messagebox.showerror("Error", f"Failed to close captions: {e}")


def check_captions_opened():
    """Check if captions opened successfully."""
    if UIA_AVAILABLE:
        try:
            with uia.UIAutomationInitializerInThread():
                root_caption = find_live_captions_root()
                if root_caption:
                    status_label.config(text="Captions: OPEN", foreground="green")
                else:
                    status_label.config(text="Captions: Not detected", foreground="orange")
        except Exception:
            status_label.config(text="Captions: Status unknown", foreground="orange")
    else:
        status_label.config(text="Captions: Opening...", foreground="blue")


def check_captions_closed():
    """Check if captions closed successfully."""
    if UIA_AVAILABLE:
        try:
            with uia.UIAutomationInitializerInThread():
                root_caption = find_live_captions_root()
                if not root_caption:
                    status_label.config(text="Captions: CLOSED", foreground="green")
                else:
                    status_label.config(text="Captions: Still open", foreground="orange")
        except Exception:
            status_label.config(text="Captions: Status unknown", foreground="orange")
    else:
        status_label.config(text="Captions: Closing...", foreground="blue")


# --- Caption copying (Copy Now + Auto) ---

is_auto_running = False
auto_thread: threading.Thread | None = None
last_copied_text = ""

def _clean_text(text: str) -> str:
    return " ".join(text.split()).strip()


def copy_now():
    """Copy the current captions once."""
    global last_copied_text

    if not UIA_AVAILABLE:
        messagebox.showerror("Error", "UIAutomation not available.\nInstall: pip install uiautomation")
        return

    try:
        with uia.UIAutomationInitializerInThread():
            text = get_captions_text()

        clean = _clean_text(text)
        if not clean:
            status_label.config(text="Live Captions not found or empty.", foreground="#C06010")
            return

        pyperclip.copy(clean)
        last_copied_text = clean
        preview_box.configure(state=tk.NORMAL)
        preview_box.delete("1.0", tk.END)
        preview_box.insert(tk.END, clean)
        preview_box.configure(state=tk.DISABLED)
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
        interval = float(interval_var.get().strip() or "1.0")
    except ValueError:
        status_label.config(text="Invalid interval.", foreground="red")
        return

    if interval < 0.2:
        interval = 0.2

    is_auto_running = True
    auto_btn.config(text="Stop Auto")
    status_label.config(text="Auto running…", foreground="#106ba3")

    auto_thread = threading.Thread(target=_auto_loop, args=(interval,), daemon=True)
    auto_thread.start()


def _auto_loop(interval: float) -> None:
    """Background loop to poll captions and copy when text changes."""
    global last_copied_text, is_auto_running

    try:
        while is_auto_running:
            try:
                if UIA_AVAILABLE:
                    with uia.UIAutomationInitializerInThread():
                        text = get_captions_text()
                else:
                    text = ""

                clean = _clean_text(text)
                if clean and clean != last_copied_text:
                    last_copied_text = clean
                    pyperclip.copy(clean)
                    root.after(0, _update_preview, clean)
                    root.after(0, lambda: status_label.config(text="Auto-copied.", foreground="green"))

                time.sleep(interval)
            except Exception:
                time.sleep(1.0)
    finally:
        # Ensure button state resets if thread exits unexpectedly
        if is_auto_running:
            root.after(0, lambda: auto_btn.config(text="Start Auto"))


def _update_preview(text: str) -> None:
    preview_box.configure(state=tk.NORMAL)
    preview_box.delete("1.0", tk.END)
    preview_box.insert(tk.END, text)
    preview_box.configure(state=tk.DISABLED)

# --- GUI setup ---

root: tk.Tk
status_label: ttk.Label
preview_box: scrolledtext.ScrolledText
auto_btn: ttk.Button
interval_var: tk.StringVar


def build_ui() -> None:
    global root, status_label, preview_box, auto_btn, interval_var

    root = tk.Tk()
    root.title("Caption Copier")
    root.geometry("650x520")
    root.minsize(520, 420)

    main_frame = ttk.Frame(root, padding=12)
    main_frame.pack(fill=tk.BOTH, expand=True)

    controls = ttk.Frame(main_frame)
    controls.pack(fill=tk.X, pady=(0, 8))

    copy_btn = ttk.Button(controls, text="Copy Now", command=copy_now)
    copy_btn.pack(side=tk.LEFT)

    auto_btn = ttk.Button(controls, text="Start Auto", command=toggle_auto)
    auto_btn.pack(side=tk.LEFT, padx=(8, 0))

    ttk.Label(controls, text="Interval (s):").pack(side=tk.LEFT, padx=(16, 4))
    interval_var = tk.StringVar(value="1.0")
    interval_entry = ttk.Entry(controls, textvariable=interval_var, width=6)
    interval_entry.pack(side=tk.LEFT)

    open_btn = ttk.Button(controls, text="Open Captions", command=open_captions)
    open_btn.pack(side=tk.LEFT, padx=(16, 0))

    close_btn = ttk.Button(controls, text="Close Captions", command=close_captions)
    close_btn.pack(side=tk.LEFT, padx=(8, 0))

    status_label = ttk.Label(main_frame, text="Ready", foreground="green")
    status_label.pack(anchor=tk.W, pady=(0, 6))

    preview_frame = ttk.LabelFrame(main_frame, text="Last Copied Text", padding=8)
    preview_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

    preview_box = scrolledtext.ScrolledText(preview_frame, wrap=tk.WORD, height=12, font=("Segoe UI", 10))
    preview_box.pack(fill=tk.BOTH, expand=True)
    preview_box.configure(state=tk.DISABLED)

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


if __name__ == "__main__":
    if not require_password():
        sys.exit(0)
    build_ui()
    root.mainloop()
