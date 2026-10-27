# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Data-format manifest, migration, backup and rollback (B14).

The core never guesses a data root: every entry point takes an explicit
memory_root (one directory per character). All I/O is synchronous and the
module imports nothing from the host.

Storage formats (design §17.14.3, §17.19)
-----------------------------------------
* versioned JSON envelopes: companion_state.json (schema), memory_deletions.json
  and memory_corrections.json (schema/schema_version), source_events.json,
  idempotency_keys.json, user_directives.json (version), persona_card.json,
  persona_versions.json, evolution_proposals.json (schema_version);
* host bare arrays: facts.json, facts_archive.json, recent.json,
  reflections.json, surfaced.json, synth_backoff.json, persona_corrections.json;
* host bare objects: persona.json, scoped_tombstones.json;
* append-only ndjson: events.ndjson (journal) and outbox.ndjson;
* the journal sentinel events_applied.json (position marker, legacy event-id
  marker accepted and migrated to a full-replay marker);
* SQLite: time_indexed.db (schema_meta version row, additive migration).

Policy (frozen for B14)
-----------------------
* Unknown or newer versions are refused, never downgraded or guessed; the
  error carries the file, the detected version and a recovery hint.
* Corrupt files are refused and left untouched (restore from a verified backup).
* migrate is idempotent and stepwise: detect -> preflight -> backup -> apply ->
  verify; any failure restores the backup automatically.
* rollback restores the backup payload and then, with preserve_new=True,
  merges the records written after the backup (deletions, corrections,
  reflections, persona rows, new conversations/facts/events) so a rollback
  never loses post-switch data. Merge conflicts are reported, not silent.
* backup copies the tree verbatim and writes a manifest with per-file sha256 so
  a later rollback can prove the backup is intact.

No user data is ever touched implicitly: tests and the CLI only operate on
explicit temporary or dedicated paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

#: Current core data version written by this build.
CORE_DATA_VERSION = 1
#: Root-level marker written by a successful migration (hidden dot-file).
MANIFEST_FILENAME = ".rin_memory_manifest.json"
#: Backup manifest name (inside the backup root, outside the payload copy).
BACKUP_MANIFEST_FILENAME = "backup_manifest.json"
#: Backup payload directory (an exact copy of memory_root).
PAYLOAD_DIRNAME = "payload"
#: Single-writer lock is never copied or migrated.
LOCK_FILENAME = ".rin_memory.writer.lock"
_SQLITE_HEADER = b"SQLite format 3\x00"

STATUS_ABSENT = "absent"
STATUS_OK = "ok"
STATUS_LEGACY = "legacy"
STATUS_UNKNOWN = "unknown"
STATUS_CORRUPT = "corrupt"

_RECOVERY_HINT_UNKNOWN = (
    "refusing to downgrade: run the newer rin-memory build, or restore "
    "a verified backup created before the upgrade"
)
_RECOVERY_HINT_CORRUPT = (
    "file is corrupt and was left untouched; restore it from a verified "
    "backup or repair it manually before retrying"
)


# -- small helpers -----------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(
        path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
    )


def _load_json(path: Path) -> tuple[Any, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"unreadable: {exc}"
    if not raw.strip():
        return None, "empty file is not valid JSON"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc.msg} at line {exc.lineno}"


