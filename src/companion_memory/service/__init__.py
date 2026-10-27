"""Standalone service package (S04).

Importing this package must not pull in the HTTP transport or a model client:
the core stays importable with zero runtime dependencies, and only
`companion_memory.service.server` needs a socket. Keep the re-exports lazy-ish
by importing only the assembly here.
"""

from __future__ import annotations

from .memory_service import (
    MemoryService,
    ServiceSettings,
    ServiceStartupError,
    ServiceState,
)

__all__ = [
    "MemoryService",
    "ServiceSettings",
    "ServiceStartupError",
    "ServiceState",
]
