import aiosqlite

from app.config import DATA_DIR

DATABASE_PATH = DATA_DIR / "jri.db"


async def get_db() -> aiosqlite.Connection:
    """Open and return an aiosqlite connection to the main database."""
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db() -> None:
    """Create database tables if they don't already exist."""
    db = await get_db()
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                github_id INTEGER UNIQUE NOT NULL,
                github_login TEXT NOT NULL,
                github_name TEXT,
                github_avatar_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                repo_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        await db.commit()
    finally:
        await db.close()