def _read_lines(path: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Parse an ndjson file; a torn final line is tolerated (ignored)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return None, f"unreadable: {exc}"
    rows: list[dict[str, Any]] = []
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                continue  # torn tail, write-side heals it
            return None, f"invalid JSON on line {index + 1}"
        if not isinstance(value, dict):
            return None, f"line {index + 1} is not a JSON object"
        rows.append(value)
    return rows, None


def _iter_character_dirs(memory_root: Path) -> list[Path]:
    if not memory_root.exists():
        return []
    result: list[Path] = []
    for entry in sorted(memory_root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        if ".backup-" in entry.name:
            continue
        result.append(entry)
    return result


# -- storage specs and detection --------------------------------------------


def _validate_events(rows: list[dict[str, Any]]) -> str | None:
    for index, row in enumerate(rows):
        for key in ("event_id", "type", "ts", "payload"):
            if key not in row:
                return f"event line {index + 1} has no {key!r}"
        if not isinstance(row.get("payload"), Mapping):
            return f"event line {index + 1} payload is not an object"
    return None


def _validate_outbox(rows: list[dict[str, Any]]) -> str | None:
    allowed = {"pending", "attempt", "done", "dead_letter"}
    for index, row in enumerate(rows):
        if "op_id" not in row or "type" not in row:
            return f"outbox line {index + 1} lacks op_id/type"
        if row.get("status") not in allowed:
            return f"outbox line {index + 1} has unknown status {row.get('status')!r}"
    return None


def _validate_objects(value: Any) -> str | None:
    if not isinstance(value, list):
        return "expected a JSON array"
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return f"row {index} is not an object"
    return None


def _validate_persona(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return "expected a persona object"
    for entity, doc in value.items():
        if not isinstance(doc, Mapping):
            return f"entity {entity!r} is not an object"
        if not isinstance(doc.get("facts", []), list):
            return f"entity {entity!r} facts is not an array"
    return None


def _validate_tombstones(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return "expected a subject map"
    for subject, rows in value.items():
        if not isinstance(rows, list):
            return f"subject {subject!r} is not an array"
    return None


@dataclass(frozen=True)
class StorageSpec:
    """One file whose format this build can detect and (when legacy) migrate."""

    name: str
    filename: str
    kind: str  # json_object | json_array | ndjson | sentinel | sqlite
    description: str
    current_version: int = CORE_DATA_VERSION
    version_key: str | None = None
    array_key: str | None = None
    optional: bool = True
    recovery_hint: str = _RECOVERY_HINT_CORRUPT
    validator: Callable[[Any], str | None] | None = None


#: Every per-character store the B02-B13 core reads or writes.
STORAGE_SPECS: tuple[StorageSpec, ...] = (
    StorageSpec(
        "events", "events.ndjson", "ndjson",
        "append-only journal (host line shape, optional position)",
        validator=_validate_events,
    ),
    StorageSpec(
        "sentinel", "events_applied.json", "sentinel",
        "journal replay marker; legacy event-id-only marker migrates to full replay",
    ),
    StorageSpec(
        "outbox", "outbox.ndjson", "ndjson",
        "at-least-once operation queue",
        validator=_validate_outbox,
    ),
    StorageSpec(
        "cursors", "cursors.json", "json_object",
        "per-character named ISO cursors (host bare map)",
        validator=lambda value: (
            None if isinstance(value, Mapping) and all(
                isinstance(item, str) for item in value.values()
            ) else "expected a string-to-ISO map"
        ),
    ),
    StorageSpec(
        "source_ledger", "source_events.json", "json_object",
        "source dedupe ledger",
        version_key="schema_version",
    ),
    StorageSpec(
        "idempotency", "idempotency_keys.json", "json_object",
        "request idempotency keys",
        version_key="schema_version",
    ),
    StorageSpec(
        "turn_requests", "turn_requests.json", "json_object",
        "whole-turn commit records (S02)",
        version_key="schema_version",
    ),
    StorageSpec(
        "tombstones", "scoped_tombstones.json", "json_object",
        "host scoped tombstone map",
        validator=_validate_tombstones,
    ),
    StorageSpec(
        "facts", "facts.json", "json_array",
        "active facts (host bare array or versioned envelope)",
        array_key="facts", validator=_validate_objects,
    ),
    StorageSpec(
        "facts_archive", "facts_archive.json", "json_array",
        "archived facts (host bare array or versioned envelope)",
        array_key="archive", validator=_validate_objects,
    ),
    StorageSpec(
        "recent", "recent.json", "json_array",
        "recent conversation window (host bare array)",
        validator=_validate_objects,
    ),
    StorageSpec(
        "timeindex", "time_indexed.db", "sqlite",
        "SQLite time index; schema_meta row is the version marker",
    ),
    StorageSpec(
        "deletions", "memory_deletions.json", "json_object",
        "deletion receipts, tombstones and barrier version",
        version_key="schema",
    ),
    StorageSpec(
        "corrections", "memory_corrections.json", "json_object",
        "correction records and block version",
        version_key="schema",
    ),
    StorageSpec(
        "directives", "user_directives.json", "json_object",
        "mention directives (version envelope)",
        version_key="version",
    ),
    StorageSpec(
        "state", "companion_state.json", "json_object",
        "companion state document (schema envelope)",
        version_key="schema",
    ),
    StorageSpec(
        "reflections", "reflections.json", "json_array",
        "reflection rows (host bare array)",
        validator=_validate_objects,
    ),
    StorageSpec(
        "surfaced", "surfaced.json", "json_array",
        "surfaced-reflection ledger (host bare array)",
        validator=_validate_objects,
    ),
    StorageSpec(
        "synth_backoff", "synth_backoff.json", "json_array",
        "synthesis backoff (host bare array)",
        validator=_validate_objects,
    ),
    StorageSpec(
        "growth_uses", "growth_uses.json", "json_object",
        "growth usage counters (schema envelope)",
        version_key="schema",
    ),
    StorageSpec(
        "persona", "persona.json", "json_object",
        "host persona document {entity: {facts: [...]}}",
        validator=_validate_persona,
    ),
    StorageSpec(
        "persona_corrections", "persona_corrections.json", "json_array",
        "persona correction queue (host bare array)",
        validator=_validate_objects,
    ),
    StorageSpec(
        "persona_card", "persona_card.json", "json_object",
        "current persona card (B10)",
        version_key="schema_version",
    ),
    StorageSpec(
        "persona_versions", "persona_versions.json", "json_object",
        "published persona versions (B11)",
        version_key="schema_version",
    ),
    StorageSpec(
        "evolution_proposals", "evolution_proposals.json", "json_object",
        "evolution proposals (B12)",
        version_key="schema_version",
    ),
)


@dataclass(frozen=True)
class StorageStatus:
    name: str
    filename: str
    path: str
    status: str
    version: int | None = None
    detail: str = ""
    hint: str = ""
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_ABSENT, STATUS_OK, STATUS_LEGACY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "filename": self.filename,
            "path": self.path,
            "status": self.status,
            "version": self.version,
            "detail": self.detail,
            "hint": self.hint,
            "counts": dict(self.counts),
        }


@dataclass(frozen=True)
class CharacterFormat:
    character: str
    path: str
    stores: tuple[StorageStatus, ...]
    problems: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "character": self.character,
            "path": self.path,
            "stores": [store.to_dict() for store in self.stores],
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class ManifestReport:
    memory_root: str
    characters: tuple[CharacterFormat, ...]
    problems: tuple[str, ...]
    generated_at: str
    manifest: Mapping[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "memory_root": self.memory_root,
            "generated_at": self.generated_at,
            "manifest": dict(self.manifest or {}),
            "characters": [item.to_dict() for item in self.characters],
            "problems": list(self.problems),
        }


class DataFormatManifest:
    """Detect every known store version under one explicit memory root."""

    def __init__(self, specs: Sequence[StorageSpec] = STORAGE_SPECS) -> None:
        self._specs = tuple(specs)

    @property
    def specs(self) -> tuple[StorageSpec, ...]:
        return self._specs

    def detect(self, memory_root: Path | str) -> ManifestReport:
        root = Path(memory_root).expanduser()
        characters: list[CharacterFormat] = []
        problems: list[str] = []
        for character_dir in _iter_character_dirs(root):
            stores: list[StorageStatus] = []
            character_problems: list[str] = []
            for spec in self._specs:
                status = self._detect_store(spec, character_dir / spec.filename)
                stores.append(status)
                if not status.ok:
                    label = f"{character_dir.name}/{spec.filename}"
                    character_problems.append(
                        f"{label}: {status.status} ({status.detail})",
                    )
            characters.append(CharacterFormat(
                character=character_dir.name,
                path=str(character_dir),
                stores=tuple(stores),
                problems=tuple(character_problems),
            ))
            problems.extend(character_problems)
        return ManifestReport(
            memory_root=str(root),
            characters=tuple(characters),
            problems=tuple(problems),
            generated_at=_now_iso(),
            manifest=self._read_marker(root),
        )

    def verify(self, memory_root: Path | str) -> "VerifyReport":
        root = Path(memory_root).expanduser()
        report = self.detect(root)
        per_character: dict[str, dict[str, int]] = {}
        for item in report.characters:
            counts: dict[str, int] = {"events": 0, "facts": 0, "reflections": 0,
                                      "persona_entries": 0, "deletions": 0,
                                      "corrections": 0, "state_subjects": 0}
            for store in item.stores:
                if store.name == "events":
                    counts["events"] = int(store.counts.get("lines", 0))
                elif store.name == "facts":
                    counts["facts"] = int(store.counts.get("rows", 0))
                elif store.name == "reflections":
                    counts["reflections"] = int(store.counts.get("rows", 0))
                elif store.name == "persona":
                    counts["persona_entries"] = int(store.counts.get("entries", 0))
                elif store.name == "deletions":
                    counts["deletions"] = int(store.counts.get("rows", 0))
                elif store.name == "corrections":
                    counts["corrections"] = int(store.counts.get("rows", 0))
                elif store.name == "state":
                    counts["state_subjects"] = int(store.counts.get("subjects", 0))
            per_character[item.character] = counts
        return VerifyReport(
            ok=report.ok,
            memory_root=report.memory_root,
            generated_at=report.generated_at,
            characters=len(report.characters),
            per_character=per_character,
            problems=list(report.problems),
            manifest=dict(report.manifest or {}),
        )

    # -- per-file detection ---------------------------------------------

    def _detect_store(self, spec: StorageSpec, path: Path) -> StorageStatus:
        if not path.exists():
            return StorageStatus(
                name=spec.name, filename=spec.filename, path=str(path),
                status=STATUS_ABSENT,
            )
        if spec.kind == "ndjson":
            return self._detect_ndjson(spec, path)
        if spec.kind == "sqlite":
            return self._detect_sqlite(spec, path)
        if spec.kind == "sentinel":
            return self._detect_sentinel(spec, path)
        return self._detect_json(spec, path)

    def _detect_json(self, spec: StorageSpec, path: Path) -> StorageStatus:
        value, error = _load_json(path)
        if error:
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_CORRUPT,
                detail=error, hint=spec.recovery_hint,
            )
        if spec.kind == "json_array":
            version = spec.current_version
            items: Any = value
            if isinstance(value, Mapping):
                version = value.get("schema_version")
                if isinstance(version, bool) or not isinstance(version, int):
                    return StorageStatus(
                        spec.name, spec.filename, str(path), STATUS_UNKNOWN,
                        detail="envelope without integer schema_version",
                        hint=spec.recovery_hint,
                    )
                items = value.get(spec.array_key or "")
            elif not isinstance(value, list):
                return StorageStatus(
                    spec.name, spec.filename, str(path), STATUS_CORRUPT,
                    detail="expected an array or versioned envelope",
                    hint=spec.recovery_hint,
                )
            counts = {"rows": len(items) if isinstance(items, list) else 0}
            return self._version_status(spec, path, version, items, counts)
        if not isinstance(value, Mapping):
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_CORRUPT,
                detail="expected a JSON object",
                hint=spec.recovery_hint,
            )
        version = spec.current_version
        if spec.version_key is not None:
            raw_version = value.get(spec.version_key)
            if raw_version is None and spec.version_key == "schema":
                raw_version = value.get("schema_version")
            if raw_version is None and spec.version_key == "schema_version":
                raw_version = value.get("schema")
            if raw_version is None and spec.name == "state":
                raw_version = spec.current_version  # host may omit the default
            if isinstance(raw_version, bool) or not isinstance(raw_version, int):
                return StorageStatus(
                    spec.name, spec.filename, str(path), STATUS_UNKNOWN,
                    detail=(
                        f"missing or non-integer {spec.version_key!r}"
                        if raw_version is not None else "missing version marker"
                    ),
                    hint=(
                        "host bare-key mapping is not migrated by B14; export "
                        "it or start a fresh store" if spec.name == "idempotency"
                        else spec.recovery_hint
                    ),
                )
            version = raw_version
        counts = self._counts_for(spec, value)
        return self._version_status(spec, path, version, value, counts)

    def _version_status(
        self, spec: StorageSpec, path: Path, version: int, payload: Any,
        counts: Mapping[str, int],
    ) -> StorageStatus:
        if spec.validator is not None:
            error = spec.validator(payload)
            if error:
                return StorageStatus(
                    spec.name, spec.filename, str(path), STATUS_CORRUPT,
                    version=version, detail=error, hint=spec.recovery_hint,
                    counts=counts,
                )
        if version > spec.current_version:
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_UNKNOWN,
                version=version,
                detail=f"version {version} is newer than {spec.current_version}",
                hint=spec.recovery_hint, counts=counts,
            )
        if version < spec.current_version:
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_LEGACY,
                version=version,
                detail=f"version {version} needs migration to {spec.current_version}",
                hint="run rin-memory migrate --memory-root <root>",
                counts=counts,
            )
        return StorageStatus(
            spec.name, spec.filename, str(path), STATUS_OK,
            version=version, counts=counts,
        )

    def _detect_ndjson(self, spec: StorageSpec, path: Path) -> StorageStatus:
        rows, error = _read_lines(path)
        if error:
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_CORRUPT,
                detail=error, hint=spec.recovery_hint,
            )
        assert rows is not None
        if spec.validator is not None:
            error = spec.validator(rows)
            if error:
                return StorageStatus(
                    spec.name, spec.filename, str(path), STATUS_CORRUPT,
                    detail=error, hint=spec.recovery_hint,
                )
        return StorageStatus(
            spec.name, spec.filename, str(path), STATUS_OK,
            version=spec.current_version, counts={"lines": len(rows)},
        )

    def _detect_sentinel(self, spec: StorageSpec, path: Path) -> StorageStatus:
        value, error = _load_json(path)
        if error:
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_CORRUPT,
                detail=error, hint=spec.recovery_hint,
            )
        if not isinstance(value, Mapping):
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_CORRUPT,
                detail="expected a sentinel object", hint=spec.recovery_hint,
            )
        position = value.get("last_applied_position")
        if isinstance(position, int) and not isinstance(position, bool):
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_OK,
                version=spec.current_version, counts={"position": position},
            )
        legacy_id = value.get("last_applied_event_id")
        if isinstance(legacy_id, str) and legacy_id.strip():
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_LEGACY,
                version=0,
                detail="legacy event-id sentinel without a position; migrate to full replay",
                hint="run rin-memory migrate --memory-root <root>",
            )
        if not value:
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_LEGACY, version=0,
                detail="empty sentinel; migrate to position -1",
                hint="run rin-memory migrate --memory-root <root>",
            )
        return StorageStatus(
            spec.name, spec.filename, str(path), STATUS_CORRUPT,
            detail="sentinel has neither position nor legacy event id",
            hint=spec.recovery_hint,
        )

    def _detect_sqlite(self, spec: StorageSpec, path: Path) -> StorageStatus:
        try:
            header = path.read_bytes()[:16]
        except OSError as exc:
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_CORRUPT,
                detail=f"unreadable: {exc}", hint=spec.recovery_hint,
            )
        if header != _SQLITE_HEADER:
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_CORRUPT,
                detail="not a SQLite database", hint=spec.recovery_hint,
            )
        try:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        except sqlite3.DatabaseError as exc:
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_CORRUPT,
                detail=f"cannot open SQLite database: {exc}",
                hint=spec.recovery_hint,
            )
        try:
            tables = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'",
                )
            }
            if "schema_meta" not in tables:
                return StorageStatus(
                    spec.name, spec.filename, str(path), STATUS_LEGACY,
                    version=0,
                    detail="host database without schema_meta; additive migration required",
                    hint="run rin-memory migrate --memory-root <root>",
                )
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'",
            ).fetchone()
            if row is None:
                return StorageStatus(
                    spec.name, spec.filename, str(path), STATUS_CORRUPT,
                    detail="schema_meta has no schema_version row",
                    hint=spec.recovery_hint,
                )
            try:
                version = int(str(row[0]).strip())
            except (TypeError, ValueError):
                return StorageStatus(
                    spec.name, spec.filename, str(path), STATUS_CORRUPT,
                    detail=f"non-integer schema_version {row[0]!r}",
                    hint=spec.recovery_hint,
                )
        except sqlite3.DatabaseError as exc:
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_CORRUPT,
                detail=f"cannot read SQLite schema: {exc}",
                hint=spec.recovery_hint,
            )
        finally:
            connection.close()
        if version > spec.current_version:
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_UNKNOWN,
                version=version,
                detail=f"version {version} is newer than {spec.current_version}",
                hint=spec.recovery_hint,
            )
        if version < spec.current_version:
            return StorageStatus(
                spec.name, spec.filename, str(path), STATUS_LEGACY,
                version=version, detail="time index version needs migration",
                hint="run rin-memory migrate --memory-root <root>",
            )
        return StorageStatus(
            spec.name, spec.filename, str(path), STATUS_OK,
            version=version,
        )

    @staticmethod
    def _counts_for(spec: StorageSpec, value: Mapping[str, Any]) -> dict[str, int]:
        if spec.name in ("deletions", "corrections"):
            rows = value.get(
                "deletions" if spec.name == "deletions" else "corrections",
            )
            return {"rows": len(rows) if isinstance(rows, list) else 0}
        if spec.name == "state":
            subjects = value.get("subjects")
            return {"subjects": len(subjects) if isinstance(subjects, Mapping) else 0}
        if spec.name == "persona":
            entries = 0
            for doc in value.values():
                if isinstance(doc, Mapping) and isinstance(doc.get("facts"), list):
                    entries += len(doc["facts"])
            return {"entries": entries}
        if spec.name == "directives":
            rows = value.get("directives")
            return {"rows": len(rows) if isinstance(rows, list) else 0}
        return {}

    @staticmethod
    def _read_marker(root: Path) -> dict[str, Any] | None:
        value, error = _load_json(root / MANIFEST_FILENAME)
        if error or not isinstance(value, Mapping):
            return None
        return dict(value)


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    memory_root: str
    generated_at: str
    characters: int
    per_character: Mapping[str, Mapping[str, int]]
    problems: Sequence[str]
    manifest: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "memory_root": self.memory_root,
            "generated_at": self.generated_at,
            "characters": self.characters,
            "per_character": {
                name: dict(counts) for name, counts in self.per_character.items()
            },
            "problems": list(self.problems),
            "manifest": dict(self.manifest),
        }


