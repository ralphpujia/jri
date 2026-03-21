from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth_utils import decode_session_token
from app.database import get_db

router = APIRouter(tags=["pages"])

_templates_dir = Path(__file__).resolve().parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


async def _is_logged_in(request: Request) -> bool:
    """Check if the user has a valid session, without raising errors."""
    token = request.cookies.get("session")
    if not token:
        return False
    try:
        user_id = decode_session_token(token)
    except Exception:
        return False
    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
    return row is not None


@router.get("/")
async def landing(request: Request):
    if await _is_logged_in(request):
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("landing.html", {"request": request})
