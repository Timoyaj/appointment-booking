"""Current time, injectable for tests.

The cancellation cutoff is measured against "now", so tests need to move "now"
without waiting. Production always reads the real clock.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

_lock = threading.RLock()
_frozen: datetime | None = None


def now() -> datetime:
    """Timezone-aware UTC now (or the frozen test time)."""
    with _lock:
        return _frozen if _frozen is not None else datetime.now(timezone.utc)


def freeze(at: datetime) -> datetime:
    global _frozen
    with _lock:
        _frozen = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        return _frozen.astimezone(timezone.utc)


def reset() -> None:
    global _frozen
    with _lock:
        _frozen = None