# -- backup ------------------------------------------------------------------


@dataclass(frozen=True)
class BackupReport:
    ok: bool
    memory_root: str
    backup_root: str
    files: int
    bytes: int
    created_at: str
    manifest_path: str
    format_manifest: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "memory_root": self.memory_root,
            "backup_root": self.backup_root,
            "files": self.files,
            "bytes": self.bytes,
            "created_at": self.created_at,
            "manifest_path": self.manifest_path,
            "format_manifest": dict(self.format_manifest),
        }


class MigrationError(Exception):
    """A migration/rollback refused or failed; carries a report and code."""

    def __init__(
        self, message: str, *, code: str = "migration_failed",
        report: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = str(message)
        self.code = code
        self.report = dict(report or {})


def _iter_payload_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name == LOCK_FILENAME or path.name.startswith("."):
            continue
        yield path


def backup(
    memory_root: Path | str, backup_root: Path | str,
    *, manifest: DataFormatManifest | None = None,
) -> BackupReport:
    """Copy one explicit memory root verbatim and write its integrity manifest."""
    source = Path(memory_root).expanduser()
    target = Path(backup_root).expanduser()
    if not source.is_dir():
        raise MigrationError(
            f"memory root does not exist: {str(source)!r}", code="bad_argument",
        )
    try:
        source_resolved = source.resolve()
        target_resolved = target.resolve()
    except OSError as exc:
        raise MigrationError(f"cannot resolve paths: {exc}", code="bad_argument") from exc
    if target_resolved == source_resolved or source_resolved in target_resolved.parents:
        raise MigrationError(
            "backup root must not be inside the memory root", code="bad_argument",
        )
    payload = target / PAYLOAD_DIRNAME
    if payload.exists() and any(payload.iterdir()):
        raise MigrationError(
            f"backup payload already exists and is not empty: {str(payload)!r}",
            code="backup_exists",
        )
    created_at = _now_iso()
    files: list[dict[str, Any]] = []
    total_bytes = 0
    payload.mkdir(parents=True, exist_ok=True)
    for path in _iter_payload_files(source):
        relative = path.relative_to(source)
        destination = payload / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        size = destination.stat().st_size
        total_bytes += size
        files.append({
            "path": relative.as_posix(),
            "sha256": _sha256_file(destination),
            "bytes": size,
        })
    format_manifest = (manifest or DataFormatManifest()).detect(source).to_dict()
    manifest_payload = {
        "schema_version": CORE_DATA_VERSION,
        "created_at": created_at,
        "source": str(source_resolved),
        "core_data_version": CORE_DATA_VERSION,
        "files": files,
        "format_manifest": format_manifest,
    }
    manifest_path = target / BACKUP_MANIFEST_FILENAME
    _atomic_write_json(manifest_path, manifest_payload)
    return BackupReport(
        ok=True,
        memory_root=str(source_resolved),
        backup_root=str(target_resolved),
        files=len(files),
        bytes=total_bytes,
        created_at=created_at,
        manifest_path=str(manifest_path),
        format_manifest=format_manifest,
    )


def _read_backup_manifest(backup_root: Path) -> dict[str, Any]:
    path = backup_root / BACKUP_MANIFEST_FILENAME
    value, error = _load_json(path)
    if error or not isinstance(value, Mapping):
        raise MigrationError(
            f"backup manifest is missing or unreadable: {str(path)!r}",
            code="backup_invalid",
        )
    files = value.get("files")
    if not isinstance(files, list):
        raise MigrationError(
            "backup manifest has no files list", code="backup_invalid",
        )
    return dict(value)


def _verify_backup(backup_root: Path, manifest_payload: Mapping[str, Any]) -> list[str]:
    """Return mismatched paths; an empty list means the backup is intact."""
    payload = backup_root / PAYLOAD_DIRNAME
    problems: list[str] = []
    for item in manifest_payload.get("files") or ():
        if not isinstance(item, Mapping):
            problems.append("<invalid manifest entry>")
            continue
        relative = str(item.get("path") or "")
        expected = str(item.get("sha256") or "")
        target = payload / relative
        if not relative or not target.is_file():
            problems.append(f"missing: {relative}")
            continue
        if expected and _sha256_file(target) != expected:
            problems.append(f"hash mismatch: {relative}")
    return problems


# -- migration steps ---------------------------------------------------------


@dataclass
class MigrationContext:
    memory_root: Path
    backup_root: Path | None
    manifest_before: ManifestReport
    manifest: DataFormatManifest


@dataclass(frozen=True)
class MigrationStep:
    name: str
    description: str
    needs_apply: Callable[[MigrationContext], bool]
    apply: Callable[[MigrationContext], dict[str, Any]]
    verify: Callable[[MigrationContext], None] | None = None


def _legacy_sentinel_characters(context: MigrationContext) -> list[Path]:
    result: list[Path] = []
    for character in context.manifest_before.characters:
        for store in character.stores:
            if store.name == "sentinel" and store.status == STATUS_LEGACY:
                result.append(Path(character.path))
                break
    return result


def _legacy_timeindex_characters(context: MigrationContext) -> list[Path]:
    result: list[Path] = []
    for character in context.manifest_before.characters:
        for store in character.stores:
            if store.name == "timeindex" and store.status == STATUS_LEGACY:
                result.append(Path(character.path))
                break
    return result


def _sentinel_needs_apply(context: MigrationContext) -> bool:
    return bool(_legacy_sentinel_characters(context))


def _sentinel_apply(context: MigrationContext) -> dict[str, Any]:
    migrated: list[str] = []
    for character_dir in _legacy_sentinel_characters(context):
        path = character_dir / "events_applied.json"
        value, error = _load_json(path)
        legacy_id = value.get("last_applied_event_id") if isinstance(value, Mapping) else None
        _atomic_write_json(path, {
            "schema_version": CORE_DATA_VERSION,
            "last_applied_position": -1,
            "last_applied_event_id": legacy_id,
            "ts": _now_iso(),
        })
        migrated.append(character_dir.name)
    return {"characters": migrated}


def _sentinel_verify(context: MigrationContext) -> None:
    report = context.manifest.detect(context.memory_root)
    remaining = [
        character.character
        for character in report.characters
        for store in character.stores
        if store.name == "sentinel" and store.status not in (STATUS_OK, STATUS_ABSENT)
    ]
    if remaining:
        raise MigrationError(
            f"sentinel still not migrated: {remaining}", code="verify_failed",
        )


def _timeindex_needs_apply(context: MigrationContext) -> bool:
    return bool(_legacy_timeindex_characters(context))


def _timeindex_apply(context: MigrationContext) -> dict[str, Any]:
    migrated: list[str] = []
    for character_dir in _legacy_timeindex_characters(context):
        path = character_dir / "time_indexed.db"
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT)",
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(CORE_DATA_VERSION),),
            )
            connection.execute("COMMIT")
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()
        migrated.append(character_dir.name)
    return {"characters": migrated}


