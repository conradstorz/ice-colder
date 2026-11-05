"""Shared scaffolding for the settings routes (helpers only, no router)."""

from collections.abc import Callable

from fastapi import Response
from loguru import logger

from ice_edge.config import config_store
from ice_edge.web import context


def save_or_rerender(area: str, render: Callable[[str], Response]) -> Response:
    """Persist the live config; on failure log it and re-render the form with
    the error, on success HX-redirect to `/settings/<area>`."""
    try:
        config_store.save_config(context.config)
    except Exception as e:
        logger.error(f"settings/{area}: save_config failed: {e}")
        return render(f"Could not save changes: {e}")
    return context.hx_redirect(f"/settings/{area}")


def split_lines(raw: str) -> list[str]:
    """Split on newlines and commas, strip, and drop blanks."""
    return [
        part.strip() for part in raw.replace(",", "\n").splitlines() if part.strip()
    ]
