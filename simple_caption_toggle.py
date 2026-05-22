#!/usr/bin/env python3
"""
Simple Captions Toggle

Very small utility app with just:
- Open Captions button
- Close Captions button
- Status label

Uses:
- Windows Live Captions (Ctrl+Win+L)
- UIAutomation to detect/close the Live Captions window when possible
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

import time
import threading
import os

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

try:
    import pyperclip
    PYPERCLIP_AVAILABLE = True
except ImportError:
    PYPERCLIP_AVAILABLE = False

#
# NOTE: This app is intentionally clipboard-driven (no Chrome remote-debugging / Playwright).
# Workflow:
# - You click "Copy" on a ChatGPT/Perplexity answer
# - This app detects the clipboard change and pastes into your selected Google Doc
#


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
                if class_name == "LiveCaptionsDesktopWindow" or (
                    name and "Live Captions" in name
                ):
                    return ctrl
            except Exception:
                continue
    except Exception:
        pass
    return None


def get_captions_text() -> str:
    """Get the current text from Windows Live Captions."""
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


class SimpleCaptionsApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Simple Captions Toggle")
        # Start at a comfortable default size but allow full resizing / maximize
        self.root.geometry("600x320")
        self.root.minsize(420, 220)
        self.root.resizable(True, True)

        # Auto-copy state
        self.is_auto_running = False
        self.auto_thread: threading.Thread | None = None
        self.last_copied_text: str = ""
        self.interval_var = tk.StringVar(value="1.0")

        # Persistent targets + last captured AI answers
        self.chatgpt_window_title: str | None = None
        self.perplexity_window_title: str | None = None
        self.gdoc_window_title: str | None = None
        # Cache native window handles for fast focusing (avoids scanning all windows every click)
        self.chatgpt_hwnd: int | None = None
        self.perplexity_hwnd: int | None = None
        self.gdoc_hwnd: int | None = None
        self.last_ai_answer: dict[str, str] = {"ChatGPT": "", "Perplexity": ""}
        self._last_clipboard_seen: str = ""
        # Prevent clipboard watcher from reacting to our own clipboard writes during paste.
        self._ignore_clipboard_until: float = 0.0
        # Tunables for speed (lower = faster, but too low can reduce reliability on slower PCs)
        self._paste_focus_delay_s: float = 0.01
        self._paste_clipboard_settle_s: float = 0.01
        self._paste_clear_delay_s: float = 0.01

        # Auto AI → GDoc state
        self._last_pasted_chatgpt: str = ""
        self._last_pasted_perplexity: str = ""
        self._auto_ai_to_gdoc_enabled: bool = True  # controlled by checkbox

        # Use a modern-looking style
        style = ttk.Style(self.root)
        try:
            # Use the OS default theme if available
            style.theme_use(style.theme_use())
        except Exception:
            pass

        main = ttk.Frame(self.root, padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        # Title label
        title = ttk.Label(
            main,
            text="Windows Live Captions",
            font=("Segoe UI", 12, "bold"),
        )
        title.pack(anchor=tk.W, pady=(0, 8))

        # Description
        desc = ttk.Label(
            main,
            text="Quickly open or close Windows Live Captions.\n"
                 "You can resize this window or leave it docked to the side.",
            justify=tk.LEFT,
        )
        desc.pack(anchor=tk.W, pady=(0, 10))

        # Top controls row
        btn_row = ttk.Frame(main)
        btn_row.pack(fill=tk.X, pady=(0, 8))

        self.copy_now_btn = ttk.Button(
            btn_row, text="Copy Now", command=self.copy_captions
        )
        self.copy_now_btn.pack(side=tk.LEFT, padx=(0, 4))

        self.auto_btn = ttk.Button(
            btn_row, text="Start Auto", command=self.toggle_auto
        )
        self.auto_btn.pack(side=tk.LEFT, padx=(4, 4))

        ttk.Label(btn_row, text="Interval (s):").pack(side=tk.LEFT, padx=(12, 4))
        self.interval_entry = ttk.Entry(btn_row, textvariable=self.interval_var, width=6)
        self.interval_entry.pack(side=tk.LEFT)

        # Target selection + paste answers to Google Doc
        gdoc_row = ttk.Frame(main)
        gdoc_row.pack(fill=tk.X, pady=(0, 8))

        self.select_chatgpt_btn = ttk.Button(
            gdoc_row, text="Select ChatGPT Window (optional)", command=self.select_chatgpt_window
        )
        self.select_chatgpt_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.select_perplexity_btn = ttk.Button(
            gdoc_row, text="Select Perplexity Window (optional)", command=self.select_perplexity_window
        )
        self.select_perplexity_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.select_gdoc_btn = ttk.Button(
            gdoc_row, text="Select Google Doc", command=self.select_gdoc_window
        )
        self.select_gdoc_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.send_chatgpt_btn = ttk.Button(
            gdoc_row,
            text="Paste Last ChatGPT",
            command=lambda: self.one_click_paste_latest("ChatGPT"),
        )
        self.send_chatgpt_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.send_perplexity_btn = ttk.Button(
            gdoc_row,
            text="Perplexity → GDoc",
            command=lambda: self.one_click_paste_latest("Perplexity"),
        )
        self.send_perplexity_btn.pack(side=tk.LEFT)

        # Auto AI → GDoc toggle
        self.auto_ai_var = tk.BooleanVar(value=True)
        self.auto_ai_chk = ttk.Checkbutton(
            gdoc_row,
            text="Auto‑paste when I click Copy in AI",
            variable=self.auto_ai_var,
            command=self._on_auto_ai_toggle,
        )
        self.auto_ai_chk.pack(side=tk.LEFT, padx=(10, 0))

        # Replace mode: clear doc before pasting a new answer
        self.replace_doc_var = tk.BooleanVar(value=True)
        self.replace_doc_chk = ttk.Checkbutton(
            gdoc_row,
            text="Replace doc (clear old answer)",
            variable=self.replace_doc_var,
        )
        self.replace_doc_chk.pack(side=tk.LEFT, padx=(10, 0))

        # Open / Close buttons
        oc_row = ttk.Frame(main)
        oc_row.pack(fill=tk.X, pady=(0, 8))

        self.open_btn = ttk.Button(
            oc_row, text="🟢 Open Captions", command=self.open_captions
        )
        self.open_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 6))

        self.close_btn = ttk.Button(
            oc_row, text="🔴 Close Captions", command=self.close_captions
        )
        self.close_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(6, 0))

        # Live text preview
        preview_frame = ttk.LabelFrame(main, text="Last Copied Text", padding=6)
        preview_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 4))

        self.preview_box = scrolledtext.ScrolledText(
            preview_frame, wrap=tk.WORD, height=6
        )
        self.preview_box.pack(fill=tk.BOTH, expand=True)
        self.preview_box.configure(state=tk.DISABLED)

        # Status at the bottom
        self.status_label = ttk.Label(main, text="Ready", foreground="green")
        self.status_label.pack(anchor=tk.W, pady=(2, 0))

        # Dependency warning
        warnings = []
        if not UIA_AVAILABLE:
            warnings.append("UIAutomation")
        if not KEYBOARD_AVAILABLE:
            warnings.append("Keyboard")
        if warnings:
            self.status_label.config(
                text=f"⚠ Missing: {', '.join(warnings)} (pip install {' '.join(w.lower() for w in warnings)})",
                foreground="orange",
            )

        # Start clipboard watcher (captures last copied ChatGPT/Perplexity answer)
        # and (optionally) auto-pastes immediately into the selected Google Doc.
        self.root.after(50, self._clipboard_watch_loop)

    # --- Captions control ---

    def open_captions(self) -> None:
        """Open Windows Live Captions via keyboard shortcut."""
        if not KEYBOARD_AVAILABLE:
            messagebox.showerror(
                "Error",
                "Keyboard library not available.\nInstall: pip install keyboard",
            )
            return

        try:
            keyboard.press_and_release("ctrl+win+l")
            self.status_label.config(text="Opening captions…", foreground="blue")
            # Give Windows a moment and then check
            self.root.after(600, self._check_opened)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open captions: {e}")
            self.status_label.config(text="Error opening captions.", foreground="red")

    def _check_opened(self) -> None:
        if UIA_AVAILABLE:
            try:
                with uia.UIAutomationInitializerInThread():
                    root_caption = find_live_captions_root()
                if root_caption:
                    self.status_label.config(
                        text="Captions: OPEN", foreground="green"
                    )
                else:
                    self.status_label.config(
                        text="Captions: Not detected", foreground="#C06010"
                    )
            except Exception:
                self.status_label.config(
                    text="Captions: Status unknown", foreground="#C06010"
                )
        else:
            self.status_label.config(
                text="Captions: Opening… (UIA not installed)", foreground="blue"
            )

    def close_captions(self) -> None:
        """Close Windows Live Captions via UIA if possible, else via shortcut."""
        if not UIA_AVAILABLE and not KEYBOARD_AVAILABLE:
            messagebox.showerror(
                "Error",
                "Neither UIAutomation nor keyboard libraries are available.\n"
                "Install: pip install uiautomation keyboard",
            )
            return

        try:
            closed = False
            if UIA_AVAILABLE:
                with uia.UIAutomationInitializerInThread():
                    root_caption = find_live_captions_root()
                    if root_caption:
                        # Try to click the close (X) button
                        for child, depth in uia.WalkControl(
                            root_caption, includeTop=False, maxDepth=6
                        ):
                            try:
                                if child.ControlTypeName == "ButtonControl":
                                    name = (child.Name or "").lower()
                                    if "close" in name or (child.Name or "") in (
                                        "×",
                                        "x",
                                        "✕",
                                    ):
                                        child.Click()
                                        closed = True
                                        break
                            except Exception:
                                continue

            # Fallback: toggle shortcut
            if not closed and KEYBOARD_AVAILABLE:
                keyboard.press_and_release("ctrl+win+l")

            self.status_label.config(text="Closing captions…", foreground="blue")
            self.root.after(600, self._check_closed)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to close captions: {e}")
            self.status_label.config(text="Error closing captions.", foreground="red")

    def _check_closed(self) -> None:
        if UIA_AVAILABLE:
            try:
                with uia.UIAutomationInitializerInThread():
                    root_caption = find_live_captions_root()
                if not root_caption:
                    self.status_label.config(
                        text="Captions: CLOSED", foreground="green"
                    )
                else:
                    self.status_label.config(
                        text="Captions: Still open", foreground="#C06010"
                    )
            except Exception:
                self.status_label.config(
                    text="Captions: Status unknown", foreground="#C06010"
                )
        else:
            self.status_label.config(
                text="Captions: Closing… (UIA not installed)", foreground="blue"
            )

    def copy_captions(self) -> None:
        """Copy current Live Captions text to clipboard."""
        if not UIA_AVAILABLE:
            messagebox.showerror(
                "Error",
                "UIAutomation not available.\nInstall: pip install uiautomation",
            )
            return
        if not PYPERCLIP_AVAILABLE:
            messagebox.showerror(
                "Error",
                "pyperclip not available.\nInstall: pip install pyperclip",
            )
            return

        try:
            with uia.UIAutomationInitializerInThread():
                text = get_captions_text()

            clean = " ".join(text.split()).strip()
            if not clean:
                self.status_label.config(
                    text="No captions detected to copy.", foreground="#C06010"
                )
                return

            pyperclip.copy(clean)
            self.last_copied_text = clean
            self._update_preview(clean)
            self.status_label.config(
                text=f"Copied to clipboard ({len(clean)} chars).", foreground="green"
            )
        except Exception as e:
            messagebox.showerror("Error", f"Failed to copy captions: {e}")
            self.status_label.config(text="Error copying captions.", foreground="red")

    # --- Google Docs paste ---

    def _list_google_docs_windows(self) -> list[str]:
        """Return titles of all top-level windows that look like Google Docs."""
        if not UIA_AVAILABLE:
            return []
        titles: list[str] = []
        try:
            with uia.UIAutomationInitializerInThread():
                root_ctrl = uia.GetRootControl()
                for ctrl in root_ctrl.GetChildren():
                    try:
                        name = (ctrl.Name or "").strip()
                        if name and "google docs" in name.lower():
                            titles.append(name)
                    except Exception:
                        continue
        except Exception:
            return []
        return titles

    def _list_top_level_window_titles(self) -> list[str]:
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

    def _pick_window_dialog(self, titles: list[str], dialog_title: str, prompt: str) -> str | None:
        """Show a list dialog and return the chosen window title."""
        if not titles:
            return None
        if len(titles) == 1:
            return titles[0]

        dialog = tk.Toplevel(self.root)
        dialog.title(dialog_title)
        dialog.geometry("600x320")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text=prompt).pack(pady=(10, 8), padx=10, anchor=tk.W)

        listbox = tk.Listbox(dialog, height=min(12, len(titles)))
        for t in titles:
            listbox.insert(tk.END, t)
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        listbox.selection_set(0)

        chosen: dict[str, str | None] = {"title": None}

        btns = ttk.Frame(dialog)
        btns.pack(pady=(0, 10))

        def on_ok() -> None:
            try:
                idx = int(listbox.curselection()[0])
            except Exception:
                chosen["title"] = None
                dialog.destroy()
                return
            chosen["title"] = titles[idx]
            dialog.destroy()

        ttk.Button(btns, text="OK", command=on_ok).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT, padx=6)

        dialog.focus_set()
        dialog.wait_window(dialog)
        return chosen["title"]

    def _match_source_windows(self, source_label: str) -> list[str]:
        """Best-effort match for ChatGPT / Perplexity browser windows."""
        all_titles = self._list_top_level_window_titles()
        sl = source_label.lower()

        if sl == "chatgpt":
            needles = ["chatgpt", "chat gpt", "openai"]
        elif sl == "perplexity":
            needles = ["perplexity"]
        else:
            needles = [sl]

        matches: list[str] = []
        for t in all_titles:
            tl = t.lower()
            if any(n in tl for n in needles):
                matches.append(t)
        return matches

    def _get_foreground_window_title(self) -> str:
        """Best-effort: return the current foreground window title."""
        if not UIA_AVAILABLE:
            return ""
        try:
            with uia.UIAutomationInitializerInThread():
                fg = uia.GetForegroundControl()
                return (fg.Name or "").strip()
        except Exception:
            return ""

    def _get_foreground_hwnd(self) -> int | None:
        """Return NativeWindowHandle for the current foreground window (more reliable than title)."""
        if not UIA_AVAILABLE:
            return None
        try:
            with uia.UIAutomationInitializerInThread():
                fg = uia.GetForegroundControl()
                hwnd = getattr(fg, "NativeWindowHandle", None)
                return int(hwnd) if hwnd else None
        except Exception:
            return None

    def _clipboard_watch_loop(self) -> None:
        """
        Watches clipboard changes and remembers the latest copied AI answer
        for ChatGPT / Perplexity (based on the currently focused window).

        This prevents captions auto-copy from becoming the "answer" because we
        ignore clipboard updates equal to the last caption text we copied.
        """
        try:
            # Ignore clipboard churn caused by our own paste routine
            if time.time() < self._ignore_clipboard_until:
                return
            if PYPERCLIP_AVAILABLE:
                clip = pyperclip.paste()
                if clip is None:
                    clip = ""
                clip = str(clip).strip()

                if clip and clip != self._last_clipboard_seen:
                    self._last_clipboard_seen = clip

                    # Ignore caption auto-copy content
                    if clip != self.last_copied_text and not clip.startswith(("ChatGPT Answer:", "Perplexity Answer:")):
                        fg_title = self._get_foreground_window_title().lower()
                        fg_hwnd = self._get_foreground_hwnd()
                        # Prefer explicit selected windows if set
                        if self.chatgpt_hwnd and fg_hwnd and self.chatgpt_hwnd == fg_hwnd:
                            self.last_ai_answer["ChatGPT"] = clip
                            self.status_label.config(text="Captured latest ChatGPT answer.", foreground="green")
                            self._maybe_autopaste_to_gdoc("ChatGPT", clip, return_focus_hwnd=fg_hwnd)
                        elif self.chatgpt_window_title and self.chatgpt_window_title.lower() in fg_title:
                            self.last_ai_answer["ChatGPT"] = clip
                            self.status_label.config(text="Captured latest ChatGPT answer.", foreground="green")
                            self._maybe_autopaste_to_gdoc("ChatGPT", clip, return_focus_hwnd=fg_hwnd)
                        elif self.perplexity_hwnd and fg_hwnd and self.perplexity_hwnd == fg_hwnd:
                            self.last_ai_answer["Perplexity"] = clip
                            self.status_label.config(text="Captured latest Perplexity answer.", foreground="green")
                            self._maybe_autopaste_to_gdoc("Perplexity", clip, return_focus_hwnd=fg_hwnd)
                        elif self.perplexity_window_title and self.perplexity_window_title.lower() in fg_title:
                            self.last_ai_answer["Perplexity"] = clip
                            self.status_label.config(text="Captured latest Perplexity answer.", foreground="green")
                            self._maybe_autopaste_to_gdoc("Perplexity", clip, return_focus_hwnd=fg_hwnd)
                        else:
                            # Heuristic fallback
                            if "chatgpt" in fg_title or "chat.openai" in fg_title or "openai" in fg_title:
                                self.last_ai_answer["ChatGPT"] = clip
                                self.status_label.config(text="Captured latest ChatGPT answer.", foreground="green")
                                self._maybe_autopaste_to_gdoc("ChatGPT", clip, return_focus_hwnd=fg_hwnd)
                            elif "perplexity" in fg_title:
                                self.last_ai_answer["Perplexity"] = clip
                                self.status_label.config(text="Captured latest Perplexity answer.", foreground="green")
                                self._maybe_autopaste_to_gdoc("Perplexity", clip, return_focus_hwnd=fg_hwnd)
        finally:
            # keep looping
            self.root.after(50, self._clipboard_watch_loop)

    def _maybe_autopaste_to_gdoc(self, source_label: str, text: str, return_focus_hwnd: int | None = None) -> None:
        """
        Immediately paste newly copied AI text into the selected Google Doc.
        This is the fastest path (no separate polling loop).
        """
        # Keep flag in sync with checkbox (if present)
        try:
            self._auto_ai_to_gdoc_enabled = bool(self.auto_ai_var.get())
        except Exception:
            pass

        if not self._auto_ai_to_gdoc_enabled:
            return
        if not self.gdoc_window_title:
            return

        clean = (text or "").strip()
        if not clean:
            return

        if source_label == "ChatGPT":
            if clean == self._last_pasted_chatgpt:
                return
        elif source_label == "Perplexity":
            if clean == self._last_pasted_perplexity:
                return

        labeled = f"{source_label} Answer:\n{clean}\n\n"

        # Do the paste on the Tk event loop to avoid re-entrancy from clipboard polling.
        def _do() -> None:
            try:
                replace = bool(self.replace_doc_var.get())
            except Exception:
                replace = False
            self._focus_and_paste_into_google_doc(
                self.gdoc_window_title,
                labeled,
                replace=replace,
                return_focus_hwnd=return_focus_hwnd,
            )
            if source_label == "ChatGPT":
                self._last_pasted_chatgpt = clean
            elif source_label == "Perplexity":
                self._last_pasted_perplexity = clean

        self.root.after(0, _do)

    def _on_auto_ai_toggle(self) -> None:
        """Hooked to the 'Auto‑paste when I click Copy in AI' checkbox."""
        self._auto_ai_to_gdoc_enabled = bool(self.auto_ai_var.get())

    def select_chatgpt_window(self) -> None:
        titles = self._match_source_windows("ChatGPT") or self._list_top_level_window_titles()
        chosen = self._pick_window_dialog(
            titles,
            "Select ChatGPT Window",
            "Pick the ChatGPT desktop app window (or any window that contains ChatGPT).",
        )
        if chosen:
            self.chatgpt_window_title = chosen
            self.chatgpt_hwnd = self._get_hwnd_by_title(chosen)
            self.status_label.config(text="ChatGPT tab selected.", foreground="green")

    def select_perplexity_window(self) -> None:
        titles = self._match_source_windows("Perplexity") or self._list_top_level_window_titles()
        chosen = self._pick_window_dialog(
            titles,
            "Select Perplexity Window",
            "Pick the Perplexity app/window (optional).",
        )
        if chosen:
            self.perplexity_window_title = chosen
            self.perplexity_hwnd = self._get_hwnd_by_title(chosen)
            self.status_label.config(text="Perplexity tab selected.", foreground="green")

    def select_gdoc_window(self) -> None:
        titles = self._list_google_docs_windows()
        if not titles:
            messagebox.showwarning(
                "Google Docs Not Found",
                "No Google Docs window detected.\nOpen your Google Doc in a browser and try again.",
            )
            return
        chosen = self._pick_window_dialog(
            titles,
            "Select Google Docs Window",
            "Pick the Google Doc window/tab where answers should be pasted.",
        )
        if chosen:
            self.gdoc_window_title = chosen
            self.gdoc_hwnd = self._get_hwnd_by_title(chosen)
            self.status_label.config(text="Google Doc selected.", foreground="green")

    def _focus_window_by_title(self, window_title: str) -> bool:
        """Focus a top-level window by exact title match."""
        if not UIA_AVAILABLE:
            return False
        try:
            with uia.UIAutomationInitializerInThread():
                root_ctrl = uia.GetRootControl()
                for ctrl in root_ctrl.GetChildren():
                    try:
                        name = (ctrl.Name or "").strip()
                        if name == window_title:
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

    def _get_hwnd_by_title(self, window_title: str) -> int | None:
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

    def _focus_window(self, window_title: str | None, hwnd: int | None) -> bool:
        """
        Fast focus path: prefer focusing by handle; fallback to title scan.
        """
        if not UIA_AVAILABLE:
            return False
        # Fast path: focus by window handle
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
                # fall back to title scan
                pass
        if window_title:
            return self._focus_window_by_title(window_title)
        return False

    def paste_last_answer_to_google_doc(self, source_label: str) -> None:
        """Paste the last captured ChatGPT/Perplexity answer into the selected Google Doc."""
        if not UIA_AVAILABLE:
            messagebox.showerror(
                "Error",
                "UIAutomation is required to focus the Google Docs window.\nInstall: pip install uiautomation",
            )
            return
        if not KEYBOARD_AVAILABLE:
            messagebox.showerror(
                "Error",
                "Keyboard library required to paste (Ctrl+V).\nInstall: pip install keyboard",
            )
            return
        if not PYPERCLIP_AVAILABLE:
            messagebox.showerror(
                "Error",
                "pyperclip required to read/write clipboard.\nInstall: pip install pyperclip",
            )
            return

        if not self.gdoc_window_title:
            self.select_gdoc_window()
            if not self.gdoc_window_title:
                return

        answer = (self.last_ai_answer.get(source_label) or "").strip()
        if not answer:
            messagebox.showwarning(
                "No Saved Answer",
                f"No {source_label} answer saved yet.\n"
                f"Copy the answer once in the {source_label} app/window and try again.",
            )
            return

        labeled_text = f"{source_label} Answer:\n{answer}\n\n"
        try:
            replace = bool(self.replace_doc_var.get())
        except Exception:
            replace = False
        self._focus_and_paste_into_google_doc(self.gdoc_window_title, labeled_text, replace=replace)

    def capture_latest_answer_now(self, source_label: str) -> None:
        """
        One-click capture: focus the saved ChatGPT/Perplexity window and try to click the latest 'Copy' button.
        Falls back to Ctrl+C (copy selection) if clicking isn't possible.
        """
        if not UIA_AVAILABLE:
            messagebox.showerror("Error", "UIAutomation required. Install: pip install uiautomation")
            return
        if not KEYBOARD_AVAILABLE:
            messagebox.showerror("Error", "Keyboard required. Install: pip install keyboard")
            return
        if not PYPERCLIP_AVAILABLE:
            messagebox.showerror("Error", "pyperclip required. Install: pip install pyperclip")
            return

        if source_label == "ChatGPT":
            title, hwnd = self.chatgpt_window_title, self.chatgpt_hwnd
            if not title:
                self.select_chatgpt_window()
                title, hwnd = self.chatgpt_window_title, self.chatgpt_hwnd
        else:
            title, hwnd = self.perplexity_window_title, self.perplexity_hwnd
            if not title:
                self.select_perplexity_window()
                title, hwnd = self.perplexity_window_title, self.perplexity_hwnd

        if not title and not hwnd:
            return

        # Stop auto-copy temporarily so captions don't overwrite clipboard mid-capture
        was_auto = self.is_auto_running
        if was_auto:
            self.is_auto_running = False
            self.root.after(0, lambda: self.auto_btn.config(text="Start Auto"))

        # Save clipboard
        try:
            prev_clip = pyperclip.paste()
        except Exception:
            prev_clip = ""

        try:
            if not self._focus_window(title, hwnd):
                messagebox.showwarning("Window Not Found", f"Could not focus {source_label}.")
                return
            time.sleep(0.02)

            copied = self._try_click_latest_copy_button(hwnd)
            if not copied:
                # fallback to copying selection
                keyboard.press_and_release("ctrl+c")

            # Wait briefly for clipboard change
            new_text = ""
            for _ in range(80):  # up to ~0.8s
                time.sleep(0.01)
                try:
                    cur = pyperclip.paste().strip()
                except Exception:
                    cur = ""
                if cur and cur != prev_clip and cur != self.last_copied_text:
                    new_text = cur
                    break

            if not new_text:
                messagebox.showwarning(
                    "Capture Failed",
                    f"Couldn't capture the latest {source_label} answer.\n"
                    f"Tip: select the answer text, then click again.",
                )
                return

            self.last_ai_answer[source_label] = new_text
            self.status_label.config(text=f"Captured latest {source_label} answer.", foreground="green")
        finally:
            # Restore clipboard to what it was before capture to minimize disruption
            try:
                pyperclip.copy(prev_clip)
            except Exception:
                pass
            if was_auto:
                self.is_auto_running = True
                self.root.after(0, lambda: self.auto_btn.config(text="Stop Auto"))

    def _try_click_latest_copy_button(self, hwnd: int | None) -> bool:
        """
        Best-effort: find and click the last visible button named like 'Copy' in the source window.
        Works for many apps/pages, but may fail if the UI isn't exposed to UIA.
        """
        if not UIA_AVAILABLE or not hwnd:
            return False
        try:
            with uia.UIAutomationInitializerInThread():
                root = uia.ControlFromHandle(hwnd)
                candidates = []
                for child, depth in uia.WalkControl(root, includeTop=True, maxDepth=18):
                    try:
                        if child.ControlTypeName == "ButtonControl":
                            name = (child.Name or "").strip().lower()
                            aid = (getattr(child, "AutomationId", "") or "").strip().lower()
                            help_text = (getattr(child, "HelpText", "") or "").strip().lower()

                            # ChatGPT/Chrome often exposes "Copy" via HelpText/tooltip, sometimes Name is empty.
                            is_copy = (
                                name == "copy"
                                or "copy" in name
                                or "copy" in aid
                                or "copy" in help_text
                            )
                            if not is_copy:
                                continue

                            # Prefer buttons that are actually on screen
                            try:
                                if getattr(child, "IsOffscreen", False):
                                    continue
                            except Exception:
                                pass

                            candidates.append(child)
                    except Exception:
                        continue
                if not candidates:
                    return False
                # Click the lowest one on the screen (usually the latest message)
                def _bottom_y(ctrl) -> int:
                    try:
                        rect = ctrl.BoundingRectangle
                        return int(getattr(rect, "bottom", 0))
                    except Exception:
                        return 0

                btn = sorted(candidates, key=_bottom_y)[-1]
                try:
                    btn.Click()
                    return True
                except Exception:
                    return False
        except Exception:
            return False

    def one_click_paste_latest(self, source_label: str) -> None:
        """
        True one-click flow:
        - Capture latest answer from ChatGPT/Perplexity (click Copy if possible; fallback to Ctrl+C selection).
        - Paste into selected Google Doc.
        """
        self.capture_latest_answer_now(source_label)
        self.paste_last_answer_to_google_doc(source_label)

    # Playwright / Chrome remote-debugging features intentionally removed.

    def _focus_and_paste_into_google_doc(
        self,
        window_title: str,
        text_to_paste: str,
        *,
        replace: bool = False,
        return_focus_hwnd: int | None = None,
    ) -> None:
        """Focus selected Google Docs window and paste provided text (via clipboard + Ctrl+V)."""
        # Prefer handle if we have it
        hwnd = self.gdoc_hwnd if self.gdoc_window_title == window_title else self._get_hwnd_by_title(window_title)
        if not self._focus_window(window_title, hwnd):
            messagebox.showwarning(
                "Window Not Found",
                "Could not focus the selected Google Docs window. Try again.",
            )
            return
        time.sleep(self._paste_focus_delay_s)
        try:
            # Suppress clipboard watcher while we temporarily overwrite the clipboard for paste
            self._ignore_clipboard_until = time.time() + 0.35
            if replace:
                # Clear existing doc content first (acts like a live answer box)
                keyboard.press_and_release("ctrl+a")
                time.sleep(self._paste_clear_delay_s)
                keyboard.press_and_release("backspace")
                time.sleep(self._paste_clear_delay_s)
            pyperclip.copy(text_to_paste)
            time.sleep(self._paste_clipboard_settle_s)
            keyboard.press_and_release("ctrl+v")
            self.status_label.config(text="Pasted answer into Google Doc.", foreground="green")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to paste: {e}")
            self.status_label.config(text="Error pasting into Google Doc.", foreground="red")
        finally:
            # Keep suppression a bit longer to avoid reacting to any late clipboard updates
            self._ignore_clipboard_until = max(self._ignore_clipboard_until, time.time() + 0.12)

            # Restore focus back to where you were (keeps cursor in ChatGPT/Perplexity)
            if return_focus_hwnd:
                try:
                    with uia.UIAutomationInitializerInThread():
                        ctrl = uia.ControlFromHandle(return_focus_hwnd)
                        try:
                            ctrl.SetFocus()
                        except Exception:
                            pass
                except Exception:
                    pass

    # --- Auto copy loop ---

    def toggle_auto(self) -> None:
        """Start or stop automatic copying of captions."""
        if not UIA_AVAILABLE: 
            messagebox.showerror(
                "Error",
                "UIAutomation not available.\nInstall: pip install uiautomation",
            )
            return
        if not PYPERCLIP_AVAILABLE:
            messagebox.showerror(
                "Error",
                "pyperclip not available.\nInstall: pip install pyperclip",
            )
            return

        if self.is_auto_running:
            self.is_auto_running = False
            self.auto_btn.config(text="Start Auto")
            self.status_label.config(text="Auto stopped.", foreground="#C06010")
            return

        try:
            interval = float(self.interval_var.get().strip() or "1.0")
        except ValueError:
            self.status_label.config(text="Invalid interval.", foreground="red")
            return
        if interval < 0.2:
            interval = 0.2

        self.is_auto_running = True
        self.auto_btn.config(text="Stop Auto")
        self.status_label.config(text="Auto running…", foreground="#106ba3")

        self.auto_thread = threading.Thread(
            target=self._auto_loop, args=(interval,), daemon=True
        )
        self.auto_thread.start()

    def _auto_loop(self, interval: float) -> None:
        """Background loop to poll captions and auto-copy when text changes."""
        try:
            while self.is_auto_running:
                try:
                    if UIA_AVAILABLE:
                        with uia.UIAutomationInitializerInThread():
                            text = get_captions_text()
                    else:
                        text = ""

                    clean = " ".join(text.split()).strip()
                    if clean and clean != self.last_copied_text:
                        self.last_copied_text = clean
                        if PYPERCLIP_AVAILABLE:
                            pyperclip.copy(clean)
                        self.root.after(0, self._update_preview, clean)
                        self.root.after(
                            0,
                            lambda: self.status_label.config(
                                text="Auto-copied.", foreground="green"
                            ),
                        )

                    time.sleep(interval)
                except Exception:
                    time.sleep(1.0)
        finally:
            if self.is_auto_running:
                self.root.after(
                    0,
                    lambda: self.auto_btn.config(text="Start Auto"),
                )

    def _update_preview(self, text: str) -> None:
        self.preview_box.configure(state=tk.NORMAL)
        self.preview_box.delete("1.0", tk.END)
        self.preview_box.insert(tk.END, text)
        self.preview_box.configure(state=tk.DISABLED)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    app = SimpleCaptionsApp()
    app.run() 