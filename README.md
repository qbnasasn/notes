# GSI Notes

A small self-hosted notes app: file tree, markdown editor, and an AI assistant that can edit and create documents — all operating directly on a folder of plain markdown files backed by git.

Built because every off-the-shelf option (SilverBullet, BookStack, HedgeDoc, Nextcloud, FileBrowser) failed the same requirement: **content must be plain files on disk that scripts and AI agents can write to directly**, with the UI picking changes up automatically — not rows in a database only the app can reach.

## Features

- **File tree** with drag-and-drop move, inline rename (F2), and a drop target for moving items back to the root
- **Markdown editor** (EasyMDE) with a formatting toolbar and live inline styling
- **Preview** tab with print support
- **Find in document** (Ctrl+F) with match navigation
- **AI assistant** that can rewrite the open document or create a new one, with one-click undo on every AI edit
- **Auto-save** every 15s, unsaved-changes indicator, and a warning before you close with unsaved work
- **Multi-user accounts** — Argon2-hashed passwords in Postgres, admin-managed, with login throttling and instant session revocation
- **Every write is committed to git** — full history of every change, attributed to whoever made it, including the AI's

## Safety

The write path assumes it is *not* the only writer, because it usually isn't:

- **Conflict detection** — saves carry a version tag; if the file changed underneath you (another tab, an AI agent, an editor on the box) you choose whether to keep yours or reload theirs. Nothing is silently overwritten.
- **Atomic writes** — temp file + `fsync` + rename, so a crash can never leave a truncated file.
- **Binary guard** — refuses to open or overwrite non-text files. (Loading a PDF into a text editor and saving it back destroys it.)
- **Sanitized rendering** — markdown can legally contain raw HTML; it is scrubbed with DOMPurify before rendering.

## Running it

```bash
createdb gsinotes                      # plus a role for it; see DATABASE_URL below
psql -d gsinotes -f schema.sql
cp .env.example .env                   # then edit: set SECRET_KEY and DATABASE_URL
docker compose up -d --build
```

Create the first (admin) user once the container is up:

```bash
docker exec -it gsi-notes python3 -c "
import auth; auth.pool.open(); auth.pool.wait()
auth.create_user('you@example.com', 'a-long-password', 'Your Name', is_admin=True)"
```

After that, admins manage users from the **👥 Users** button in the app.

Then open <http://localhost:3737>.

Point `volumes:` in `docker-compose.yml` at whatever folder holds your notes. Make it a git repo (`git init`) to get version history.

### Configuration

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres holding user accounts (`postgresql://user:pass@host:5432/gsinotes`) |
| `SECRET_KEY` | Signing key for session cookies (`openssl rand -hex 32`) |
| `CONTENT_ROOT` | Content path *inside* the container (default `/content`) |
| `AI_UPSTREAM` | OpenAI-compatible `/v1/chat/completions` endpoint |
| `AI_MODEL` | Model name to request |

The AI assistant works with any OpenAI-compatible server (llama.cpp, Ollama, vLLM, …). It asks the model for a small JSON action to edit or create a file; it does not require native tool-calling support.

Run the container as your own UID (`user: "1000:1000"`) so files it creates stay owned by you.

## Notes

- Accounts are app-local; there's no SSO integration yet.
- `BUILD_NOTES.md` has implementation detail and a list of gotchas worth reading before changing anything.

## License

MIT
