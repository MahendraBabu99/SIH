"""HTTP route registration entry point for the AIFT Flask application."""

from __future__ import annotations

from .handlers import register_routes

__all__ = ["register_routes"]
