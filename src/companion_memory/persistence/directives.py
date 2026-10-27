"""Per-character user mention restrictions (`user_directives.json`), B03.

A directive records explicit user intent ("别再提 X / stop saying X /
その話はもう") so a later session can steer clear of a topic the user asked to
drop. It is **not** a memory entry and it is deliberately outside the B03
deletion barrier: deleting a memory entry, fact, episode or correction never
removes a directive, because the user's prohibition was never derived from
that content. A directive stops applying only when its TTL expires or when the
user clears it (`clear`); expiry is filtered on read and reclaimed by
`purge_expired`.

On-disk shape is the host's versioned envelope. `version` is the *schema*
version (fixed at 1), not a data version:

    {"version": 1,
     "directives": [{"term": str, "kind": str, "locale": str,
                     "created_at": float, "last_seen_at": float,
                     "expire_at": float, "hit_count": int,
                     "source": str}, ...]}

Unknown top-level keys and unknown entry fields round-trip verbatim, so the
N.E.K.O build keeps reading and writing the same file.

Strict read, fail closed (deliberate divergence from the host): the host reads
an unparsable or malformed file as empty and starts over. For an explicit
prohibition that is the worst available reading — the damaged rows are
silently discarded, the next session re-offends on the banned topic, and the
next write overwrites the file before a human can repair it. This store
therefore raises `CorruptStoreError` for unparsable JSON, a wrong top-level
shape, or any single malformed row, and `InvalidSchemaError` for a missing or
non-1 `version`, without rewriting a byte. A missing file is a fresh store;
`clear`/`purge_expired` validate before writing, so they never launder a
damaged file into an empty one.

Rules:
  * a term is `str.strip()`-ed and must be 2..40 characters; a term that
    violates this raises `ValueError` on write and `CorruptStoreError` on read;
  * `kind`, `locale` and `source` are strings, the three timestamps are epoch
    seconds (normalised to a finite float), and `hit_count` is an int >= 1;
  * dedup key is `(kind, term.casefold())`; a hit keeps the first spelling,
    `kind`, `locale` and `created_at`, bumps `hit_count`, sets
    `last_seen_at = now`, and renews
    `expire_at = now + min(ttl_seconds * hit_count, ttl_max_seconds)`;
    `record(..., ttl_seconds=...)` overrides the renewal window for one write,
    bounded to `(0, ttl_max_seconds]`;
  * at most `max_stored` rows are kept: expired rows are dropped first, then
    the newest `max_stored` by `last_seen_at`; if rotation drops the row this
    call just wrote, `record` returns `{}` instead of claiming success;
  * `active` returns unexpired rows (`expire_at > now`), newest `last_seen_at`
    first, capped at `max_active`.

`render_block` renders the localized title plus one `- term` line per
entry and returns "" when nothing is active. B03 has no token counter yet, so
the block is budgeted by an estimate (`limit_tokens * 4` characters, whole
bullet lines only); B06 replaces this with the real `TokenCounter` and
per-language tokenization. A partial title is never injected: when the
localized title alone exceeds the budget the block is empty.

Every write passes the injected `WritePermit` first, each file path has its
own `threading.RLock` for read-modify-write pairs, and all IO is synchronous
blocking: callers run these methods through `asyncio.to_thread`.
"""

from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Any

from ..errors import CorruptStoreError, InvalidSchemaError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit

#: File name inside `<character_dir>`; the host uses this exact name.
DIRECTIVES_FILENAME = "user_directives.json"

#: Schema version of the envelope (fixed; not a data version).
DIRECTIVES_SCHEMA = 1

#: Base TTL of a first hit: three days, matching the host.
DEFAULT_TTL_SECONDS = 3 * 86400
#: Ceiling of the repeat-scaled TTL: thirty days, matching the host.
DEFAULT_TTL_MAX_SECONDS = 30 * 86400
#: Rows kept on disk per character.
DEFAULT_MAX_STORED = 60
#: Rows returned by `active` per read.
DEFAULT_MAX_ACTIVE = 20

#: Stripped term length invariant, shared by the read and write paths.
_TERM_MIN_LEN = 2
_TERM_MAX_LEN = 40
#: B03 has no token counter; four characters per token is the documented estimate.
_ESTIMATED_CHARS_PER_TOKEN = 4

_REQUIRED_ENTRY_FIELDS = (
    "term", "kind", "locale", "created_at", "last_seen_at", "expire_at",
    "hit_count", "source",
)

#: One RLock per directives file; created lazily, never held across an await (none here).
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


