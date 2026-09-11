"""Vercel Python entrypoint: re-export the FastAPI app as an ASGI callable.

Vercel's Python runtime imports this file and serves whatever module-level
object is named `app`. All the actual routes/logic live in ../app.py --
this file only exists because Vercel expects an entrypoint under /api.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app  # noqa: E402,F401
