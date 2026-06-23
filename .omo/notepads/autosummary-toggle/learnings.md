## 2026-06-23 /autosummary implementation

### Changes made
- **summary.py:37** — Added `SUMMARY_AUTO_ENABLED = False` (global flag, default OFF)
- **summary.py:297,304** — Added auto-summary status line to `format_chat_summary_info()`
- **summary.py:384-389** — Added `SUMMARY_AUTO_ENABLED` guard in `save_message_to_history()` auto-flush
- **summary.py:440-444** — Added `SUMMARY_AUTO_ENABLED` guard in `save_transcribed_media()` auto-flush
- **summary.py:734-771** — Added `cmd_autosummary` handler (patterned after `cmd_summary_comics`)
- **main.py:1997** — Added `BotCommand("autosummary", ...)` to GROUP_COMMANDS

### Key decisions
- Flag is global (not per-chat), same pattern as `SUMMARY_COMICS_ENABLED`
- No persistent storage — resets on restart (same as comics toggle)
- Auto-summary uses existing `SUMMARY_MAX_MESSAGES_PER_REQUEST` (1500)
- Manual `/summary` behavior unchanged
