"""Сжатие больших ответов мини-приложения: линия с росписью не должна весить мегабайт."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.web.auth import COMPRESS_MIN_BYTES, gzip_middleware


async def _make_client(payload_size: int) -> TestClient:
    async def big(_: web.Request) -> web.Response:
        return web.json_response({"data": "x" * payload_size})

    async def small(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[gzip_middleware])
    app.router.add_get("/big", big)
    app.router.add_get("/small", small)
    return TestClient(TestServer(app))


@pytest.mark.parametrize("path", ["/big", "/small"])
async def test_gzip_only_for_big_payloads(path: str) -> None:
    client = await _make_client(COMPRESS_MIN_BYTES * 4)
    await client.start_server()
    try:
        response = await client.get(
            path, headers={"Accept-Encoding": "gzip"}, auto_decompress=False
        )
        assert response.status == 200
        compressed = response.headers.get("Content-Encoding") == "gzip"
        assert compressed is (path == "/big")
    finally:
        await client.close()


async def test_client_without_gzip_gets_plain_json() -> None:
    client = await _make_client(COMPRESS_MIN_BYTES * 4)
    await client.start_server()
    try:
        response = await client.get("/big", headers={"Accept-Encoding": "identity"})
        assert "Content-Encoding" not in response.headers
        assert response.status == 200
    finally:
        await client.close()
