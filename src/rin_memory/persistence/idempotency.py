"""Per-character request idempotency: key records plus a staging copy.

A keyed memory write reserves a key before doing work and completes it with
the response, so a retry can be answered without repeating the effect:

  * unknown key -> a `pending` record is written, status `new`;
  * same key + same fingerprint while `pending` -> `in_progress` (another
    attempt is in flight);
  * same key + same fingerprint when `done` -> `done`, and the caller replays
    the stored response; the response is written to
    `idempotency_staging/<sha256(key)[:32]>.json` before the record turns
    `done`, so it survives a restart;
  * same key + a different fingerprint -> `conflict` (never overwritten);
  * same key when `cancelled` -> `new` (a cancelled reservation may retry).

Files live under the per-character directory:
`idempotency_keys.json` (`{"schema_version": 1, "keys": {<digest>: {...}}}`)
and `idempotency_staging/` (one JSON document per completed response).

Invariants: a damaged key file raises CorruptStoreError and is never treated
as empty (an empty read written back would erase every `done` record and let
a retried effect apply twice); an unknown schema raises InvalidSchemaError
before any write; every write passes the injected WritePermit first; each
read-modify-write cycle runs under one `threading.Lock` per key file.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Mapping

from ..errors import CorruptStoreError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .schema import load_versioned

IDEMPOTENCY_KEYS_FILENAME = "idempotency_keys.json"
STAGING_DIRNAME = "idempotency_staging"
IDEMPOTENCY_SCHEMA = 1

KEY_STATE_PENDING = "pending"
KEY_STATE_DONE = "done"
KEY_STATE_CANCELLED = "cancelled"
TERMINAL_KEY_STATES = frozenset({KEY_STATE_DONE, KEY_STATE_CANCELLED})
_KNOWN_KEY_STATES = frozenset({KEY_STATE_PENDING}) | TERMINAL_KEY_STATES

ReservationStatus = Literal["new", "in_progress", "done", "conflict"]

#: One lock per key file; created lazily, never held across an await (none here).
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def key_digest(key: str) -> str:
    """The 32-hex-character digest used for key records and staging file names."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Reservation:
    """Outcome of :meth:`IdempotencyStore.reserve`; `record` is a copy."""

    status: ReservationStatus
    record: dict[str, Any]