#: Localized block titles. The core writes its own concise prompt strings
#: instead of copying the host's templates, so B06 can replace them together
#: with the real token budget without touching storage.
_TITLE_TEMPLATES: dict[str, str] = {
    "zh": "[用户最近明确要求不要提及以下内容（共{n}项）]",
    "zh-TW": "[使用者最近明確要求不要提及以下內容（共{n}項）]",
    "en": "[The user recently asked not to mention the following ({n} item(s))]",
    "ja": "[ユーザーが最近、話題に出さないよう明示した内容（{n}件）]",
    "ko": "[사용자가 최근에 언급하지 말라고 명확히 요청한 항목 ({n}개)]",
    "ru": "[Пользователь недавно явно просил не упоминать следующее ({n} шт.)]",
    "es": "[El usuario pidió recientemente no mencionar lo siguiente ({n} elemento(s))]",
    "pt": "[O usuário pediu recentemente para não mencionar o seguinte ({n} item(ns))]",
}
_LANGUAGE_ALIASES: dict[str, str] = {
    key.casefold(): key for key in _TITLE_TEMPLATES
}


class UserDirectiveStore:
    """Per-character mention restrictions (see the module docstring).

    One instance serves every character; locks are per directives file. The
    store keeps no in-memory cache: every read loads and validates the file,
    every write is a load -> mutate -> rotate -> save cycle under the lock.
    """

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        ttl_max_seconds: float = DEFAULT_TTL_MAX_SECONDS,
        max_stored: int = DEFAULT_MAX_STORED,
        max_active: int = DEFAULT_MAX_ACTIVE,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._ttl_seconds = _require_positive_seconds(ttl_seconds, "ttl_seconds")
        self._ttl_max_seconds = _require_positive_seconds(
            ttl_max_seconds, "ttl_max_seconds",
        )
        self._max_stored = _require_positive_int(max_stored, "max_stored")
        self._max_active = _require_positive_int(max_active, "max_active")

    # -- paths -----------------------------------------------------------

    def _path(self, character: str) -> Path:
        """Resolve `<character_dir>/user_directives.json` without creating anything.

        The atomic store creates the parent directory on write, so a read of
        an absent character leaves no directory behind and no filesystem write
        can happen outside the write permit.
        """
        return self._roots.character_dir(
            character, create=False,
        ).joinpath(DIRECTIVES_FILENAME)

    def _now(self) -> float:
        return self._clock.now().timestamp()

    # -- document shape --------------------------------------------------

    def _load(self, path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Load, version-gate and fully validate the file; never writes.

        Returns the envelope (unknown top-level keys kept for write-back) and
        the validated entry rows. Any damaged row fails the whole load: a row
        that cannot be read is a user prohibition that would be dropped, and
        this store never silently forgets one.
        """
        raw = self._store.load(path)
        if raw is None:
            return {"version": DIRECTIVES_SCHEMA, "directives": []}, []
        if not isinstance(raw, dict):
            raise CorruptStoreError(
                f"expected a JSON object, found {type(raw).__name__}", path=path,
            )
        version = raw.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise InvalidSchemaError(
                f"missing or non-integer directives version: {version!r}",
                path=path,
            )
        if version != DIRECTIVES_SCHEMA:
            raise InvalidSchemaError(
                f"unsupported directives version {version}; this build supports "
                f"{[DIRECTIVES_SCHEMA]}", path=path,
            )
        items = raw.get("directives")
        if not isinstance(items, list):
            raise CorruptStoreError(
                f"expected a directives array, found {type(items).__name__}",
                path=path,
            )
        entries = [
            self._validated_entry(item, index, path)
            for index, item in enumerate(items)
        ]
        return dict(raw), entries

    @staticmethod
    def _validated_entry(
        raw: Any, index: int, path: Path,
    ) -> dict[str, Any]:
        """Validate one row and return a copy with normalised known fields."""
        if not isinstance(raw, dict):
            raise CorruptStoreError(
                f"directives[{index}]: expected an object, found "
                f"{type(raw).__name__}", path=path,
            )
        missing = [key for key in _REQUIRED_ENTRY_FIELDS if key not in raw]
        if missing:
            raise CorruptStoreError(
                f"directives[{index}]: missing required fields {missing}",
                path=path,
            )
        term = _normalize_term(raw.get("term"))
        if term is None:
            raise CorruptStoreError(
                f"directives[{index}]: term must be a string of "
                f"{_TERM_MIN_LEN}..{_TERM_MAX_LEN} characters after stripping, "
                f"found {raw.get('term')!r}", path=path,
            )
        entry = dict(raw)  # unknown fields round-trip verbatim
        entry["term"] = term
        for field_name in ("kind", "locale", "source"):
            value = raw.get(field_name)
            if not isinstance(value, str):
                raise CorruptStoreError(
                    f"directives[{index}]: {field_name} must be a string, "
                    f"found {type(value).__name__}", path=path,
                )
            entry[field_name] = value
        for field_name in ("created_at", "last_seen_at", "expire_at"):
            entry[field_name] = _epoch_seconds(
                raw.get(field_name), field_name, index, path,
            )
        hit_count = raw.get("hit_count")
        if isinstance(hit_count, bool) or not isinstance(hit_count, int) or hit_count < 1:
            raise CorruptStoreError(
                f"directives[{index}]: hit_count must be an integer >= 1, "
                f"found {hit_count!r}", path=path,
            )
        return entry

    def _save(
        self,
        path: Path,
        payload: dict[str, Any],
        entries: list[dict[str, Any]],
    ) -> None:
        """Persist the host shape under the write permit (unknown keys kept)."""
        document = dict(payload)
        document["version"] = DIRECTIVES_SCHEMA
        document["directives"] = entries
        self._permit.assert_writable("save_user_directives", str(path))
        self._store.save(path, document)

    # -- public API ------------------------------------------------------

    def record(
        self,
        character: str,
        *,
        term: str,
        kind: str,
        locale: str,
        source: str = "regex",
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Register one directive; a `(kind, term.casefold())` hit renews it.

        Returns a copy of the persisted row, or `{}` when rotation dropped
        the row this call wrote (the store is at cap and every kept row is
        newer), so a caller never reports a directive that is not on disk.
        Invalid input raises `ValueError` before the file is touched; an
        explicit `ttl_seconds` must fall in `(0, ttl_max_seconds]`.
        """
        term = _require_term(term)
        kind = _require_label(kind, "kind")
        locale = _require_label(locale, "locale")
        source = _require_label(source, "source")
        override = self._resolve_ttl_override(ttl_seconds)
        now = self._now()
        path = self._path(character)
        with _lock_for(path):
            payload, entries = self._load(path)
            key = (kind, term.casefold())
            written: dict[str, Any] | None = None
            for entry in entries:
                if (entry["kind"], entry["term"].casefold()) == key:
                    hits = entry["hit_count"] + 1
                    entry["hit_count"] = hits
                    entry["last_seen_at"] = now
                    entry["expire_at"] = now + (
                        override if override is not None
                        else self._effective_ttl(hits)
                    )
                    # First-hit spelling, kind, locale and created_at stay.
                    written = entry
                    break
            if written is None:
                written = {
                    "term": term,
                    "kind": kind,
                    "locale": locale,
                    "created_at": now,
                    "last_seen_at": now,
                    "expire_at": now + (
                        override if override is not None
                        else self._effective_ttl(1)
                    ),
                    "hit_count": 1,
                    "source": source,
                }
                entries.append(written)
            kept = self._rotate(entries, now=now)
            self._save(path, payload, kept)
            if not any(item is written for item in kept):
                return {}
            return dict(written)

    def active(
        self, character: str, *, limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Unexpired directives, newest `last_seen_at` first.

        `limit` defaults to `max_active`; an explicit limit must be a
        positive integer. Returned rows are copies: mutating them never changes
        what the next call reads.
        """
        cap = (
            self._max_active if limit is None
            else _require_positive_int(limit, "limit")
        )
        now = self._now()
        path = self._path(character)
        with _lock_for(path):
            _, entries = self._load(path)
        alive = [
            dict(entry) for entry in entries if entry["expire_at"] > now
        ]
        alive.sort(key=lambda entry: entry["last_seen_at"], reverse=True)
        return alive[:cap]

    def clear(self, character: str) -> None:
        """Drop every directive for the character and persist the empty list.

        The file is validated before it is written: a damaged file is never
        replaced by an empty one, so a user-visible clear cannot hide damage.
        """
        path = self._path(character)
        with _lock_for(path):
            payload, _entries = self._load(path)
            self._save(path, payload, [])

    def purge_expired(self, character: str) -> int:
        """Delete expired directives and persist; returns how many were removed.

        A missing file or a file with nothing expired is left untouched (no
        write, so no permit is taken).
        """
        now = self._now()
        path = self._path(character)
        with _lock_for(path):
            payload, entries = self._load(path)
            kept = [entry for entry in entries if entry["expire_at"] > now]
            removed = len(entries) - len(kept)
            if removed:
                self._save(path, payload, kept)
            return removed

    def render_block(
        self, character: str, language: str = "zh", limit_tokens: int = 200,
    ) -> str:
        """Render active terms as a localized prompt fragment ("" when empty).

        The block is the localized title plus one `- term` line per active
        directive, newest first. B03 has no token counter, so the budget is the
        estimate `limit_tokens * 4` characters and is applied to whole bullet
        lines: trailing terms are dropped rather than a line being cut in half,
        and the title alone always fits or the block is empty. B06 wires the
        real `TokenCounter` here and replaces the estimate.
        """
        tokens = _require_positive_int(limit_tokens, "limit_tokens")
        terms = [entry["term"] for entry in self.active(character)]
        if not terms:
            return ""
        template = _TITLE_TEMPLATES[_language_key(language)]
        budget = tokens * _ESTIMATED_CHARS_PER_TOKEN
        # Reserve room with the largest possible count so truncation cannot
        # produce a title longer than the budget it was measured against.
        title = template.format(n=len(terms))
        lines: list[str] = []
        length = len(title)
        for term in terms:
            line = f"- {term}"
            if length + 1 + len(line) > budget:
                break
            lines.append(line)
            length += 1 + len(line)
        if not lines:
            return ""
        title = template.format(n=len(lines))
        return title + "\n" + "\n".join(lines)

    # -- internals -------------------------------------------------------

    def _effective_ttl(self, hit_count: int) -> float:
        """TTL for a directive repeated `hit_count` times, capped at the max."""
        return min(
            self._ttl_seconds * hit_count, self._ttl_max_seconds,
        )

    def _resolve_ttl_override(self, ttl_seconds: float | None) -> float | None:
        """Validate the optional explicit TTL; `None` means use the repeat window."""
        if ttl_seconds is None:
            return None
        if isinstance(ttl_seconds, bool):
            raise ValueError("ttl_seconds must be a number")
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError):
            raise ValueError("ttl_seconds must be a number") from None
        if not math.isfinite(ttl) or not (0 < ttl <= self._ttl_max_seconds):
            raise ValueError(
                f"ttl_seconds must be in (0, {self._ttl_max_seconds}], "
                f"found {ttl_seconds!r}"
            )
        return ttl

    def _rotate(
        self, entries: list[dict[str, Any]], *, now: float,
    ) -> list[dict[str, Any]]:
        """Cap the stored rows at `max_stored`, expired rows first."""
        if len(entries) <= self._max_stored:
            return entries
        alive = [entry for entry in entries if entry["expire_at"] > now]
        if len(alive) > self._max_stored:
            # Same ordering as active: newest last_seen survives.
            alive.sort(key=lambda entry: entry["last_seen_at"], reverse=True)
            alive = alive[:self._max_stored]
        return alive


# -- validation helpers --------------------------------------------------


def _normalize_term(value: Any) -> str | None:
    """`str.strip()` plus the 2..40 length invariant, or None when invalid."""
    if not isinstance(value, str):
        return None
    term = value.strip()
    if not (_TERM_MIN_LEN <= len(term) <= _TERM_MAX_LEN):
        return None
    return term


def _require_term(term: Any) -> str:
    """Write-path term check: ValueError instead of a corrupt-file error."""
    normalized = _normalize_term(term)
    if normalized is None:
        raise ValueError(
            f"term must be a string of {_TERM_MIN_LEN}..{_TERM_MAX_LEN} "
            f"characters after stripping, found {term!r}"
        )
    return normalized


def _require_label(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _epoch_seconds(value: Any, field_name: str, index: int, path: Path) -> float:
    """Read-path timestamp check: finite epoch seconds, normalised to float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CorruptStoreError(
            f"directives[{index}]: {field_name} must be epoch seconds, "
            f"found {value!r}", path=path,
        )
    number = float(value)
    if not math.isfinite(number):
        raise CorruptStoreError(
            f"directives[{index}]: {field_name} must be finite, "
            f"found {value!r}", path=path,
        )
    return number


def _require_positive_seconds(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a positive number") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field_name} must be a positive number")
    return number


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _language_key(language: Any) -> str:
    """Map a language tag to a template key; unknown tags fall back to English.

    Exact keys win (`zh-TW`), then the primary subtag (`pt-BR` -> pt), then
    English.
    """
    if not isinstance(language, str):
        return "en"
    code = language.strip().replace("_", "-").casefold()
    if code in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[code]
    primary = code.split("-", 1)[0]
    return _LANGUAGE_ALIASES.get(primary, "en")


__all__ = [
    "DEFAULT_MAX_ACTIVE",
    "DEFAULT_MAX_STORED",
    "DEFAULT_TTL_MAX_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "DIRECTIVES_FILENAME",
    "DIRECTIVES_SCHEMA",
    "UserDirectiveStore",
]