def _timeindex_verify(context: MigrationContext) -> None:
    report = context.manifest.detect(context.memory_root)
    remaining = [
        character.character
        for character in report.characters
        for store in character.stores
        if store.name == "timeindex" and store.status not in (STATUS_OK, STATUS_ABSENT)
    ]
    if remaining:
        raise MigrationError(
            f"time index still not migrated: {remaining}", code="verify_failed",
        )


def _stamp_needs_apply(context: MigrationContext) -> bool:
    marker = context.manifest_before.manifest
    return not marker or int(marker.get("schema_version") or 0) != CORE_DATA_VERSION


def _stamp_apply(context: MigrationContext) -> dict[str, Any]:
    payload = {
        "schema_version": CORE_DATA_VERSION,
        "core_data_version": CORE_DATA_VERSION,
        "migrated_at": _now_iso(),
        "memory_root": str(context.memory_root),
        "backup_root": str(context.backup_root) if context.backup_root else None,
        "characters": [item.character for item in context.manifest_before.characters],
    }
    _atomic_write_json(context.memory_root / MANIFEST_FILENAME, payload)
    return {"marker": str(context.memory_root / MANIFEST_FILENAME)}


def _stamp_verify(context: MigrationContext) -> None:
    value, error = _load_json(context.memory_root / MANIFEST_FILENAME)
    if error or not isinstance(value, Mapping):
        raise MigrationError("migration marker missing after apply", code="verify_failed")
    if int(value.get("schema_version") or 0) != CORE_DATA_VERSION:
        raise MigrationError("migration marker has the wrong version", code="verify_failed")


