# GSI Notes — Build Notes

Dev-facing notes for this app itself (`/home/qbnasasn/GSI-Notes/`). Not user content — that lives in `machine_vault/`. See `machine_vault/GSI-Notes/Enhancements` for the user's live feature request list.

## What this is
Custom FastAPI + vanilla JS notes app: file tree (Wunderbaum) + markdown editor with toolbar (EasyMDE) + AI panel (Stratum), all operating directly on `machine_vault/` as a git repo. Built after rejecting SilverBullet, BookStack, HedgeDoc, Nextcloud, and FileBrowser — none of them let an AI agent drop a file on disk and have it just appear without going through a database/import step.

## Stack
- Backend: `app.py` (FastAPI, single file)
- Frontend: `static/index.html` (single file, CDN deps: EasyMDE, Wunderbaum, marked.js, bootstrap-icons)
- Auth: HMAC-signed session cookie (`SECRET_KEY` env var) — stateless, survives container restarts
- Every file write auto-commits to git in `machine_vault/`
- AI panel talks to Stratum (`172.17.0.1:11435`, OpenAI-compatible) using a JSON-action protocol (`{"action":"edit",...}` / `{"action":"create",...}`) since Stratum doesn't support real OpenAI tool-calling (confirmed: `tools` param is silently ignored, no `tool_calls` ever returned)

## Fixed so far
- Session store was in-memory (`sessions: set()`) — wiped on every container rebuild, causing silent-looking save failures. Replaced with stateless HMAC tokens (2026-08-21).
- Wunderbaum's `e.node.isFolder` doesn't exist as a method — real check is `node.data.folder` (or `children !== null`, since files get `null` and folders get an array). Broke click-to-open and drag-and-drop until fixed.
- Wunderbaum defaults to a white theme via CSS custom properties (`--wb-*`), and hardcodes `overflow-y: scroll` (always-on scrollbar) rather than `auto`.
- Nested double-scrollbar: `#tree-panel` and Wunderbaum's own container were both scrollable at once.

## Write-path hardening (2026-08-25)
- **Optimistic concurrency.** `GET /api/file` returns `mtime` (an opaque version tag); `PUT /api/file?mtime=…` returns **409** if the file changed since. Frontend offers keep-mine / reload-theirs. This is what protects against an external agent's write being silently clobbered — the app's core premise.
- **The version tag is a STRING on purpose.** `st_mtime_ns` is ~1.8e18, ~200× past JS `Number.MAX_SAFE_INTEGER` (9.0e15). As a JSON number it loses precision in the browser and every save produced a *phantom* 409. `version_of()` returns `f"{mtime_ns}-{size}"`. Do not "simplify" this back to an int.
- **Atomic writes.** `atomic_write()` = temp file in the same dir + `os.fsync` + `os.replace`. Temp files are named `.tmp-gsinotes-*` so the dotfile filter already hides them from the tree.
- **Binary guard.** `is_text_file()` (extension blocklist + NUL-byte + UTF-8 decode check) gates both read and write with 415. Before this, opening a PDF loaded `errors="replace"` mojibake and saving wrote it back — silent destruction of the file.
- **Delete targeting.** Was one `deleteSelected()` that preferred the *tree selection* over the open file, so the editor's trash icon could nuke a whole folder. Split into `deleteOpenFile()` (toolbar) and `deleteTreeSelection()` (tree), each naming its target in the confirm.
- **AI undo.** `applyActionIfAny` snapshots `lastSavedContent` before an edit and attaches it to the chat note; `undoAiEdit()` restores it.
- **Tree expansion** is captured (`wbTree.root.visit`) and restored around `wbTree.load()`.

