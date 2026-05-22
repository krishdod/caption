#!/usr/bin/env python3
"""
Live Interview Agent v2
Fixes: env-based API key, thread-safe queue, duplicate guard,
       UIA guard, friendly error UX, and clean prompt logic.
"""

import logging
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import pyperclip

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("interview_agent.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)
logging.getLogger("uiautomation").setLevel(logging.WARNING)


def _bind_readonly_copyable(widget):
    """Allow select/copy in a Text widget while blocking edits (DISABLED blocks copy on many Tk builds)."""
    nav_keys = {
        "Left",
        "Right",
        "Up",
        "Down",
        "Home",
        "End",
        "Next",
        "Prior",
        "Shift_L",
        "Shift_R",
        "Control_L",
        "Control_R",
        "Alt_L",
        "Alt_R",
        "Meta_L",
        "Meta_R",
        "Escape",
        "Win_L",
        "Win_R",
        "Caps_Lock",
        "Num_Lock",
    }

    def on_key(event):
        if event.keysym in nav_keys:
            return
        # Ctrl/Cmd + copy / select-all / copy (Insert)
        if event.state & 0x0004 and event.keysym.lower() in ("c", "a", "insert"):
            return
        if event.state & 0x20000 and event.keysym.lower() in ("c", "a"):  # extended modifier (some platforms)
            return
        if event.state & 0x40000 and event.keysym.lower() in ("c", "a"):
            return
        return "break"

    widget.bind("<Key>", on_key, add=True)
    widget.bind("<<Paste>>", lambda e: "break", add=True)
    widget.bind("<<Cut>>", lambda e: "break", add=True)


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
    from groq import Groq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

SYSTEM_PROMPT_TEMPLATE = """You are a Live Interview Agent.

Candidate Resume:
{resume}

Job Description:
{jd}

Rules:
1. Answer only with the final interview answer — no labels, no headings, no commentary outside the answer.
2. Speak in first person as the candidate.
3. Keep every answer natural, human, realistic, and easy to say out loud.
4. No bullet points, no dashes, no markdown formatting.
5. No perfect numbers or polished metrics unless clearly stated in the resume.
6. Size the answer to the question — short for simple, medium for detailed, never over-explain.
7. Behavioral questions — use a natural STAR-style flow without making it feel scripted.
8. Technical questions — answer from experience, not theory. Keep it practical.
9. Coding questions — first explain the overall approach in 2-3 sentences, then give the code, then walk through key lines simply.
10. Keep answers neutral — resume is the main source, JD only lightly referenced.
11. If something is not in the resume, give the safest honest answer that fits the background.
12. give ansawer according to the live interview thaat it is not over explained
13. Never sound robotic or AI-generated."""

is_auto_running       = False
last_caption_text     = ""
last_sent_question    = ""
session_system_prompt = ""
groq_client           = None
answer_queue          = queue.Queue()
MIN_MEANINGFUL_CHARS  = 18


def get_api_key():
    typed = api_key_var.get().strip()
    return typed if typed else GROQ_API_KEY


def init_groq():
    global groq_client
    if not GROQ_AVAILABLE:
        messagebox.showerror("Missing", "Run: pip install groq")
        return False
    key = get_api_key()
    if not key:
        messagebox.showerror("API Key Missing",
            "Enter your Groq API key in the field\nor set GROQ_API_KEY env variable:\n\n"
            "Windows:   set GROQ_API_KEY=gsk_...\nMac/Linux: export GROQ_API_KEY=gsk_...")
        return False
    try:
        groq_client = Groq(api_key=key)
        log.info("Groq client initialised.")
        return True
    except Exception as exc:
        log.error("Groq init failed: %s", exc)
        messagebox.showerror("Groq Error", f"Could not connect: {exc}")
        return False


def ask_groq_worker(question):
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": session_system_prompt},
                {"role": "user",   "content": question},
            ],
            temperature=0.6,
            max_tokens=700,
        )
        answer = response.choices[0].message.content.strip()
        log.info("Answer received (%d chars).", len(answer))
        answer_queue.put(("ok", answer))
    except Exception as exc:
        log.error("Groq request failed: %s", exc)
        answer_queue.put(("err", str(exc)))


