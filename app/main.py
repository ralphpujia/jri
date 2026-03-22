import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.config import DATA_DIR
from app.database import init_db
from app.routers import auth, pages, projects, chat, ralph, uploads, sse

logger = logging.getLogger(__name__)


async def _ensure_shared_dolt_server() -> None:
    """Start the shared Dolt server if not already running."""
    port_file = Path.home() / ".beads" / "shared-server" / "dolt-server.port"
    if port_file.exists():
        # Check if the server process is alive
        pid_file = Path.home() / ".beads" / "shared-server" / "dolt-server.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                import os
                os.kill(pid, 0)  # Check if process exists
                logger.info("Shared Dolt server already running (PID %d)", pid)
                return
            except (ValueError, OSError):
                pass  # Process not running, start it

    logger.info("Starting shared Dolt server...")
    proc = await asyncio.create_subprocess_exec(
        "bd", "dolt", "start",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode == 0:
        logger.info("Shared Dolt server started: %s", stdout.decode().strip())
    else:
        logger.warning(
            "Failed to start shared Dolt server (rc=%d): %s",
            proc.returncode, stderr.decode().strip(),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure data directory exists and initialize database
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    await init_db()
    try:
        await _ensure_shared_dolt_server()
    except Exception:
        logger.exception("Could not ensure shared Dolt server — bd init may be slow")
    yield


app = FastAPI(title="Just Ralph It", lifespan=lifespan)

# Increase multipart upload limit (default is 1MB, we allow 3MB files)
from starlette.formparsers import MultiPartParser
MultiPartParser.max_file_size = 1024 * 1024 * 10  # 10MB

# Mount static files
_static_dir = Path(__file__).resolve().parent.parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Include routers
app.include_router(pages.router)
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(chat.router)
app.include_router(ralph.router)
app.include_router(uploads.router)
app.include_router(sse.router)


from starlette.types import ASGIApp, Receive, Scope, Send


class SubdomainMiddleware:
    """Route subdomain requests without wrapping responses (avoids StreamingResponse issues)."""
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            subdomain = headers.get(b"x-subdomain", b"").decode()
            if subdomain:
                from app.routers.deploy_proxy import handle_subdomain_request
                from starlette.requests import Request as StarletteRequest
                request = StarletteRequest(scope, receive, send)
                response = await handle_subdomain_request(request, subdomain)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(SubdomainMiddleware)
