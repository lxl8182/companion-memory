"""Write permit implementations for isolated roots and for denial tests.

Production roots are constructed with a host-backed permit (B08) that maps
`WriteRejected` onto the existing maintenance-mode / save-fence response.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from ..errors import WriteRejected


class NullWritePermit:
    """Permit for isolated test/demo roots: never rejects, records nothing.

    Its name is deliberately explicit so a production wiring mistake is
    visible in code review; the host adapter supplies the real fence.
    """

    def assert_writable(self, operation: str, target: str) -> None:
        return None

    @contextmanager
    def transaction(self, operation: str, target: str) -> Iterator[None]:
        yield


class RejectingWritePermit:
    """Rejects every write; proves stores honour the permit and tests 409 paths."""

    def __init__(self, *, code: str = "write_rejected", reason: str = "write permit denied") -> None:
        self.code = code
        self.reason = reason

    def assert_writable(self, operation: str, target: str) -> None:
        raise WriteRejected(
            f"{self.reason}: {operation} on {target}", code=self.code,
        )

    @contextmanager
    def transaction(self, operation: str, target: str) -> Iterator[None]:
        self.assert_writable(operation, target)
        yield
