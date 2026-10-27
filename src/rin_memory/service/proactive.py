"""Scoped HTTP consumer for source-backed proactive generation."""

from __future__ import annotations

from typing import Mapping


async def handle(api, action: str, body: Mapping, identity: Mapping) -> dict:
    from .server import ApiError

    if not identity.get("memory_outputs"):
        raise ApiError("forbidden", "this identity has no output capability")
    scope = api.protocol.scope(body)
    service = api._service
    core = service.core
    character = service.settings.character
    proactive = core.proactive_service
    owner = scope["client_id"]
    session = scope["session_id"]
    if not session:
        raise ApiError("invalid_argument", "a bound session_id is required")
    if action == "observe":
        text = body.get("text")
        event = str(body.get("request_id") or "")
        if not isinstance(text, str) or not event:
            raise ApiError("invalid_argument", "text and request_id are required")
        return proactive.absorb_user_turn(
            character, text=text, subject=scope["subject"], scene=scope["scene"],
            request_id=owner + ":" + event,
        )
    if action == "poll":
        request = str(body.get("request_id") or "")
        if not request:
            raise ApiError("invalid_argument", "request_id is required")
        proactive.derive(character, subject=scope["subject"], scene=scope["scene"])
        selected = proactive.claim(
            character, subject=scope["subject"], scene=scope["scene"],
            owner=owner, session_id=session, request_id=request,
        )
        if selected["status"] != "ready":
            return selected
        candidate = selected["candidate"]
        try:
            context = await api.context(dict(body, text=candidate["text"]))
        except Exception:
            proactive.release_claim(
                character, candidate["candidate_id"], lease_id=candidate["lease_id"],
                owner=owner, session_id=session,
            )
            raise
        return {
            "status": "ready", "candidate": {
                key: candidate.get(key) for key in (
                    "candidate_id", "lease_id", "text", "why", "intent", "cause", "lease_until",
                )
            }, "context": context,
        }
    candidate_id = str(body.get("candidate_id") or "")
    lease_id = str(body.get("lease_id") or "")
    if not candidate_id or not lease_id:
        raise ApiError("invalid_argument", "candidate_id and lease_id are required")
    try:
        candidate = proactive.leased_candidate(character, candidate_id, lease_id, owner, session)
    except ValueError as exc:
        raise ApiError("forbidden", str(exc)) from exc
    if candidate.get("subject") != scope["subject"] or candidate.get("lease_scene", "") != scope["scene"]:
        raise ApiError("forbidden", "candidate differs from the authenticated scope")
    if action == "use":
        context = api.protocol.validate(str(body.get("context_receipt_id") or ""), scope)
        api.protocol._record_consumer_ref(
            body, scope, context, str(candidate.get("lease_request") or candidate_id), identity=identity,
        )
        return {"status": "recorded"}
    if action == "check":
        return proactive.check_delivery(
            character, candidate_id, lease_id=lease_id, owner=owner, session_id=session,
        )
    if action == "release":
        proactive.release_claim(character, candidate_id, lease_id=lease_id, owner=owner,
                                session_id=session, postpone=body.get("reason") == "model_pass")
        return {"status": "released"}
    if action == "complete":
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise ApiError("invalid_argument", "a non-empty output of at most 20000 characters is required")
        # A committed output survives the short-lived context receipt. A lost
        # completion response must recover that same output after restart.
        if not candidate.get("output_id"):
            api.protocol.validate(str(body.get("context_receipt_id") or ""), scope)
        result = proactive.complete_claim(
            character, candidate_id, lease_id=lease_id, owner=owner,
            session_id=session, text=text,
        )
        payload = result.to_dict()
        if result.output_id:
            payload["output"] = core.output_service.get(character, result.output_id, include_text=True)
        return payload
    raise ApiError("not_found", "unknown proactive operation")
