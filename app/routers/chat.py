import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from app.auth_utils import get_current_user
from app.config import DATA_DIR
from app.database import get_db
from app.prompts.ralphy import RALPHY_SYSTEM_PROMPT
from app.sse_bus import sse_bus

router = APIRouter(prefix="/api/projects", tags=["chat"])

ALLOWED_TOOLS = "Bash(bd:*) Bash(git:*) Read Glob Grep Write(AGENTS.md) Edit(AGENTS.md)"
MAX_MESSAGE_LENGTH = 50_000

ALLOWED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
}
MAX_FILE_SIZE = 3 * 1024 * 1024  # 3 MB
MAX_ATTACHMENTS = 3


class ChatRequest(BaseModel):
    message: str


async def _get_project_for_user(user: dict, project_name: str) -> dict:
    """Fetch a project row ensuring it belongs to the authenticated user."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM projects WHERE user_id = ? AND name = ?",
            (user["id"], project_name),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return dict(row)


async def _ensure_session_id(project_id: int, current_session_id: str | None) -> tuple[str, bool]:
    """Return (session_id, is_new). Creates and stores a new UUID if needed."""
    if current_session_id:
        return current_session_id, False

    session_id = str(uuid.uuid4())
    async with get_db() as db:
        await db.execute(
            "UPDATE projects SET ralph_session_id = ? WHERE id = ?",
            (session_id, project_id),
        )
        await db.commit()
    return session_id, True


def _build_claude_args(
    session_id: str, is_new_session: bool, user_message: str
) -> list[str]:
    """Build the argument list for the claude CLI subprocess."""
    args = ["claude", "-p", "--model", "opus", "--max-budget-usd", "5"]

    if is_new_session:
        args += ["--session-id", session_id]
        args += ["--system-prompt", RALPHY_SYSTEM_PROMPT]
    else:
        args += ["--resume", session_id]

    args += [
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--allowedTools", ALLOWED_TOOLS,
        "--", user_message,
    ]
    return args


async def _validate_attachments(attachments: list[UploadFile]) -> list[tuple[str, bytes]]:
    """Validate attachments and return list of (filename, content) tuples."""
    if len(attachments) > MAX_ATTACHMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many attachments. Maximum is {MAX_ATTACHMENTS}.",
        )

    validated: list[tuple[str, bytes]] = []
    for attachment in attachments:
        if attachment.content_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"File type '{attachment.content_type}' is not allowed. "
                       f"Allowed types: {', '.join(sorted(ALLOWED_MIME_TYPES))}",
            )

        content = await attachment.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File '{attachment.filename}' exceeds the 3MB size limit.",
            )

        validated.append((attachment.filename or "unnamed", content))

    return validated


def _save_attachments(
    project_dir: str, validated: list[tuple[str, bytes]]
) -> tuple[Path, list[Path]]:
    """Save validated attachments to .tmp_attachments/ and return (tmp_dir, paths)."""
    tmp_dir = Path(project_dir) / ".tmp_attachments"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[Path] = []
    for filename, content in validated:
        dest = tmp_dir / filename
        dest.write_bytes(content)
        saved_paths.append(dest)

    return tmp_dir, saved_paths


def _prepend_attachment_info(message: str, saved_paths: list[Path]) -> str:
    """Prepend attachment file references to the user message."""
    file_descriptions = ", ".join(
        f"{p.name} (saved at {p})" for p in saved_paths
    )
    prefix = f"The user attached file(s): {file_descriptions}. Please read and analyze them."
    return f"{prefix}\n\n{message}"


async def _stream_claude(
    project_name: str,
    project_dir: str,
    session_id: str,
    is_new_session: bool,
    user_message: str,
    tmp_attachments_dir: Path | None = None,
):
    """Async generator that spawns claude CLI and yields SSE events."""
    args = _build_claude_args(session_id, is_new_session, user_message)

    env = {"BD_ACTOR": "ralphy"}

    await sse_bus.publish(project_name, "ralphy_processing", {"status": "start"})

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **env},
        )

        async for raw_line in proc.stdout:
            line = raw_line.decode().strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            if msg_type == "assistant":
                # Extract content from the message
                content_blocks = data.get("message", {}).get("content", [])
                for block in content_blocks:
                    if block.get("type") == "text":
                        event = {"type": "text", "content": block["text"]}
                        yield f"data: {json.dumps(event)}\n\n"
                    elif block.get("type") == "thinking":
                        event = {"type": "thinking", "content": block["thinking"]}
                        yield f"data: {json.dumps(event)}\n\n"
                    elif block.get("type") == "tool_use":
                        event = {"type": "tool_use", "name": block["name"], "input": block.get("input", {})}
                        yield f"data: {json.dumps(event)}\n\n"

            elif msg_type == "content_block_start":
                content_block = data.get("content_block", {})
                if content_block.get("type") == "tool_use":
                    event = {"type": "tool_use", "name": content_block["name"], "input": content_block.get("input", {})}
                    yield f"data: {json.dumps(event)}\n\n"

            elif msg_type == "content_block_delta":
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    event = {"type": "text", "content": delta["text"]}
                    yield f"data: {json.dumps(event)}\n\n"
                elif delta.get("type") == "thinking_delta":
                    event = {"type": "thinking", "content": delta["thinking"]}
                    yield f"data: {json.dumps(event)}\n\n"

            elif msg_type == "result":
                result_text = data.get("result", "")
                event = {"type": "done", "result": result_text}
                yield f"data: {json.dumps(event)}\n\n"

        await proc.wait()

        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read()
            stderr_text = stderr_bytes.decode().strip()
            context_keywords = ["context", "token", "length", "too long", "max_tokens"]
            if any(kw in stderr_text.lower() for kw in context_keywords):
                event = {
                    "type": "error",
                    "message": "The conversation is getting too long. Please start a new session.",
                }
            else:
                event = {
                    "type": "error",
                    "message": f"Claude exited with code {proc.returncode}: {stderr_text}",
                }
            yield f"data: {json.dumps(event)}\n\n"

    except Exception as exc:
        event = {"type": "error", "message": str(exc)}
        yield f"data: {json.dumps(event)}\n\n"

    finally:
        # Clean up temp attachments
        if tmp_attachments_dir and tmp_attachments_dir.exists():
            shutil.rmtree(tmp_attachments_dir, ignore_errors=True)
        await sse_bus.publish(project_name, "ralphy_processing", {"status": "end"})


@router.post("/{name}/chat")
async def chat(
    name: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    project = await _get_project_for_user(user, name)
    github_username: str = user["github_username"]
    project_dir = str(DATA_DIR / github_username / name)

    content_type = request.headers.get("content-type", "")
    tmp_attachments_dir: Path | None = None

    if "multipart/form-data" in content_type:
        form = await request.form()
        message = form.get("message")
        if not message or not isinstance(message, str):
            raise HTTPException(status_code=400, detail="Field 'message' is required.")

        attachments: list[UploadFile] = [
            v for _, v in form.multi_items()
            if isinstance(v, UploadFile)
        ]

        if attachments:
            validated = await _validate_attachments(attachments)
            tmp_attachments_dir, saved_paths = _save_attachments(project_dir, validated)
            message = _prepend_attachment_info(message, saved_paths)

        user_message = message
    else:
        # Assume JSON
        try:
            body = ChatRequest(**(await request.json()))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body.")
        user_message = body.message

    if len(user_message) > MAX_MESSAGE_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Message too long. Maximum {MAX_MESSAGE_LENGTH} characters.",
        )

    session_id, is_new = await _ensure_session_id(
        project["id"], project.get("ralph_session_id")
    )

    return StreamingResponse(
        _stream_claude(
            project_name=name,
            project_dir=project_dir,
            session_id=session_id,
            is_new_session=is_new,
            user_message=user_message,
            tmp_attachments_dir=tmp_attachments_dir,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
