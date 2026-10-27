"""Management endpoints reuse the console views and the service's one core."""
from __future__ import annotations

import secrets
import os
import asyncio
from dataclasses import asdict
from typing import Any, Mapping

from ..persistence.atomic import FileSystemAtomicStore
from ..persistence.persona_card import with_name_line
from .chat import ChatBridge
from .console import ApiError, RinApp, _section_view
from .memory_service import MemoryService

#: Name of the pre-card metadata file. Read once for migration, then removed.
LEGACY_NAME_FILE = "service_character.json"


class ManagementApi:
    def __init__(self, service: MemoryService, *, display_name: str = "",
                 subject: str = "private", config_missing=()) -> None:
        self.service = service
        self.subject = subject
        self.config_missing = list(config_missing)
        self.token = secrets.token_urlsafe(32)
        # One bridge per service: it reuses this service's core, model and
        # capability state, so the console conversation and `/v1/chat` cannot
        # diverge into two writers or two model configurations.
        self.chat = ChatBridge(
            core=service.core, character=service.settings.character,
            service=service,
        )
        self.console = RinApp(
            core=service.core, mode="service", root=service.settings.memory_root,
            read_only=False, demo=False, character=service.settings.character,
            display_name=display_name or service.settings.character,
            chat=self.chat,
        )
        # A name used to live in its own file, outside the card; that split made
        # it the one value no editor could reach. It is now a card section, and
        # any leftover file is folded in once and then retired.
        self._retired_metadata = service.settings.memory_root / LEGACY_NAME_FILE
        self._store = FileSystemAtomicStore()
        self._migrate_legacy_name()
        # The local console manages the configured character across its scopes.
        # A caller-supplied subject is never used to widen that authority.

    def _migrate_legacy_name(self) -> None:
        """Bring any earlier name storage onto the identity section's first line.

        Two earlier shapes exist: a name-only card section (`identity_name`)
        written by the first version of this feature, and a metadata file that
        predates the card owning a name at all. Both are folded into the
        identity line and then removed, so exactly one place holds the name.
        """
        card = self.service.core.persona_card_service.get(self.console.character)
        if not card.initial_revision:
            return
        # A retired name-only section: its text is the name unless the identity
        # line already declares one.
        stale = [section for section in card.sections
                 if section.section_id == "identity_name"]
        if stale:
            legacy_section_name = stale[0].text.strip()
            has_name = bool(self.service.core.persona_card_service.display_name(
                self.console.character))
            try:
                operations = [{"op": "delete", "section_id": "identity_name"}]
                if legacy_section_name and not has_name:
                    identity = next(
                        (section for section in card.sections
                         if section.section_id == "identity"), None,
                    )
                    if identity is not None:
                        operations.append({
                            "op": "replace", "section_id": "identity",
                            "text": with_name_line(identity.text, legacy_section_name),
                        })
                self.service.core.persona_card_service.revise(self.console.character, {
                    "base_revision": card.revision,
                    "operations": operations,
                    "reason": "name_section_folded_into_identity",
                    "origin": "user_edit", "actor": "system",
                })
            except Exception:  # noqa: BLE001 - retried on the next start
                pass

        legacy = self._store.load(self._retired_metadata)
        if not legacy:
            return
        if legacy.get("character_id") != self.service.settings.character:
            raise ValueError("character.character_id differs from this memory root")
        name = str(legacy.get("display_name") or "").strip()
        if name and not self.service.core.persona_card_service.display_name(
                self.console.character):
            try:
                self.service.core.persona_card_service.rename(
                    self.console.character, name,
                    reason="name_migrated_from_host_file",
                )
            except Exception:  # noqa: BLE001 - keep the file and retry later
                return
        if self.service.core.persona_card_service.display_name(self.console.character):
            try:
                self._retired_metadata.unlink()
            except OSError:
                pass

    def initialization(self) -> dict[str, Any]:
        card = self.service.core.persona_card_service.get(self.console.character)
        published = self.service.core.persona_publish.current(self.console.character)
        missing = []
        if not card.initial_revision:
            missing.append("initial_persona")
        if not published.available:
            missing.append("first_publish")
        self.service._refresh_chat_capability()
        return {
            "character_id": self.console.character,
            "display_name": self.console.display_name(),
            "subject": self.subject, "initialized": card.initial_revision > 0,
            "published": published.available,
            "revision": card.revision, "version": published.version,
            "missing": missing, "capabilities": self.service.state.to_dict(),
            "config_missing": self.config_missing,
        }

    def bootstrap(self) -> dict[str, Any]:
        return {"client_id": "console", "write_token": self.token,
                "initialization": self.initialization()}

    def initialize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        character = payload.get("character_id", self.console.character)
        if character != self.console.character:
            raise ApiError("scope_mismatch", 403, "character_id differs from stack configuration")
        display_name = payload.get("display_name")
        text = payload.get("text")
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 80:
            raise ApiError("invalid_argument", 400, "display_name must contain 1 to 80 characters")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ApiError("invalid_argument", 400, "text must contain 1 to 4000 characters")
        with self.console._lock:
            receipt = self.service.core.persona_card_service.import_initial(
                self.console.character, [{
                    "section_id": "identity", "kind": "identity",
                    # The name owns the first line of the identity section.
                    "text": with_name_line(text.strip(), display_name.strip()),
                    "scope": {"subject": self.subject, "applies_to": "all"},
                    "editable_by": {"user": True, "evolution": False}, "frozen": True,
                }], source="console_initialization", actor="user",
            )
            if receipt.status == "rejected":
                raise ApiError("invalid_argument", 400, receipt.reason)
            # Initialization imports the card exactly once; a retry reports the
            # duplicate and changes neither the name nor any edited section.
            result = self.console._revision_receipt(receipt)
            result["initialization"] = self.initialization()
            return result

    def identity(self, client: str, token: str) -> dict:
        if client == "console" and secrets.compare_digest(token, self.token):
            return {"client_id": client, "subjects": [self.subject], "admin": True}
        spec = self.service.settings.clients.get(client)
        if isinstance(spec, Mapping):
            expected = os.environ.get(spec.get("token_env", ""), "")
            if expected and secrets.compare_digest(token, expected):
                return {"client_id": client, "subjects": spec.get("subjects", [self.subject]),
                        "plugin_id": spec.get("plugin_id", ""), "admin": False,
                        # A limited, explicitly configured capability: the
                        # desktop host owns the local character editor and may
                        # maintain the semantic persona card. It is not the
                        # console identity and grants nothing else.
                        "host_persona": bool(spec.get("host_persona", False)),
                        # A second, independent grant: the chat client may ask
                        # for the user's own corrections/forgettings from inside
                        # a conversation. It reaches no console route, no other
                        # subject and no bulk edit, and it never implies the
                        # persona capability.
                        "memory_tools": bool(spec.get("memory_tools", False))}
        raise ApiError("unauthorized", 401, "a current client identity is required")

    def scoped(self, payload: Mapping, identity: Mapping) -> dict:
        result = dict(payload)
        character = result.get("character_id", self.console.character)
        subject = result.get("subject", identity["subjects"][0])
        if character != self.console.character or subject not in identity["subjects"]:
            raise ApiError("scope_mismatch", 403, "character or subject is outside the client identity")
        if result.get("client_id", identity["client_id"]) != identity["client_id"]:
            raise ApiError("scope_mismatch", 403, "client_id differs from authenticated identity")
        if identity.get("plugin_id") and result.get("plugin_id", identity["plugin_id"]) != identity["plugin_id"]:
            raise ApiError("scope_mismatch", 403, "plugin_id differs from authenticated identity")
        result.update(character_id=character, subject=subject, client_id=identity["client_id"])
        if identity.get("plugin_id"):
            result["plugin_id"] = identity["plugin_id"]
        return result

    def check_scope(self, payload: Mapping) -> None:
        if payload.get("character_id", self.console.character) != self.console.character:
            raise ApiError("scope_mismatch", 403, "character_id differs from stack configuration")
        if payload.get("subject", self.subject) != self.subject:
            raise ApiError("scope_mismatch", 403, "console subject differs from stack configuration")

    # -- limited host persona capability (S05-H01) -----------------------

    @staticmethod
    def require_host_persona(identity: Mapping) -> None:
        """Refuse everything unless this identity was granted the capability.

        The grant is per client in `stack.toml` (`host_persona = true`) and is
        checked here as well as in the transport, so a routing mistake cannot
        silently widen a normal client into a persona editor.
        """
        if not identity.get("host_persona"):
            raise ApiError("forbidden", 403, "this identity has no persona management capability")

    def host_persona(self) -> dict[str, Any]:
        """The card the host editor renders: sections, revision, published version.

        Deliberately the same domain reads the console uses; the host never
        reconstructs persona semantics from `characters.json`.
        """
        character = self.console.character
        card = self.service.core.persona_card_service.get(character)
        published = self.service.core.persona_publish.current(character)
        versions = self.service.core.persona_versions.load(character)
        current = versions.get("current") if isinstance(versions, Mapping) else None
        return {
            "character_id": character,
            "revision": card.revision,
            "initial_revision": card.initial_revision,
            "display_name": self.console.display_name(),
            "sections": [_section_view(section.to_mapping()) for section in card.sections],
            "published": {
                "available": bool(published.available),
                "version": published.version,
                "revision": published.revision,
                "target": str(current.get("target") or "") if isinstance(current, Mapping) else "",
                "published_at": (
                    current.get("published_at") if isinstance(current, Mapping) else None
                ),
                "stale": bool(published.available) and published.revision != card.revision,
            },
        }

    def host_revise(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Apply explicit host edits under base_revision CAS.

        A stale base revision is a real conflict: the caller keeps its draft and
        re-reads the current card. This path never falls back to a local save,
        and never re-reads the latest revision on the caller's behalf.
        """
        from .console import ApiError as _ConsoleError

        base_revision = payload.get("base_revision")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int):
            raise ApiError("invalid_argument", 400, "base_revision must be an integer")
        operations = payload.get("operations")
        if not isinstance(operations, list) or not operations:
            raise ApiError("invalid_argument", 400, "operations must be a non-empty list")
        for operation in operations:
            if not isinstance(operation, Mapping) or not operation.get("op") or not operation.get("section_id"):
                raise ApiError("invalid_argument", 400, "each operation needs op and section_id")
        try:
            result = self.console.revise(dict(payload))
        except _ConsoleError as exc:
            if exc.code == "revision_conflict":
                raise ApiError(
                    "revision_conflict", 409,
                    "base_revision changed; keep the draft and refresh",
                    extra=getattr(exc, "extra", {}),
                ) from exc
            raise ApiError(exc.code, exc.status, exc.message) from exc
        if result.get("status") == "rejected":
            raise ApiError("rejected", 422, str(result.get("reason") or "operation rejected"))
        self.service._refresh_chat_capability()
        return result

    def host_publish(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Publish the revision the host is showing; never a silently newer one."""
        revision = payload.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ApiError("invalid_argument", 400, "revision must be an integer")
        target = str(payload.get("target") or "next_turn")
        if target != "next_turn":
            # The host only owns the text turn; realtime/game consumers keep
            # their own publish boundaries.
            raise ApiError("invalid_argument", 400, "the host may only publish next_turn")
        result = self.console.publish({"target": target, "revision": revision})
        if result.get("conflict"):
            raise ApiError("revision_conflict", 409, "the card changed; refresh before publishing")
        if result.get("status") == "rejected":
            raise ApiError("rejected", 422, str(result.get("reason") or "publish rejected"))
        self.service._refresh_chat_capability()
        return result

    def protocol_get(self, path: str, body: Mapping):
        core = self.service.core
        if path == "/v1/receipts":
            return self.service.protocol.receipts(self.service.protocol.scope(body))
        if path == "/v1/cleanup/pending":
            plugin = str(body.get("plugin_id") or "")
            if not plugin:
                raise ApiError("invalid_argument", 400, "plugin_id is required")
            return {"items": core.deletion_service.pending_plugin_deletions(self.console.character, plugin)}
        if path.startswith("/v1/cleanup/receipts/"):
            receipt = core.deletion_service.get(self.console.character, path.rsplit("/", 1)[1])
            if receipt is None:
                raise ApiError("not_found", 404, "unknown deletion")
            return self.console._deletion_receipt_view(receipt)
        return None

    # -- limited memory tools for a chat client (DM04) -------------------

    @staticmethod
    def require_memory_tools(identity: Mapping) -> None:
        """Refuse unless this identity was granted the conversation memory tools.

        The grant is per client in `stack.toml` (`memory_tools = true`). It is
        deliberately separate from `host_persona`: a chat surface may let a
        speaker correct or forget their own facts without gaining any authority
        over the persona card or the console surface.
        """
        if not identity.get("memory_tools"):
            raise ApiError("forbidden", 403, "this identity has no memory tool capability")

    def _memory_scope(self, payload: Mapping, identity: Mapping) -> dict:
        """Bind a memory-tool call to the authenticated client and one subject."""
        subject = str(payload.get("subject") or (identity.get("subjects") or ["private"])[0])
        if subject not in (identity.get("subjects") or []):
            raise ApiError("scope_mismatch", 403, "subject is outside the client identity")
        return {"subject": subject, "scene": str(payload.get("scene") or ""),
                "session_id": str(payload.get("session_id") or "")}

    def memory_entries(self, payload: Mapping, identity: Mapping) -> dict:
        """What this speaker could forget, already filtered to their subject."""
        self.require_memory_tools(identity)
        scope = self._memory_scope(payload, identity)
        text = payload.get("text")
        if text is not None and not isinstance(text, str):
            raise ApiError("invalid_argument", 400, "text must be a string")
        result = self.service.core.deletion_service.deletable_entries(
            self.console.character, query=(text or "").strip(),
            types=("fact", "reflection"), limit=20, subject=scope["subject"],
        )
        return {"subject": scope["subject"], "items": list(result.get("items") or ())}

    def memory_correct(self, payload: Mapping, identity: Mapping) -> dict:
        """Record the user's own correction from inside the conversation.

        The rules stay in the core (correction engine, barrier, idempotency);
        this endpoint only restricts who may ask and for which subject. A
        correction always lands in the shared scope so it reaches the web
        conversation as well as this QQ one — a personal preference is not a
        plugin-local fact.
        """
        self.require_memory_tools(identity)
        scope = self._memory_scope(payload, identity)
        old_text = payload.get("old_text")
        new_text = payload.get("new_text")
        for name, value in (("old_text", old_text), ("new_text", new_text)):
            if value is not None and not isinstance(value, str):
                raise ApiError("invalid_argument", 400, f"{name} must be a string")
        if not (old_text or "").strip() and not (new_text or "").strip():
            raise ApiError("invalid_argument", 400, "old_text or new_text is required")
        request_id = payload.get("request_id")
        if request_id is not None and not isinstance(request_id, str):
            raise ApiError("invalid_argument", 400, "request_id must be a string")
        from companion_memory.contracts import CorrectionRequest, Scope

        with self.console._lock:
            receipt = asyncio.run(self.service.core.correction_service.correct(
                CorrectionRequest(
                    scope=Scope(self.console.character, scope["subject"]),
                    action="replace",
                    old_text=(old_text or "").strip(),
                    new_text=(new_text or "").strip(),
                    request_id=(request_id or None),
                ),
            ))
        return {
            "correction_id": receipt.correction_id,
            "version": receipt.version,
            "applied": bool(receipt.applied),
            "duplicate": bool(receipt.duplicate),
            "status": receipt.status,
            "invalidated_fact_ids": list(receipt.invalidated_fact_ids),
            "replacement_fact_id": receipt.replacement_fact_id,
        }

    def memory_forget(self, payload: Mapping, identity: Mapping) -> dict:
        """Delete exactly what the speaker named, in their own subject only.

        `target` (type+id) is the precise form; free `text` is resolved against
        the subject's own entries, and several matches are reported instead of
        guessed. Nothing here reaches another subject, another plugin's keys or
        the whole store.
        """
        self.require_memory_tools(identity)
        scope = self._memory_scope(payload, identity)
        from companion_memory.contracts import DeletionRequest, DeletionTarget, Scope

        target = payload.get("target")
        source_key = payload.get("source_key")
        text = payload.get("text")
        episode_id = payload.get("episode_id")
        for name, value in (("source_key", source_key), ("text", text), ("episode_id", episode_id)):
            if value is not None and not isinstance(value, str):
                raise ApiError("invalid_argument", 400, f"{name} must be a string")
        targets: list[DeletionTarget] = []
        if isinstance(target, Mapping):
            target_type = str(target.get("type") or "")
            target_id = str(target.get("id") or "")
            if target_type not in ("fact", "reflection"):
                raise ApiError("invalid_argument", 400, "target.type must be fact or reflection")
            if not target_id:
                raise ApiError("invalid_argument", 400, "target.id is required")
            if not self.service.core.deletion_service.owns_entry(
                self.console.character, scope["subject"], target_type, target_id,
            ):
                raise ApiError("forbidden", 403, "target is outside this subject")
            targets.append(DeletionTarget(target_type, target_id))
        if (episode_id or "").strip():
            raise ApiError("forbidden", 403, "episode deletion requires the management interface")
        if (source_key or "").strip():
            from ..event_sources import parse_source_key

            parsed = parse_source_key(source_key.strip()) or {}
            if (parsed.get("character") != self.console.character
                    or parsed.get("subject") != scope["subject"]
                    or parsed.get("plugin_id") != identity.get("plugin_id")):
                raise ApiError("forbidden", 403, "source is outside this client identity")
        if not targets and not (source_key or "").strip() and not (episode_id or "").strip():
            needle = (text or "").strip()
            if not needle:
                raise ApiError("invalid_argument", 400, "target, source_key, episode_id or text is required")
            listed = self.service.core.deletion_service.deletable_entries(
                self.console.character, query=needle,
                types=("fact", "reflection"), limit=8, subject=scope["subject"],
            )
            matches = [item for item in listed.get("items") or ()
                       if needle in str(item.get("text") or "")]
            if not matches:
                raise ApiError("not_found", 404, "nothing in this conversation's memory matches that")
            if len(matches) > 1:
                # Several candidates is an answer, not a failure: the caller
                # repeats the question instead of deleting the wrong row.
                raise ApiError(
                    "ambiguous_target", 409,
                    "several memories match; ask which one",
                    extra={"candidates": [
                        {"type": item.get("type"), "id": item.get("id"),
                         "text": str(item.get("text") or "")[:200]}
                        for item in matches
                    ]},
                )
            targets.append(DeletionTarget(str(matches[0]["type"]), str(matches[0]["id"])))

        request = DeletionRequest(
            scope=Scope(self.console.character, scope["subject"]),
            mode="episode" if (episode_id or "").strip() else "entry",
            targets=tuple(targets),
            source_keys=((source_key or "").strip(),) if (source_key or "").strip() else (),
            episode_ids=((episode_id or "").strip(),) if (episode_id or "").strip() else (),
            reason="user_request_in_conversation",
        )
        with self.console._lock:
            receipt = asyncio.run(self.service.core.deletion_service.forget(request))
        view = self.console._deletion_receipt_view(receipt)
        view["subject"] = scope["subject"]
        return view

    def protocol_post(self, path: str, payload: Mapping):
        core = self.service.core
        if path == "/v1/context/reserve":
            return self.service.protocol.reserve(payload)
        if path == "/v1/context/validate":
            context = self.service.protocol.validate(str(payload.get("context_receipt_id") or ""), self.service.protocol.scope(payload))
            return {"status": "valid", "context_receipt_id": context["context_receipt_id"], "persona_version": context["persona_version"]}
        if path == "/v1/events":
            return asyncio.run(self.service.protocol.event(payload))
        if path == "/v1/receipts":
            return self.service.protocol.receipt(payload)
        if path == "/v1/maintenance":
            result = asyncio.run(core.maintenance.run_once(self.console.character))
            return asdict(result)
        if path == "/v1/cleanup/retry":
            force = payload.get("force", False)
            if not isinstance(force, bool):
                raise ApiError("invalid_argument", 400, "force must be a boolean")
            receipt = asyncio.run(core.deletion_service.retry(self.console.character, str(payload.get("deletion_id") or ""), force=force))
            return self.console._deletion_receipt_view(receipt)
        if path == "/v1/cleanup/retract-correction":
            receipt = asyncio.run(core.correction_service.retract(self.console.character, str(payload.get("correction_id") or "")))
            return asdict(receipt)
        if path.startswith("/v1/cleanup/") and path.endswith("/ack"):
            deletion_id = path.split("/")[3]
            status = payload.get("status")
            if status == "completed":
                status = "cleaned"
            if status not in ("cleaned", "failed") or not payload.get("plugin_id"):
                raise ApiError("invalid_argument", 400, "plugin_id and a valid status are required")
            receipt = asyncio.run(core.deletion_service.record_plugin_receipt(
                self.console.character, deletion_id, payload["plugin_id"], status,
                cleaned=payload.get("cleaned", []), failed=payload.get("failed", [])))
            return self.console._deletion_receipt_view(receipt)
        return None

    def get(self, path: str, query: Mapping[str, list[str]], *, identity: Mapping | None = None) -> Any:
        if path == "/v1/host/persona":
            self.require_host_persona(identity or {})
            return self.host_persona()
        # The conversation memory tools are capability routes like the host
        # persona one: dispatched before the width check and re-checked here,
        # so a routing mistake cannot hand them to a normal client.
        if path == "/v1/memory/entries":
            return self.memory_entries({key: values[0] for key, values in query.items() if values},
                                       identity or {})
        self.check_scope({key: values[0] for key, values in query.items() if values})
        result = self.protocol_get(path, {key: values[0] for key, values in query.items() if values})
        if result is not None:
            return result
        if path == "/v1/initialization":
            return self.initialization()
        if path == "/v1/bootstrap":
            return self.bootstrap()
        if path == "/v1/export":
            return self.console.export()[0]
        if path == "/v1/engine/status":
            return dict(self.console._engine_status(),
                        capabilities=self.service.state.to_dict(),
                        config_missing=self.config_missing)
        return self.console.get("/api/" + path.removeprefix("/v1/"), query)

    def post(self, path: str, payload: Mapping[str, Any], *, identity: Mapping | None = None) -> dict[str, Any]:
        if path in ("/v1/host/persona", "/v1/host/persona/publish"):
            self.require_host_persona(identity or {})
            self.check_scope(payload)
            if path == "/v1/host/persona":
                return self.host_revise(payload)
            return self.host_publish(payload)
        if path in ("/v1/memory/correct", "/v1/memory/forget"):
            if path == "/v1/memory/correct":
                return self.memory_correct(payload, identity or {})
            return self.memory_forget(payload, identity or {})
        self.check_scope(payload)
        result = self.protocol_post(path, payload)
        if result is not None:
            return result
        if path == "/v1/characters":
            return self.initialize(payload)
        if path in ("/v1/demo/reset", "/v1/persona/sync"):
            raise ApiError("not_found", 404, "route is not part of the standalone service")
        with self.console._lock:
            result = self.console.post("/api/" + path.removeprefix("/v1/"), payload)
        if result.get("conflict"):
            raise ApiError("revision_conflict", 409, "base_revision changed; keep the draft and refresh")
        if result.get("status") == "rejected":
            raise ApiError("rejected", 422, str(result.get("reason") or "operation rejected"))
        self.service._refresh_chat_capability()
        return result