def dispatch_question(question):
    threading.Thread(target=ask_groq_worker, args=(question,), daemon=True).start()


def get_silence_gap_seconds() -> float:
    try:
        value = float(silence_gap_var.get().strip())
        if value < 0.8:
            return 0.8
        if value > 8.0:
            return 8.0
        return value
    except Exception:
        return 2.5


def find_live_captions_root():
    if not UIA_AVAILABLE:
        return None
    try:
        for ctrl in uia.GetRootControl().GetChildren():
            try:
                if (ctrl.ClassName == "LiveCaptionsDesktopWindow" or
                        "Live Captions" in (ctrl.Name or "")):
                    return ctrl
            except Exception:
                continue
    except Exception:
        pass
    return None


def get_captions_text():
    if not UIA_AVAILABLE:
        return ""
    try:
        root_cap = find_live_captions_root()
        if not root_cap:
            return ""
        for child, _ in uia.WalkControl(root_cap, includeTop=False, maxDepth=6):
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
    if KEYBOARD_AVAILABLE:
        keyboard.press_and_release("ctrl+win+l")


def force_close_captions():
    try:
        subprocess.Popen(["taskkill", "/f", "/im", "LiveCaptions.exe"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as exc:
        log.warning("Could not close captions: %s", exc)


def clean_text(text):
    return " ".join(text.split()).strip()


def extract_new_portion(previous_window: str, current_window: str) -> str:
    """
    Live Captions is a rolling window; remove the overlapped prefix that
    already existed in the previous finalized window.
    """
    prev = clean_text(previous_window)
    curr = clean_text(current_window)
    if not curr:
        return ""
    if not prev:
        return curr
    if curr == prev:
        return ""

    prev_words = prev.split()
    curr_words = curr.split()
    max_overlap = min(len(prev_words), len(curr_words))

    # Find largest overlap where suffix(prev) == prefix(curr), case-insensitive.
    overlap = 0
    for size in range(max_overlap, 0, -1):
        left = [w.lower() for w in prev_words[-size:]]
        right = [w.lower() for w in curr_words[:size]]
        if left == right:
            overlap = size
            break

    if overlap > 0:
        fresh = " ".join(curr_words[overlap:])
        return clean_text(fresh)
    return curr


def looks_like_question_or_statement(text: str) -> bool:
    clean = clean_text(text)
    if len(clean) < MIN_MEANINGFUL_CHARS:
        return False

    filler_tokens = {
        "ok", "okay", "so", "right", "um", "uh", "hmm", "like", "well", "yeah",
        "you", "know", "please", "thanks", "thank", "alright",
    }
    tokens = [t for t in re.findall(r"[a-zA-Z']+", clean.lower()) if t]
    if not tokens:
        return False

    # Reject text that is mostly filler.
    non_filler = [t for t in tokens if t not in filler_tokens]
    if len(non_filler) <= 2:
        return False
    return True


def start_session():
    global session_system_prompt
    resume = resume_box.get("1.0", tk.END).strip()
    jd     = jd_box.get("1.0", tk.END).strip()
    if not resume:
        messagebox.showwarning("Missing Resume", "Please paste the candidate resume first.")
        return
    if not init_groq():
        return
    session_system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        resume=resume, jd=jd or "Not provided")
    set_status("Session started. Ready for questions.", "#4CAF50")
    log.info("Session started.")


def send_manual_question():
    global last_sent_question
    q = manual_q_box.get("1.0", tk.END).strip()
    if not q:
        return
    if not session_system_prompt:
        messagebox.showwarning("No Session", "Click Start Session first.")
        return
    if q == last_sent_question:
        set_status("Same question already answered.", "orange")
        return
    last_sent_question = q
    set_status("Generating answer...", "#2196F3")
    dispatch_question(q)


def toggle_auto():
    global is_auto_running
    if is_auto_running:
        is_auto_running = False
        auto_btn.config(text="Start Auto Capture")
        set_status("Auto capture stopped.", "orange")
        force_close_captions()
        return
    if not session_system_prompt:
        messagebox.showwarning("No Session", "Click Start Session first.")
        return
    if not UIA_AVAILABLE:
        messagebox.showerror("Missing", "pip install uiautomation")
        return
    open_captions()
    is_auto_running = True
    auto_btn.config(text="Stop Auto Capture")
    set_status("Auto capture running...", "#2196F3")
    threading.Thread(target=auto_loop, daemon=True).start()


def auto_loop():
    global last_caption_text, last_sent_question
    time.sleep(1.5)
    buffer_text = ""
    stable_since = time.monotonic()
    CHECK_INTERVAL = 0.5

    while is_auto_running:
        try:
            with uia.UIAutomationInitializerInThread():
                raw = get_captions_text()
            text = clean_text(raw)
            root.after(0, lambda t=text: update_live_caption_ticker(t))

            if text:
                if text != buffer_text:
                    buffer_text = text
                    stable_since = time.monotonic()
                else:
                    if (time.monotonic() - stable_since) >= get_silence_gap_seconds():
                        new_text = extract_new_portion(last_caption_text, buffer_text)
                        last_caption_text = buffer_text
                        buffer_text = ""
                        stable_since = time.monotonic()

                        if not looks_like_question_or_statement(new_text):
                            log.debug("Ignored non-meaningful/filler fragment: %r", new_text)
                            continue
                        if new_text == last_sent_question:
                            log.debug("Duplicate skipped.")
                            continue

                        last_sent_question = new_text
                        root.after(0, lambda t=new_text: update_question_preview(t))
                        root.after(0, lambda: set_status("Generating answer...", "#2196F3"))
                        dispatch_question(new_text)
        except Exception as exc:
            log.warning("Auto loop error: %s", exc)
        time.sleep(CHECK_INTERVAL)


def poll_answer_queue():
    try:
        while True:
            status, payload = answer_queue.get_nowait()
            if status == "ok":
                show_answer(payload)
            else:
                set_status("Could not generate answer. See interview_agent.log.", "#F44336")
    except queue.Empty:
        pass
    finally:
        root.after(200, poll_answer_queue)


def show_answer(text):
    answer_box.configure(state=tk.NORMAL)
    answer_box.delete("1.0", tk.END)
    answer_box.insert(tk.END, text)
    pyperclip.copy(text)
    set_status("Answer ready — auto copied to clipboard.", "#4CAF50")


def update_question_preview(text):
    manual_q_box.delete("1.0", tk.END)
    manual_q_box.insert(tk.END, text)


def update_live_caption_ticker(text):
    content = text if text else "Listening... (no caption text detected)"
    live_caption_box.configure(state=tk.NORMAL)
    live_caption_box.delete("1.0", tk.END)
    live_caption_box.insert(tk.END, content)


def copy_answer():
    text = answer_box.get("1.0", tk.END).strip()
    if text:
        pyperclip.copy(text)
        set_status("Copied!", "#4CAF50")


def clear_all():
    manual_q_box.delete("1.0", tk.END)
    answer_box.configure(state=tk.NORMAL)
    answer_box.delete("1.0", tk.END)
    set_status("Cleared.", "gray")


def set_status(msg, color="gray"):
    status_box.configure(state=tk.NORMAL)
    status_box.delete("1.0", tk.END)
    status_box.insert(tk.END, msg)
    status_box.configure(fg=color)


def toggle_setup_panel():
    global setup_visible
    if setup_visible:
        try:
            split.forget(setup_panel)
        except Exception:
            pass
        setup_toggle_btn.config(text="Show Setup")
        _set_subtitle("Interview Focus Mode")
        setup_visible = False
    else:
        # Tk PanedWindow has no reliable insert() on all builds; use add(..., before=).
        try:
            split.add(setup_panel, minsize=320, before=live_panel)
        except tk.TclError as exc:
            log.warning("Could not restore setup panel: %s", exc)
            return
        setup_toggle_btn.config(text="Hide Setup")
        _set_subtitle("Setup on left, live interview on right")
        setup_visible = True
        root.update_idletasks()
        try:
            # Keep setup readable while leaving most space for live workspace.
            split.sash_place(0, 360, 0)
        except Exception:
            pass


# ─── GUI ──────────────────────────────────────────────────────────────────────
root = tk.Tk()
root.title("Live Interview Agent v3")
root.geometry("1220x820")
root.configure(bg="#141821")
root.minsize(900, 680)

# Softer dark palette + improved readability
DARK_BG = "#141821"
SURFACE_BG = "#1B2333"
PANEL_BG = "#222B3D"
BORDER = "#2C3650"
TEXT_FG = "#E0E4EC"
MUTED_FG = "#A9B3C9"
HEADING = "#F3F5F8"
ACCENT = "#3FAF8D"
PRIMARY = "#4A90E2"
WARN = "#D0893F"
BTN_BG = "#2C3650"
INPUT_BG = "#1A2233"
ANSWER_BG = "#161F2E"
ANSWER_FG = "#A9F0C8"
FONT_BODY = ("Segoe UI", 10)
FONT_HEAD = ("Segoe UI Semibold", 11)

style = ttk.Style()
style.theme_use("clam")
style.configure("TFrame", background=DARK_BG)
style.configure("Panel.TFrame", background=SURFACE_BG)
style.configure("TLabel", background=DARK_BG, foreground=TEXT_FG, font=FONT_BODY)
style.configure("Muted.TLabel", background=DARK_BG, foreground=MUTED_FG, font=FONT_BODY)
style.configure("TButton", background=BTN_BG, foreground=TEXT_FG, font=FONT_BODY, padding=(10, 6))
style.map("TButton", background=[("active", "#36405C")])
style.configure("TLabelFrame", background=SURFACE_BG, foreground=HEADING, font=FONT_HEAD, bordercolor=BORDER)
style.configure("TLabelFrame.Label", background=SURFACE_BG, foreground=HEADING, font=FONT_HEAD)

main = ttk.Frame(root, padding=14)
main.pack(fill=tk.BOTH, expand=True)

title_row = ttk.Frame(main)
title_row.pack(fill=tk.X, pady=(0, 10))
ttk.Label(
    title_row,
    text="Live Interview Agent",
    font=("Segoe UI Semibold", 14),
    foreground=HEADING,
).pack(side=tk.LEFT)
def _set_subtitle(text: str) -> None:
    subtitle_entry.configure(state=tk.NORMAL)
    subtitle_entry.delete(0, tk.END)
    subtitle_entry.insert(0, text)
    subtitle_entry.configure(state="readonly")


subtitle_entry = tk.Entry(
    title_row,
    state="readonly",
    readonlybackground=DARK_BG,
    fg=MUTED_FG,
    font=FONT_BODY,
    relief=tk.FLAT,
    borderwidth=0,
    highlightthickness=0,
    insertwidth=0,
)
subtitle_entry.pack(side=tk.LEFT, padx=(12, 0), fill=tk.X, expand=True)
_set_subtitle("Setup on left, live interview on right")

setup_toggle_btn = tk.Button(
    title_row,
    text="Hide Setup",
    bg=BTN_BG,
    fg=TEXT_FG,
    font=FONT_BODY,
    relief=tk.FLAT,
    padx=10,
    pady=5,
    command=toggle_setup_panel,
)
setup_toggle_btn.pack(side=tk.RIGHT)

split = tk.PanedWindow(main, orient=tk.HORIZONTAL, sashrelief=tk.FLAT, bg=BORDER, bd=0, sashwidth=8)
split.pack(fill=tk.BOTH, expand=True)

setup_panel = tk.Frame(split, bg=SURFACE_BG, padx=12, pady=12)
live_panel = tk.Frame(split, bg=SURFACE_BG, padx=12, pady=12)
split.add(setup_panel, minsize=320)
split.add(live_panel, minsize=520)
setup_visible = True

# Setup / configuration (left side)
setup_header = tk.Frame(setup_panel, bg=SURFACE_BG)
setup_header.pack(fill=tk.X, pady=(0, 10))
tk.Label(
    setup_header,
    text="Configuration",
    bg=SURFACE_BG,
    fg=HEADING,
    font=("Segoe UI Semibold", 12),
).pack(anchor="w")
tk.Label(
    setup_header,
    text="Complete once before starting your interview",
    bg=SURFACE_BG,
    fg=MUTED_FG,
    font=FONT_BODY,
).pack(anchor="w", pady=(2, 0))

resume_frame = ttk.LabelFrame(setup_panel, text="Candidate Resume", padding=10)
resume_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
resume_box = scrolledtext.ScrolledText(
    resume_frame,
    height=14,
    wrap=tk.WORD,
    bg=INPUT_BG,
    fg=TEXT_FG,
    insertbackground=HEADING,
    font=("Segoe UI", 10),
    relief=tk.FLAT,
    padx=8,
    pady=8,
    spacing1=2,
    spacing2=2,
    spacing3=2,
)
resume_box.pack(fill=tk.BOTH, expand=True)

jd_frame = ttk.LabelFrame(setup_panel, text="Job Description (optional)", padding=10)
jd_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
jd_box = scrolledtext.ScrolledText(
    jd_frame,
    height=10,
    wrap=tk.WORD,
    bg=INPUT_BG,
    fg=TEXT_FG,
    insertbackground=HEADING,
    font=("Segoe UI", 10),
    relief=tk.FLAT,
    padx=8,
    pady=8,
    spacing1=2,
    spacing2=2,
    spacing3=2,
)
jd_box.pack(fill=tk.BOTH, expand=True)

key_frame = ttk.LabelFrame(setup_panel, text="Groq API Key", padding=10)
key_frame.pack(fill=tk.X, pady=(0, 10))
api_key_var = tk.StringVar(value=GROQ_API_KEY)
tk.Entry(
    key_frame,
    textvariable=api_key_var,
    show="*",
    bg=INPUT_BG,
    fg=TEXT_FG,
    insertbackground=HEADING,
    font=FONT_BODY,
    relief=tk.FLAT,
).pack(fill=tk.X, expand=True, ipady=6)

start_row = tk.Frame(setup_panel, bg=SURFACE_BG)
start_row.pack(fill=tk.X)
tk.Button(
    start_row,
    text="Start Session",
    bg=ACCENT,
    fg="#0E1A16",
    font=("Segoe UI Semibold", 11),
    relief=tk.FLAT,
    padx=14,
    pady=8,
    command=start_session,
).pack(side=tk.RIGHT)

# Live interview area (right side)
live_header = tk.Frame(live_panel, bg=SURFACE_BG)
live_header.pack(fill=tk.X, pady=(0, 10))
tk.Label(
    live_header,
    text="Live Workspace",
    bg=SURFACE_BG,
    fg=HEADING,
    font=("Segoe UI Semibold", 12),
).pack(anchor="w")
tk.Label(
    live_header,
    text="Watch captions, review question, and use the answer directly",
    bg=SURFACE_BG,
    fg=MUTED_FG,
    font=FONT_BODY,
).pack(anchor="w", pady=(2, 0))

ticker_frame = ttk.LabelFrame(live_panel, text="Live Captions Ticker (raw rolling text)", padding=10)
ticker_frame.pack(fill=tk.X, pady=(0, 10))
live_caption_box = scrolledtext.ScrolledText(
    ticker_frame,
    height=3,
    wrap=tk.WORD,
    bg=PANEL_BG,
    fg="#9FC9FF",
    insertbackground=HEADING,
    font=("Segoe UI", 10, "italic"),
    relief=tk.FLAT,
    padx=10,
    pady=8,
    spacing1=2,
    spacing2=2,
    spacing3=2,
)
live_caption_box.pack(fill=tk.BOTH, expand=True)
_bind_readonly_copyable(live_caption_box)
live_caption_box.insert(tk.END, "Listening... (no caption text detected)")

q_frame = ttk.LabelFrame(live_panel, text="Interview Question", padding=10)
q_frame.pack(fill=tk.X, pady=(0, 10))
manual_q_box = scrolledtext.ScrolledText(
    q_frame,
    height=4,
    wrap=tk.WORD,
    bg=INPUT_BG,
    fg=TEXT_FG,
    insertbackground=HEADING,
    font=("Segoe UI", 10),
    relief=tk.FLAT,
    padx=8,
    pady=8,
    spacing1=2,
    spacing2=2,
    spacing3=2,
)
manual_q_box.pack(fill=tk.BOTH, expand=True)

action_bar = tk.Frame(live_panel, bg=SURFACE_BG)
action_bar.pack(fill=tk.X, pady=(0, 10))
tk.Button(
    action_bar,
    text="Get Answer",
    bg=PRIMARY,
    fg="white",
    font=("Segoe UI Semibold", 10),
    relief=tk.FLAT,
    padx=12,
    pady=6,
    command=send_manual_question,
).pack(side=tk.LEFT, padx=(0, 8))

auto_btn = tk.Button(
    action_bar,
    text="Start Auto Capture",
    bg=WARN,
    fg="white",
    font=("Segoe UI", 10),
    relief=tk.FLAT,
    padx=12,
    pady=6,
    command=toggle_auto,
)
auto_btn.pack(side=tk.LEFT, padx=(0, 8))

tk.Label(
    action_bar,
    text="Silence gap (s):",
    bg=SURFACE_BG,
    fg=MUTED_FG,
    font=FONT_BODY,
).pack(side=tk.LEFT, padx=(8, 4))
silence_gap_var = tk.StringVar(value="2.5")
tk.Entry(
    action_bar,
    textvariable=silence_gap_var,
    width=5,
    bg=INPUT_BG,
    fg=TEXT_FG,
    insertbackground=HEADING,
    relief=tk.FLAT,
    font=FONT_BODY,
).pack(side=tk.LEFT, padx=(0, 10), ipady=4)

tk.Button(
    action_bar,
    text="Copy Answer",
    bg=BTN_BG,
    fg=TEXT_FG,
    font=FONT_BODY,
    relief=tk.FLAT,
    padx=10,
    pady=6,
    command=copy_answer,
).pack(side=tk.RIGHT, padx=(8, 0))

tk.Button(
    action_bar,
    text="Clear",
    bg=BTN_BG,
    fg=TEXT_FG,
    font=FONT_BODY,
    relief=tk.FLAT,
    padx=10,
    pady=6,
    command=clear_all,
).pack(side=tk.RIGHT)

ans_frame = ttk.LabelFrame(live_panel, text="Answer (ready to speak)", padding=10)
ans_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
answer_box = scrolledtext.ScrolledText(
    ans_frame,
    height=16,
    wrap=tk.WORD,
    bg=ANSWER_BG,
    fg=ANSWER_FG,
    insertbackground=HEADING,
    font=("Segoe UI", 11),
    relief=tk.FLAT,
    padx=12,
    pady=10,
    spacing1=3,
    spacing2=4,
    spacing3=3,
)
answer_box.pack(fill=tk.BOTH, expand=True)
_bind_readonly_copyable(answer_box)

status_box = scrolledtext.ScrolledText(
    live_panel,
    height=2,
    wrap=tk.WORD,
    bg=DARK_BG,
    fg=MUTED_FG,
    insertbackground=HEADING,
    font=FONT_BODY,
    relief=tk.FLAT,
    padx=0,
    pady=4,
    highlightthickness=0,
)
status_box.pack(fill=tk.X, expand=False, anchor=tk.W)
_bind_readonly_copyable(status_box)
status_box.insert(tk.END, "Paste resume, add API key, and start session.")

missing = []
if not UIA_AVAILABLE:      missing.append("uiautomation")
if not KEYBOARD_AVAILABLE: missing.append("keyboard")
if not GROQ_AVAILABLE:     missing.append("groq")
if missing:
    set_status(f"Run first:  pip install {' '.join(missing)}", "orange")

root.after(200, poll_answer_queue)

if __name__ == "__main__":
    root.mainloop()