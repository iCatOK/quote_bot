---
slug: autosummary-toggle
status: awaiting-approval
intent: clear
pending-action: write .omo/plans/autosummary-toggle.md
approach: Add SUMMARY_AUTO_ENABLED global toggle (default OFF) + /autosummary command (patterned after /summarycomics); auto-summaries use same 1500-msg limit as manual /summary
---

# Draft: autosummary-toggle

## Components (topology ledger)
| id | outcome | status | evidence path |
|---|---|---|---|
| summary.py:global-flag | Add SUMMARY_AUTO_ENABLED=False (default OFF) | active | summary.py:35-45 |
| summary.py:cmd-autosummary | New /autosummary command handler (toggle + status) | active | summary.py:692-726 (cmd_summary_comics as template) |
| summary.py:auto-flush-guard | Guard save_message_to_history() + save_transcribed_media() with SUMMARY_AUTO_ENABLED | active | summary.py:382-389, 437-444 |
| summary.py:format-info | Show auto-summary status in /info command | active | summary.py:267-301 |
| main.py:group-commands | Register /autosummary in GROUP_COMMANDS | active | main.py:1991-1998 |

## Open assumptions (announced defaults)
| assumption | adopted default | rationale | reversible? |
|---|---|---|---|
| Message limit for auto-summary | Use same limit as manual `/summary`: `SUMMARY_MAX_MESSAGES_PER_REQUEST` (1500). No separate constant needed. | User explicitly requested 1500, matching manual summary | Yes |
| `/autosummary` in command list | Yes, add to GROUP_COMMANDS so it appears in Telegram's `/` picker | User needs to discover the toggle | Yes |
| Auto-summary status in `/info` | Yes, add line to format_chat_summary_info() | Diagnostics command already shows summary state | Yes |
| Default state of auto-summary | `SUMMARY_AUTO_ENABLED = False` (OFF by default) | User explicitly requested off-by-default | Yes |
| Should `/autosummary` require args? | No — bare call shows current status (same pattern as /summarycomics) | Matches existing UX convention | Yes |

## Findings (cited - path:lines)
- **Current auto-trigger**: `summary.py:45` — `SUMMARY_AUTO_TRIGGER_DELTA = timedelta(days=1)`; `summary.py:382-389` and `summary.py:437-444` — flush logic in `save_message_to_history()` and `save_transcribed_media()`
- **Comics toggle pattern**: `summary.py:35` — `SUMMARY_COMICS_ENABLED = True`; `summary.py:692-726` — `cmd_summary_comics()` handler
- **Auto-summary handler**: `summary.py:638-661` — `_send_auto_summary()` generates + sends summary from accumulated buffer
- **Current message limit**: `summary.py:51` — `SUMMARY_MAX_MESSAGES_PER_REQUEST = 1500`; applied in `_generate_summary()` at `summary.py:327-328`
- **Info command**: `summary.py:267-301` — `format_chat_summary_info()` already shows comics status
- **Command registration**: `main.py:1991-1998` — `GROUP_COMMANDS` list

## Decisions (with rationale)
1. **Pattern-match /summarycomics exactly** — The user explicitly said "по аналогии включения рисования комикса". Same global-flag + simple toggling handler approach.
2. **250 limit applies only to auto-summaries** — Auto-summaries are frequent (daily) background tasks; 250 messages gives a good snapshot. Manual `/summary` stays full-buffer.
3. **No separate message limit** — Auto-summary uses the same limit as manual `/summary`: `SUMMARY_MAX_MESSAGES_PER_REQUEST = 1500`. No new constant needed. The existing `_generate_summary()` already applies this limit internally.
4. **`SUMMARY_AUTO_ENABLED` wraps the entire flush-if block** — Both `save_message_to_history()` and `save_transcribed_media()` check this flag before deciding whether to auto-flush.

## Scope IN
- Add `SUMMARY_AUTO_ENABLED = False` global variable (default OFF)
- Add `/autosummary` command handler in summary.py (patterned after `cmd_summary_comics`)
- Guard auto-flush in `save_message_to_history()` with `SUMMARY_AUTO_ENABLED`
- Guard auto-flush in `save_transcribed_media()` with `SUMMARY_AUTO_ENABLED`
- Show auto-summary status in `format_chat_summary_info()`
- Register `/autosummary` in `GROUP_COMMANDS` in main.py
- Auto-summary uses existing `SUMMARY_MAX_MESSAGES_PER_REQUEST` (1500) — no change needed

## Scope OUT (Must NOT have)
- No change to manual `/summary` command behavior
- No change to `_generate_summary()` signature or its internal `SUMMARY_MAX_MESSAGES_PER_REQUEST` limit
- No per-chat toggle (global flag only, same as comics toggle)
- No persistent storage of the toggle state (resets on restart, same as comics toggle)
- No changes to docker-compose.yml, render.yaml, flask_app.py, or any other file

## Open questions
None — all forks resolvable by adopting defaults.

## Approval gate
status: approved
