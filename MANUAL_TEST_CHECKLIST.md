# Windows manual-test checklist for Caption Copier (`simple_caption_tool.py`)

Platform-independent unit tests cover logic only. Items below need a real Windows desktop.

Run: `python simple_caption_tool.py`

## Core scenarios

- [ ] 1. ChatGPT desktop app — listed only if process is verified; Send pastes and Enter.
- [ ] 2. ChatGPT companion window — detectable when process verified; Send works.
- [ ] 3. Chrome with an active ChatGPT tab — listed as TabItem; Send verifies URL + composer.
- [ ] 4. Chrome with ChatGPT in an inactive tab — exact tab activated; wrong tab rejected; no paste to other apps.
- [ ] 5. Edge with multiple ChatGPT tabs — each TabItem listed (ordinal/runtime id); correct tab only.
- [ ] 6. Target closed after selection — GPT status Target lost; Send disabled.
- [ ] 7. Another app focused before Send — foreground HWND verified; on failure no Ctrl+V.
- [ ] 8. Minimized ChatGPT target — restored and verified before paste.
- [ ] 9. Empty Text to Send — Send disabled / blocked.
- [ ] 10. Rapid Start/Stop/Start Auto Capture — wait for prior worker; generation IDs ignore stale UI msgs.
- [ ] 11. Rapid double-click on Send — second click ignored while Sending.
- [ ] 12. Live Captions slow start — Opening; timeout error after ~5s if missing.
- [ ] 13. Live Captions already open — Ready immediately.
- [ ] 14. Close app while Auto Capture running — cancel + clean shutdown.

## Review blockers (must pass)

- [ ] Auto Capture updates **Live Caption** only — never overwrites **Text to Send**.
- [ ] Auto Capture never auto-sends to GPT.
- [ ] Clipboard restored only when prior plain-text capture succeeded (`clipboard_try_get`).
- [ ] Close uses verified Live Captions close control only (no Win+Ctrl+L close fallback).
- [ ] Notepad titled "ChatGPT" is **not** listed as a target.
- [ ] Wrong ChatGPT tab (`Interview` vs `Personal`) rejected on send.
- [ ] Lookalike URLs rejected; only `https://chatgpt.com/` and `https://chat.openai.com/`.
- [ ] Composer paste refuses address bar / find-in-page.
- [ ] Select GPT Target Refresh shows Searching… without freezing UI.
- [ ] Hidden (non-minimized) targets rejected.
- [ ] Chrome may need `--force-renderer-accessibility` for tab/URL UIA.
