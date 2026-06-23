# autosummary-toggle - Work Plan

## TL;DR (For humans)

**What you'll get:** Возможность включать/выключать авто-саммари через команду `/autosummary` в чате. После включения саммари будут генерироваться автоматически раз в день, как сейчас. После выключения — только по ручному `/summary`. По умолчанию авто-саммари выключено.

**Why this approach:** Минимальные изменения — новый флаг + команда-переключатель по тому же шаблону, что `/summarycomics`. Никаких новых констант, лимит сообщений (1500) не меняется.

**What it will NOT do:** Не меняет поведение ручного `/summary`. Не сохраняет состояние toggle между рестартами (как и `SUMMARY_COMICS_ENABLED`). Не добавляет per-chat настройки.

**Effort:** Quick
**Risk:** Low — изменение изолировано в двух файлах, паттерн уже есть (/summarycomics)
**Decisions to sanity-check:** Флаг OFF по умолчанию, лимит 1500 совпадает с ручным саммари

---

## Scope
### Must have
- Global flag `SUMMARY_AUTO_ENABLED = False` (default OFF)
- `/autosummary` command — toggle ON/OFF, show status on bare call
- Guard auto-flush in `save_message_to_history()` with the flag
- Guard auto-flush in `save_transcribed_media()` with the flag
- Show auto-summary status in `format_chat_summary_info()` (/info command)
- Register `/autosummary` in `GROUP_COMMANDS` in main.py
- Auto-summary uses existing `SUMMARY_MAX_MESSAGES_PER_REQUEST` (1500) — no new limit

### Must NOT have (guardrails)
- No change to manual `/summary` behavior
- No change to `_generate_summary()` or its signature
- No per-chat toggle
- No persistent storage of toggle state
- No new message limit constants

## Verification strategy
- Test decision: tests-after (manual verification via lsp_diagnostics + code review)
- Evidence: lsp_diagnostics clean, code review confirms all 5 changes

## Execution strategy
### Parallel execution waves
Wave 1: summary.py changes + main.py command registration (independent files → parallel)

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
|---|---|---|---|
| T1. summary.py: all changes | — | — | T2 |
| T2. main.py: register command | — | — | T1 |

## Todos
- [x] 1. summary.py: Add SUMMARY_AUTO_ENABLED flag + /autosummary handler + guard flush + update /info
  What to do / Must NOT do:
   1. Add `SUMMARY_AUTO_ENABLED = False` near line 35
   2. Add `/autosummary` command handler (copy pattern of `cmd_summary_comics` at line 692, rephrase for auto-summary)
   3. In `save_message_to_history()` (line 382): wrap the auto-flush `if` block with `SUMMARY_AUTO_ENABLED and`
   4. In `save_transcribed_media()` (line 437): same guard
   5. In `format_chat_summary_info()` (line 294): add line `\nАвто-саммари: {status}`
   Must NOT: change manual `/summary` logic, change `_generate_summary()`, add new constants
  References: summary.py:35,45,382-389,437-444,692-726,267-301
  Acceptance criteria: lsp_diagnostics clean on summary.py
  Commit: N
- [x] 2. main.py: Register /autosummary in GROUP_COMMANDS
  What to do / Must NOT do:
   Add to GROUP_COMMANDS list at line 1996:
   `BotCommand(command="autosummary", description="Включить/выключить авто-саммари раз в день")`
   Must NOT: change anything else
  References: main.py:1991-1998
  Acceptance criteria: lsp_diagnostics clean on main.py
  Commit: N

## Final verification wave
- [x] F1. Plan compliance audit — all 5 changes present
- [x] F2. Code quality review — lsp_diagnostics clean, logic matches pattern
- [x] F3. Real manual QA — Read both files, verify every change
- [x] F4. Scope fidelity — no unintended changes

## Commit strategy
One commit after all verification passes.

## Success criteria
- `/autosummary` shows status, `/autosummary 0` disables, `/autosummary 1` enables auto-summary
- With auto-summary disabled: no auto-flush happens even if delta exceeded
- With auto-summary enabled: auto-flush works as before (1500 msg limit)
- `/info` shows auto-summary status