class IdempotencyStore:
    """Keyed-write idempotency records for one data root (see module docstring)."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit

    # -- paths and documents ---------------------------------------------

    def _keys_path(self, character: str) -> Path:
        return self._roots.character_dir(character, create=False).joinpath(
            IDEMPOTENCY_KEYS_FILENAME,
        )

    def _staging_path(self, character: str, digest: str) -> Path:
        return self._roots.character_dir(character, create=False).joinpath(
            STAGING_DIRNAME, f"{digest}.json",
        )

    def _now_iso(self) -> str:
        return self._clock.now().isoformat()

    def _load_keys(self, path: Path) -> dict[str, dict[str, Any]]:
        """Load and validate every key record; never writes."""
        document = load_versioned(
            self._store, path,
            default={"schema_version": IDEMPOTENCY_SCHEMA, "keys": {}},
        )
        records = document.get("keys")
        if not isinstance(records, dict):
            raise CorruptStoreError(
                "idempotency document has no 'keys' object", path=path,
            )
        for digest, record in records.items():
            if not isinstance(digest, str) or not digest:
                raise CorruptStoreError(f"invalid key digest {digest!r}", path=path)
            _validate_record(digest, record, path)
        return records

    def _save_keys(self, path: Path, records: Mapping[str, Mapping[str, Any]]) -> None:
        self._store.save(
            path, {"schema_version": IDEMPOTENCY_SCHEMA, "keys": dict(records)},
        )

    def _read_staging(self, character: str, key: str) -> dict[str, Any] | None:
        """The staged response of `key`, or None when there is none."""
        path = self._staging_path(character, key_digest(key))
        raw = self._store.load(path)
        if raw is None:
            return None
        if not isinstance(raw, dict) or raw.get("key") != key:
            raise CorruptStoreError(
                "staging document does not belong to this key", path=path,
            )
        response = raw.get("response")
        if response is not None and not isinstance(response, Mapping):
            raise CorruptStoreError("staged response must be an object", path=path)
        return None if response is None else dict(response)

    @staticmethod
    def _new_record(
        key: str,
        fingerprint: str,
        *,
        state: str,
        response: Mapping[str, Any] | None,
        now: str,
    ) -> dict[str, Any]:
        return {
            "key": key,
            "fingerprint": fingerprint,
            "state": state,
            "response": None if response is None else dict(response),
            "created_at": now,
            "updated_at": now,
        }

    # -- public API ------------------------------------------------------

    def reserve(
        self, character: str, key: str, fingerprint: str,
    ) -> Reservation:
        """Reserve `key` for `fingerprint`; see the module docstring for states."""
        _require_key(key)
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("fingerprint must be a nonempty string")
        digest = key_digest(key)
        path = self._keys_path(character)
        with _lock_for(path):
            records = self._load_keys(path)
            record = records.get(digest)
            if record is not None and record["key"] != key:
                raise CorruptStoreError(
                    f"key digest {digest} is recorded for another key", path=path,
                )
            if record is None or record["state"] == KEY_STATE_CANCELLED:
                record = self._new_record(
                    key, fingerprint, state=KEY_STATE_PENDING, response=None,
                    now=self._now_iso(),
                )
                records[digest] = record
                self._permit.assert_writable("save", str(path))
                self._save_keys(path, records)
                return Reservation("new", dict(record))
            if record["fingerprint"] != fingerprint:
                return Reservation("conflict", dict(record))
            if record["state"] == KEY_STATE_PENDING:
                return Reservation("in_progress", dict(record))
            staged = self._read_staging(character, key)
            if staged is not None:
                record = dict(record, response=staged)
            return Reservation("done", dict(record))

    def complete(
        self, character: str, key: str, response: Mapping[str, Any],
    ) -> None:
        """Mark a pending reservation done and stage its response.

        Terminal records are never moved again: a completed key keeps its
        first response and a cancelled key stays cancelled.
        """
        _require_key(key)
        if not isinstance(response, Mapping):
            raise ValueError("response must be a mapping")
        payload = dict(response)
        digest = key_digest(key)
        path = self._keys_path(character)
        with _lock_for(path):
            records = self._load_keys(path)
            record = records.get(digest)
            if record is None:
                raise KeyError(f"no reservation for key {key!r}")
            if record["key"] != key:
                raise CorruptStoreError(
                    f"key digest {digest} is recorded for another key", path=path,
                )
            if record["state"] in TERMINAL_KEY_STATES:
                return
            now = self._now_iso()
            updated = dict(record)
            updated.update({"state": KEY_STATE_DONE, "response": payload, "updated_at": now})
            staging = self._staging_path(character, digest)
            with self._permit.transaction("save", str(path)):
                self._store.save(
                    staging,
                    {"key": key, "response": payload, "completed_at": now},
                )
                records[digest] = updated
                self._save_keys(path, records)

    def cancel(self, character: str, key: str) -> None:
        """Cancel a pending reservation (a done key stays done)."""
        _require_key(key)
        digest = key_digest(key)
        path = self._keys_path(character)
        with _lock_for(path):
            records = self._load_keys(path)
            record = records.get(digest)
            if record is not None and record["key"] != key:
                raise CorruptStoreError(
                    f"key digest {digest} is recorded for another key", path=path,
                )
            if record is not None and record["state"] in TERMINAL_KEY_STATES:
                return
            now = self._now_iso()
            if record is None:
                updated = self._new_record(
                    key, "", state=KEY_STATE_CANCELLED, response=None, now=now,
                )
            else:
                updated = dict(record)
                updated.update({"state": KEY_STATE_CANCELLED, "updated_at": now})
            records[digest] = updated
            self._permit.assert_writable("save", str(path))
            self._save_keys(path, records)

    def lookup(self, character: str, key: str) -> dict[str, Any] | None:
        """The stored record of `key` (a copy), or None when it was never seen."""
        _require_key(key)
        digest = key_digest(key)
        path = self._keys_path(character)
        with _lock_for(path):
            records = self._load_keys(path)
            record = records.get(digest)
            if record is None:
                return None
            if record["key"] != key:
                raise CorruptStoreError(
                    f"key digest {digest} is recorded for another key", path=path,
                )
            if record["state"] == KEY_STATE_DONE:
                staged = self._read_staging(character, key)
                if staged is not None:
                    record = dict(record, response=staged)
            return dict(record)

    def purge(self, character: str, older_than: datetime) -> int:
        """Drop done/cancelled records older than `older_than` and their staging.

        Returns the number of records dropped. Pending records are never
        dropped: they are the only proof an effect may still be in flight.
        """
        if not isinstance(older_than, datetime) or older_than.utcoffset() is None:
            raise ValueError("older_than must be a timezone-aware datetime")
        path = self._keys_path(character)
        with _lock_for(path):
            records = self._load_keys(path)
            expired = [
                digest for digest, record in records.items()
                if record["state"] in TERMINAL_KEY_STATES
                and _updated_at(record) < older_than
            ]
            if not expired:
                return 0
            for digest in expired:
                self._permit.assert_writable(
                    "delete", str(self._staging_path(character, digest)),
                )
            self._permit.assert_writable("save", str(path))
            for digest in expired:
                try:
                    self._staging_path(character, digest).unlink()
                except FileNotFoundError:
                    pass
                records.pop(digest, None)
            self._save_keys(path, records)
            return len(expired)


def _validate_record(digest: str, record: Any, path: Path) -> None:
    """Reject a damaged key record instead of degrading it to "unknown"."""
    if not isinstance(record, dict):
        raise CorruptStoreError(
            f"idempotency record {digest} is not an object", path=path,
        )
    key = record.get("key")
    if not isinstance(key, str) or not key.strip():
        raise CorruptStoreError(
            f"idempotency record {digest} has no key", path=path,
        )
    if key_digest(key) != digest:
        raise CorruptStoreError(
            f"idempotency record {digest} does not match its key", path=path,
        )
    fingerprint = record.get("fingerprint")
    if not isinstance(fingerprint, str):
        raise CorruptStoreError(
            f"idempotency record {digest} has no fingerprint", path=path,
        )
    state = record.get("state")
    if not isinstance(state, str) or state not in _KNOWN_KEY_STATES:
        raise CorruptStoreError(
            f"idempotency record {digest} has an unknown state {state!r}", path=path,
        )
    response = record.get("response")
    if response is not None and not isinstance(response, Mapping):
        raise CorruptStoreError(
            f"idempotency record {digest} has a non-object response", path=path,
        )
    for field_name in ("created_at", "updated_at"):
        _parse_timestamp(record.get(field_name), digest, field_name, path)


def _parse_timestamp(value: Any, digest: str, field_name: str, path: Path) -> datetime:
    if not isinstance(value, str):
        raise CorruptStoreError(
            f"idempotency record {digest} has no {field_name}", path=path,
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CorruptStoreError(
            f"idempotency record {digest} has an invalid {field_name}", path=path,
            cause=exc,
        ) from exc
    if parsed.utcoffset() is None:
        raise CorruptStoreError(
            f"idempotency record {digest} {field_name} has no timezone", path=path,
        )
    return parsed


def _updated_at(record: Mapping[str, Any]) -> datetime:
    return datetime.fromisoformat(str(record["updated_at"]))


def _require_key(key: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("idempotency key must be a nonempty string")
    return key


__all__ = [
    "IDEMPOTENCY_KEYS_FILENAME",
    "IDEMPOTENCY_SCHEMA",
    "KEY_STATE_CANCELLED",
    "KEY_STATE_DONE",
    "KEY_STATE_PENDING",
    "STAGING_DIRNAME",
    "IdempotencyStore",
    "Reservation",
    "key_digest",
]
