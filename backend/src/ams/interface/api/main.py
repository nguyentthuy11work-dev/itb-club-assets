"""Uvicorn factory entry point."""

from ams.interface.api.app import create_app

__all__ = ["create_app"]
