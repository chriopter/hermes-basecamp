"""Lazy Basecamp platform plugin entry point."""
from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """Register without importing Hermes gateway modules during discovery."""
    from .plugin import register as register_plugin

    register_plugin(ctx)


__all__ = ["register"]
