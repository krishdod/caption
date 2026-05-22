# AI Interview Agent (Simple Live Captions Companion)

A lightweight Windows desktop helper that reads text from Windows Live Captions and helps you answer interview questions using a local AI model (Ollama-compatible).

## Highlights
- Live capture from Windows Live Captions (Win+Ctrl+L)
- Two capture modes:
  - "🎤 Start Transcribing": continuous new-text capture (with smart de-duplication)
  - "Capture Latest": on-demand capture of only the newly added part since your last click
- Robust transcript de-duplication (sentence-level) and clear/reset behavior
- AI features:
  - 🤖 AI Answer (with your JD + Resume + latest question)
  - Auto AI Answer (optional)
  - AI Rephrase
  - AI Diagnose & AI Test
- Setup wizard for Job Description (JD) and Resume

## Requirements
- Windows 10/11
- Python 3.10+
- Packages from `requirements.txt`:
  - `uiautomation`, `pyperclip`, `ollama`
  - (optional for file reading) `PyPDF2` or `pdfplumber`, `python-docx`, `docx2txt`
- Windows Live Captions enabled (Win+Ctrl+L)

Install dependencies:
```bash
pip install -r requirements.txt
```

## Run
```bash
python caption_gui_simple.py
```
Then open Windows Live Captions (Win+Ctrl+L) so the overlay is visible.

## First-Time Setup
On launch, you’ll see a setup wizard:
1) Paste or upload your Job Description
2) Paste or upload your Resume
3) Click "🚀 Start Interview Agent"

These are used to tailor the AI answer.

## Main Controls
- Copy Now: Grab the current captions once
- Start Auto: Continuously poll and copy changes
- 🎤 Start Transcribing: Continuous capture of only new content (smart de-duplication)
- Capture Latest: On-demand capture; appends only the newly added part since your last click
- 🗑️ Clear: Clears transcript/preview and resets all internal trackers, so only truly upcoming text is considered
- 🤖 AI Answer: Generates an answer using the latest question (+ JD + Resume)
- AI Rephrase: Cleans/rephrases captured text
- AI Diagnose / AI Test: Check local AI connectivity and basic responses

## How De-duplication Works
- The app tracks the last 10 normalized sentences it has printed and avoids re-printing them
- If de-duplication is too strict, the app falls back to printing raw delta so you always see something
- Clearing the transcript resets all trackers (seen sentences, deltas, histories)

## AI Configuration (Ollama)
The app uses the official Ollama Python library.
- Default host: `http://localhost:11434`
- Default model: `gpt-oss:20b-cloud`

You can override via environment variables:
```bash
set OLLAMA_HOST=http://localhost:11434
set OLLAMA_MODEL=llama3.2:1b
python caption_gui_simple.py
```

The prompt for 🤖 AI Answer follows your strict template:
- First-person, direct, professional, conversational
- Uses JD + Resume + latest detected question
- STAR method for behavioral questions (when applicable)
- Output is only the answer text (no headings/preambles)

## Tips
- Make sure Live Captions overlay is visible; otherwise there’s nothing to capture
- Use "Capture Latest" for click-only flow; use 🎤 transcribe for continuous flow
- If you ever see repeats, click 🗑️ Clear to reset and then resume

## Troubleshooting
- "Ollama not reachable": Run `ollama serve`, pull a model: `ollama pull llama3.2:1b`
- Use "AI Diagnose" for a quick connectivity report
- If the app isn’t capturing text, verify Windows Live Captions is active and on top
- PDF/DOC/DOCX reading requires optional packages; install the ones you need

## Known Limitations
- Live Captions returns accumulated text; the app applies delta logic + sentence de-duplication to avoid repeats, but rare edge cases can still occur
- Some complex formatting in PDFs/DOCs may extract imperfectly without the appropriate reader libraries

## License
MIT (see repository license if provided)
