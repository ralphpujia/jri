import asyncio
import json
import re
import shutil

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.auth_utils import get_current_user
from app.config import DATA_DIR, RALPH_BOT_GITHUB_TOKEN
from app.database import get_db

router = APIRouter(prefix="/projects", tags=["projects"])

_NAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$")


class CreateProjectRequest(BaseModel):
    name: str
    description: str


async def _run(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


@router.post("")
async def create_project(
    body: CreateProjectRequest,
    user: dict = Depends(get_current_user),
):
    name = body.name
    description = body.description

    # --- Validation ---
    if not (1 <= len(name) <= 100) or not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid project name")

    github_username: str = user["github_username"]
    user_name: str = user.get("github_name") or github_username
    user_email: str = user.get("github_email") or f"{github_username}@users.noreply.github.com"
    user_id: int = user["id"]

    # Check uniqueness in DB
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        if await cursor.fetchone():
            raise HTTPException(status_code=409, detail="Project name already exists")

    project_dir = DATA_DIR / github_username / name
    github_repo_url = f"https://github.com/ralphpujia/{name}"
    token = RALPH_BOT_GITHUB_TOKEN

    try:
        # 1. Create project directory
        project_dir.mkdir(parents=True, exist_ok=True)
        cwd = str(project_dir)

        # 2. git init
        rc, _, err = await _run(["git", "init"], cwd=cwd)
        if rc != 0:
            raise RuntimeError(f"git init failed: {err}")

        # 3. git config
        await _run(["git", "config", "user.name", "ralphpujia"], cwd=cwd)
        await _run(["git", "config", "user.email", "ralphpujia@users.noreply.github.com"], cwd=cwd)

        # 4. bd init
        rc, _, err = await _run(["bd", "init"], cwd=cwd)
        if rc != 0:
            raise RuntimeError(f"bd init failed: {err}")

        # 5. Create AGENTS.md
        agents_md = (
            f"# {name}\n"
            f"\n"
            f"{description}\n"
            f"\n"
            f"## Project Info\n"
            f"- Repository: {github_repo_url}\n"
            f"- Created by: {github_username}\n"
        )
        (project_dir / "AGENTS.md").write_text(agents_md)

        # 6. Create uploads/ directory
        (project_dir / "uploads").mkdir(exist_ok=True)

        # 7. Git add all and commit
        await _run(["git", "add", "."], cwd=cwd)
        commit_msg = (
            f"Initial project setup\n"
            f"\n"
            f"Co-authored-by: {user_name} <{user_email}>"
        )
        rc, _, err = await _run(["git", "commit", "-m", commit_msg], cwd=cwd)
        if rc != 0:
            raise RuntimeError(f"git commit failed: {err}")

        # 8. Create GitHub repo
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.github.com/user/repos",
                headers=headers,
                json={
                    "name": name,
                    "description": description,
                    "private": False,
                    "auto_init": False,
                },
                timeout=30,
            )
            if resp.status_code == 422 and "already exists" in resp.text.lower():
                raise HTTPException(status_code=409, detail="GitHub repo already exists")
            if resp.status_code >= 400:
                raise RuntimeError(f"GitHub create repo failed ({resp.status_code}): {resp.text}")

            # 9. Add user as collaborator
            resp2 = await client.put(
                f"https://api.github.com/repos/ralphpujia/{name}/collaborators/{github_username}",
                headers=headers,
                json={"permission": "push"},
                timeout=30,
            )
            if resp2.status_code >= 400:
                raise RuntimeError(f"GitHub add collaborator failed ({resp2.status_code}): {resp2.text}")

        # 10. Add remote
        rc, _, err = await _run(
            ["git", "remote", "add", "origin", f"https://x-access-token:{token}@github.com/ralphpujia/{name}.git"],
            cwd=cwd,
        )
        if rc != 0:
            raise RuntimeError(f"git remote add failed: {err}")

        # 11. Push
        rc, _, err = await _run(["git", "push", "-u", "origin", "main"], cwd=cwd)
        if rc != 0:
            raise RuntimeError(f"git push failed: {err}")

    except HTTPException:
        # Re-raise HTTP exceptions (like 409) after cleanup
        if project_dir.exists():
            shutil.rmtree(project_dir)
        raise
    except Exception as exc:
        # Clean up on any failure
        if project_dir.exists():
            shutil.rmtree(project_dir)
        raise HTTPException(status_code=500, detail=str(exc))

    # 12. Insert into SQLite
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO projects (user_id, name, description, github_repo_url) VALUES (?, ?, ?, ?)",
            (user_id, name, description, github_repo_url),
        )
        await db.commit()
        project_id = cursor.lastrowid

    # 13. Return JSON
    return {
        "id": project_id,
        "name": name,
        "description": description,
        "github_repo_url": github_repo_url,
    }


