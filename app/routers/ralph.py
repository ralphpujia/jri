"""Ralph loop control endpoints."""

import asyncio
import json
import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.auth_utils import get_current_user
from app.config import DATA_DIR, STRIPE_SECRET_KEY
from app.database import get_db
from app.ralph_loop import RalphLoop

logger = logging.getLogger(__name__)

stripe.api_key = STRIPE_SECRET_KEY
if STRIPE_SECRET_KEY.startswith("pk_"):
    logger.warning(
        "STRIPE_SECRET_KEY starts with 'pk_' — this looks like a publishable key, not a secret key"
    )

router = APIRouter(prefix="/api/projects", tags=["ralph"])

# Global dict of active loops keyed by project name
_loops: dict[str, RalphLoop] = {}


async def _get_project(name: str, user: dict) -> dict:
    """Look up project by name for the authenticated user. Returns row dict."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, name, ralph_loop_status, ralph_loop_current_issue, ralph_loop_iteration, stripe_payment_id "
            "FROM projects WHERE user_id = ? AND name = ?",
            (user["id"], name),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return dict(row)


@router.post("/{name}/ralph/start")
async def ralph_start(name: str, user: dict = Depends(get_current_user)):
    """Begin the Ralph loop for a project."""
    project = await _get_project(name, user)

    if name in _loops and _loops[name].status == "running":
        raise HTTPException(status_code=409, detail="Ralph loop is already running")

    github_username: str = user["github_username"]
    project_dir = str(DATA_DIR / github_username / name)

    user_name = user.get("github_name") or github_username
    user_email = user.get("github_email") or f"{github_username}@users.noreply.github.com"

    loop = RalphLoop(
        project_id=project["id"],
        project_dir=project_dir,
        project_name=name,
        user_github_name=user_name,
        user_github_email=user_email,
    )
    _loops[name] = loop
    await loop.start()

    return {"status": "running"}


@router.post("/{name}/ralph/stop")
async def ralph_stop(name: str, user: dict = Depends(get_current_user)):
    """Gracefully stop the Ralph loop after the current iteration."""
    await _get_project(name, user)

    loop = _loops.get(name)
    if loop is None:
        raise HTTPException(status_code=404, detail="No active Ralph loop")

    await loop.stop()
    return {"status": "stopped"}


@router.post("/{name}/ralph/resume")
async def ralph_resume(name: str, user: dict = Depends(get_current_user)):
    """Resume (same as start) the Ralph loop."""
    return await ralph_start(name, user=user)


@router.get("/{name}/ralph/stream")
async def ralph_stream(name: str):
    """SSE endpoint that streams Ralph's stdout in real time."""
    loop = _loops.get(name)
    if loop is None:
        raise HTTPException(status_code=404, detail="No active Ralph loop")

    async def _generate():
        queue = loop.subscribe()
        try:
            while True:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps({'line': line})}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                # Stop streaming once loop is no longer running
                if loop.status not in ("running", "stopping"):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            loop.unsubscribe(queue)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{name}/notifications")
async def get_notifications(name: str, user: dict = Depends(get_current_user)):
    """Return unacknowledged notifications for a project."""
    project = await _get_project(name, user)
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, message, beads_issue_id, created_at "
            "FROM notifications "
            "WHERE project_id = ? AND acknowledged = 0 "
            "ORDER BY created_at DESC",
            (project["id"],),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.post("/{name}/notifications/{notification_id}/acknowledge")
async def acknowledge_notification(
    name: str, notification_id: int, user: dict = Depends(get_current_user)
):
    """Mark a notification as acknowledged."""
    project = await _get_project(name, user)
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE notifications SET acknowledged = 1 "
            "WHERE id = ? AND project_id = ?",
            (notification_id, project["id"]),
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Notification not found")
    return {"status": "acknowledged"}


@router.get("/{name}/ralph/status")
async def ralph_status(name: str, user: dict = Depends(get_current_user)):
    """Return current Ralph loop state."""
    project = await _get_project(name, user)

    loop = _loops.get(name)
    if loop is None:
        return {
            "status": project.get("ralph_loop_status", "idle"),
            "current_issue": project.get("ralph_loop_current_issue"),
            "iteration": project.get("ralph_loop_iteration", 0),
            "recent_output": [],
        }

    recent = list(loop.stdout_lines)[-50:]
    return {
        "status": loop.status,
        "current_issue": loop.current_issue_id,
        "iteration": loop.iteration,
        "recent_output": recent,
    }
