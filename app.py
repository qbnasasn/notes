import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx

CONTENT_ROOT = Path(os.environ.get("CONTENT_ROOT", "/content")).resolve()
APP_PASSWORD = os.environ["APP_PASSWORD"]
SECRET_KEY = os.environ["SECRET_KEY"].encode()
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
AI_UPSTREAM = os.environ.get("AI_UPSTREAM", "http://172.17.0.1:11435/v1/chat/completions")
AI_MODEL = os.environ.get("AI_MODEL", "DeepSeek-R1-Distill-Qwen-14B-Q4_0")

app = FastAPI()


def safe_path(rel: str) -> Path:
    rel = (rel or "").lstrip("/")
    p = (CONTENT_ROOT / rel).resolve()
    if p != CONTENT_ROOT and CONTENT_ROOT not in p.parents:
        raise HTTPException(400, "invalid path")
    return p


def make_session_token() -> str:
    payload = f"admin:{int(time.time()) + SESSION_MAX_AGE}"
    sig = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload.encode()).decode() + "." + sig


def verify_session_token(token: str) -> bool:
    try:
        payload_b64, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(payload_b64.encode()).decode()
        expected_sig = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return False
        _, expiry = payload.rsplit(":", 1)
        return int(expiry) > int(time.time())
    except Exception:
        return False


def require_session(request: Request):
    token = request.cookies.get("session")
    if not token or not verify_session_token(token):
        raise HTTPException(401, "unauthorized")


BINARY_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz",
    ".zip", ".gz", ".bz2", ".xz", ".tar", ".7z", ".rar",
    ".gguf", ".bin", ".safetensors", ".pt", ".pth", ".onnx", ".npy", ".npz",
    ".mp3", ".mp4", ".wav", ".mov", ".avi", ".mkv", ".webm",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".so", ".dll", ".dylib", ".exe", ".o", ".a", ".pyc",
    ".db", ".sqlite", ".sqlite3", ".lmdb", ".mdb",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}


def is_text_file(p: Path) -> bool:
    """Conservative check: only files we can safely round-trip as UTF-8 text."""
    if p.suffix.lower() in BINARY_EXTENSIONS:
        return False
    try:
        with p.open("rb") as f:
            chunk = f.read(8192)
    except Exception:
        return False
    if b"\x00" in chunk:
        return False
    # A multi-byte character may be split at the chunk boundary; allow trimming it.
    for trim in range(4):
        try:
            (chunk[: len(chunk) - trim] if trim else chunk).decode("utf-8")
            return True
        except UnicodeDecodeError:
            continue
    return False


def version_of(p: Path) -> str:
    """Opaque version tag for optimistic concurrency.

    Returned as a STRING on purpose: nanosecond mtimes are ~1.8e18, far past
    JavaScript's MAX_SAFE_INTEGER (9.0e15), so a JSON number would silently
    lose precision in the browser and cause phantom conflicts.
    """
    st = p.stat()
    return f"{st.st_mtime_ns}-{st.st_size}"


