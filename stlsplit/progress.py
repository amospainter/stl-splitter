"""Lightweight progress reporting threaded through the pipeline so long-running
jobs (many cuts/connectors) can report incremental status to a caller (e.g.
the web UI's job polling endpoint)."""
from __future__ import annotations

from typing import Callable


class ProgressReporter:
    """Call `step(message)` at each meaningful milestone. If `set_total` was
    called with a known step count, `fraction` in the callback is 0..1;
    otherwise it's None (indeterminate progress)."""

    def __init__(self, on_update: Callable[[str, float | None], None] | None = None):
        self._on_update = on_update
        self._total: int | None = None
        self._done = 0

    def set_total(self, total: int) -> None:
        self._total = max(total, 1)

    def step(self, message: str) -> None:
        self._done += 1
        fraction = min(self._done / self._total, 1.0) if self._total else None
        if self._on_update:
            self._on_update(message, fraction)
