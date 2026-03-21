import asyncio
import json
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from app.auth_utils import get_current_user
from app.config import DATA_DIR
from app.database import get_db
from app.prompts.ralphy import RALPHY_SYSTEM_PROMPT
from app.sse_bus import sse_bus

router = APIRouter(prefix="/api/projects", tags=["chat"])

ALLOWED_TOOLS = "Bash(bd:*) Bash(git:*) Read Write Edit Glob Grep"


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
    args = ["claude", "-p", "--model", "opus"]

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


async def _stream_claude(
    project_name: str,
    project_dir: str,
    session_id: str,
    is_new_session: bool,
    user_message: str,
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
                # Extract text content from the message
                content_blocks = data.get("message", {}).get("content", [])
                for block in content_blocks:
                    if block.get("type") == "text":
                        event = {"type": "text", "content": block["text"]}
                        yield f"data: {json.dumps(event)}\n\n"

            elif msg_type == "content_block_delta":
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    event = {"type": "text", "content": delta["text"]}
                    yield f"data: {json.dumps(event)}\n\n"

            elif msg_type == "result":
                result_text = data.get("result", "")
                event = {"type": "done", "result": result_text}
                yield f"data: {json.dumps(event)}\n\n"

        await proc.wait()

        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read()
            stderr_text = stderr_bytes.decode().strip()
            event = {"type": "error", "message": f"Claude exited with code {proc.returncode}: {stderr_text}"}
            yield f"data: {json.dumps(event)}\n\n"

    except Exception as exc:
        event = {"type": "error", "message": str(exc)}
        yield f"data: {json.dumps(event)}\n\n"

    finally:
        await sse_bus.publish(project_name, "ralphy_processing", {"status": "end"})


@router.post("/{name}/chat")
async def chat(
    name: str,
    body: ChatRequest,
    user: dict = Depends(get_current_user),
):
    project = await _get_project_for_user(user, name)
    github_username: str = user["github_username"]
    project_dir = str(DATA_DIR / github_username / name)

    session_id, is_new = await _ensure_session_id(
        project["id"], project.get("ralph_session_id")
    )

    return StreamingResponse(
        _stream_claude(
            project_name=name,
            project_dir=project_dir,
            session_id=session_id,
            is_new_session=is_new,
            user_message=body.message,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
