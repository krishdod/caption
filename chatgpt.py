#!/usr/bin/env python3
"""
ChatGPT-like Interface with Windows Live Captions Integration
Automatically types caption text into the chat input field.

Production-hardened version:
- Python >= 3.10 check
- Dependency check used in main()
- Rotating file logs
- Graceful thread shutdown
- Background loading of Ollama models
- Safer logging (no prompt content dump)
- Uses configurable caption_check_interval
- Fixed assistant message replacement logic
"""

import sys
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import threading
from threading import Lock
import time
import logging
from logging.handlers import RotatingFileHandler
import re
import os
import json
import uuid
from copy import deepcopy
from string import Template

# ------------------------------
# Python version check
# ------------------------------
if sys.version_info < (3, 10):
    # Minimal GUI error, then exit
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "Python Version Error",
        "This application requires Python 3.10 or higher.\n"
        f"Detected version: {sys.version.split()[0]}"
    )
    root.destroy()
    sys.exit(1)

# ------------------------------
# Check dependencies
# ------------------------------
def check_dependencies() -> bool:
    """Check if required dependencies are installed."""
    missing = []
    optional_missing = []

    try:
        import uiautomation  # noqa: F401
    except ImportError:
        missing.append("uiautomation")

    try:
        import ollama  # noqa: F401
    except ImportError:
        missing.append("ollama")

    try:
        import keyboard  # noqa: F401
    except ImportError:
        optional_missing.append("keyboard")

    if missing:
        root = tk.Tk()
        root.withdraw()  # Hide main window
        messagebox.showerror(
            "Missing Dependencies",
            "Please install required packages:\n\n"
            f"{', '.join(missing)}\n\n"
            f"Run:\n    pip install {' '.join(missing)}"
        )
        root.destroy()
        return False

    if optional_missing:
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            "Optional Dependency Missing",
            "Optional packages are missing (some features may be limited):\n\n"
            f"{', '.join(optional_missing)}\n\n"
            "For full functionality, run:\n"
            f"    pip install {' '.join(optional_missing)}"
        )
        root.destroy()

    return True

# Import after checking (we still guard with try/except)
try:
    import uiautomation as uia
except ImportError:
    uia = None

try:
    import ollama
except ImportError:
    ollama = None

try:
    import keyboard
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False

# ------------------------------
# Logging setup
# ------------------------------
LOG_LEVEL = logging.INFO
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

