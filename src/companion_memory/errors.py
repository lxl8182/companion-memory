"""Typed errors shared by the core; adapters map them onto host HTTP codes.

B02 freezes these names so a caller can tell "the file is corrupt" apart from
"this build does not understand the schema" apart from "a write permit said
no". Adapters must translate `code` (and `path`), never message text.
"""

from collections.abc import Mapping


class SourceConflictError(ValueError):
    """A source key was reused for a different event within the same scope."""


class StoreError(Exception):
    """Base class for persistence failures that callers must distinguish.

    `code` is stable and machine-readable. `path` identifies the affected file
    when the failure is file-scoped. `cause` keeps the original exception so an
    adapter can log it without the core depending on it.
    """

    code = "store_error"

    def __init__(
        self,
        message: str,
        *,
        path: object = None,
        code: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.path = None if path is None else str(path)
        if code is not None:
            self.code = code
        self.cause = cause


class CorruptStoreError(StoreError):
    """A store file exists but cannot be parsed or has the wrong shape.

    The original file is never rewritten, replaced by an empty store, or
    "repaired" in place: the caller must inspect, repair or move it.
    """

    code = "store_corrupt"


class InvalidSchemaError(StoreError):
    """A store declares a schema version this build does not support.

    Raised before any write, so an unknown (usually newer) store is left
    exactly as found instead of being downgraded in place.
    """

    code = "schema_unsupported"


class WriteRejected(StoreError):
    """A write permit refused the operation (maintenance mode / save fence).

    Host adapters map this onto the existing 409 `cloudsave_write_fence_active`
    or limited-mode response instead of a 5xx.
    """

    code = "write_rejected"


class UnregisteredEventTypeError(ValueError):
    """An event type has no registered recovery handler.

    Writing such an event would create a journal entry the reconciler cannot
    replay, so the journal refuses the write instead of accepting it.
    """


class RecoveryConflictError(StoreError):
    """The journal sentinel moved while a replay round was in flight.

    Nothing from that round was applied: the caller must re-derive its work
    from the journal rather than write a stale sentinel backwards.
    """

    code = "recovery_conflict"


# -- B03: deletion barrier, corrections and directives -----------------------


class DeletionNotFoundError(StoreError):
    """No deletion record with this id exists for the character."""

    code = "deletion_not_found"


class DeletionConflictError(StoreError):
    """A deletion_id was reused with a different payload (never overwritten)."""

    code = "deletion_conflict"


class CorrectionNotFoundError(StoreError):
    """No correction record with this id exists for the character."""

    code = "correction_not_found"


class BarrierStaleError(StoreError):
    """A deletion/correction moved past the snapshot an in-flight write captured.

    Background work (extraction, reflection, revision) captures a barrier
    snapshot before it starts and re-checks it before committing: when this
    raises, the result was derived from content the user has since deleted or
    corrected and must be dropped instead of written back.
    """

    code = "barrier_stale"


class ContentBlockedError(StoreError):
    """The write targets content the deletion barrier already removed.

    Live event ingestion returns a `rejected` receipt for this case instead of
    raising; in-process callers that must not silently continue can raise it.
    """

    code = "content_blocked"


class NotConfiguredError(StoreError):
    """The operation needs a service this engine instance was not wired with.

    B03 keeps `MemoryEngine` constructible for the B00/B02 demos; correct and
    forget raise this instead of returning an empty success.
    """

    code = "not_configured"


class RootLockConflict(StoreError):
    """Another live process owns the data root, or this one already does.

    Raised *before* a writable core is assembled, so the loser of a race never
    touches a file. `detail` carries the lock path and the recorded owner for
    diagnostics; the owner record is never used as the exclusion mechanism.
    """

    code = "root_lock_conflict"

    def __init__(
        self,
        message: str,
        *,
        path: object = None,
        cause: BaseException | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message, path=path, cause=cause)
        self.detail = dict(detail or {})
