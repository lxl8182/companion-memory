"""Default clock port implementation."""

from datetime import datetime, timezone
import time


class SystemClock:
    """Wall clock in UTC plus a monotonic source for budgets and deadlines."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()