DEFAULT_STEPS: tuple[MigrationStep, ...] = (
    MigrationStep(
        "sentinel_position",
        "rewrite legacy event-id-only journal sentinels to position -1 (full replay)",
        _sentinel_needs_apply, _sentinel_apply, _sentinel_verify,
    ),
    MigrationStep(
        "timeindex_schema_meta",
        "add the additive schema_meta version row to host SQLite time indexes",
        _timeindex_needs_apply, _timeindex_apply, _timeindex_verify,
    ),
    MigrationStep(
        "manifest_stamp",
        "write the root data-format manifest marker",
        _stamp_needs_apply, _stamp_apply, _stamp_verify,
    ),
)


@dataclass(frozen=True)
class MigrationStepResult:
    name: str
    status: str  # applied | skipped | failed | planned
    detail: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": dict(self.detail),
            "error": self.error,
        }


@dataclass(frozen=True)
class MigrationReport:
    ok: bool
    dry_run: bool
    memory_root: str
    backup_root: str | None
    steps: tuple[MigrationStepResult, ...]
    manifest_before: Mapping[str, Any]
    manifest_after: Mapping[str, Any] | None = None
    error: str = ""
    rolled_back: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "memory_root": self.memory_root,
            "backup_root": self.backup_root,
            "steps": [step.to_dict() for step in self.steps],
            "manifest_before": dict(self.manifest_before),
            "manifest_after": (
                dict(self.manifest_after) if self.manifest_after else None
            ),
            "error": self.error,
            "rolled_back": self.rolled_back,
        }


