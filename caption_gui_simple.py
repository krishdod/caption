#!/usr/bin/env python3
"""
Simple Caption GUI
Basic front-end for copying Windows Live Captions text using UI Automation.
- Copy Now button to grab current captions once
- Start/Stop Auto to poll at an interval and auto-copy when text changes
- Live preview area for last copied text (no timestamps)

Requirements: uiautomation, pyperclip, tkinter (builtin)
Run: python caption_gui_simple.py
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import threading
import time
import logging
import os
import json
import uuid
from copy import deepcopy
from string import Template
import requests
import re

import uiautomation as uia
import pyperclip
import ollama

try:
    import keyboard
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False

def _extract_unseen_sentences(chunk: str, recent_list: list[str], n=10) -> str:
    """Return only sentences from chunk not seen in last n in recent_list."""
    if not chunk:
        return ""
    parts = [p.strip() for p in re.split(r"[.!?\n]+", chunk) if p.strip()]
    new_parts: list[str] = []
    for p in parts:
        norm = p.strip().lower()
        if norm and norm not in recent_list[-n:]:
            recent_list.append(norm)
            if len(recent_list) > n:
                del recent_list[0]
            new_parts.append(p)
    return ". ".join(new_parts)


# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ------------------------------
# Prompt manager configuration
# ------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_FILE = os.path.join(BASE_DIR, "prompt_presets.json")
PROMPT_ACTIONS = {
    "manual": "Manual Answer",
    "auto": "Auto Answer",
    "rephrase": "Rephrase"
}

DEFAULT_PROMPTS = {
    "manual": [
        {
            "id": "manual_default",
            "name": "Interview Coach (Default)",
            "template": (
                "Act as my expert interview coach. Use my voice and respond in the first person.\n"
                "\n"
                "Job Description:\n$jd\n"
                "\n"
                "Resume:\n$resume\n"
                "\n"
                "Question:\n$question\n"
                "\n"
                "Transcript Snippet:\n$transcript\n"
                "\n"
                "Provide a concise, confident answer tailored to the JD and resume. Use STAR when appropriate.\n"
                "\n"
                "Answer:"
            )
        }
    ],
    "auto": [
        {
            "id": "auto_default",
            "name": "Auto Interview Answer",
            "template": (
                "You are my interview copilot. Craft a spoken response I can say immediately.\n"
                "\n"
                "Job Description:\n$jd\n"
                "\n"
                "Resume:\n$resume\n"
                "\n"
                "Latest Question:\n$question\n"
                "\n"
                "Most recent transcript chunk:\n$transcript\n"
                "\n"
                "Deliver only the answer text in first person, confident, conversational tone."
            )
        }
    ],
    "rephrase": [
        {
            "id": "rephrase_default",
            "name": "Clean Transcript",
            "template": (
                "Rewrite the following transcript into clean, fluent English, removing filler words but keeping meaning.\n"
                "\n"
                "$transcript"
            )
        }
    ]
}

# ------------------------------
# Ollama configuration
# ------------------------------
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
# Set to the model you have pulled into Ollama
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b-cloud")

# ------------------------------
# Ollama helper using official library
# ------------------------------
def ask_ollama(prompt: str, *, model: str | None = None, temperature: float = 0.2) -> str:
    """Send a chat completion request using the official Ollama library.

    Returns model text or empty string on failure.
    """
    selected_model = model or OLLAMA_MODEL
    try:
        response = ollama.chat(
            model=selected_model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": temperature}
        )
        
        if response and "message" in response and "content" in response["message"]:
            return response["message"]["content"].strip()
        return ""
            
    except Exception as e:
        logger.error(f"Ollama request failed: {e}")
        return ""

# ------------------------------
# File reading helpers
# ------------------------------

def read_text_file(file_path: str) -> str:
    """Read plain text file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except UnicodeDecodeError:
        # Try with different encoding
        with open(file_path, 'r', encoding='latin-1') as f:
            return f.read()

def read_pdf_file(file_path: str) -> str:
    """Read PDF file using PyPDF2 or pdfplumber."""
    try:
        import PyPDF2
        with open(file_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            text = ""
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
            return text.strip()
    except ImportError:
        try:
            import pdfplumber  # type: ignore
            with pdfplumber.open(file_path) as pdf:
                text = ""
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
                return text.strip()
        except ImportError:
            return "PDF reading not available. Install PyPDF2 or pdfplumber."
    except Exception as e:
        return f"Error reading PDF: {str(e)}"

def read_docx_file(file_path: str) -> str:
    """Read DOCX file using python-docx."""
    try:
        from docx import Document
        doc = Document(file_path)
        text = ""
        for paragraph in doc.paragraphs:
            text += paragraph.text + "\n"
        return text
    except ImportError:
        return "DOCX reading not available. Install python-docx."

def read_doc_file(file_path: str) -> str:
    """Read DOC file using python-docx2txt."""
    try:
        import docx2txt
        return docx2txt.process(file_path)
    except ImportError:
        return "DOC reading not available. Install docx2txt."

def read_file_content(file_path: str) -> str: 
    """Read file content based on extension."""
    ext = os.path.splitext(file_path)[1].lower()
    
    if ext == '.txt':
        return read_text_file(file_path)
    elif ext == '.pdf':
        return read_pdf_file(file_path)
    elif ext == '.docx':
        return read_docx_file(file_path)
    elif ext == '.doc':
        return read_doc_file(file_path)
    else:
        return f"Unsupported file type: {ext}"

# ------------------------------
# Question detection helpers
# ------------------------------

def detect_question(text: str) -> bool:
    """Detect if text contains interview questions."""
    if not text or len(text.strip()) < 5:
        return False
    
    text_lower = text.lower().strip()
    
    # Question patterns - more comprehensive
    question_indicators = [
        "what", "how", "why", "when", "where", "which", "who",
        "tell me about", "describe", "explain", "can you",
        "do you have", "have you", "are you", "would you",
        "what is your", "how do you", "what are your",
        "experience with", "familiar with", "knowledge of",
        "challenge", "difficult", "problem", "situation",
        "strengths", "weaknesses", "goals", "motivation",
        "walk me through", "give me an example", "tell me about a time",
        "how would you", "what would you do", "describe a time",
        "can you tell me", "do you know", "are you familiar",
        "have you worked", "what's your", "how's your"
    ]
    
    # Check for question marks
    if "?" in text:
        return True
    
    # Check for question patterns anywhere in the text (not just at start)
    for indicator in question_indicators:
        if indicator in text_lower:
            return True
    
    # Check for interview-specific phrases
    interview_phrases = [
        "interview", "candidate", "position", "role", "company",
        "team", "project", "responsibilities", "qualifications",
        "background", "experience", "skills", "education"
    ]
    
    phrase_count = sum(1 for phrase in interview_phrases if phrase in text_lower)
    if phrase_count >= 1:  # Lowered threshold
        return True
    
    return False

def extract_latest_question(text: str) -> str:
    """Extract the most recent question or prompt from the text."""
    # Heuristic: Use the text after last question mark, or last sentence
    text = text.strip()
    if not text:
        return ''
    qmarks = [m.start() for m in re.finditer(r'\?', text)]
    if qmarks:
        last_qmark = qmarks[-1]
        # Everything after last question mark, up to next major punctuation if any
        candidate = text[last_qmark+1:].strip()
        if candidate:
            return candidate
        # otherwise fallback to up-to-last-qmark
        return text[:last_qmark+1].splitlines()[-1].strip()
    # Fallback: use the last 'sentence' split by period/exclamation/question/line
    candidates = re.split(r'[.!?\n]', text)
    if candidates:
        return candidates[-1].strip()
    return text

# ------------------------------
# Core functions with robust error handling
# ------------------------------

def find_live_captions_root():
    """Find the handle of the Live Captions window with robust error handling."""
    try:
        root_control = uia.GetRootControl()
        for ctrl in root_control.GetChildren():
            try:
                class_name = ctrl.ClassName
                name = ctrl.Name
                if class_name == "LiveCaptionsDesktopWindow" or "Live Captions" in name:
                    return ctrl
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"UIA root access failed: {e}")
    return None