# Rotating file handler for production use
try:
    log_handler = RotatingFileHandler(
        "chat_caption_app.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    log_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(log_handler)
except Exception as e:
    logger.warning(f"Could not set up rotating file handler: {e}")

# ------------------------------
# Prompt manager configuration
# ------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_FILE = os.path.join(BASE_DIR, "prompt_presets.json")
PROMPT_ACTIONS = {
    "chat": "Chat"
}

DEFAULT_PROMPTS = {
    "chat": [
        {
            "id": "chat_default",
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
    ]
}

# ------------------------------
# Configuration
# ------------------------------
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def load_config() -> dict:
    """Load configuration from file or create default."""
    default_config = {
        "ollama_host": "http://localhost:11434",
        "ollama_model": "gpt-oss:20b-cloud",
        "max_history_length": 10,
        "caption_check_interval": 0.3,
        "theme": "dark",
    }

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_config = json.load(f)
                if isinstance(user_config, dict):
                    default_config.update(user_config)
        except Exception as e:
            logger.warning(f"Failed to load config: {e}, using defaults")
    else:
        # Create default config file
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(default_config, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to create config file: {e}")

    return default_config


CONFIG = load_config()
OLLAMA_HOST = os.getenv("OLLAMA_HOST", CONFIG.get("ollama_host", "http://localhost:11434"))
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", CONFIG.get("ollama_model", "gpt-oss:20b-cloud"))

# ------------------------------
# Ollama helper
# ------------------------------
def ask_ollama(
    prompt: str,
    *,
    model: str | None = None,
    temperature: float = 0.2,
    stop_event: threading.Event | None = None,
) -> str:
    """Send a chat completion request using the official Ollama library."""
    if ollama is None:
        logger.error("Ollama library not available")
        return ""

    selected_model = model or OLLAMA_MODEL

    try:
        # Check if stopped before making request
        if stop_event and stop_event.is_set():
            logger.info("ask_ollama: stop_event set before request")
            return ""

        response = ollama.chat(
            model=selected_model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": temperature},
        )

        # Check if stopped after request
        if stop_event and stop_event.is_set():
            logger.info("ask_ollama: stop_event set after response, discarding result")
            return ""

        if response and isinstance(response, dict):
            message = response.get("message", {})
            content = message.get("content", "")
            if content:
                return content.strip()

        logger.warning("Ollama returned empty or unexpected response format")
        return ""
    except ConnectionError as e:
        logger.error(f"Ollama connection failed. Is Ollama running? {e}")
        return ""
    except Exception as e:
        logger.error(f"Ollama request failed: {e}")
        return ""


def get_available_models() -> list[str]:
    """Get list of available Ollama models."""
    if ollama is None:
        return [OLLAMA_MODEL]
    try:
        models = ollama.list()
        if models and isinstance(models, dict) and "models" in models:
            return [m.get("name", "") for m in models["models"] if m.get("name")]
        return [OLLAMA_MODEL]
    except Exception as e:
        logger.warning(f"Failed to get models list: {e}")
        return [OLLAMA_MODEL]

# ------------------------------
# Caption capture functions
# ------------------------------
def find_live_captions_root():
    """Find the handle of the Live Captions window."""
    if uia is None:
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
    except Exception as e:
        logger.warning(f"UIA root access failed: {e}")
    return None


def get_full_captions_text() -> str:
    """
    Returns the entire text visible in the Live Captions overlay.
    Uses multiple strategies for robustness.
    """
    if uia is None:
        return ""

    try:
        root = find_live_captions_root()
        if not root:
            return ""

        # Strategy 1: Look for TextBlock controls (most common)
        text_blocks = []
        for child, depth in uia.WalkControl(root, includeTop=False, maxDepth=8):
            try:
                if child.ControlTypeName == "TextControl" and child.ClassName == "TextBlock":
                    txt = (child.Name or "").strip()
                    if txt:
                        text_blocks.append(txt)
            except Exception:
                continue

        if text_blocks:
            return max(text_blocks, key=len)

        # Strategy 2: any text-like control
        for child, depth in uia.WalkControl(root, includeTop=False, maxDepth=8):
            try:
                if "Text" in child.ControlTypeName:
                    txt = (child.Name or "").strip()
                    if txt and len(txt) > 5:
                        return txt
            except Exception:
                continue

        # Strategy 3: root name
        try:
            root_text = (root.Name or "").strip()
            if root_text and len(root_text) > 5:
                return root_text
        except Exception:
            pass

    except Exception as e:
        logger.warning(f"Text extraction failed: {e}")

    return ""


def _extract_unseen_sentences(chunk: str, recent_list: list[str], n: int = 10) -> str:
    """Return only sentences from chunk not seen in last n items in recent_list."""
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


def _clean_text(text: str) -> str:
    """Clean caption text - normalize whitespace."""
    if not text:
        return ""
    text = " ".join(text.split())
    return text.strip()

# ------------------------------
# ChatGPT-like GUI
# ------------------------------
class ChatGPTInterface:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ChatGPT")
        self.root.geometry("1400x800")
        self.root.minsize(1000, 600)
        self.root.configure(bg="#212121")

        # Caption monitoring
        self.is_monitoring = False
        self.monitor_thread: threading.Thread | None = None
        self.last_full_caption = ""  # Store the last full caption to detect new content
        self.seen_sentences: list[str] = []
        self.dedup_recent_sentences: list[str] = []  # Track last n seen
        self.captions_open = False
        self.caption_interval = float(CONFIG.get("caption_check_interval", 0.3))
        if self.caption_interval < 0.1:
            self.caption_interval = 0.1

        # AI and context data
        self.jd_text = ""
        self.resume_text = ""
        self.prompt_presets, self.prompt_selection = self._load_prompt_data()
        self._ensure_prompt_defaults()

        # Conversation history for context
        self.conversation_history: list[dict[str, str]] = []
        self.max_history_length = CONFIG.get("max_history_length", 10)

        # Thread safety
        self.history_lock = Lock()
        self.monitor_lock = Lock()
        self.ai_stop_event: threading.Event | None = None
        self.current_ai_thread: threading.Thread | None = None

        # Track last assistant "Thinking..." message position (optional improvement)
        self._last_assistant_placeholder_index: str | None = None

        self._build_ui()
        self._check_initial_captions_state()

        # Handle window close for graceful shutdown
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI BUILDING ----------
    def _build_ui(self):
        """Build the ChatGPT-like interface."""
        # Main container with sidebar
        main_container = tk.Frame(self.root, bg="#212121")
        main_container.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        # Left sidebar for JD/Resume
        sidebar = tk.Frame(main_container, bg="#2d2d2d", width=300)
        sidebar.pack(side=tk.LEFT, fill=tk.BOTH, padx=(0, 8))
        sidebar.pack_propagate(False)

        sidebar_title = tk.Label(
            sidebar,
            text="Context",
            bg="#2d2d2d",
            fg="#ececf1",
            font=("Segoe UI", 12, "bold"),
        )
        sidebar_title.pack(pady=(12, 8))

        # JD section
        jd_label = tk.Label(
            sidebar,
            text="Job Description:",
            bg="#2d2d2d",
            fg="#ececf1",
            font=("Segoe UI", 10),
        )
        jd_label.pack(anchor=tk.W, padx=12, pady=(8, 4))

        self.jd_text_area = scrolledtext.ScrolledText(
            sidebar,
            wrap=tk.WORD,
            bg="#40414f",
            fg="#ececf1",
            insertbackground="#ececf1",
            font=("Segoe UI", 9),
            height=8,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#565869",
        )
        self.jd_text_area.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        self.jd_text_area.insert("1.0", "Paste your Job Description here...")
        self.jd_text_area.config(fg="#8e8ea0")

        # Resume section
        resume_label = tk.Label(
            sidebar,
            text="Resume:",
            bg="#2d2d2d",
            fg="#ececf1",
            font=("Segoe UI", 10),
        )
        resume_label.pack(anchor=tk.W, padx=12, pady=(8, 4))

        self.resume_text_area = scrolledtext.ScrolledText(
            sidebar,
            wrap=tk.WORD,
            bg="#40414f",
            fg="#ececf1",
            insertbackground="#ececf1",
            font=("Segoe UI", 9),
            height=8,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#565869",
        )
        self.resume_text_area.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        self.resume_text_area.insert("1.0", "Paste your Resume here...")
        self.resume_text_area.config(fg="#8e8ea0")

        # Model selection
        model_label = tk.Label(
            sidebar,
            text="AI Model:",
            bg="#2d2d2d",
            fg="#ececf1",
            font=("Segoe UI", 10),
        )
        model_label.pack(anchor=tk.W, padx=12, pady=(8, 4))

        # Start with default, populate async
        self.model_var = tk.StringVar(value=OLLAMA_MODEL)
        self.model_combo = ttk.Combobox(
            sidebar,
            textvariable=self.model_var,
            values=[OLLAMA_MODEL],
            state="readonly",
        )
        self.model_combo.pack(fill=tk.X, padx=12, pady=(0, 8))

        # Load actual models in background
        threading.Thread(target=self._populate_models, daemon=True).start()

        # Prompt selection
        prompt_label = tk.Label(
            sidebar,
            text="Prompt Template:",
            bg="#2d2d2d",
            fg="#ececf1",
            font=("Segoe UI", 10),
        )
        prompt_label.pack(anchor=tk.W, padx=12, pady=(8, 4))

        prompt_names = [p["name"] for p in self.prompt_presets.get("chat", [])]
        current_name = self._get_prompt_name_by_id("chat", self.prompt_selection.get("chat"))
        self.prompt_var = tk.StringVar(value=current_name)
        self.prompt_combo = ttk.Combobox(
            sidebar,
            textvariable=self.prompt_var,
            values=prompt_names,
            state="readonly",
        )
        self.prompt_combo.pack(fill=tk.X, padx=12, pady=(0, 8))
        self.prompt_combo.bind("<<ComboboxSelected>>", lambda e: self._handle_prompt_selection())

        manage_btn = tk.Button(
            sidebar,
            text="📝 Manage Prompts",
            command=self._open_prompt_manager,
            bg="#40414f",
            fg="white",
            activebackground="#565869",
            activeforeground="white",
            borderwidth=0,
            padx=16,
            pady=8,
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        manage_btn.pack(padx=12, pady=(0, 8))

        clear_history_btn = tk.Button(
            sidebar,
            text="🗑️ Clear History",
            command=self._clear_conversation_history,
            bg="#40414f",
            fg="white",
            activebackground="#565869",
            activeforeground="white",
            borderwidth=0,
            padx=16,
            pady=8,
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        clear_history_btn.pack(padx=12, pady=(0, 8))

        export_btn = tk.Button(
            sidebar,
            text="💾 Export Chat",
            command=self._export_conversation,
            bg="#40414f",
            fg="white",
            activebackground="#565869",
            activeforeground="white",
            borderwidth=0,
            padx=16,
            pady=8,
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        export_btn.pack(padx=12, pady=(0, 12))

        # Main content area
        main_frame = tk.Frame(main_container, bg="#212121")
        main_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=0, pady=0)

        # Top bar with caption controls
        top_bar = tk.Frame(main_frame, bg="#212121", height=50)
        top_bar.pack(fill=tk.X, padx=12, pady=(12, 8))
        top_bar.pack_propagate(False)

        self.btn_frame = tk.Frame(top_bar, bg="#212121")
        self.btn_frame.pack(side=tk.RIGHT)

        self.open_captions_btn = tk.Button(
            self.btn_frame,
            text="Open Captions",
            command=self.open_captions,
            bg="#40414f",
            fg="white",
            activebackground="#565869",
            activeforeground="white",
            borderwidth=0,
            padx=16,
            pady=8,
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        self.open_captions_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.close_captions_btn = tk.Button(
            self.btn_frame,
            text="Close Captions",
            command=self.close_captions,
            bg="#40414f",
            fg="white",
            activebackground="#565869",
            activeforeground="white",
            borderwidth=0,
            padx=16,
            pady=8,
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        self.close_captions_btn.pack(side=tk.LEFT, padx=(8, 0))

        # Status indicator
        self.status_label = tk.Label(
            top_bar,
            text="Ready",
            bg="#212121",
            fg="#8e8ea0",
            font=("Segoe UI", 9),
        )
        self.status_label.pack(side=tk.LEFT)

        # Chat area
        chat_container = tk.Frame(main_frame, bg="#212121")
        chat_container.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        self.chat_display = scrolledtext.ScrolledText(
            chat_container,
            wrap=tk.WORD,
            bg="#212121",
            fg="#ececf1",
            insertbackground="#ececf1",
            font=("Segoe UI", 11),
            borderwidth=0,
            highlightthickness=0,
            padx=16,
            pady=16,
            state=tk.DISABLED,
        )
        self.chat_display.pack(fill=tk.BOTH, expand=True)

        self.chat_display.tag_config("user", foreground="#ececf1", font=("Segoe UI", 11))
        self.chat_display.tag_config("assistant", foreground="#ececf1", font=("Segoe UI", 11))

        # Input area
        input_frame = tk.Frame(main_frame, bg="#212121")
        input_frame.pack(fill=tk.X, padx=12, pady=(0, 12))

        input_container = tk.Frame(input_frame, bg="#40414f", relief=tk.FLAT, bd=0)
        input_container.pack(fill=tk.X, ipady=4)

        self.input_field = tk.Text(
            input_container,
            wrap=tk.WORD,
            bg="#40414f",
            fg="#ececf1",
            insertbackground="#ececf1",
            font=("Segoe UI", 11),
            borderwidth=0,
            highlightthickness=0,
            padx=16,
            pady=12,
            height=3,
        )
        self.input_field.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
        self.input_field.bind("<Return>", self._on_enter_key)
        self.input_field.bind("<Shift-Return>", lambda e: None)

        self.input_field.insert("1.0", "Ask anything")
        self.input_field.config(fg="#8e8ea0")
        self.input_field.bind("<FocusIn>", self._on_input_focus_in)
        self.input_field.bind("<FocusOut>", self._on_input_focus_out)

    # ---------- Model list population ----------
    def _populate_models(self):
        """Load available models from Ollama in background."""
        models = get_available_models()
        if not models:
            models = [OLLAMA_MODEL]

        def update_combo():
            self.model_combo.configure(values=models)
            if self.model_var.get() not in models:
                self.model_var.set(models[0])

        try:
            self.root.after(0, update_combo)
        except Exception as e:
            logger.warning(f"Failed to update model combo: {e}")

    # ---------- Input handlers ----------
    def _on_input_focus_in(self, event):
        if self.input_field.get("1.0", tk.END).strip() == "Ask anything":
            self.input_field.delete("1.0", tk.END)
            self.input_field.config(fg="#ececf1")

    def _on_input_focus_out(self, event):
        if not self.input_field.get("1.0", tk.END).strip():
            self.input_field.insert("1.0", "Ask anything")
            self.input_field.config(fg="#8e8ea0")

    def _on_enter_key(self, event):
        if event.state & 0x1:  # Shift is pressed
            return
        self.send_message()
        return "break"

    # ---------- Message sending & AI ----------
    def send_message(self):
        """Send the current message to AI."""
        message = self.input_field.get("1.0", tk.END).strip()
        if not message or message == "Ask anything":
            return

        # JD & Resume
        jd_content = self.jd_text_area.get("1.0", tk.END).strip()
        self.jd_text = jd_content if jd_content and jd_content != "Paste your Job Description here..." else ""

        resume_content = self.resume_text_area.get("1.0", tk.END).strip()
        self.resume_text = resume_content if resume_content and resume_content != "Paste your Resume here..." else ""

        self._add_message("user", message)

        self.input_field.delete("1.0", tk.END)
        self.input_field.config(fg="#8e8ea0")
        self.input_field.insert("1.0", "Ask anything")

        # Show "Thinking..."
        self._add_message("assistant", "Thinking...")

        self.ai_stop_event = threading.Event()
        self.current_ai_thread = threading.Thread(
            target=self._generate_ai_response,
            args=(message,),
            daemon=True,
        )
        self.current_ai_thread.start()

        self._show_stop_button()
        self.root.update_idletasks()

    def _generate_ai_response(self, question: str):
        """Generate AI response using Ollama with conversation history."""
        try:
            jd_content = self.jd_text_area.get("1.0", tk.END).strip()
            self.jd_text = jd_content if jd_content and jd_content != "Paste your Job Description here..." else ""

            resume_content = self.resume_text_area.get("1.0", tk.END).strip()
            self.resume_text = resume_content if resume_content and resume_content != "Paste your Resume here..." else ""

            # Add user message to history
            with self.history_lock:
                self.conversation_history.append({"role": "user", "content": question})
                if len(self.conversation_history) > self.max_history_length * 2:
                    self.conversation_history = self.conversation_history[-(self.max_history_length * 2):]
                history_count = len(self.conversation_history) - 1

            context = {
                "question": question,
                "transcript": question,
                "jd": self.jd_text or "Not provided",
                "resume": self.resume_text or "Not provided",
            }

            # Build history section
            history_section = ""
            with self.history_lock:
                if len(self.conversation_history) > 1:
                    history_section = "\n\n**Previous Conversation History (for context):**\n"
                    history_messages = self.conversation_history[:-1]
                    if len(history_messages) > 10:
                        history_messages = history_messages[-10:]

                    for i in range(0, len(history_messages), 2):
                        user_msg = history_messages[i]
                        history_section += f"\nUser: {user_msg['content']}\n"
                        if i + 1 < len(history_messages):
                            assistant_msg = history_messages[i + 1]
                            assistant_content = assistant_msg["content"]
                            if len(assistant_content) > 200:
                                assistant_content = assistant_content[:200] + "..."
                            history_section += f"Assistant: {assistant_content}\n"

                    history_section += "\n**End of Previous Conversation**\n\n"

            context_section = ""
            if self.jd_text and self.jd_text != "Not provided":
                context_section += f"Job Description:\n{self.jd_text}\n\n"
            if self.resume_text and self.resume_text != "Not provided":
                context_section += f"Resume:\n{self.resume_text}\n\n"

            prompt_template = self._render_prompt("chat", context)

            if not prompt_template.strip():
                if context_section:
                    prompt_text = f"{context_section}{history_section}Current Question: {question}"
                else:
                    prompt_text = f"{history_section}Current Question: {question}"
            else:
                has_placeholders = any(x in prompt_template for x in ("$jd", "$resume", "$question", "$transcript"))
                if has_placeholders:
                    prompt_text = f"{prompt_template}{history_section}"
                else:
                    if context_section:
                        prompt_text = f"{context_section}{prompt_template}\n\n{history_section}Current Question: {question}"
                    else:
                        prompt_text = f"{prompt_template}\n\n{history_section}Current Question: {question}"

            selected_model = self.model_var.get() if hasattr(self, "model_var") else OLLAMA_MODEL

            logger.info(
                "Sending prompt to Ollama (model=%s, length=%d chars, history_messages=%d)",
                selected_model,
                len(prompt_text),
                history_count,
            )

            if self.ai_stop_event and self.ai_stop_event.is_set():
                return

            result = ask_ollama(
                prompt_text,
                model=selected_model,
                temperature=0.3,
                stop_event=self.ai_stop_event,
            )

            if self.ai_stop_event and self.ai_stop_event.is_set():
                msg = "Response stopped by user."
                with self.history_lock:
                    self.conversation_history.append({"role": "assistant", "content": msg})
                self.root.after(0, lambda: self._replace_last_assistant_message(msg))
                return

            if result:
                with self.history_lock:
                    self.conversation_history.append({"role": "assistant", "content": result})
                self.root.after(0, lambda: self._replace_last_assistant_message(result))
            else:
                error_msg = "Sorry, I couldn't generate a response. Please check your Ollama connection."
                with self.history_lock:
                    self.conversation_history.append({"role": "assistant", "content": error_msg})
                self.root.after(0, lambda: self._replace_last_assistant_message(error_msg))

        except Exception as e:
            logger.error(f"AI response generation failed: {e}")
            error_msg = f"Error: {str(e)}"
            with self.history_lock:
                self.conversation_history.append({"role": "assistant", "content": error_msg})
            self.root.after(0, lambda: self._replace_last_assistant_message(error_msg))
        finally:
            self.root.after(0, self._hide_stop_button)

    # ---------- Chat display helpers ----------
    def _replace_last_assistant_message(self, new_content: str):
        """Replace only the last assistant message (used to remove 'Thinking...')."""
        self.chat_display.config(state=tk.NORMAL)
        content = self.chat_display.get("1.0", tk.END)
        lines = content.split("\n")

        # Find last index where line starts with "Assistant: "
        last_idx = -1
        for i, line in enumerate(lines):
            if line.startswith("Assistant: "):
                last_idx = i

        if last_idx == -1:
            # No assistant lines found, just append
            self.chat_display.insert(tk.END, f"Assistant: {new_content}\n\n", "assistant")
        else:
            lines[last_idx] = f"Assistant: {new_content}"
            new_text = "\n".join(lines).rstrip() + "\n\n"
            self.chat_display.delete("1.0", tk.END)
            self.chat_display.insert("1.0", new_text, "assistant")

        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

    def _add_message(self, role: str, content: str):
        """Add a message to the chat display."""
        self.chat_display.config(state=tk.NORMAL)

        prefix = "You: " if role == "user" else "Assistant: "
        tag = role

        self.chat_display.insert(tk.END, prefix + content + "\n\n", tag)
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

    # ---------- Captions / monitoring ----------
    def _check_initial_captions_state(self):
        """Check if captions are already open."""
        if uia is None:
            self._update_status("UIAutomation not available (captions disabled)", "#ef4444")
            logger.warning("UIAutomation not available, captions will not work.")
            return

        try:
            with uia.UIAutomationInitializerInThread():
                root = find_live_captions_root()
                self.captions_open = root is not None
                if self.captions_open:
                    self._update_status("Captions: Already open - Starting monitor...", "#10a37f")
                    logger.info("Captions already open, starting monitoring")
                    self._start_monitoring()
                else:
                    self._update_status("Ready - Click 'Open Captions' to start", "#8e8ea0")
                    logger.info("Captions not open, waiting for user action")
        except Exception as e:
            logger.error(f"Error checking initial captions state: {e}")
            self._update_status("Ready", "#8e8ea0")

    def open_captions(self):
        """Open Windows Live Captions."""
        if not KEYBOARD_AVAILABLE:
            self._update_status("Keyboard library not available", "#ef4444")
            return
        if uia is None:
            self._update_status("UIAutomation not available", "#ef4444")
            return

        try:
            if self.captions_open:
                self._update_status("Captions already open", "#8e8ea0")
                return

            keyboard.press_and_release("ctrl+win+l")
            time.sleep(0.5)

            with uia.UIAutomationInitializerInThread():
                root = find_live_captions_root()
                if root:
                    self.captions_open = True
                    self._update_status("Captions: Open - Starting monitor...", "#10a37f")
                    self._start_monitoring()
                else:
                    self._update_status("Failed to open captions - window not found", "#ef4444")
                    logger.warning("Captions window not found after opening attempt")
        except Exception as e:
            logger.error(f"Failed to open captions: {e}")
            self._update_status(f"Error: {str(e)}", "#ef4444")

    def close_captions(self):
        """Close Windows Live Captions."""
        if not KEYBOARD_AVAILABLE:
            self._update_status("Keyboard library not available", "#ef4444")
            return
        if uia is None:
            self._update_status("UIAutomation not available", "#ef4444")
            return

        try:
            if not self.captions_open:
                self._update_status("Captions already closed", "#8e8ea0")
                return

            keyboard.press_and_release("ctrl+win+l")
            time.sleep(0.3)

            with uia.UIAutomationInitializerInThread():
                root = find_live_captions_root()
                if not root:
                    self.captions_open = False
                    self._stop_monitoring()
                    self._update_status("Captions: Closed", "#8e8ea0")
                    return

            # Fallback: Alt+F4
            with uia.UIAutomationInitializerInThread():
                root = find_live_captions_root()
                if root:
                    keyboard.press_and_release("alt+f4")
                    time.sleep(0.2)
                    if not find_live_captions_root():
                        self.captions_open = False
                        self._stop_monitoring()
                        self._update_status("Captions: Closed", "#8e8ea0")
                    else:
                        self._update_status("Could not close captions", "#ef4444")
        except Exception as e:
            logger.error(f"Failed to close captions: {e}")
            self._update_status(f"Error: {str(e)}", "#ef4444")

    def _start_monitoring(self):
        """Start monitoring captions and typing into input field."""
        if self.is_monitoring or uia is None:
            return

        self.is_monitoring = True
        self.last_full_caption = ""
        self.dedup_recent_sentences.clear()
        self.monitor_thread = threading.Thread(target=self._monitor_captions, daemon=True)
        self.monitor_thread.start()
        self._update_status("Monitoring captions...", "#10a37f")

    def _stop_monitoring(self):
        """Stop monitoring captions."""
        self.is_monitoring = False

    def _monitor_captions(self):
        """Monitor captions and automatically type into input field."""
        if uia is None:
            return

        consecutive_errors = 0
        max_errors = 5

        try:
            with uia.UIAutomationInitializerInThread():
                while self.is_monitoring:
                    try:
                        if not find_live_captions_root():
                            if self.captions_open:
                                self.root.after(
                                    0,
                                    lambda: self._update_status("Captions window closed", "#ef4444"),
                                )
                                self.captions_open = False
                                self._stop_monitoring()
                                break
                            time.sleep(1.0)
                            continue

                        full_text = get_full_captions_text()
                        if full_text:
                            clean_full = _clean_text(full_text)
                            if not clean_full:
                                time.sleep(self.caption_interval)
                                continue

                            new_content = self._detect_new_content_v3(clean_full)

                            if new_content:
                                dedup = _extract_unseen_sentences(new_content, self.dedup_recent_sentences)
                                if not dedup and len(new_content.strip()) >= 3:
                                    dedup = new_content
                            else:
                                dedup = ""

                            if dedup and dedup.strip():
                                self.last_full_caption = clean_full
                                consecutive_errors = 0

                                logger.info("New caption text detected: %s...", dedup[:50])
                                self.root.after(0, lambda text=dedup: self._append_to_input(text))
                                self.root.after(
                                    0,
                                    lambda: self._update_status(
                                        f"Captions: Active - {dedup[:30]}...", "#10a37f"
                                    ),
                                )

                        consecutive_errors = 0
                        time.sleep(self.caption_interval)

                    except Exception as e:
                        consecutive_errors += 1
                        logger.warning(
                            "Caption monitoring error (%d/%d): %s",
                            consecutive_errors,
                            max_errors,
                            e,
                        )

                        if consecutive_errors >= max_errors:
                            logger.error("Too many consecutive errors, stopping monitoring")
                            self.root.after(
                                0,
                                lambda: self._update_status(
                                    "Monitoring stopped - too many errors", "#ef4444"
                                ),
                            )
                            self._stop_monitoring()
                            break

                        time.sleep(0.5)

        except Exception as e:
            logger.error(f"Caption monitoring failed: {e}")
            self.root.after(
                0,
                lambda: self._update_status(f"Monitoring error: {str(e)[:50]}", "#ef4444"),
            )

    def _detect_new_content_v3(self, current_caption: str) -> str:
        """Duplicate the detection logic from caption_gui_simple.py."""
        if not self.last_full_caption:
            return current_caption

        # If nothing changed, there's nothing to do
        if current_caption == self.last_full_caption:
            return ""

        # If the new caption is shorter or equal, assume no new content
        if len(current_caption) <= len(self.last_full_caption):
            return ""

        # Only treat the text as new if it grew by at least a few characters
        length_increase = len(current_caption) - len(self.last_full_caption)
        if length_increase < 5:
            return ""

        # Try to find the last two words of the previous caption as an anchor
        last_words = self.last_full_caption.split()
        if len(last_words) >= 2:
            last_two = " ".join(last_words[-2:])
            if last_two in current_caption:
                pos = current_caption.find(last_two)
                if pos != -1:
                    new_part = current_caption[pos + len(last_two):].strip()
                    if len(new_part) > 2:
                        return new_part

        # Fallback: difference in word count
        current_words = current_caption.split()
        last_words = self.last_full_caption.split()
        if len(current_words) > len(last_words):
            new_words = current_words[len(last_words):]
            return " ".join(new_words)

        return ""

    def _append_to_input(self, text: str):
        """Append text to the input field."""
        try:
            current = self.input_field.get("1.0", tk.END).strip()

            if current == "Ask anything":
                self.input_field.delete("1.0", tk.END)
                self.input_field.config(fg="#ececf1")
                current = ""

            if current and not current.endswith((" ", ".", "!", "?")):
                self.input_field.insert(tk.END, " ")
            else:
                self.input_field.config(fg="#ececf1")

            self.input_field.insert(tk.END, text)
            self.input_field.see(tk.END)
        except Exception as e:
            logger.error(f"Failed to append to input: {e}")

    # ---------- Status ----------
    def _update_status(self, message: str, color: str = "#8e8ea0"):
        self.status_label.config(text=message, fg=color)

    # ---------- Prompt presets ----------
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
        """Save prompt presets and selection to disk."""
        data = {"presets": self.prompt_presets, "selected": self.prompt_selection}
        try:
            with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f"Failed to save prompt presets: {e}")

    def _ensure_prompt_defaults(self) -> None:
        """Ensure default prompts exist."""
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
        """Get prompt by ID."""
        if not prompt_id:
            return None
        for prompt in self.prompt_presets.get(action_key, []):
            if prompt.get("id") == prompt_id:
                return prompt
        return None

    def _get_prompt_name_by_id(self, action_key: str, prompt_id: str | None) -> str:
        """Get prompt name by ID."""
        prompt = self._get_prompt_by_id(action_key, prompt_id)
        if prompt:
            return prompt.get("name", "")
        prompts = self.prompt_presets.get(action_key, [])
        return prompts[0]["name"] if prompts else ""

    def _handle_prompt_selection(self):
        """Handle prompt selection change."""
        name = self.prompt_var.get()
        for prompt in self.prompt_presets.get("chat", []):
            if prompt.get("name") == name:
                self.prompt_selection["chat"] = prompt["id"]
                self._save_prompt_data()
                break

    def _render_prompt(self, action_key: str, context: dict[str, str]) -> str:
        """Render prompt template with context."""
        prompt = self._get_prompt_by_id(action_key, self.prompt_selection.get(action_key))
        if not prompt:
            logger.warning(f"No prompt found for action: {action_key}")
            return ""
        template_str = prompt.get("template", "")
        if not template_str:
            logger.warning("Prompt template is empty")
            return ""

        try:
            rendered = Template(template_str).safe_substitute(context)
            return rendered
        except Exception as e:
            logger.error(f"Prompt rendering failed: {e}")
            return template_str

    # ---------- Prompt manager UI ----------
    def _open_prompt_manager(self):
        """Open prompt manager window."""
        win = tk.Toplevel(self.root)
        win.title("Prompt Manager")
        win.geometry("700x500")
        win.configure(bg="#212121")

        list_frame = tk.Frame(win, bg="#212121")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        tk.Label(
            list_frame,
            text="Prompts",
            bg="#212121",
            fg="#ececf1",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor=tk.W)

        prompt_listbox = tk.Listbox(
            list_frame,
            bg="#40414f",
            fg="#ececf1",
            font=("Segoe UI", 10),
            selectbackground="#565869",
        )
        prompt_listbox.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        for prompt in self.prompt_presets.get("chat", []):
            prompt_listbox.insert(tk.END, prompt["name"])

        btn_frame = tk.Frame(win, bg="#212121")
        btn_frame.pack(fill=tk.X, padx=12, pady=(0, 12))

        def add_prompt():
            self._edit_prompt_dialog(win, mode="add")
            prompt_listbox.delete(0, tk.END)
            for prompt in self.prompt_presets.get("chat", []):
                prompt_listbox.insert(tk.END, prompt["name"])

        def edit_prompt():
            selection = prompt_listbox.curselection()
            if not selection:
                messagebox.showinfo("Prompt Manager", "Select a prompt to edit.")
                return
            name = prompt_listbox.get(selection[0])
            prompt = self._get_prompt_by_name("chat", name)
            if prompt:
                self._edit_prompt_dialog(win, mode="edit", prompt=prompt)
                prompt_listbox.delete(0, tk.END)
                for prompt in self.prompt_presets.get("chat", []):
                    prompt_listbox.insert(tk.END, prompt["name"])

        tk.Button(
            btn_frame,
            text="Add",
            command=add_prompt,
            bg="#40414f",
            fg="white",
            padx=16,
            pady=8,
        ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(
            btn_frame,
            text="Edit",
            command=edit_prompt,
            bg="#40414f",
            fg="white",
            padx=16,
            pady=8,
        ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(
            btn_frame,
            text="Close",
            command=win.destroy,
            bg="#40414f",
            fg="white",
            padx=16,
            pady=8,
        ).pack(side=tk.RIGHT)

    def _get_prompt_by_name(self, action_key: str, name: str) -> dict | None:
        for prompt in self.prompt_presets.get(action_key, []):
            if prompt.get("name") == name:
                return prompt
        return None

    def _edit_prompt_dialog(self, parent, mode: str, prompt: dict | None = None):
        """Edit or add prompt dialog."""
        dialog = tk.Toplevel(parent)
        dialog.title("Edit Prompt" if mode == "edit" else "Add Prompt")
        dialog.geometry("600x500")
        dialog.configure(bg="#212121")

        tk.Label(
            dialog,
            text="Prompt Name:",
            bg="#212121",
            fg="#ececf1",
            font=("Segoe UI", 10),
        ).pack(anchor=tk.W, padx=12, pady=(12, 4))
        name_var = tk.StringVar(value=prompt["name"] if prompt else "")
        name_entry = tk.Entry(
            dialog,
            textvariable=name_var,
            bg="#40414f",
            fg="#ececf1",
            font=("Segoe UI", 10),
            insertbackground="#ececf1",
        )
        name_entry.pack(fill=tk.X, padx=12, pady=(0, 12))

        tk.Label(
            dialog,
            text="Prompt Template (use $question, $jd, $resume, $transcript):",
            bg="#212121",
            fg="#ececf1",
            font=("Segoe UI", 10),
        ).pack(anchor=tk.W, padx=12)
        text_area = scrolledtext.ScrolledText(
            dialog,
            wrap=tk.WORD,
            bg="#40414f",
            fg="#ececf1",
            font=("Segoe UI", 10),
            insertbackground="#ececf1",
            height=15,
        )
        text_area.pack(fill=tk.BOTH, expand=True, padx=12, pady=(8, 12))
        if prompt:
            text_area.insert("1.0", prompt.get("template", ""))

        def save():
            name = name_var.get().strip()
            template = text_area.get("1.0", tk.END).strip()
            if not name or not template:
                messagebox.showerror("Error", "Name and template cannot be empty.")
                return

            if mode == "edit" and prompt:
                prompt["name"] = name
                prompt["template"] = template
            else:
                new_prompt = {
                    "id": uuid.uuid4().hex,
                    "name": name,
                    "template": template,
                }
                self.prompt_presets.setdefault("chat", []).append(new_prompt)
                self.prompt_selection["chat"] = new_prompt["id"]

            self._save_prompt_data()
            prompt_names = [p["name"] for p in self.prompt_presets.get("chat", [])]
            self.prompt_combo["values"] = prompt_names
            self.prompt_var.set(name)
            dialog.destroy()

        btn_frame = tk.Frame(dialog, bg="#212121")
        btn_frame.pack(fill=tk.X, padx=12, pady=(0, 12))
        tk.Button(
            btn_frame,
            text="Save",
            command=save,
            bg="#40414f",
            fg="white",
            padx=16,
            pady=8,
        ).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(
            btn_frame,
            text="Cancel",
            command=dialog.destroy,
            bg="#40414f",
            fg="white",
            padx=16,
            pady=8,
        ).pack(side=tk.RIGHT)

    # ---------- History & export ----------
    def _clear_conversation_history(self):
        with self.history_lock:
            self.conversation_history.clear()
        self._update_status("Conversation history cleared", "#10a37f")
        logger.info("Conversation history cleared by user")

    def _export_conversation(self):
        if not self.conversation_history:
            messagebox.showinfo("Export", "No conversation to export.")
            return

        filename = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[
                ("Text files", "*.txt"),
                ("JSON files", "*.json"),
                ("All files", "*.*"),
            ],
        )

        if not filename:
            return

        try:
            with self.history_lock:
                history_copy = list(self.conversation_history)

            if filename.endswith(".json"):
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(history_copy, f, indent=2, ensure_ascii=False)
            else:
                with open(filename, "w", encoding="utf-8") as f:
                    f.write("Conversation Export\n")
                    f.write("=" * 50 + "\n\n")
                    for msg in history_copy:
                        role = msg["role"].title()
                        content = msg["content"]
                        f.write(f"{role}: {content}\n\n")

            self._update_status(
                f"Conversation exported to {os.path.basename(filename)}", "#10a37f"
            )
            messagebox.showinfo(
                "Export",
                f"Conversation exported successfully to:\n{filename}",
            )
        except Exception as e:
            logger.error(f"Export failed: {e}")
            messagebox.showerror("Export Error", f"Failed to export conversation:\n{str(e)}")

    # ---------- Stop button ----------
    def _show_stop_button(self):
        if hasattr(self, "stop_ai_btn"):
            self.stop_ai_btn.pack(side=tk.LEFT, padx=(8, 0))
        else:
            if hasattr(self, "btn_frame"):
                self.stop_ai_btn = tk.Button(
                    self.btn_frame,
                    text="⏹️ Stop",
                    command=self._stop_ai_response,
                    bg="#ef4444",
                    fg="white",
                    activebackground="#dc2626",
                    activeforeground="white",
                    borderwidth=0,
                    padx=16,
                    pady=8,
                    font=("Segoe UI", 10),
                    cursor="hand2",
                )
                self.stop_ai_btn.pack(side=tk.LEFT, padx=(8, 0))

    def _hide_stop_button(self):
        if hasattr(self, "stop_ai_btn"):
            self.stop_ai_btn.pack_forget()

    def _stop_ai_response(self):
        if self.ai_stop_event:
            self.ai_stop_event.set()
            self._update_status("Stopping AI response...", "#ef4444")
            logger.info("User requested to stop AI response")

    # ---------- Lifecycle ----------
    def _on_close(self):
        """Graceful shutdown: stop threads and close app."""
        logger.info("Application closing requested.")

        # Stop caption monitoring
        self.is_monitoring = False

        # Stop AI thread
        if self.ai_stop_event:
            self.ai_stop_event.set()

        # Join threads safely
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=2)

        if self.current_ai_thread and self.current_ai_thread.is_alive():
            self.current_ai_thread.join(timeout=5)

        self.root.destroy()

    def run(self):
        """Start the application."""
        self.root.mainloop()

# ------------------------------
# Main entry
# ------------------------------
def main():
    if not check_dependencies():
        return
    app = ChatGPTInterface()
    app.run()


if __name__ == "__main__":
    main()
