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

## Known limitations / not yet done
- Single shared password, no real multi-user accounts (tracked, not urgent per user)
- Auto-save runs on a 15s interval, not truly instant
- AI edit protocol depends on the model reliably emitting clean JSON — reasonably reliable on Qwen2.5-Coder-7B in testing, but a 7B model, not guaranteed
- If Stratum ever gets real tool-calling support (separate project, tracked by user in another session), the `/api/chat` handler in `app.py` could be simplified to use it instead of the JSON-text protocol

## Verification approach
A headless Chromium (Playwright, via a venv at `/tmp/.../scratchpad/pwenv`) is used to actually click through and screenshot changes before calling them done — several bugs above were only caught this way after documentation-based assumptions turned out wrong twice in a row.