async def _get_issue_count(project_dir: str) -> int:
    """Run `bd list --json` in the project directory and count issues."""
    try:
        rc, stdout, _ = await _run(["bd", "list", "--json"], cwd=project_dir)
        if rc != 0:
            return 0
        issues = json.loads(stdout)
        return len(issues)
    except Exception:
        return 0


@router.get("")
async def list_projects(user: dict = Depends(get_current_user)):
    user_id: int = user["id"]
    github_username: str = user["github_username"]

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, name, description, github_repo_url, ralph_loop_status, created_at "
            "FROM projects WHERE user_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()

    results = []
    for row in rows:
        row_dict = dict(row)
        project_dir = str(DATA_DIR / github_username / row_dict["name"])
        issue_count = await _get_issue_count(project_dir)
        results.append(
            {
                "id": row_dict["id"],
                "name": row_dict["name"],
                "description": row_dict["description"],
                "github_repo_url": row_dict["github_repo_url"],
                "issue_count": issue_count,
                "ralph_loop_status": row_dict["ralph_loop_status"],
                "created_at": row_dict["created_at"],
            }
        )

    return results


@router.get("/{name}/agents-md")
async def get_agents_md(name: str, user: dict = Depends(get_current_user)):
    github_username: str = user["github_username"]
    user_id: int = user["id"]

    # Verify project belongs to user
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Project not found")

    agents_path = DATA_DIR / github_username / name / "AGENTS.md"

    if not agents_path.exists():
        return {"content": "", "exists": False}

    content = agents_path.read_text()
    return {"content": content, "exists": True}


@router.get("/{name}")
async def get_project(name: str, user: dict = Depends(get_current_user)):
    user_id: int = user["id"]
    github_username: str = user["github_username"]

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, name, description, github_repo_url, ralph_session_id, "
            "ralph_loop_status, ralph_loop_current_issue, ralph_loop_iteration, created_at "
            "FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")

    row_dict = dict(row)
    project_dir = str(DATA_DIR / github_username / row_dict["name"])
    issue_count = await _get_issue_count(project_dir)

    return {
        "id": row_dict["id"],
        "name": row_dict["name"],
        "description": row_dict["description"],
        "github_repo_url": row_dict["github_repo_url"],
        "issue_count": issue_count,
        "ralph_session_id": row_dict["ralph_session_id"],
        "ralph_loop_status": row_dict["ralph_loop_status"],
        "ralph_loop_current_issue": row_dict["ralph_loop_current_issue"],
        "ralph_loop_iteration": row_dict["ralph_loop_iteration"],
        "created_at": row_dict["created_at"],
    }


@router.delete("/{name}", status_code=204)
async def delete_project(
    name: str,
    delete_repo: bool = True,
    user: dict = Depends(get_current_user),
):
    user_id: int = user["id"]
    github_username: str = user["github_username"]

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, ralph_loop_status FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")

    row_dict = dict(row)

    if row_dict["ralph_loop_status"] == "running":
        raise HTTPException(status_code=409, detail="Cannot delete while Ralph is running")

    # Delete GitHub repo if requested
    if delete_repo:
        token = RALPH_BOT_GITHUB_TOKEN
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"https://api.github.com/repos/ralphpujia/{name}",
                headers=headers,
                timeout=30,
            )
            # Ignore 404 (repo already gone)
            if resp.status_code >= 400 and resp.status_code != 404:
                raise HTTPException(
                    status_code=500,
                    detail=f"GitHub delete repo failed ({resp.status_code}): {resp.text}",
                )

    # Delete project directory
    project_dir = DATA_DIR / github_username / name
    if project_dir.exists():
        shutil.rmtree(project_dir)

    # Delete from SQLite
    async with get_db() as db:
        await db.execute("DELETE FROM projects WHERE id = ?", (row_dict["id"],))
        await db.commit()

    return Response(status_code=204)