def atomic_write(p: Path, text: str) -> None:
    """Write via a temp file + rename so a crash can never leave a truncated file."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-gsinotes-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def git_commit(rel_path: str, message: str | None = None):
    base = ["git", "-c", f"safe.directory={CONTENT_ROOT}"]
    try:
        subprocess.run(base + ["add", "-A", "--", rel_path], cwd=CONTENT_ROOT, check=False, capture_output=True)
        subprocess.run(
            base + ["commit", "-m", message or f"edit: {rel_path}"],
            cwd=CONTENT_ROOT, check=False, capture_output=True,
        )
    except Exception:
        pass


@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    if not secrets.compare_digest(body.get("password", ""), APP_PASSWORD):
        raise HTTPException(401, "wrong password")
    token = make_session_token()
    response = JSONResponse({"ok": True})
    # `secure` is decided per-request, not hardcoded: Cloudflare terminates TLS and
    # sets X-Forwarded-Proto, but the LAN URL (http://10.0.0.10:3737) is plain HTTP.
    # Hardcoding secure=True would make LAN login silently impossible.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        "session", token,
        httponly=True, samesite="lax", max_age=SESSION_MAX_AGE,
        secure=(proto == "https"),
    )
    return response


@app.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("session")
    return response


@app.get("/api/tree")
def tree(request: Request):
    require_session(request)

    def walk(dir_path: Path):
        entries = []
        for child in sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if child.name.startswith(".") or child.name == "container_backups":
                continue
            rel = str(child.relative_to(CONTENT_ROOT))
            if child.is_dir():
                entries.append({"name": child.name, "path": rel, "type": "dir", "children": walk(child)})
            else:
                entries.append({"name": child.name, "path": rel, "type": "file"})
        return entries

    return walk(CONTENT_ROOT)


@app.get("/api/file")
def read_file(path: str, request: Request):
    require_session(request)
    p = safe_path(path)
    if not p.is_file():
        raise HTTPException(404, "not found")
    if not is_text_file(p):
        raise HTTPException(415, f"{p.name} is not a text file — opening it here would corrupt it.")
    return {"content": p.read_text(encoding="utf-8"), "mtime": version_of(p)}


@app.put("/api/file")
async def write_file(path: str, request: Request, mtime: str | None = None):
    require_session(request)
    p = safe_path(path)
    if p.exists():
        if not p.is_file():
            raise HTTPException(400, "not a file")
        if not is_text_file(p):
            raise HTTPException(415, f"refusing to overwrite {p.name} — it is not a text file")
        # Optimistic concurrency: reject if the file changed since the client read it.
        if mtime is not None and version_of(p) != mtime:
            raise HTTPException(409, "file changed on disk since it was opened")
    body = await request.body()
    atomic_write(p, body.decode("utf-8"))
    git_commit(path)
    return {"ok": True, "mtime": version_of(p)}


@app.post("/api/mkdir")
async def mkdir(request: Request):
    require_session(request)
    body = await request.json()
    p = safe_path(body["path"])
    p.mkdir(parents=True, exist_ok=True)
    return {"ok": True}


@app.post("/api/newfile")
async def newfile(request: Request):
    require_session(request)
    body = await request.json()
    p = safe_path(body["path"])
    if p.exists():
        raise HTTPException(400, "already exists")
    atomic_write(p, body.get("content", ""))
    git_commit(body["path"])
    return {"ok": True, "mtime": version_of(p)}


@app.delete("/api/path")
def delete_path(path: str, request: Request):
    require_session(request)
    p = safe_path(path)
    if p == CONTENT_ROOT:
        raise HTTPException(400, "refusing to delete root")
    if p.is_dir():
        shutil.rmtree(p)
    elif p.is_file():
        p.unlink()
    else:
        raise HTTPException(404, "not found")
    git_commit(path, message=f"delete: {path}")
    return {"ok": True}


@app.post("/api/move")
async def move_path(request: Request):
    require_session(request)
    body = await request.json()
    src = safe_path(body["from"])
    dst = safe_path(body["to"])
    if not src.exists():
        raise HTTPException(404, "source not found")
    if dst.exists():
        raise HTTPException(400, "a file or folder already exists at the destination")
    if src == dst or src in dst.parents:
        raise HTTPException(400, "cannot move a folder into itself")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    base = ["git", "-c", f"safe.directory={CONTENT_ROOT}"]
    try:
        subprocess.run(base + ["add", "-A"], cwd=CONTENT_ROOT, check=False, capture_output=True)
        subprocess.run(
            base + ["commit", "-m", f"move: {body['from']} -> {body['to']}"],
            cwd=CONTENT_ROOT, check=False, capture_output=True,
        )
    except Exception:
        pass
    return {"ok": True}


def strip_reasoning(text: str) -> str:
    """Remove <think>…</think> blocks.

    The default model is an R1 distill, which can emit chain-of-thought inline.
    Left in place it would corrupt the JSON edit/create protocol below.
    """
    out = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # An unterminated opening tag means the whole tail is reasoning.
    out = re.sub(r"<think>.*\Z", "", out, flags=re.DOTALL | re.IGNORECASE)
    return out.strip()


CHAT_SYSTEM_PROMPT = """You are a writing/editing assistant embedded in a notes app. You can directly edit the document the user has open, or create a brand new document, by replying with ONLY a single JSON object (no markdown code fence, no extra prose before or after it).

Use these exact shapes:
- To edit the currently open document, replacing its entire content: {"action": "edit", "content": "<the full new document content>"}
- To create a new document at a path: {"action": "create", "path": "<relative path, e.g. research/idea.md>", "content": "<the new document's content>"}
- For anything else (answering a question, discussing the document, asking for clarification): just reply normally in plain text/markdown. Do NOT use JSON for a normal reply.

Only use the "edit" or "create" JSON action when the user is clearly asking you to write, rewrite, clean up, format, or create a document. When you do use it, output ONLY that JSON object and nothing else - it will be applied automatically, so any extra text would corrupt the document."""


def build_messages(body: dict) -> list:
    messages = body.get("messages", [])
    file_content = body.get("file_content")
    current_path = body.get("current_path")
    context = CHAT_SYSTEM_PROMPT
    if current_path:
        context += f"\n\nThe user currently has this document open: {current_path}"
    if file_content:
        context += f"\n\nIts current content:\n\n{file_content}"
    return [{"role": "system", "content": context}] + messages


@app.post("/api/chat/stream")
async def chat_stream(request: Request):
    """Proxy the upstream SSE stream as NDJSON.

    Each line is {"t": "c"|"r", "v": "..."} — content vs reasoning. Reasoning is
    kept separate (rather than dropped) so the UI can show it in a collapsible
    block; models that don't produce it simply never emit "r" lines.
    """
    require_session(request)
    body = await request.json()
    messages = build_messages(body)

    async def gen():
        payload = {"model": AI_MODEL, "messages": messages, "stream": True}
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", AI_UPSTREAM, json=payload) as r:
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if not chunk or chunk == "[DONE]":
                            continue
                        try:
                            delta = json.loads(chunk)["choices"][0].get("delta", {}) or {}
                        except Exception:
                            continue
                        if delta.get("reasoning_content"):
                            yield json.dumps({"t": "r", "v": delta["reasoning_content"]}) + "\n"
                        if delta.get("content"):
                            yield json.dumps({"t": "c", "v": delta["content"]}) + "\n"
        except Exception as e:
            yield json.dumps({"t": "e", "v": f"{type(e).__name__}: {e}"}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/chat")
async def chat(request: Request):
    require_session(request)
    body = await request.json()
    messages = build_messages(body)
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(AI_UPSTREAM, json={"model": AI_MODEL, "messages": messages, "stream": False})
        r.raise_for_status()
        data = r.json()
    return {"reply": strip_reasoning(data["choices"][0]["message"].get("content") or "")}


@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
