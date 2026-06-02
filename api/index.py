"""Vercel serverless entrypoint.

Vercel's Python runtime detects the ASGI `app` callable and serves it.
All routes are rewritten to this function via vercel.json.
"""

from app.main import app  # noqa: F401  (re-exported for Vercel to discover)
