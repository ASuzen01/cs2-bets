"""Общие фикстуры тестов.

Обычно тесты идут на локальном SQLite. Если задан `TEST_DATABASE_URL`
(например, строка подключения к Neon или локальному Postgres), весь набор
прогоняется на Postgres — каждый тест в своей схеме, чтобы не мешали друг другу.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.db import Database

PG_URL = os.getenv("TEST_DATABASE_URL", "").strip()


def _admin_execute(sql: str) -> None:
    import psycopg

    with psycopg.connect(PG_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql)


@pytest.fixture(autouse=True)
def database_backend(monkeypatch):
    """Подменяет конструктор Database: SQLite по умолчанию, Postgres по флагу."""
    if not PG_URL:
        yield
        return

    schema = f"t_{uuid.uuid4().hex[:12]}"
    _admin_execute(f"CREATE SCHEMA {schema}")

    original_init = Database.__init__

    def init(self, path=None, *, start_balance: int = 1000, url: str = "", schema_: str = "", **kwargs):
        original_init(self, path, start_balance=start_balance, url=PG_URL, schema=schema)

    # тесты вызывают Database(path, start_balance=...)
    monkeypatch.setattr(Database, "__init__", init)
    try:
        yield
    finally:
        _admin_execute(f"DROP SCHEMA {schema} CASCADE")
