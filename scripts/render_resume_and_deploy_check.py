#!/usr/bin/env python3
"""Trigger deploy + wait for the pushed commit, then smoke-check.

Usage:
  RENDER_API_KEY=<key> python scripts/render_resume_and_deploy_check.py

Targets the new Render workspace service "cs2-bets-bot-new":
1. POST /v1/services/{id}/deploys  -> trigger deploy
2. Poll deploy status until complete
3. Smoke-check https://cs2-bets-bot-new.onrender.com/ping and /api/state
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.render.com"
BASE = "https://cs2-bets-bot-new.onrender.com"
SERVICE_NAME = "cs2-bets-bot-new"
DEPLOY_WAIT = int(os.getenv("DEPLOY_WAIT", "900"))  # seconds


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    token = os.getenv("RENDER_API_KEY", "")
    if not token:
        print("RENDER_API_KEY не задан", file=sys.stderr)
        sys.exit(2)
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def http_get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "render-deploy-check/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> int:
    # find service
    status, raw = call("GET", "/v1/services")
    services = [s["service"] for s in raw] if isinstance(raw, list) else []
    svc = None
    for s in services:
        if s.get("name") == SERVICE_NAME:
            svc = s
            break
    if svc is None:
        print(f"Сервис {SERVICE_NAME} не найден. Видимые:", [s.get("name") for s in services], file=sys.stderr)
        return 1
    sid = svc["id"]
    print(f"Сервис: {svc['name']} id={sid} suspended={svc.get('suspended')} autoDeploy={svc.get('autoDeploy')}")

    # trigger deploy
    st, res = call("POST", f"/v1/services/{sid}/deploys")
    print(f"trigger deploy -> HTTP {st}:", json.dumps(res, ensure_ascii=False)[:200])
    if st not in (200, 201, 202):
        return 1
    deploy_id = res.get("id")

    # wait
    deadline = time.time() + DEPLOY_WAIT
    while time.time() < deadline:
        st, d = call("GET", f"/v1/deploys/{deploy_id}")
        if st == 200:
            ds = d.get("status")
            print(f"  deploy {deploy_id} status={ds} ({time.strftime('%H:%M:%S')})")
            if ds in ("success", "failed", "error"):
                if ds == "success":
                    break
                return 1
        time.sleep(15)
    else:
        print("Истёк лимит ожидания deploy", file=sys.stderr)
        return 1

    # smoke
    for path in ("/ping", "/api/state"):
        st, body = http_get(BASE + path)
        print(f"{path} -> HTTP {st}: {body[:120]}")
        if st != 200:
            print("SMOKE FAIL", file=sys.stderr)
            return 1
    print("OK: релиз на продакшне завершён")
    return 0


if __name__ == "__main__":
    sys.exit(main())