def get_full_captions_text() -> str:
    """Returns the entire text visible in the Live Captions overlay with robust error handling."""
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
    except Exception as e:
        logger.warning(f"Text extraction failed: {e}")
    return ""

# ------------------------------
# GUI Application
# ------------------------------
class CaptionGUI:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("AI Interview Agent")
        self.root.geometry("800x700")
        self.root.minsize(600, 500)

        self.is_auto_running = False
        self.auto_thread: threading.Thread | None = None
        self.last_copied_text = ""
        self.transcript_buffer: list[str] = []
        
        # Captions toggle state
        self.captions_open = False
        
        # Transcription control
        self.is_transcribing = False
        self.transcription_thread: threading.Thread | None = None
        self.last_full_caption = ""  # Store the last full caption to detect new content
        self.caption_history = []  # Store recent captions for better detection
        self.last_click_caption = ""  # Snapshot of caption at last manual capture
        self.seen_sentences: set[str] = set()  # Track normalized sentences to avoid repeats
        self.dedup_recent_sentences: list[str] = []  # Track last 10 seen
        
        # Store JD and Resume data
        self.jd_text = ""
        self.resume_text = ""
        self.setup_complete = False

        # Word document target for saving AI answers (ChatGPT / Perplexity)
        self.word_doc_path: str | None = None

        # Prompt presets
        self.prompt_presets, self.prompt_selection = self._load_prompt_data()
        self._ensure_prompt_defaults()
        self.prompt_choice_vars: dict[str, tk.StringVar] = {}
        self.prompt_comboboxes: dict[str, ttk.Combobox] = {}
        self.prompt_manager_window: tk.Toplevel | None = None
        self.prompt_tree: ttk.Treeview | None = None
        self.prompt_preview: scrolledtext.ScrolledText | None = None

        # Show setup wizard first
        self._show_setup_wizard()

    def _show_setup_wizard(self) -> None:
        """Show setup wizard for JD and Resume."""
        # Clear any existing widgets
        for widget in self.root.winfo_children():
            widget.destroy()
        
        # Setup wizard container
        wizard_frame = ttk.Frame(self.root, padding=40)
        wizard_frame.pack(fill=tk.BOTH, expand=True)
        
        # Title
        title_label = ttk.Label(wizard_frame, text="🤖 AI Interview Agent Setup", 
                               font=("Arial", 16, "bold"))
        title_label.pack(pady=(0, 20))
        
        # Instructions
        instructions = ttk.Label(wizard_frame, 
                               text="Please provide your Job Description and Resume to get started.\nThe AI will use this information to generate personalized interview answers.",
                               font=("Arial", 10))
        instructions.pack(pady=(0, 30))
        
        # Job Description section
        jd_frame = ttk.LabelFrame(wizard_frame, text="Job Description", padding=15)
        jd_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 15))
        
        jd_controls = ttk.Frame(jd_frame)
        jd_controls.pack(fill=tk.X, pady=(0, 5))
        
        ttk.Label(jd_controls, text="Upload JD file or paste text:").pack(side=tk.LEFT)
        ttk.Button(jd_controls, text="Upload JD", 
                  command=self._upload_jd_wizard).pack(side=tk.RIGHT)
        
        self.jd_wizard_text = scrolledtext.ScrolledText(jd_frame, wrap=tk.WORD, height=8)
        self.jd_wizard_text.pack(fill=tk.BOTH, expand=True, pady=(5, 0))
        
        # Resume section
        resume_frame = ttk.LabelFrame(wizard_frame, text="Your Resume", padding=15)
        resume_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 20))
        
        resume_controls = ttk.Frame(resume_frame)
        resume_controls.pack(fill=tk.X, pady=(0, 5))
        
        ttk.Label(resume_controls, text="Upload resume file or paste text:").pack(side=tk.LEFT)
        ttk.Button(resume_controls, text="Upload Resume", 
                  command=self._upload_resume_wizard).pack(side=tk.RIGHT)
        
        self.resume_wizard_text = scrolledtext.ScrolledText(resume_frame, wrap=tk.WORD, height=8)
        self.resume_wizard_text.pack(fill=tk.BOTH, expand=True, pady=(5, 0))
        
        # Continue button
        continue_btn = ttk.Button(wizard_frame, text="🚀 Start Interview Agent", 
                                 command=self._complete_setup, style="Accent.TButton")
        continue_btn.pack(pady=(20, 0))
        
        # Status
        self.wizard_status = ttk.Label(wizard_frame, text="", foreground="green")
        self.wizard_status.pack(pady=(10, 0))

    def _upload_resume_wizard(self) -> None:
        """Upload resume file in wizard."""
        file_path = filedialog.askopenfilename(
            title="Select Resume File",
            filetypes=[
                ("All supported", "*.pdf;*.docx;*.doc;*.txt"),
                ("PDF files", "*.pdf"),
                ("Word documents", "*.docx;*.doc"),
                ("Text files", "*.txt"),
                ("All files", "*.*")
            ]
        )
        if file_path:
            try:
                content = read_file_content(file_path)
                
                # Check for errors in content
                if content.startswith(("PDF reading not available", "DOCX reading not available", "DOC reading not available")):
                    self.wizard_status.config(text=f"❌ {content}", foreground="red")
                    return
                
                if content.startswith("Unsupported file type"):
                    self.wizard_status.config(text=f"❌ {content}", foreground="red")
                    return
                
                if content.startswith("Error reading"):
                    self.wizard_status.config(text=f"❌ {content}", foreground="red")
                    return
                
                # Clean up the content
                content = content.strip()
                if not content:
                    self.wizard_status.config(text="❌ File appears to be empty", foreground="red")
                    return
                
                self.resume_wizard_text.delete("1.0", tk.END)
                self.resume_wizard_text.insert("1.0", content)
                self.wizard_status.config(text="✅ Resume uploaded successfully!", foreground="green")
                
            except Exception as e:
                self.wizard_status.config(text=f"❌ Error uploading file: {str(e)}", foreground="red")

    def _upload_jd_wizard(self) -> None:
        """Upload JD file in wizard."""
        file_path = filedialog.askopenfilename(
            title="Select Job Description File",
            filetypes=[
                ("All supported", "*.pdf;*.docx;*.doc;*.txt"),
                ("PDF files", "*.pdf"),
                ("Word documents", "*.docx;*.doc"),
                ("Text files", "*.txt"),
                ("All files", "*.*")
            ]
        )
        if file_path:
            try:
                content = read_file_content(file_path)
                
                # Check for errors in content
                if content.startswith(("PDF reading not available", "DOCX reading not available", "DOC reading not available")):
                    self.wizard_status.config(text=f"❌ {content}", foreground="red")
                    return
                
                if content.startswith("Unsupported file type"):
                    self.wizard_status.config(text=f"❌ {content}", foreground="red")
                    return
                
                if content.startswith("Error reading"):
                    self.wizard_status.config(text=f"❌ {content}", foreground="red")
                    return
                
                # Clean up the content
                content = content.strip()
                if not content:
                    self.wizard_status.config(text="❌ File appears to be empty", foreground="red")
                    return
                
                self.jd_wizard_text.delete("1.0", tk.END)
                self.jd_wizard_text.insert("1.0", content)
                self.wizard_status.config(text="✅ Job Description uploaded successfully!", foreground="green")
                
            except Exception as e:
                self.wizard_status.config(text=f"❌ Error uploading file: {str(e)}", foreground="red")

    def _complete_setup(self) -> None:
        """Complete setup and show main interface."""
        try:
            # Get text from wizard
            self.jd_text = self.jd_wizard_text.get("1.0", tk.END).strip()
            self.resume_text = self.resume_wizard_text.get("1.0", tk.END).strip()
            
            # Validate
            if not self.jd_text:
                self.wizard_status.config(text="❌ Please enter a Job Description", foreground="red")
                return
            
            if not self.resume_text:
                self.wizard_status.config(text="❌ Please enter or upload your Resume", foreground="red")
                return
            
            # Complete setup
            self.setup_complete = True
            self.wizard_status.config(text="✅ Setup complete! Loading main interface...", foreground="green")
            
            # Clear wizard and show main UI immediately
            self._build_ui()
            
        except Exception as e:
            self.wizard_status.config(text=f"❌ Error: {str(e)}", foreground="red")

    def _build_ui(self) -> None:
        # Clear any existing widgets
        for widget in self.root.winfo_children():
            widget.destroy()
        
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill=tk.BOTH, expand=True)

        # Controls row
        controls = ttk.Frame(container)
        controls.pack(fill=tk.X, pady=(0, 8))

        self.copy_btn = ttk.Button(controls, text="Copy Now", command=self.copy_now)
        self.copy_btn.pack(side=tk.LEFT)

        # Open Windows Captions button (toggle)
        # Check initial state and set button text accordingly
        initial_captions_state = self._check_captions_window_open()
        self.captions_open = initial_captions_state
        btn_text = "Close Captions" if initial_captions_state else "Open Captions"
        self.open_captions_btn = ttk.Button(controls, text=btn_text, command=self.toggle_windows_captions)
        self.open_captions_btn.pack(side=tk.LEFT, padx=(8, 0))

        # Close Captions button (clicks the X button directly)
        self.close_captions_btn = ttk.Button(controls, text="❌ Close", command=self.close_captions_window)
        self.close_captions_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.auto_btn = ttk.Button(controls, text="Start Auto", command=self.toggle_auto)
        self.auto_btn.pack(side=tk.LEFT, padx=(8, 0))

        # Transcription toggle button
        self.transcribe_btn = ttk.Button(controls, text="🎤 Start Transcribing", command=self.toggle_transcription, style="Accent.TButton")
        self.transcribe_btn.pack(side=tk.LEFT, padx=(8, 0))

        # Click-only capture button
        self.capture_latest_btn = ttk.Button(controls, text="Capture Latest", command=self.capture_latest)
        self.capture_latest_btn.pack(side=tk.LEFT, padx=(8, 0))

        # Clear transcript button
        self.clear_transcript_btn = ttk.Button(controls, text="🗑️ Clear", command=self.clear_transcript)
        self.clear_transcript_btn.pack(side=tk.LEFT, padx=(4, 0))


        # Interval selector
        interval_wrap = ttk.Frame(controls)
        interval_wrap.pack(side=tk.LEFT, padx=(16, 0))
        ttk.Label(interval_wrap, text="Interval (s):").pack(side=tk.LEFT)
        self.interval_var = tk.StringVar(value="1.0")
        self.interval_entry = ttk.Entry(interval_wrap, textvariable=self.interval_var, width=6)
        self.interval_entry.pack(side=tk.LEFT, padx=(6, 0))

        # Status
        self.status_var = tk.StringVar(value="Ready")
        self.status_lbl = ttk.Label(container, textvariable=self.status_var, foreground="green")
        self.status_lbl.pack(anchor=tk.W, pady=(0, 6))
        
        # Setup status
        setup_status = "✅ Setup Complete" if self.setup_complete else "❌ Setup Required"
        setup_color = "green" if self.setup_complete else "red"
        self.setup_status_lbl = ttk.Label(container, text=f"Status: {setup_status}", foreground=setup_color)
        self.setup_status_lbl.pack(anchor=tk.W, pady=(0, 6))

        # Options row
        opts = ttk.Frame(container)
        opts.pack(fill=tk.X, pady=(0, 8))
        self.accumulate_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Accumulate transcript", variable=self.accumulate_var).pack(side=tk.LEFT)
        self.auto_ai_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Auto AI Answer", variable=self.auto_ai_var).pack(side=tk.LEFT, padx=(12, 0))

        # Last snippet
        preview_frame = ttk.LabelFrame(container, text="AI Answer Preview", padding=8)
        preview_frame.pack(fill=tk.BOTH, expand=True)
        self.preview = scrolledtext.ScrolledText(preview_frame, wrap=tk.WORD, height=12)
        self.preview.pack(fill=tk.BOTH, expand=True)

        # Transcript
        transcript_frame = ttk.LabelFrame(container, text="Transcript (accumulated)", padding=8)
        transcript_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.transcript_box = scrolledtext.ScrolledText(transcript_frame, wrap=tk.WORD, height=10)
        self.transcript_box.pack(fill=tk.BOTH, expand=True)

        # Interview context
        ctx = ttk.LabelFrame(container, text="Interview Agent", padding=8)
        ctx.pack(fill=tk.X, pady=(8, 0))
        
        # Question + action
        q_row = ttk.Frame(ctx)
        q_row.pack(fill=tk.X, expand=False, pady=(0, 6))
        ttk.Label(q_row, text="Enter or capture interviewer question:").pack(anchor=tk.W)
        self.question_entry = ttk.Entry(q_row)
        self.question_entry.pack(fill=tk.X, expand=True)
        
        # Prompt selection panel
        prompt_frame = ttk.LabelFrame(ctx, text="Prompt Selection", padding=8)
        prompt_frame.pack(fill=tk.X, pady=(8, 0))
        self.prompt_choice_vars.clear()
        self.prompt_comboboxes.clear()
        prompt_columns = ttk.Frame(prompt_frame)
        prompt_columns.pack(fill=tk.X)
        for action_key, action_label in PROMPT_ACTIONS.items():
            col = ttk.Frame(prompt_columns)
            col.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=4)
            ttk.Label(col, text=f"{action_label} Prompt").pack(anchor=tk.W)
            names = [p["name"] for p in self.prompt_presets.get(action_key, [])]
            current_name = self._get_prompt_name_by_id(action_key, self.prompt_selection.get(action_key))
            var = tk.StringVar(value=current_name)
            combo = ttk.Combobox(col, state="readonly", textvariable=var, values=names)
            combo.pack(fill=tk.X, pady=(2, 0))
            combo.bind("<<ComboboxSelected>>", lambda _e, k=action_key, v=var: self._handle_prompt_selection(k, v.get()))
            self.prompt_choice_vars[action_key] = var
            self.prompt_comboboxes[action_key] = combo
        ttk.Button(prompt_frame, text="📝 Manage Prompts", command=self.open_prompt_manager).pack(anchor=tk.E, pady=(8, 0))
        
        actions = ttk.Frame(ctx)
        actions.pack(fill=tk.X, expand=False, pady=(6, 0))
        
        # Main AI Answer button - bigger and more prominent
        self.main_ai_btn = ttk.Button(actions, text="🤖 AI Answer", command=self.ai_answer, style="Accent.TButton")
        self.main_ai_btn.pack(side=tk.LEFT, padx=(0, 8))
        
        # Buttons to send clipboard answers to a local Word document
        ttk.Button(
            actions,
            text="ChatGPT → DOC",
            command=lambda: self.append_clipboard_to_doc("ChatGPT"),
        ).pack(side=tk.LEFT, padx=(4, 0))
        
        ttk.Button(
            actions,
            text="Perplexity → DOC",
            command=lambda: self.append_clipboard_to_doc("Perplexity"),
        ).pack(side=tk.LEFT, padx=(4, 0))
        
        ttk.Button(
            actions,
            text="Select Word DOC",
            command=self.select_word_doc,
        ).pack(side=tk.LEFT, padx=(8, 0))

        # Buttons to send clipboard answers directly into an open Google Doc tab
        ttk.Button(
            actions,
            text="ChatGPT → GDoc",
            command=lambda: self.send_clipboard_to_google_doc("ChatGPT"),
        ).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Button(
            actions,
            text="Perplexity → GDoc",
            command=lambda: self.send_clipboard_to_google_doc("Perplexity"),
        ).pack(side=tk.LEFT, padx=(4, 0))
        
        # Secondary buttons
        ttk.Button(actions, text="⚙️ Setup", command=self._show_setup_wizard).pack(side=tk.LEFT, padx=(4, 0))


    def _load_prompt_data(self) -> tuple[dict[str, list[dict]], dict[str, str]]:
        """Load prompt presets and selection from disk."""
        if os.path.exists(PROMPTS_FILE):
            try:
                with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, json.JSONDecodeError):
                raw = {}
        else:
            raw = {}

        if isinstance(raw, dict) and "presets" in raw:
            presets = raw.get("presets", {})
            selected = raw.get("selected", {})
        else:
            # backwards compatibility or empty file
            presets = raw if isinstance(raw, dict) else {}
            selected = {}

        cleaned: dict[str, list[dict]] = {}
        for action_key in PROMPT_ACTIONS:
            cleaned[action_key] = []
            for entry in presets.get(action_key, []):
                if (
                    isinstance(entry, dict)
                    and entry.get("id")
                    and entry.get("name")
                    and entry.get("template")
                ):
                    cleaned[action_key].append(entry)
        return cleaned, selected

    def _save_prompt_data(self) -> None:
        data = {
            "presets": self.prompt_presets,
            "selected": self.prompt_selection,
        }
        try:
            with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f"Failed to save prompt presets: {e}")

    def _ensure_prompt_defaults(self) -> None:
        changed = False
        for action_key, defaults in DEFAULT_PROMPTS.items():
            if action_key not in self.prompt_presets or not self.prompt_presets[action_key]:
                self.prompt_presets[action_key] = deepcopy(defaults)
                changed = True
            current_id = self.prompt_selection.get(action_key)
            if not current_id or not self._get_prompt_by_id(action_key, current_id):
                self.prompt_selection[action_key] = self.prompt_presets[action_key][0]["id"]
                changed = True
        if changed:
            self._save_prompt_data()

    def _get_prompt_by_id(self, action_key: str, prompt_id: str | None) -> dict | None:
        if not prompt_id:
            return None
        for prompt in self.prompt_presets.get(action_key, []):
            if prompt.get("id") == prompt_id:
                return prompt
        return None

    def _get_prompt_by_name(self, action_key: str, name: str) -> dict | None:
        if not name:
            return None
        for prompt in self.prompt_presets.get(action_key, []):
            if prompt.get("name") == name:
                return prompt
        return None

    def _get_prompt_name_by_id(self, action_key: str, prompt_id: str | None) -> str:
        prompt = self._get_prompt_by_id(action_key, prompt_id)
        if prompt:
            return prompt.get("name", "")
        prompts = self.prompt_presets.get(action_key, [])
        return prompts[0]["name"] if prompts else ""

    def _handle_prompt_selection(self, action_key: str, name: str) -> None:
        prompt = self._get_prompt_by_name(action_key, name)
        if not prompt:
            return
        self.prompt_selection[action_key] = prompt["id"]
        self._save_prompt_data()

    def _refresh_prompt_controls(self) -> None:
        for action_key, combo in self.prompt_comboboxes.items():
            names = [p["name"] for p in self.prompt_presets.get(action_key, [])]
            combo["values"] = names
            current_name = self._get_prompt_name_by_id(action_key, self.prompt_selection.get(action_key))
            if current_name:
                combo.set(current_name)

    def open_prompt_manager(self) -> None:
        if self.prompt_manager_window and tk.Toplevel.winfo_exists(self.prompt_manager_window):
            self.prompt_manager_window.lift()
            return

        win = tk.Toplevel(self.root)
        win.title("Prompt Manager")
        win.geometry("780x520")
        def _close_manager() -> None:
            self.prompt_manager_window = None
            self.prompt_tree = None
            self.prompt_preview = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _close_manager)
        self.prompt_manager_window = win

        tree = ttk.Treeview(win, columns=("action", "name"), show="headings", selectmode="browse", height=10)
        tree.heading("action", text="Action")
        tree.heading("name", text="Prompt Name")
        tree.column("action", width=150, anchor=tk.W)
        tree.column("name", width=400, anchor=tk.W)
        tree.pack(fill=tk.BOTH, expand=False, padx=12, pady=(12, 6))
        tree.bind("<<TreeviewSelect>>", lambda _e: self._on_prompt_tree_select())
        self.prompt_tree = tree

        preview = scrolledtext.ScrolledText(win, wrap=tk.WORD, height=12)
        preview.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))
        preview.config(state=tk.DISABLED)
        self.prompt_preview = preview

        btn_row = ttk.Frame(win)
        btn_row.pack(fill=tk.X, padx=12, pady=(0, 12))
        ttk.Button(btn_row, text="Add", command=self._add_prompt).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Edit", command=self._edit_prompt).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btn_row, text="Duplicate", command=self._duplicate_prompt).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btn_row, text="Delete", command=self._delete_prompt).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btn_row, text="Close", command=_close_manager).pack(side=tk.RIGHT)

        self._populate_prompt_tree()

    def _populate_prompt_tree(self) -> None:
        if not self.prompt_tree:
            return
        self.prompt_tree.delete(*self.prompt_tree.get_children())
        for action_key, prompts in self.prompt_presets.items():
            for prompt in prompts:
                item_id = f"{action_key}|{prompt['id']}"
                self.prompt_tree.insert("", tk.END, iid=item_id, values=(PROMPT_ACTIONS[action_key], prompt["name"]))

    def _on_prompt_tree_select(self) -> None:
        if not (self.prompt_tree and self.prompt_preview):
            return
        selection = self.prompt_tree.selection()
        if not selection:
            self.prompt_preview.config(state=tk.NORMAL)
            self.prompt_preview.delete("1.0", tk.END)
            self.prompt_preview.config(state=tk.DISABLED)
            return
        action_key, prompt = self._get_selected_prompt_ref(selection[0])
        self.prompt_preview.config(state=tk.NORMAL)
        self.prompt_preview.delete("1.0", tk.END)
        if prompt:
            self.prompt_preview.insert(tk.END, prompt.get("template", ""))
        self.prompt_preview.config(state=tk.DISABLED)

    def _get_selected_prompt_ref(self, tree_id: str) -> tuple[str | None, dict | None]:
        if "|" not in tree_id:
            return None, None
        action_key, prompt_id = tree_id.split("|", 1)
        prompt = self._get_prompt_by_id(action_key, prompt_id)
        return action_key, prompt

    def _add_prompt(self) -> None:
        self._open_prompt_editor(mode="add")

    def _edit_prompt(self) -> None:
        if not self.prompt_tree:
            return
        selection = self.prompt_tree.selection()
        if not selection:
            messagebox.showinfo("Prompt Manager", "Select a prompt to edit.")
            return
        action_key, prompt = self._get_selected_prompt_ref(selection[0])
        if action_key and prompt:
            self._open_prompt_editor(mode="edit", action_key=action_key, prompt=prompt)

    def _duplicate_prompt(self) -> None:
        if not self.prompt_tree:
            return
        selection = self.prompt_tree.selection()
        if not selection:
            messagebox.showinfo("Prompt Manager", "Select a prompt to duplicate.")
            return
        action_key, prompt = self._get_selected_prompt_ref(selection[0])
        if not (action_key and prompt):
            return
        new_prompt = {
            "id": uuid.uuid4().hex,
            "name": f"{prompt['name']} (Copy)",
            "template": prompt.get("template", ""),
        }
        self.prompt_presets[action_key].append(new_prompt)
        self.prompt_selection.setdefault(action_key, new_prompt["id"])
        self._save_prompt_data()
        self._populate_prompt_tree()
        self._refresh_prompt_controls()

    def _delete_prompt(self) -> None:
        if not self.prompt_tree:
            return
        selection = self.prompt_tree.selection()
        if not selection:
            messagebox.showinfo("Prompt Manager", "Select a prompt to delete.")
            return
        action_key, prompt = self._get_selected_prompt_ref(selection[0])
        if not (action_key and prompt):
            return
        prompts = self.prompt_presets.get(action_key, [])
        if len(prompts) <= 1:
            messagebox.showwarning("Prompt Manager", "At least one prompt is required per action.")
            return
        if not messagebox.askyesno("Prompt Manager", f"Delete '{prompt['name']}'?"):
            return
        self.prompt_presets[action_key] = [p for p in prompts if p["id"] != prompt["id"]]
        if self.prompt_selection.get(action_key) == prompt["id"]:
            self.prompt_selection[action_key] = self.prompt_presets[action_key][0]["id"]
        self._save_prompt_data()
        self._populate_prompt_tree()
        self._refresh_prompt_controls()

    def _open_prompt_editor(self, *, mode: str, action_key: str | None = None, prompt: dict | None = None) -> None:
        is_edit = mode == "edit" and prompt is not None and action_key is not None
        editor = tk.Toplevel(self.root)
        editor.title("Edit Prompt" if is_edit else "Add Prompt")
        editor.geometry("640x520")

        tk.Label(editor, text="Prompt Name:").pack(anchor=tk.W, padx=12, pady=(12, 0))
        name_var = tk.StringVar(value=prompt["name"] if is_edit else "")
        ttk.Entry(editor, textvariable=name_var).pack(fill=tk.X, padx=12, pady=(0, 8))

        tk.Label(editor, text="Applies To:").pack(anchor=tk.W, padx=12)
        action_var = tk.StringVar(value=action_key if is_edit else list(PROMPT_ACTIONS.keys())[0])
        ttk.Combobox(editor, state="readonly", textvariable=action_var, values=list(PROMPT_ACTIONS.keys())).pack(fill=tk.X, padx=12, pady=(0, 8))
        
        tk.Label(editor, text="Prompt Template (use $question, $jd, $resume, $transcript):").pack(anchor=tk.W, padx=12)
        text_box = scrolledtext.ScrolledText(editor, wrap=tk.WORD, height=20)
        text_box.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        if is_edit:
            text_box.insert(tk.END, prompt.get("template", ""))

        btn_row = ttk.Frame(editor)
        btn_row.pack(fill=tk.X, padx=12, pady=(0, 12))

        def save_prompt() -> None:
            name = name_var.get().strip()
            target_action = action_var.get()
            template_text = text_box.get("1.0", tk.END).strip()
            if not name or not template_text:
                messagebox.showerror("Prompt Manager", "Name and template cannot be empty.")
                return
            if self._prompt_name_exists(target_action, name, exclude_id=prompt["id"] if is_edit else None):
                messagebox.showerror("Prompt Manager", "A prompt with that name already exists for this action.")
                return

            if is_edit and prompt:
                if target_action != action_key:
                    # move prompt to new action bucket
                    self.prompt_presets[action_key] = [p for p in self.prompt_presets[action_key] if p["id"] != prompt["id"]]
                    prompt["id"] = uuid.uuid4().hex  # assign new id when moving
                    self.prompt_presets.setdefault(target_action, []).append(prompt)
                    self.prompt_selection[target_action] = prompt["id"]
                prompt["name"] = name
                prompt["template"] = template_text
            else:
                new_prompt = {
                    "id": uuid.uuid4().hex,
                    "name": name,
                    "template": template_text,
                }
                self.prompt_presets.setdefault(target_action, []).append(new_prompt)
                self.prompt_selection[target_action] = new_prompt["id"]
            self._ensure_prompt_defaults()
            self._save_prompt_data()
            self._populate_prompt_tree()
            self._refresh_prompt_controls()
            editor.destroy()

        ttk.Button(btn_row, text="Save", command=save_prompt).pack(side=tk.RIGHT)
        ttk.Button(btn_row, text="Cancel", command=editor.destroy).pack(side=tk.RIGHT, padx=(0, 8))

    def _prompt_name_exists(self, action_key: str, name: str, exclude_id: str | None = None) -> bool:
        for prompt in self.prompt_presets.get(action_key, []):
            if prompt.get("name") == name and prompt.get("id") != exclude_id:
                return True
        return False

    def _render_prompt(self, action_key: str, context: dict[str, str]) -> str:
        prompt = self._get_prompt_by_id(action_key, self.prompt_selection.get(action_key))
        if not prompt:
            return ""
        template_str = prompt.get("template", "")
        try:
            return Template(template_str).safe_substitute(context)
        except Exception as e:
            logger.error(f"Prompt rendering failed: {e}")
            return template_str


    def _check_captions_window_open(self) -> bool:
        """Check if Windows Live Captions window is currently open."""
        try:
            with uia.UIAutomationInitializerInThread():
                root = find_live_captions_root()
                return root is not None
        except Exception:
            return False
    
    def _close_captions_window(self) -> bool:
        """Find and click the close button (X) in the Windows Live Captions window - optimized for speed."""
        try:
            with uia.UIAutomationInitializerInThread():
                root = find_live_captions_root()
                if not root:
                    return False
                
                # Optimized: Single pass through tree, collect all buttons and find close button
                close_button = None
                buttons_with_position = []
                
                # Single pass strategy - collect buttons and identify close button in one go
                for child, depth in uia.WalkControl(root, includeTop=False, maxDepth=8):
                    try:
                        if child.ControlTypeName == "ButtonControl":
                            # Quick check for close button indicators
                            name = (child.Name or "").lower()
                            automation_id = (child.AutomationId or "").lower()
                            
                            # Strategy 1: Direct match (fastest)
                            if ("close" in name or "close" in automation_id or 
                                name in ("×", "x", "✕") or 
                                automation_id.endswith(("closebutton", "close_button"))):
                                close_button = child
                                break  # Found it, exit immediately
                            
                            # Strategy 2: Collect for position-based search (backup)
                            try:
                                rect = child.BoundingRectangle
                                if rect:
                                    # Calculate right edge position
                                    if hasattr(rect, 'right'):
                                        right_edge = rect.right
                                    elif hasattr(rect, 'width'):
                                        right_edge = rect.left + rect.width
                                    elif isinstance(rect, (tuple, list)) and len(rect) >= 4:
                                        right_edge = rect[0] + rect[2]
                                    else:
                                        right_edge = 0
                                    buttons_with_position.append((child, right_edge))
                            except:
                                buttons_with_position.append((child, 0))
                    except Exception:
                        continue
                
                # Fallback: Use rightmost button if direct match not found
                if not close_button and buttons_with_position:
                    buttons_with_position.sort(key=lambda x: x[1], reverse=True)
                    close_button = buttons_with_position[0][0]
                
                if close_button:
                    # Click immediately - no delays
                    try:
                        close_button.Click()
                        return True
                    except Exception as e:
                        logger.error(f"Failed to click close button: {e}")
                        return False
                else:
                    logger.warning("Close button not found in captions window")
                    return False
                    
        except Exception as e:
            logger.error(f"Error closing captions window: {e}")
            return False
    
    def close_captions_window(self) -> None:
        """Close Windows Live Captions window by clicking the X button - optimized for speed."""
        try:
            # Clear transcript UI as soon as user initiates close
            if hasattr(self, "transcript_box"):
                self.clear_transcript()

            # Quick check - skip if already closed
            if not self._check_captions_window_open():
                self._set_status("Windows Captions is already closed.", "#C06010")
                return
            
            # Update status immediately (non-blocking)
            self._set_status("Closing...", "#106ba3")
            
            # Try to close by clicking the X button (fast operation)
            success = self._close_captions_window()
            
            if success:
                # Update UI immediately without waiting
                self.captions_open = False
                if hasattr(self, 'open_captions_btn'):
                    self.open_captions_btn.config(text="Open Captions")
                self._set_status("Closed.", "green")
                
                # Verify asynchronously (don't block)
                def verify_closed():
                    time.sleep(0.1)  # Minimal delay for verification
                    if not self._check_captions_window_open():
                        self.root.after(0, lambda: self._set_status("Closed.", "green"))
                    else:
                        self.root.after(0, lambda: self._set_status("Closing...", "#106ba3"))
                
                threading.Thread(target=verify_closed, daemon=True).start()
            else:
                self._set_status("Close button not found.", "#C06010")
                
        except Exception as e:
            logger.error(f"Failed to close captions window: {e}")
            self._set_status(f"Error: {str(e)}", "#C06010")

    def toggle_windows_captions(self) -> None:
        """Toggle Windows Live Captions - open if closed, close if open."""
        if not KEYBOARD_AVAILABLE:
            self._set_status("Keyboard library not available. Install 'keyboard' package: pip install keyboard", "#C06010")
            return
        
        try:
            # Check actual state of captions window
            is_currently_open = self._check_captions_window_open()
            
            if is_currently_open:
                # Captions are open - try to close them
                # First try the keyboard shortcut (might work as toggle)
                keyboard.press_and_release('ctrl+win+l')
                time.sleep(0.3)  # Wait a bit for window to respond
                
                # Check if it actually closed
                if not self._check_captions_window_open():
                    self.captions_open = False
                    self.open_captions_btn.config(text="Open Captions")
                    # Clear transcript when closing captions
                    if hasattr(self, "transcript_box"):
                        self.clear_transcript()
                    self._set_status("Windows Captions closed.", "#C06010")
                else:
                    # Shortcut didn't close it - try Alt+F4 on the window
                    try:
                        with uia.UIAutomationInitializerInThread():
                            root = find_live_captions_root()
                            if root:
                                # Try to close the window by sending Alt+F4 to it
                                keyboard.press_and_release('alt+f4')
                                time.sleep(0.2)
                                if not self._check_captions_window_open():
                                    self.captions_open = False
                                    self.open_captions_btn.config(text="Open Captions")
                                    # Clear transcript when closing captions
                                    if hasattr(self, "transcript_box"):
                                        self.clear_transcript()
                                    self._set_status("Windows Captions closed.", "#C06010")
                                else:
                                    self._set_status("Could not close captions. Try closing manually.", "#C06010")
                    except Exception as e2:
                        logger.error(f"Error closing captions window: {e2}")
                        self._set_status("Could not close captions. Try closing manually.", "#C06010")
            else:
                # Captions are closed - open them
                keyboard.press_and_release('ctrl+win+l')
                time.sleep(0.3)  # Wait a bit for window to appear
                
                # Check if it actually opened
                if self._check_captions_window_open():
                    self.captions_open = True
                    self.open_captions_btn.config(text="Close Captions")
                    self._set_status("Windows Captions opened.", "green")
                else:
                    self._set_status("Could not open captions. Try Ctrl+Win+L manually.", "#C06010")
                    
        except Exception as e:
            logger.error(f"Failed to toggle captions: {e}")
            self._set_status(f"Error: {str(e)}", "#C06010")

    def copy_now(self) -> None:
        """Copy captions now with robust error handling."""
        try:
            with uia.UIAutomationInitializerInThread():
                text = get_full_captions_text()
            
            if not text:
                self._set_status("Live Captions not found or empty.", "#C06010")
                return

            clean = self._clean_text(text)
            if not clean:
                self._set_status("No speech text detected.", "#C06010")
                return

            pyperclip.copy(clean)
            self.last_copied_text = clean
            self._update_preview(clean)
            self._set_status("Copied to clipboard.", "green")
            
        except Exception as e:
            logger.error(f"Copy now error: {e}")
            self._set_status(f"Error: {str(e)}", "#C06010")

    def toggle_auto(self) -> None:
        if self.is_auto_running:
            self.is_auto_running = False
            self.auto_btn.config(text="Start Auto")
            self._set_status("Auto stopped.", "#C06010")
            return

        # Start
        try:
            interval = float(self.interval_var.get().strip() or "1.0")
        except ValueError:
            self._set_status("Invalid interval.", "#C06010")
            return

        self.is_auto_running = True
        self.auto_btn.config(text="Stop Auto")
        self._set_status("Auto running…", "#106ba3")

        self.auto_thread = threading.Thread(target=self._auto_loop, args=(interval,), daemon=True)
        self.auto_thread.start()

    def _auto_loop(self, interval: float) -> None:
        """Auto loop with robust error handling."""
        try:
            with uia.UIAutomationInitializerInThread():
                while self.is_auto_running:
                    try:
                        text = get_full_captions_text()
                        if text:
                            clean = self._clean_text(text)
                            if clean and clean != self.last_copied_text:
                                pyperclip.copy(clean)
                                self.last_copied_text = clean
                                self.root.after(0, lambda c=clean: self._update_preview(c))
                                if self.accumulate_var.get():
                                    self.root.after(0, lambda c=clean: self._append_transcript(c))
                                
                                # Auto AI Answer if enabled
                                if self.auto_ai_var.get():
                                    self.root.after(0, lambda: self._auto_ai_response(clean))
                                else:
                                    self.root.after(0, lambda: self._set_status("Auto-copied.", "green"))
                        time.sleep(max(0.2, interval))
                    except Exception as e:
                        logger.warning(f"Auto loop iteration error: {e}")
                        time.sleep(1)  # Backoff on error
        except Exception as e:
            logger.error(f"Auto loop failed: {e}")
            self.root.after(0, lambda: self._set_status(f"Auto loop error: {str(e)}", "#C06010"))

    def _clean_text(self, text: str) -> str:
        # Basic cleanup only: normalize whitespace, drop duplicate spaces/newlines
        text = " ".join(text.split())
        return text.strip()

    def _detect_new_content_v3(self, current_caption: str) -> str:
        """Even simpler approach: Only capture if text is significantly different."""
        if not self.last_full_caption:
            return current_caption
        
        # Calculate similarity
        if current_caption == self.last_full_caption:
            return ""
        
        # If current is shorter, no new content
        if len(current_caption) <= len(self.last_full_caption):
            return ""
        
        # Simple approach: if the new caption is at least 20% longer, consider it new
        length_increase = len(current_caption) - len(self.last_full_caption)
        if length_increase < 5:  # Less than 5 characters, probably not new
            return ""
        
        # Find where the new content starts by looking for the last few words
        last_words = self.last_full_caption.split()
        if len(last_words) >= 2:
            # Look for the last 2 words in the current caption
            last_two = " ".join(last_words[-2:])
            if last_two in current_caption:
                pos = current_caption.find(last_two)
                if pos != -1:
                    new_part = current_caption[pos + len(last_two):].strip()
                    if len(new_part) > 2:  # Only if there's substantial new content
                        return new_part
        
        # Fallback: return the difference in word count
        current_words = current_caption.split()
        last_words = self.last_full_caption.split()
        
        if len(current_words) > len(last_words):
            new_words = current_words[len(last_words):]
            return " ".join(new_words)
        
        return ""

    def _is_new_content(self, new_text: str) -> bool:
        """Check if the new text contains genuinely new content."""
        if not new_text or not self.transcript_buffer:
            return True
        
        # Get the last few items from buffer
        recent_text = " ".join(self.transcript_buffer[-3:]) if len(self.transcript_buffer) >= 3 else " ".join(self.transcript_buffer)
        
        # If new text is just a repetition of recent content, it's not new
        if new_text in recent_text:
            return False
        
        # If new text is shorter than recent content and contained within it, it's not new
        if len(new_text) < len(recent_text) and new_text in recent_text:
            return False
        
        return True

    def _update_preview(self, text: str) -> None:
        self.preview.delete("1.0", tk.END)
        self.preview.insert(tk.END, text)
        self.preview.see(tk.END)

    def _set_status(self, msg: str, color: str) -> None:
        self.status_var.set(msg)
        self.status_lbl.config(foreground=color)

    def _append_transcript(self, text: str) -> None:
        if not self._is_new_content(text):
            return
        dedup = _extract_unseen_sentences(text, self.dedup_recent_sentences)
        if not dedup and text:
            # Fallback: show raw chunk if dedup (wrongly) blocks everything
            dedup = text
        if not dedup:
            return
        self.transcript_buffer.append(dedup)
        if len(self.transcript_buffer) > 20:
            self.transcript_buffer = self.transcript_buffer[-20:]
        joined = " ".join(self.transcript_buffer)
        joined = " ".join(joined.split())
        self.transcript_box.delete("1.0", tk.END)
        self.transcript_box.insert(tk.END, joined)
        self.transcript_box.see(tk.END)

    def clear_transcript(self) -> None:
        self.transcript_buffer.clear()
        self.transcript_box.delete("1.0", tk.END)
        # Reset all caption tracking so only upcoming text is detected
        self.last_full_caption = ""
        self.last_click_caption = ""
        self.last_copied_text = ""
        self.caption_history.clear()
        self.seen_sentences.clear()
        self.dedup_recent_sentences.clear()
        # Optional: clear preview so next capture shows only new
        self.preview.delete("1.0", tk.END)
        self._set_status("Transcript cleared. Waiting for new captions…", "#106ba3")

    def _auto_ai_response(self, text: str) -> None:
        """Automatically generate AI response for captured text."""
        # Check if we have required context
        if not self.jd_text or not self.resume_text:
            self._set_status("Auto-copied. (Complete setup first)", "#C06010")
            return
        
        # Check if text is long enough
        if len(text.strip()) < 10:
            self._set_status("Auto-copied.", "green")
            return
        
        # Generate AI response automatically
        self._set_status("🤖 Auto-generating AI answer...", "#106ba3")
        
        # Instead of the raw transcript/delta, use the most recent question
        latest_question = extract_latest_question(text)
        if not latest_question:
            latest_question = text.strip()

        context = {
            "question": latest_question or "",
            "transcript": text.strip(),
            "jd": self.jd_text,
            "resume": self.resume_text,
        }
        prompt_text = self._render_prompt("auto", context)
        if not prompt_text.strip():
            self._set_status("Auto-copied. (Prompt missing)", "#C06010")
            return

        def worker(prompt_payload: str) -> None:
            try:
                result = ask_ollama(prompt_payload, temperature=0.3)
                if result:
                    self.root.after(0, lambda: self._update_preview(result))
                    self.root.after(0, lambda: self._set_status("✅ Auto-answer ready! Transcript left on clipboard.", "green"))
                else:
                    self.root.after(0, lambda: self._set_status("Auto-copied. (AI failed)", "#C06010"))
            except Exception as e:
                logger.error(f"Auto AI response failed: {e}")
                self.root.after(0, lambda: self._set_status("Auto-copied. (AI error)", "#C06010"))
        
        threading.Thread(target=worker, args=(prompt_text,), daemon=True).start()


    def ai_rephrase(self) -> None:
        """Send the last captured text to Ollama to rephrase/clean it. Runs in a thread."""
        current_text = self.preview.get("1.0", tk.END).strip()
        if not current_text:
            self._set_status("Nothing to send to AI.", "#C06010")
            return

        self._set_status("Calling AI…", "#106ba3")
        context = {
            "question": self.question_entry.get().strip(),
            "transcript": current_text,
            "jd": self.jd_text,
            "resume": self.resume_text,
        }
        prompt_text = self._render_prompt("rephrase", context)
        if not prompt_text.strip():
            self._set_status("Prompt is empty. Configure it in 📝 Manage Prompts.", "#C06010")
            return

        def worker(prompt_payload: str) -> None:
            try:
                result = ask_ollama(prompt_payload)
                if result:
                    self.last_copied_text = result
                    pyperclip.copy(result)
                    self.root.after(0, lambda: self._update_preview(result))
                    self.root.after(0, lambda: self._set_status("AI rephrased and copied.", "green"))
                else:
                    self.root.after(0, lambda: self._set_status("AI returned no text.", "#C06010"))
            except Exception as e:
                logger.error(f"AI rephrase failed: {e}")
                self.root.after(0, lambda: self._set_status(f"AI error: {str(e)}", "#C06010"))

        threading.Thread(target=worker, args=(prompt_text,), daemon=True).start()


    # ------------------------------
    # Word document integration
    # ------------------------------

    def select_word_doc(self) -> None:
        """Let the user pick a Word document (.docx) to receive AI answers."""
        file_path = filedialog.askopenfilename(
            title="Select Word Document",
            filetypes=[("Word document", "*.docx"), ("All files", "*.*")],
        )
        if not file_path:
            return
        if not file_path.lower().endswith(".docx"):
            messagebox.showerror("Invalid File", "Please select a .docx Word document.")
            return
        self.word_doc_path = file_path
        self._set_status(f"Using Word file: {os.path.basename(file_path)}", "green")

    def append_clipboard_to_doc(self, source_label: str) -> None:
        """
        Append clipboard text to the selected Word document, tagged with source.

        Typical usage:
        - Copy answer from ChatGPT or Perplexity to clipboard.
        - Click the corresponding button to append it to the chosen .docx.
        """
        text = pyperclip.paste().strip()
        if not text:
            messagebox.showwarning("No Text", "Clipboard is empty – copy the AI answer first.")
            return

        if not self.word_doc_path:
            # Ask user to select a Word document first
            self.select_word_doc()
            if not self.word_doc_path:
                return

        try:
            from docx import Document  # type: ignore
        except ImportError:
            messagebox.showerror(
                "Missing dependency",
                "python-docx is required to write to Word files.\nInstall with: pip install python-docx",
            )
            return

        try:
            doc = Document(self.word_doc_path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open Word document:\n{e}")
            return

        try:
            # Add a simple heading-style label and the answer text
            doc.add_paragraph(f"{source_label} Answer:", style=None)
            doc.add_paragraph(text)
            doc.add_paragraph("")  # blank line between answers
            doc.save(self.word_doc_path)

            self._set_status(
                f"Appended {source_label} answer to {os.path.basename(self.word_doc_path)}",
                "green",
            )
        except Exception as e:
            messagebox.showerror("Error", f"Failed to write to Word document:\n{e}")
            self._set_status("Error writing to Word document.", "#C06010")

    def send_clipboard_to_google_doc(self, source_label: str) -> None:
        """
        Paste the current clipboard text into a Google Docs window chosen by the user.

        Workflow:
        - Copy the AI answer from ChatGPT or Perplexity (Ctrl+C in your browser).
        - Click the corresponding button in this app.
        - A dialog lists all detected Google Docs windows; pick one.
        - The app focuses that window and pastes the answer.
        """
        text = pyperclip.paste().strip()
        if not text:
            messagebox.showwarning("No Text", "Clipboard is empty – copy the AI answer first.")
            return

        if not KEYBOARD_AVAILABLE:
            messagebox.showerror(
                "Missing dependency",
                "The 'keyboard' package is required to send Ctrl+V.\nInstall with: pip install keyboard",
            )
            return
        # Prefix the answer with a label before pasting
        labeled_text = f"{source_label} Answer:\n{text}\n\n"

        # Discover all Google Docs windows by title
        try:
            with uia.UIAutomationInitializerInThread():
                root_ctrl = uia.GetRootControl()
                titles: list[str] = []
                for ctrl in root_ctrl.GetChildren():
                    try:
                        name = (ctrl.Name or "").strip()
                        if name and "google docs" in name.lower():
                            titles.append(name)
                    except Exception:
                        continue
        except Exception as e:
            messagebox.showerror("Error", f"Failed to locate Google Docs windows:\n{e}")
            return

        if not titles:
            messagebox.showwarning(
                "Google Docs Not Found",
                "No Google Docs window detected.\nOpen your Google Doc in a browser and try again.",
            )
            return

        # If only one window, use it directly
        if len(titles) == 1:
            self._focus_and_paste_google_doc(titles[0], labeled_text, source_label)
            return

        # Otherwise show a small dialog to choose which window
        dialog = tk.Toplevel(self.root)
        dialog.title("Select Google Docs Window")
        dialog.geometry("420x220")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text="Multiple Google Docs windows detected.\nSelect the one to receive the answer:",
        ).pack(pady=(10, 10))

        listbox = tk.Listbox(dialog, height=min(8, len(titles)))
        for t in titles:
            listbox.insert(tk.END, t)
        listbox.pack(fill=tk.BOTH, expand=True, padx=10)
        listbox.selection_set(0)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=10)

        def on_ok() -> None:
            try:
                idx = int(listbox.curselection()[0])
            except (IndexError, ValueError):
                dialog.destroy()
                return
            title = titles[idx]
            dialog.destroy()
            self._focus_and_paste_google_doc(title, labeled_text, source_label)

        def on_cancel() -> None:
            dialog.destroy()

        ttk.Button(btn_frame, text="OK", command=on_ok).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side=tk.LEFT, padx=5)

        dialog.focus_set()
        dialog.wait_window(dialog)

    def _focus_and_paste_google_doc(self, window_title: str, labeled_text: str, source_label: str) -> None:
        """Focus the given Google Docs window (by title) and paste the labeled text."""
        try:
            with uia.UIAutomationInitializerInThread():
                root_ctrl = uia.GetRootControl()
                target = None
                for ctrl in root_ctrl.GetChildren():
                    try:
                        name = (ctrl.Name or "").strip()
                        if name and name == window_title:
                            target = ctrl
                            break
                    except Exception:
                        continue

                if not target:
                    messagebox.showwarning(
                        "Window Not Found",
                        f"Could not refocus the selected Google Docs window:\n{window_title}",
                    )
                    return

                try:
                    target.SetFocus()
                except Exception:
                    pass
        except Exception as e:
            messagebox.showerror("Error", f"Failed to focus Google Docs window:\n{e}")
            return

        # Temporarily replace clipboard with the labeled text
        try:
            previous_clip = pyperclip.paste()
        except Exception:
            previous_clip = None

        try:
            pyperclip.copy(labeled_text)
            time.sleep(0.1)
            keyboard.press_and_release("ctrl+v")
            self._set_status(f"Sent {source_label} answer to Google Doc.", "green")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to paste into Google Doc:\n{e}")
            self._set_status("Error sending to Google Doc.", "#C06010")
        finally:
            if previous_clip is not None:
                try:
                    pyperclip.copy(previous_clip)
                except Exception:
                    pass

    def ai_answer(self) -> None:
        """Generate an answer based on current text and context. Simple one-click solution."""
        # Get current text from preview (what was just captured)
        current_text = self.preview.get("1.0", tk.END).strip()
        question = self.question_entry.get().strip()

        # Use current text as the main input, or question if provided
        main_text = question if question else current_text
        
        if not main_text:
            self._set_status("No text to answer. Copy some captions first.", "#C06010")
            return

        if not self.jd_text or not self.resume_text:
            self._set_status("Please complete setup first (click ⚙️ Setup).", "#C06010")
            return

        self._set_status("🤖 Generating AI answer...", "#106ba3")

        # Instead of the raw preview, use the most recent question
        latest_question = extract_latest_question(main_text)
        if not latest_question:
            latest_question = main_text.strip()

        context = {
            "question": latest_question or "",
            "transcript": current_text,
            "jd": self.jd_text,
            "resume": self.resume_text,
        }
        prompt_text = self._render_prompt("manual", context)
        if not prompt_text.strip():
            self._set_status("Prompt is empty. Configure it in 📝 Manage Prompts.", "#C06010")
            return

        def worker(prompt_payload: str) -> None:
            try:
                result = ask_ollama(prompt_payload, temperature=0.3)
                
                if result and len(result.strip()) > 10:
                    pyperclip.copy(result)
                    self.root.after(0, lambda: self._update_preview(result))
                    self.root.after(0, lambda: self._set_status("✅ Answer ready! Copied to clipboard.", "green"))
                else:
                    error_msg = "❌ AI failed to generate answer."
                    self.root.after(0, lambda: self._set_status(error_msg, "#C06010"))
            except Exception as e:
                logger.error(f"AI answer failed: {e}")
                error_msg = f"❌ AI error: {str(e)[:100]}"
                self.root.after(0, lambda: self._set_status(error_msg, "#C06010"))

        threading.Thread(target=worker, args=(prompt_text,), daemon=True).start()

    def toggle_transcription(self) -> None:
        """Toggle transcription on/off."""
        if self.is_transcribing:
            # Stop transcription
            self.is_transcribing = False
            self.transcribe_btn.config(text="🎤 Start Transcribing")
            self._set_status("Transcription stopped.", "#C06010")
        else:
            # Start transcription
            self.is_transcribing = True
            self.transcribe_btn.config(text="⏹️ Stop Transcribing")
            self._set_status("Transcription started...", "#106ba3")
            
            # Reset caption tracking for fresh start
            self.last_full_caption = ""
            self.caption_history.clear()
            
            # Start transcription thread
            self.transcription_thread = threading.Thread(target=self._transcription_loop, daemon=True)
            self.transcription_thread.start()

    def _transcription_loop(self) -> None:
        """Main transcription loop that prints new messages."""
        try:
            with uia.UIAutomationInitializerInThread():
                while self.is_transcribing:
                    try:
                        full_text = get_full_captions_text()
                        if full_text:
                            clean_full = self._clean_text(full_text)
                            
                            # Use the new approach
                            new_content = self._detect_new_content_v3(clean_full)
                            
                            # Sentence-level de-duplication
                            dedup = _extract_unseen_sentences(new_content, self.dedup_recent_sentences) if new_content else ""
                            if not dedup and new_content:
                                dedup = new_content
                            
                            if dedup and dedup.strip():
                                # Update the stored full caption
                                self.last_full_caption = clean_full
                                
                                # Update the preview with just the new content
                                self.root.after(0, lambda c=dedup: self._update_preview(c))
                                
                                if self.accumulate_var.get():
                                    self.root.after(0, lambda c=dedup: self._append_transcript(c))
                                
                                # Auto AI Answer if enabled
                                if self.auto_ai_var.get():
                                    self.root.after(0, lambda: self._auto_ai_response(dedup))
                                else:
                                    self.root.after(0, lambda: self._set_status(f"New: {dedup[:50]}...", "green"))
                                
                                # Copy to clipboard
                                pyperclip.copy(dedup)
                                
                        time.sleep(0.5)  # Check every 500ms
                    except Exception as e:
                        logger.warning(f"Transcription loop error: {e}")
                        time.sleep(1)  # Back off on error
        except Exception as e:
            logger.error(f"Transcription loop failed: {e}")
            self.root.after(0, lambda: self._set_status(f"Transcription error: {str(e)}", "#C06010"))

    def run(self) -> None:
        # Set up cleanup when window closes
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.root.mainloop()

    def _on_closing(self) -> None:
        """Clean up when closing the application."""
        # Stop transcription if running
        if self.is_transcribing:
            self.is_transcribing = False
        
        # Stop auto mode if running
        if self.is_auto_running:
            self.is_auto_running = False
        
        # Close the window
        self.root.destroy()

    def capture_latest(self) -> None:
        """On-demand capture: append only the newly added part since the last click."""
        try:
            with uia.UIAutomationInitializerInThread():
                full_text = get_full_captions_text()
        except Exception as e:
            logger.error(f"Capture latest failed: {e}")
            self._set_status("Capture failed.", "#C06010")
            return

        if not full_text:
            self._set_status("No captions detected.", "#C06010")
            return

        clean_full = self._clean_text(full_text)

        # Compute delta versus last click snapshot (longest common prefix)
        prev = self.last_click_caption or ""
        max_len = min(len(prev), len(clean_full))
        i = 0
        while i < max_len and prev[i] == clean_full[i]:
            i += 1
        delta = clean_full[i:].strip()

        # Fallback: if no delta, try last sentence heuristic
        if not delta:
            parts = [p.strip() for p in re.split(r"[\.!?\n]+", clean_full) if p.strip()]
            delta = parts[-1] if parts else ""

        # Sentence-level de-duplication before appending
        dedup = _extract_unseen_sentences(delta, self.dedup_recent_sentences)
        if not dedup and delta:
            dedup = delta
        if not dedup:
            return
        self.last_click_caption = clean_full
        self._update_preview(dedup)
        if self.accumulate_var.get():
            self._append_transcript(dedup)
        pyperclip.copy(dedup)
        self._set_status("Captured latest segment.", "green")


def main() -> None:
    app = CaptionGUI()
    app.run()


if __name__ == "__main__":
    main()