## Mobile / diff / streaming (2026-08-26)
- **The `viewport` meta tag was missing.** Without it mobile browsers lay out at ~980px and zoom out; no amount of CSS fixes that. It's the first thing to check if mobile ever looks wrong again.
- **Mobile breakpoint is 820px.** Tree becomes a fixed drawer (`transform: translateX(-100%)`, `.open` slides it in) with a backdrop; chat becomes a bottom sheet. Widths use `!important` because the desktop resizers write **inline** `style.width`, which would otherwise beat the media query. `body.chat-open` pads `#main` so the sheet doesn't cover the editor. Editor font is bumped to 16px on mobile — iOS auto-zooms on focus for anything smaller.
- **AI edits are now proposed, not applied.** `applyActionIfAny` builds a diff (jsdiff `diffLines`) and pushes a `role: 'diff'` entry into `chatHistory`; `pendingEdit` holds the payload until Apply/Discard. Only Apply touches the file. Runs of >6 unchanged lines are collapsed.
- **Streaming**: `POST /api/chat/stream` proxies the upstream SSE as **NDJSON**, one `{"t":"c"|"r"|"e","v":…}` per line — `c` = content, `r` = reasoning, `e` = error. The frontend accumulates, repaints at most every 60ms, and runs `applyActionIfAny` on the completed text.
- **Reasoning display works but is currently dormant.** DeepSeek-R1-Distill-14B on Stratum does **not** emit `<think>` blocks or `reasoning_content` — it reasons inline in prose. The collapsible block and `splitThinking()` handle both shapes and will light up automatically with a model that does emit them (e.g. qwen36-spec on 18099).
- Stratum's stream opens with a handful of **empty-content chunks** before real tokens. Don't conclude streaming is broken from the first few frames — check the whole stream.
- Testing gotcha: `let` bindings at script top level are **not** on `window`, so `page.evaluate("window.streaming")` is always undefined. Use the bare identifier.

## Rename (2026-09-01)
Rename always existed (F2 on a tree node) but was invisible — and unreachable on mobile, which has no F2 key. Added a **✏️ Rename** button to the tree actions; both paths now share `performRename()`, which fixed two real bugs found while testing:
- **Renaming the open document left `currentPath` on the old name.** The next save then *recreated a file under the old filename* (the write path creates missing files, and the conflict check doesn't fire when nothing is there). `performRename` now follows the open file — including when a folder above it is renamed — and refreshes its version tag, since the path change invalidates it.
- **`loadTree()` was called from inside Wunderbaum's `edit.apply`**, destroying the node it was still finishing with; it threw `Cannot read properties of null (reading 'options'/'update')` on every F2 rename. The reload is now deferred with `setTimeout(…, 0)` so the edit lifecycle completes first.

## Postgres-backed accounts (2026-10-02)
Replaced the single shared `APP_PASSWORD` with real user accounts. `auth.py` holds it all; `schema.sql` is idempotent.

- **Its own database.** Users live in a dedicated `gsinotes` DB owned by `gsinotes_user`, *not* inside `openwebui` — that DB holds an account and chat history, and Open WebUI runs Alembic migrations over it.
- **Argon2** password hashing (`argon2-cffi`), with rehash-on-login when parameters change. Unknown emails still spend hashing time so response timing doesn't reveal which accounts exist.
- **Sessions stay stateless** (HMAC cookie) but now carry `user_id:token_version`. Bumping `token_version` invalidates every cookie for that user — that's how "disable account" and password changes sign people out immediately. Costs one indexed lookup per request over a local socket.
- **Login throttling** via `login_attempts`: 8 failures per account and 20 per IP in a rolling 15 minutes. Counted separately on purpose — per-IP alone would let one attacker lock out everyone behind a shared NAT, per-account alone wouldn't slow a spray across many accounts.
- **Git commits are attributed per user** via `-c user.name/user.email`.
- **`git_commit` is now serialised with a process lock** and accepts multiple paths. `add`+`commit` isn't atomic; two concurrent saves collided on `.git/index.lock`, and because failures here are deliberately swallowed, commits vanished *silently*. Verified: 12 concurrent writes → 12 commits, nothing dropped.

### Infrastructure gotchas
- **Compose networks are not the default bridge.** The container sits on `172.23.x`, so the existing `host all all 172.17.0.0/16` pg_hba rule didn't match. Added `host gsinotes gsinotes_user 172.16.0.0/12` — scoped to one DB and one role, and stable if the compose network is recreated.
- `sudo -S` reads its password from stdin, so a heredoc fed to `psql` silently steals it and the SQL never runs. Use `-f file` or `-c`.
- pg_hba had dead rules for `n8n`/`n8n_vector_db` (databases that no longer exist); removed. A dated backup of the original sits next to it.

## Known limitations / not yet done
- Single shared password, no real multi-user accounts (tracked, not urgent per user)
- Auto-save runs on a 15s interval, not truly instant
- AI edit protocol depends on the model reliably emitting clean JSON — reasonably reliable on Qwen2.5-Coder-7B in testing, but a 7B model, not guaranteed
- If Stratum ever gets real tool-calling support (separate project, tracked by user in another session), the `/api/chat` handler in `app.py` could be simplified to use it instead of the JSON-text protocol

## Verification approach
A headless Chromium (Playwright, via a venv at `/tmp/.../scratchpad/pwenv`) is used to actually click through and screenshot changes before calling them done — several bugs above were only caught this way after documentation-based assumptions turned out wrong twice in a row.
