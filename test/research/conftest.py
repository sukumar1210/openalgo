"""Fixtures for the research tests: a fresh research database per test."""

import importlib

import pytest


@pytest.fixture
def research_db(tmp_path, monkeypatch):
    """research.db on its own file under tmp_path, with every table created.

    Reloading swaps the module's engine and session in place, so the service
    module, which holds a reference to this module rather than to its engine,
    sees the fresh database without being reloaded itself.
    """
    monkeypatch.setenv("RESEARCH_DATABASE_URL", f"sqlite:///{tmp_path / 'research.db'}")
    import database.research_db as module

    module = importlib.reload(module)
    module.init_db()
    yield module
    module.db_session.remove()
    module.engine.dispose()