class MigrationPlan:
    """detect -> preflight -> backup -> apply -> verify -> rollback on failure."""

    def __init__(
        self,
        steps: Sequence[MigrationStep] = DEFAULT_STEPS,
        manifest: DataFormatManifest | None = None,
    ) -> None:
        self._steps = tuple(steps)
        self._manifest = manifest or DataFormatManifest()

    @property
    def steps(self) -> tuple[MigrationStep, ...]:
        return self._steps

    def run(
        self,
        memory_root: Path | str,
        *,
        dry_run: bool = False,
        backup_root: Path | str | None = None,
    ) -> MigrationReport:
        root = Path(memory_root).expanduser()
        if not root.exists():
            raise MigrationError(
                f"memory root does not exist: {str(root)!r}", code="bad_argument",
            )
        before = self._manifest.detect(root)
        before_dict = before.to_dict()
        if before.problems:
            raise MigrationError(
                "refusing to migrate: unknown or corrupt data format",
                code="unknown_schema",
                report={
                    "ok": False,
                    "memory_root": str(root),
                    "problems": list(before.problems),
                    "manifest": before_dict,
                },
            )
        explicit_backup = Path(backup_root).expanduser() if backup_root else None
        if explicit_backup is None and not dry_run:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            explicit_backup = root.parent / f"{root.name}.backup-{stamp}"
        context = MigrationContext(
            memory_root=root, backup_root=explicit_backup,
            manifest_before=before, manifest=self._manifest,
        )
        planned: list[MigrationStep] = [
            step for step in self._steps if step.needs_apply(context)
        ]
        if dry_run:
            return MigrationReport(
                ok=True, dry_run=True, memory_root=str(root),
                backup_root=str(explicit_backup) if explicit_backup else None,
                steps=tuple(
                    MigrationStepResult(step.name, "planned",
                                        {"description": step.description})
                    for step in planned
                ),
                manifest_before=before_dict,
            )
        if not planned:
            # Nothing to migrate: do not create an empty backup or touch the root.
            return MigrationReport(
                ok=True, dry_run=False, memory_root=str(root),
                backup_root=None,
                steps=tuple(
                    MigrationStepResult(step.name, "skipped",
                                        {"description": step.description})
                    for step in self._steps
                ),
                manifest_before=before_dict,
                manifest_after=before_dict,
            )
        assert explicit_backup is not None
        backup_report = backup(root, explicit_backup, manifest=self._manifest)
        results: list[MigrationStepResult] = []
        try:
            for step in self._steps:
                if not step.needs_apply(context):
                    results.append(MigrationStepResult(
                        step.name, "skipped", {"description": step.description},
                    ))
                    continue
                detail = step.apply(context)
                if step.verify is not None:
                    step.verify(context)
                results.append(MigrationStepResult(
                    step.name, "applied",
                    {"description": step.description, **dict(detail)},
                ))
            after = self._manifest.verify(root)
            if not after.ok:
                raise MigrationError(
                    f"post-migration verify failed: {list(after.problems)}",
                    code="verify_failed",
                )
            return MigrationReport(
                ok=True, dry_run=False, memory_root=str(root),
                backup_root=str(explicit_backup), steps=tuple(results),
                manifest_before=before_dict, manifest_after=after.to_dict(),
            )
        except Exception as exc:  # noqa: BLE001 - restore then report
            failure = (
                exc.message if isinstance(exc, MigrationError)
                else f"{type(exc).__name__}: {exc}"
            )
            restored = _restore_payload(explicit_backup, root)
            results.append(MigrationStepResult(
                "rollback_on_failure", "applied", {"restored_files": restored},
            ))
            raise MigrationError(
                f"migration failed and was rolled back: {failure}",
                code="apply_failed",
                report=MigrationReport(
                    ok=False, dry_run=False, memory_root=str(root),
                    backup_root=str(explicit_backup), steps=tuple(results),
                    manifest_before=before_dict,
                    manifest_after=self._manifest.detect(root).to_dict(),
                    error=failure, rolled_back=True,
                ).to_dict(),
            ) from exc


def migrate(
    memory_root: Path | str,
    *,
    dry_run: bool = False,
    backup_root: Path | str | None = None,
    plan: MigrationPlan | None = None,
) -> MigrationReport:
    """Migrate one explicit root; idempotent, stepwise, auto-rollback on failure."""
    return (plan or MigrationPlan()).run(
        memory_root, dry_run=dry_run, backup_root=backup_root,
    )


# -- rollback ----------------------------------------------------------------


@dataclass(frozen=True)
class RollbackReport:
    ok: bool
    memory_root: str
    backup_root: str
    preserve_new: bool
    restored_files: int
    kept_current: tuple[str, ...]
    merged: Mapping[str, Any]
    conflicts: tuple[str, ...]
    verify: Mapping[str, Any]
    backup_verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "memory_root": self.memory_root,
            "backup_root": self.backup_root,
            "preserve_new": self.preserve_new,
            "restored_files": self.restored_files,
            "kept_current": list(self.kept_current),
            "merged": dict(self.merged),
            "conflicts": list(self.conflicts),
            "verify": dict(self.verify),
            "backup_verified": self.backup_verified,
        }


def _restore_payload(backup_root: Path, memory_root: Path) -> int:
    payload = backup_root / PAYLOAD_DIRNAME
    if not payload.is_dir():
        raise MigrationError(
            f"backup payload missing: {str(payload)!r}", code="backup_invalid",
        )
    restored = 0
    for source in _iter_payload_files(payload):
        relative = source.relative_to(payload)
        destination = memory_root / relative
        _atomic_write_bytes(destination, source.read_bytes())
        restored += 1
    return restored


def _capture_current(memory_root: Path) -> dict[str, dict[str, Any]]:
    """Read the merge-relevant files before the backup restore overwrites them."""
    captured: dict[str, dict[str, Any]] = {}
    for character_dir in _iter_character_dirs(memory_root):
        entry: dict[str, Any] = {}
        for filename in (
            "memory_deletions.json", "memory_corrections.json",
            "reflections.json", "persona.json", "persona_corrections.json",
            "recent.json", "facts.json", "facts_archive.json",
            "growth_uses.json", "surfaced.json", "cursors.json",
            "source_events.json", "scoped_tombstones.json",
            "user_directives.json", "companion_state.json",
            "idempotency_keys.json", "turn_requests.json",
        ):
            value, error = _load_json(character_dir / filename)
            if error is None and value is not None:
                entry[filename] = value
        for filename in ("events.ndjson", "outbox.ndjson"):
            rows, error = _read_lines(character_dir / filename)
            if error is None and rows:
                entry[filename] = rows
        captured[character_dir.name] = entry
    return captured


def _merge_deletions(restored: Any, current: Any) -> tuple[Any, list[str]]:
    base = dict(restored) if isinstance(restored, Mapping) else {}
    extra = current if isinstance(current, Mapping) else {}
    merged = {
        "schema": CORE_DATA_VERSION,
        "version": max(int(base.get("version") or 0), int(extra.get("version") or 0)),
        "deletions": [],
        "tombstones": [],
    }
    conflicts: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for row in list(base.get("deletions") or []) + list(extra.get("deletions") or []):
        if not isinstance(row, Mapping):
            continue
        deletion_id = str(row.get("deletion_id") or "")
        if not deletion_id:
            continue
        existing = by_id.get(deletion_id)
        if existing is None:
            by_id[deletion_id] = dict(row)
            continue
        if existing != dict(row):
            conflicts.append(f"deletion {deletion_id}: current record wins")
        by_id[deletion_id] = {**existing, **dict(row)}
    merged["deletions"] = list(by_id.values())
    tombstones: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in list(base.get("tombstones") or []) + list(extra.get("tombstones") or []):
        if not isinstance(row, Mapping):
            continue
        key = (
            str(row.get("type") or ""),
            str(row.get("id") or ""),
            str(row.get("text_hash") or ""),
        )
        if key not in tombstones or str(row.get("at") or "") >= str(
            tombstones[key].get("at") or "",
        ):
            tombstones[key] = dict(row)
    merged["tombstones"] = list(tombstones.values())
    return merged, conflicts


