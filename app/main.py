from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import DATA_DIR
from app.database import init_db
from app.routers import auth, pages, projects, chat, ralph, uploads, sse


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure data directory exists and initialize database
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    await init_db()
    yield


app = FastAPI(title="Just Ralph It", lifespan=lifespan)

# Mount static files
_static_dir = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Include routers
app.include_router(pages.router)
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(chat.router)
app.include_router(ralph.router)
app.include_router(uploads.router)
app.include_router(sse.router)
