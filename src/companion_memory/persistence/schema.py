"""Schema gate shared by every persistent store.

Rules (B02 acceptance):
  * a missing file is a fresh store, not an error;
  * unparsable content raises CorruptStoreError and the file is left untouched;
  * an unsupported schema version raises InvalidSchemaError before any write,
    so a newer store is never downgraded in place;
  * a store that still uses a legacy (unversioned) host shape is accepted only
    through an explicit `legacy` branch, and written back in its own shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from ..errors import CorruptStoreError, InvalidSchemaError
from ..ports import AtomicJsonStore

#: Version written by this build for core-owned stores.
SCHEMA_VERSION = 1


def load_versioned(
    store: AtomicJsonStore,
    path: Path,
    *,
    supported: frozenset[int] = frozenset({SCHEMA_VERSION}),
    default: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Load a versioned JSON object store.

    Missing file -> a fresh copy of `default` (with `schema_version` filled
    in). Existing file -> validated envelope. Never writes.
    """
    raw = store.load(path)
    if raw is None:
        fresh = dict(default or {})
        fresh.setdefault("schema_version", SCHEMA_VERSION)
        return fresh
    if not isinstance(raw, dict):
        raise CorruptStoreError(
            f"expected a JSON object, found {type(raw).__name__}", path=path,
        )
    version = raw.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise InvalidSchemaError(
            f"missing or non-integer schema_version: {version!r}", path=path,
        )
    if version not in supported:
        raise InvalidSchemaError(
            f"unsupported schema_version {version}; this build supports "
            f"{sorted(supported)}", path=path,
        )
    return raw


def wrap(payload: Mapping[str, Any], *, version: int = SCHEMA_VERSION) -> dict[str, Any]:
    """Return a copy of `payload` with the schema_version envelope key set."""
    data = dict(payload)
    data["schema_version"] = version
    return data


def load_legacy_list(
    store: AtomicJsonStore,
    path: Path,
    *,
    key: str,
    supported: frozenset[int] = frozenset({SCHEMA_VERSION}),
) -> list[Any]:
    """Load a list-shaped store that the host writes as a bare JSON array.

    The bare array is the legacy host shape and is accepted as-is. A versioned
    envelope is also accepted (future migration), with the list under `key`.
    Any other shape is corruption.
    """
    raw = store.load(path)
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        version = raw.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise InvalidSchemaError(
                f"object store without integer schema_version: {version!r}", path=path,
            )
        if version not in supported:
            raise InvalidSchemaError(
                f"unsupported schema_version {version}; this build supports "
                f"{sorted(supported)}", path=path,
            )
        items = raw.get(key)
        if not isinstance(items, list):
            raise CorruptStoreError(
                f"versioned store is missing the {key!r} array", path=path,
            )
        return items
    raise CorruptStoreError(
        f"expected a JSON array or object, found {type(raw).__name__}", path=path,
    )


def require_objects(
    items: Any,
    *,
    path: Path,
    what: str,
    required_keys: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Validate a list of records before a caller trusts or rewrites it."""
    if not isinstance(items, list):
        raise CorruptStoreError(
            f"{what}: expected a list, found {type(items).__name__}", path=path,
        )
    checked: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise CorruptStoreError(
                f"{what}[{index}]: expected an object, found {type(item).__name__}",
                path=path,
            )
        missing = [key for key in required_keys if key not in item]
        if missing:
            raise CorruptStoreError(
                f"{what}[{index}]: missing required keys {missing}", path=path,
            )
        checked.append(item)
    return checked


def write_or_raise(
    store: AtomicJsonStore,
    path: Path,
    value: Any,
    *,
    permit_check: Callable[[], None] | None = None,
) -> None:
    """Save with an optional pre-write permit check (kept explicit at call sites)."""
    if permit_check is not None:
        permit_check()
    store.save(path, value)