def _merge_corrections(restored: Any, current: Any) -> tuple[Any, list[str]]:
    base = dict(restored) if isinstance(restored, Mapping) else {}
    extra = current if isinstance(current, Mapping) else {}
    merged = {
        "schema": CORE_DATA_VERSION,
        "version": max(int(base.get("version") or 0), int(extra.get("version") or 0)),
        "corrections": [],
    }
    conflicts: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for row in list(base.get("corrections") or []) + list(extra.get("corrections") or []):
        if not isinstance(row, Mapping):
            continue
        correction_id = str(row.get("correction_id") or "")
        if not correction_id:
            continue
        existing = by_id.get(correction_id)
        if existing is None:
            by_id[correction_id] = dict(row)
            continue
        if existing != dict(row):
            conflicts.append(f"correction {correction_id}: current record wins")
        by_id[correction_id] = {**existing, **dict(row)}
    merged["corrections"] = list(by_id.values())
    return merged, conflicts


def _merge_array(restored: Any, current: Any, id_key: str) -> tuple[Any, list[str]]:
    base = list(restored) if isinstance(restored, list) else []
    extra = list(current) if isinstance(current, list) else []
    merged: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    conflicts: list[str] = []
    for row in base + extra:
        if not isinstance(row, Mapping):
            continue
        row_dict = dict(row)
        key = str(row_dict.get(id_key) or "")
        if not key:
            merged.append(row_dict)
            continue
        if key in index:
            position = index[key]
            if merged[position] != row_dict:
                conflicts.append(f"{id_key} {key}: current record wins")
            merged[position] = {**merged[position], **row_dict}
        else:
            index[key] = len(merged)
            merged.append(row_dict)
    return merged, conflicts


def _merge_persona(restored: Any, current: Any) -> tuple[Any, list[str]]:
    base = dict(restored) if isinstance(restored, Mapping) else {}
    extra = current if isinstance(current, Mapping) else {}
    merged: dict[str, Any] = {}
    conflicts: list[str] = []
    for entity in list(base) + [item for item in extra if item not in base]:
        base_doc = base.get(entity) if isinstance(base.get(entity), Mapping) else {}
        extra_doc = extra.get(entity) if isinstance(extra.get(entity), Mapping) else {}
        base_facts = list(base_doc.get("facts") or [])
        extra_facts = list(extra_doc.get("facts") or [])
        merged_facts, fact_conflicts = _merge_array(base_facts, extra_facts, "id")
        conflicts.extend(f"persona {entity}: {item}" for item in fact_conflicts)
        merged[entity] = {**dict(base_doc), **dict(extra_doc), "facts": merged_facts}
    return merged, conflicts


def _merge_dict(restored: Any, current: Any) -> tuple[Any, list[str]]:
    base = dict(restored) if isinstance(restored, Mapping) else {}
    extra = dict(current) if isinstance(current, Mapping) else {}
    conflicts: list[str] = []
    for key, value in extra.items():
        if key in base and base[key] != value:
            conflicts.append(f"key {key!r}: current value wins")
    return {**base, **extra}, conflicts


def _merge_tombstone_map(restored: Any, current: Any) -> tuple[Any, list[str]]:
    base = dict(restored) if isinstance(restored, Mapping) else {}
    extra = dict(current) if isinstance(current, Mapping) else {}
    merged: dict[str, Any] = {}
    for subject in set(base) | set(extra):
        rows, _conflicts = _merge_array(
            base.get(subject) or [], extra.get(subject) or [], "id",
        )
        merged[subject] = rows
    return merged, []


def _merge_ndjson(
    restored: Any, current: Any, id_key: str,
) -> tuple[Any, list[str]]:
    base = list(restored) if isinstance(restored, list) else []
    extra = list(current) if isinstance(current, list) else []
    merged: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    conflicts: list[str] = []
    for row in base + extra:
        if not isinstance(row, Mapping):
            continue
        row_dict = dict(row)
        key = str(row_dict.get(id_key) or "")
        if key and key in positions:
            position = positions[key]
            if merged[position] != row_dict:
                conflicts.append(f"{id_key} {key}: current line wins")
                merged[position] = {**merged[position], **row_dict}
            continue
        if key:
            positions[key] = len(merged)
        merged.append(row_dict)
    return merged, conflicts


def _merge_events(restored: Any, current: Any) -> tuple[Any, list[str]]:
    base = list(restored) if isinstance(restored, list) else []
    extra = list(current) if isinstance(current, list) else []
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    conflicts: list[str] = []
    for row in base + extra:
        if not isinstance(row, Mapping):
            continue
        row_dict = dict(row)
        event_id = str(row_dict.get("event_id") or "")
        if event_id and event_id in seen:
            continue
        if event_id:
            seen.add(event_id)
        merged.append(row_dict)
    # Renumber positions for lines that carry one so replay order stays linear.
    next_position = 0
    for row in merged:
        if isinstance(row.get("position"), int):
            row["position"] = next_position
            next_position += 1
    return merged, conflicts


def _merge_state(restored: Any, current: Any) -> tuple[Any, list[str]]:
    base = dict(restored) if isinstance(restored, Mapping) else {}
    extra = current if isinstance(current, Mapping) else {}
    merged = {
        "schema": CORE_DATA_VERSION,
        "character": base.get("character") or extra.get("character"),
        "subjects": {},
    }
    conflicts: list[str] = []
    base_subjects = base.get("subjects") if isinstance(base.get("subjects"), Mapping) else {}
    extra_subjects = extra.get("subjects") if isinstance(extra.get("subjects"), Mapping) else {}
    for subject in set(base_subjects) | set(extra_subjects):
        base_row = base_subjects.get(subject) if isinstance(base_subjects.get(subject), Mapping) else {}
        extra_row = extra_subjects.get(subject) if isinstance(extra_subjects.get(subject), Mapping) else {}
        base_version = int(base_row.get("version") or 0)
        extra_version = int(extra_row.get("version") or 0)
        if base_row and extra_row and base_version > extra_version:
            conflicts.append(
                f"state {subject}: backup version {base_version} > current "
                f"{extra_version}; backup row wins",
            )
            merged["subjects"][subject] = dict(base_row)
        else:
            merged["subjects"][subject] = {**dict(base_row), **dict(extra_row)}
    return merged, conflicts


