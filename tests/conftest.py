"""Pytest configuration: start the FastAPI server in-process and seed a test user."""

import sqlite3
import sys
import os
import threading
import time

import httpx
import pytest
import uvicorn

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.config
import app.database
import app.main
import app.routers.chat
import app.routers.pages
import app.routers.projects
import app.routers.ralph
import app.routers.uploads

TEST_PORT = 8001


@pytest.fixture(scope="session", autouse=True)
def test_data_dir(tmp_path_factory):
    """Create an isolated temp directory for all test data and patch app modules to use it.

    DATA_DIR is imported as a local binding in each router via `from app.config import DATA_DIR`,
    so patching app.config.DATA_DIR alone is not enough — every module that imported it must be
    patched individually. DATABASE_PATH is read at call-time inside get_db()/init_db(), so
    patching the module-level variable is sufficient.

    pytest cleans up tmp_path_factory directories automatically — no manual teardown needed.
    """
    tmp_dir = tmp_path_factory.mktemp("jri_data")

    app.config.DATA_DIR = tmp_dir
    app.database.DATABASE_PATH = tmp_dir / "test.db"
    app.main.DATA_DIR = tmp_dir
    app.routers.chat.DATA_DIR = tmp_dir
    app.routers.pages.DATA_DIR = tmp_dir
    app.routers.projects.DATA_DIR = tmp_dir
    app.routers.ralph.DATA_DIR = tmp_dir
    app.routers.uploads.DATA_DIR = tmp_dir

    return tmp_dir


@pytest.fixture(scope="session", autouse=True)
def live_server(test_data_dir):
    """Start the FastAPI app on localhost:8001 for the test session.

    Depends on test_data_dir so all DATA_DIR/DATABASE_PATH patches are in place
    before the server starts. Running in-process guarantees the server and the
    test token signer share the same SECRET_KEY loaded from .env.
    """
    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=TEST_PORT, log_level="error")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # signal handlers require main thread

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(60):
        try:
            with httpx.Client() as client:
                resp = client.get(f"http://localhost:{TEST_PORT}/", timeout=1)
                if resp.status_code < 500:
                    break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        pytest.fail("Server failed to start within 30 seconds")

    yield

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="session", autouse=True)
def seed_test_user(live_server):
    """Ensure the test user (id=1) exists in the isolated test DB.

    Connects to the patched DATABASE_PATH (temp file), not the real ~/jri/data/jri.db.
    No teardown needed — the entire temp DB is discarded after the session.
    """
    time.sleep(1)  # Allow lifespan startup (init_db) to finish

    conn = sqlite3.connect(app.database.DATABASE_PATH)
    conn.execute("""
        INSERT OR IGNORE INTO users
            (id, github_id, github_username, github_token, created_at)
        VALUES
            (1, 99999999, 'e2e-test-user', 'dummy-token', datetime('now'))
    """)
    conn.commit()
    conn.close()
