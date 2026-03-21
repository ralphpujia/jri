"""SSE streaming endpoint for real-time project events."""

import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.sse_bus import sse_bus

router = APIRouter(prefix="/api/projects", tags=["sse"])

KEEPALIVE_INTERVAL = 30  # seconds


@router.get("/{project_name}/events")
async def project_events(project_name: str):
    """Stream SSE events for a project.

    Returns text/event-stream. Sends a keepalive comment every 30 seconds
    and cleans up the subscription when the client disconnects.

    Auth will be added by a later task (get_current_user dependency).
    """

    async def _stream():
        queue = sse_bus.subscribe(project_name)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_INTERVAL)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
                except asyncio.TimeoutError:
                    # Keepalive comment to prevent connection timeout
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            sse_bus.unsubscribe(project_name, queue)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
