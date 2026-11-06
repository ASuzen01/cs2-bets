"""Пути к картинкам проекта (маскот и прочее)."""

from __future__ import annotations

from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC_DIR = WEB_DIR / "static"

MASCOT_PATH = STATIC_DIR / "mascot.png"

__all__ = ["MASCOT_PATH", "STATIC_DIR", "WEB_DIR"]