#: filename -> merge function; only these files preserve post-backup records.
_MERGE_RULES: dict[str, Callable[[Any, Any], tuple[Any, list[str]]]] = {
    "memory_deletions.json": _merge_deletions,
    "memory_corrections.json": _merge_corrections,
    "reflections.json": lambda base, extra: _merge_array(base, extra, "id"),
    "persona.json": _merge_persona,
    "persona_corrections.json": lambda base, extra: _merge_array(base, extra, "correction_id"),
    "recent.json": lambda base, extra: _merge_array(base, extra, "id"),
    "facts.json": lambda base, extra: _merge_array(base, extra, "id"),
    "facts_archive.json": lambda base, extra: _merge_array(base, extra, "id"),
    "surfaced.json": lambda base, extra: _merge_array(base, extra, "reflection_id"),
    "cursors.json": _merge_dict,
    "source_events.json": lambda base, extra: _merge_dict(
        (base or {}).get("entries") if isinstance(base, Mapping) else {},
        (extra or {}).get("entries") if isinstance(extra, Mapping) else {},
    ),
    "scoped_tombstones.json": _merge_tombstone_map,
    "companion_state.json": _merge_state,
    "events.ndjson": _merge_events,
    "outbox.ndjson": lambda base, extra: _merge_ndjson(base, extra, "op_id"),
    "user_directives.json": lambda base, extra: _merge_array(
        (base or {}).get("directives") if isinstance(base, Mapping) else [],
        (extra or {}).get("directives") if isinstance(extra, Mapping) else [],
        "term",
    ),
    "growth_uses.json": lambda base, extra: _merge_array(
        (base or {}).get("uses") if isinstance(base, Mapping) else [],
        (extra or {}).get("uses") if isinstance(extra, Mapping) else [],
        "id",
    ),
    "idempotency_keys.json": lambda base, extra: _merge_dict(
        (base or {}).get("keys") if isinstance(base, Mapping) else {},
        (extra or {}).get("keys") if isinstance(extra, Mapping) else {},
    ),
    # S02: keep post-backup whole-turn commits (and any deletion recorded after
    # the backup) by merging whole records keyed by request id.
    "turn_requests.json": lambda base, extra: _merge_dict(
        (base or {}).get("records") if isinstance(base, Mapping) else {},
        (extra or {}).get("records") if isinstance(extra, Mapping) else {},
    ),
}


def _merge_current(
    memory_root: Path, captured: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    merged_counts: dict[str, int] = {}
    conflicts: list[str] = []
    for character, files in captured.items():
        character_dir = memory_root / character
        for filename, current_value in files.items():
            rule = _MERGE_RULES.get(filename)
            if rule is None:
                continue
            path = character_dir / filename
            if filename in ("events.ndjson", "outbox.ndjson"):
                restored_value, error = _read_lines(path)
            else:
                restored_value, error = _load_json(path)
            if error is not None:
                conflicts.append(f"{character}/{filename}: restored file unreadable")
                continue
            if filename in ("events.ndjson", "outbox.ndjson"):
                merged_value, file_conflicts = rule(restored_value or [], current_value)
                _atomic_write_text(
                    path,
                    "".join(
                        json.dumps(row, ensure_ascii=False) + "\n"
                        for row in merged_value
                    ),
                )
            else:
                merged_value, file_conflicts = rule(restored_value, current_value)
                if filename == "source_events.json":
                    merged_value = {"schema": CORE_DATA_VERSION, "entries": merged_value}
                elif filename == "user_directives.json":
                    merged_value = {"version": CORE_DATA_VERSION,
                                    "directives": merged_value}
                elif filename == "growth_uses.json":
                    merged_value = {"schema": CORE_DATA_VERSION,
                                    "version": int(
                                        (restored_value or {}).get("version") or 0
                                        if isinstance(restored_value, Mapping) else 0,
                                    ),
                                    "uses": merged_value}
                elif filename == "idempotency_keys.json":
                    merged_value = {"schema_version": CORE_DATA_VERSION,
                                    "keys": merged_value}
                elif filename == "turn_requests.json":
                    merged_value = {"schema_version": CORE_DATA_VERSION,
                                    "records": merged_value}
                _atomic_write_json(path, merged_value)
            merged_counts[filename] = merged_counts.get(filename, 0) + 1
            conflicts.extend(f"{character}: {item}" for item in file_conflicts)
        # A rollback must replay the merged journal from the start.
        if (character_dir / "events.ndjson").is_file():
            _atomic_write_json(character_dir / "events_applied.json", {
                "schema_version": CORE_DATA_VERSION,
                "last_applied_position": -1,
                "last_applied_event_id": None,
                "ts": _now_iso(),
            })
    return merged_counts, conflicts


def rollback(
    memory_root: Path | str,
    backup_root: Path | str,
    *,
    preserve_new: bool = True,
) -> RollbackReport:
    """Restore one verified backup, then merge post-backup records when asked."""
    root = Path(memory_root).expanduser()
    backup_dir = Path(backup_root).expanduser()
    if not root.is_dir():
        raise MigrationError(
            f"memory root does not exist: {str(root)!r}", code="bad_argument",
        )
    manifest_payload = _read_backup_manifest(backup_dir)
    backup_problems = _verify_backup(backup_dir, manifest_payload)
    if backup_problems:
        raise MigrationError(
            f"backup is not intact: {backup_problems[:3]}", code="backup_corrupt",
        )
    current_files = {
        path.relative_to(root).as_posix()
        for path in _iter_payload_files(root)
    }
    payload = backup_dir / PAYLOAD_DIRNAME
    backed_up = {
        path.relative_to(payload).as_posix()
        for path in _iter_payload_files(payload)
    }
    kept_current = tuple(sorted(current_files - backed_up))
    captured = _capture_current(root) if preserve_new else {}
    restored = _restore_payload(backup_dir, root)
    merged: dict[str, Any] = {}
    conflicts: list[str] = []
    if preserve_new:
        merged, conflicts = _merge_current(root, captured)
    verify_report = DataFormatManifest().verify(root)
    report = RollbackReport(
        ok=verify_report.ok,
        memory_root=str(root),
        backup_root=str(backup_dir),
        preserve_new=preserve_new,
        restored_files=restored,
        kept_current=kept_current,
        merged=merged,
        conflicts=tuple(conflicts),
        verify=verify_report.to_dict(),
        backup_verified=True,
    )
    if not verify_report.ok:
        raise MigrationError(
            "rollback restored the backup but post-rollback verify failed",
            code="verify_failed", report=report.to_dict(),
        )
    return report


def verify(memory_root: Path | str) -> VerifyReport:
    """Verify every known store under one explicit root (no writes)."""
    return DataFormatManifest().verify(memory_root)


def make_migration_hook(
    memory_root: Path | str,
    *,
    backup_root: Path | str | None = None,
) -> Callable[[str | None], dict[str, Any]]:
    """Maintenance-compatible one-shot hook: migrate once, then report skipped.

    The maintenance loop calls hooks per character; a data-format migration is
    root-scoped, so the first call migrates and later calls are no-ops.
    """
    state = {"ran": False}

    def hook(character: str | None = None) -> dict[str, Any]:
        if state["ran"]:
            return {"status": "skipped", "reason": "already migrated"}
        report = migrate(memory_root, backup_root=backup_root)
        state["ran"] = True
        return {"status": "ok", **report.to_dict()}

    return hook


__all__ = [
    "BACKUP_MANIFEST_FILENAME",
    "CORE_DATA_VERSION",
    "DEFAULT_STEPS",
    "DataFormatManifest",
    "MANIFEST_FILENAME",
    "ManifestReport",
    "MigrationContext",
    "MigrationError",
    "MigrationPlan",
    "MigrationReport",
    "MigrationStep",
    "MigrationStepResult",
    "RollbackReport",
    "STORAGE_SPECS",
    "StorageSpec",
    "StorageStatus",
    "VerifyReport",
    "backup",
    "make_migration_hook",
    "migrate",
    "rollback",
    "verify",
]
