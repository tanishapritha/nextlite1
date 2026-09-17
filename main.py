"""Entry point for the Glaze Dental Clinic WhatsApp bot.

Run locally:
    uvicorn main:app --reload

Deploy on Render:
    Start command: uvicorn main:app --host 0.0.0.0 --port $PORT
"""
from app.main import app  # noqa: F401

__all__ = ["app"]
