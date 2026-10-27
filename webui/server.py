"""铃芽 RIN local WebUI server (stdlib only, loopback by default).

The server is a thin, read-mostly JSON facade over one explicitly supplied
memory root.  It never looks up NEKO data on its own, never talks to the
network and never starts a second service:

* GET  /api/meta /api/snapshot /api/overview /api/timeline /api/events/{id}
       /api/facts /api/reflections /api/persona /api/evolution /api/cleanup
       /api/state /api/search /api/export /api/engine/status
       /api/chat/history
* POST /api/persona/{revise,freeze,restore,publish,rollback,adopt,sync}
       /api/evolution/{settings,mode,evaluate,retry,propose}
       /api/evolution/{id}/{accept,reject,withdraw}
       /api/cleanup/{correct,delete} /api/demo/reset
       /api/chat/send
* GET  / redirects to the watercolour console
       designs/tidebell/index-watercolor.html; /designs/* serves that frontend
       and its assets; /testbed/* serves the isolated C04 test frontend.  All
       static routes are no-store, traversal-safe and use an explicit MIME map.

Modes: --demo (default) seeds a throwaway root; --memory-root reads an existing
root read-only unless --allow-write is passed, and may point at the real
nekodata/memory root (the demo builders still refuse it).  --character picks the
character directory; with one directory under the root it is auto-detected.
Writes are serialized by one process-wide
lock and every response is JSON with a uniform error envelope.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import shutil
import sys
import tempfile
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

WEBUI_DIR = Path(__file__).resolve().parent
ASSETS_DIR = WEBUI_DIR / "assets"
TESTBED_DIR = WEBUI_DIR / "testbed"
DESIGNS_DIR = WEBUI_DIR / "designs"
LANDING_PAGE = "/designs/tidebell/index-watercolor.html"
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import seed as seed_data  # noqa: E402  (same directory, script-mode import)

APP_NAME = "铃芽"
ABBREVIATION = "RIN"
VERSION = "0.1.0"
CHARACTER = seed_data.CHARACTER
DISPLAY_NAME = seed_data.DISPLAY_NAME
MAX_BODY_BYTES = 1024 * 1024
DEFAULT_PORT = 8787
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


class StartupError(Exception):
    """A configuration problem that ends the process with a fixed exit code."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


from companion_memory.service.console import RinApp as ConsoleApp, ApiError
from companion_memory.service.listening import ExclusiveAddrMixin


class RinApp(ConsoleApp):
    def __init__(self, **kwargs):
        super().__init__(
            **kwargs,
            close_core=seed_data.release_live_lock,
            reset_core=seed_data.build_memory_core,
        )


# -- HTTP handler ------------------------------------------------------------


class RinHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RinWebUI/0.1"
    sys_version = ""

    @property
    def app(self) -> RinApp:
        return self.server.app  # type: ignore[attr-defined]

    # -- logging: path/status only, never request or response bodies -----

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        del size
        path = urlsplit(self.path).path
        sys.stderr.write(f"[rin] {self.command} {path} {code}\n")

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        try:
            detail = format % args if args else format
        except (TypeError, ValueError):
            detail = format
        sys.stderr.write(f"[rin] request error: {detail}\n")

    # -- methods --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self._handle("POST")

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib name
        self._handle("HEAD")

    def _handle(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            # Read (and size/JSON-validate) the body before any routing
            # decision, so a 404/405 never leaves unread bytes on a keep-alive
            # connection (the next request line would otherwise be corrupted).
            payload = self._read_json_body() if method == "POST" else {}
            if path == "/api/export":
                if method != "GET":
                    raise ApiError("method_not_allowed", 405, "GET required")
                payload, filename = self.app.export()
                self._send_json(
                    200, payload,
                    extra={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                    },
                )
                return
            if path.startswith("/api/") or path == "/api":
                if method == "GET":
                    if _is_post_route(path) and not _is_get_route(path):
                        raise ApiError("method_not_allowed", 405, "method not allowed")
                    result = self.app.get(path, parse_qs(parsed.query))
                elif method == "POST":
                    if _is_get_route(path) and not _is_post_route(path):
                        raise ApiError("method_not_allowed", 405, "method not allowed")
                    result = self.app.post(path, payload)
                else:
                    raise ApiError("method_not_allowed", 405, "method not allowed")
                self._send_json(200, result)
                return
            if method not in ("GET", "HEAD"):
                raise ApiError("method_not_allowed", 405, "method not allowed")
            self._serve_static(path, head=(method == "HEAD"))
        except ApiError as exc:
            self._send_error_json(exc)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001 - never leak internals or text
            sys.stderr.write(f"[rin] internal error: {type(exc).__name__}\n")
            self._send_error_json(ApiError("internal", 500, "internal server error"))

    # -- request body ---------------------------------------------------

    def _read_json_body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError("invalid_request", 400, "chunked request bodies are not supported")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise ApiError("invalid_request", 400, "invalid Content-Length") from None
        if length < 0:
            raise ApiError("invalid_request", 400, "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise ApiError("payload_too_large", 413, "request body exceeds 1MB")
        data = self.rfile.read(length) if length else b""
        if len(data) != length:
            raise ApiError("invalid_request", 400, "incomplete request body")
        if not data:
            return {}
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError("invalid_json", 400, "request body is not valid JSON") from None
        if not isinstance(payload, dict):
            raise ApiError("invalid_json", 400, "request body must be a JSON object")
        return payload

    # -- responses ------------------------------------------------------

    def _send_json(
        self, status: int, payload: Any,
        *,
        extra: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, exc: ApiError) -> None:
        self._send_json(exc.status, {
            "error": {"code": exc.code, "message": exc.message},
        })

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    # -- static files ---------------------------------------------------

    def _serve_static(self, path: str, *, head: bool) -> None:
        if path in ("/", "/index.html"):
            # The watercolour console is the only shipped frontend.
            self.send_response(302)
            self.send_header("Location", LANDING_PAGE)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        elif path in ("/testbed", "/testbed/"):
            target = TESTBED_DIR / "index.html"
        elif path.startswith("/testbed/"):
            relative = path[len("/testbed/"):]
            if (
                not relative
                or relative.startswith("/")
                or "\\" in relative
                or ".." in relative.split("/")
            ):
                raise ApiError("not_found", 404, "testbed asset not found")
            target = (TESTBED_DIR / relative).resolve()
            try:
                target.relative_to(TESTBED_DIR.resolve())
            except ValueError:
                raise ApiError("not_found", 404, "testbed asset not found") from None
        elif path.startswith("/designs/"):
            relative = path[len("/designs/"):]
            if (
                not relative
                or relative.startswith("/")
                or "\\" in relative
                or ".." in relative.split("/")
            ):
                raise ApiError("not_found", 404, "design asset not found")
            target = (DESIGNS_DIR / relative).resolve()
            try:
                target.relative_to(DESIGNS_DIR.resolve())
            except ValueError:
                raise ApiError("not_found", 404, "design asset not found") from None
        elif path.startswith("/assets/"):
            relative = path[len("/assets/"):]
            if (
                not relative
                or relative.startswith("/")
                or "\\" in relative
                or ".." in relative.split("/")
            ):
                raise ApiError("not_found", 404, "asset not found")
            target = (ASSETS_DIR / relative).resolve()
            try:
                target.relative_to(ASSETS_DIR.resolve())
            except ValueError:
                raise ApiError("not_found", 404, "asset not found") from None
        else:
            raise ApiError("not_found", 404, f"unknown path {path!r}")
        if not target.is_file():
            raise ApiError("not_found", 404, "file not found")
        content_type = MIME_TYPES.get(target.suffix.lower())
        if content_type is None:
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        body = target.read_bytes()
        if head:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self._send_bytes(200, body, content_type)


#: Route tables used to answer 405 (instead of 404) on a known path.
_GET_EXACT = frozenset({
    "/api/meta", "/api/snapshot", "/api/overview", "/api/timeline",
    "/api/facts", "/api/reflections", "/api/persona", "/api/evolution",
    "/api/cleanup", "/api/state", "/api/search", "/api/export",
    "/api/engine/status", "/api/chat/history", "/api/model/config",
    "/testbed", "/testbed/",
})
_POST_EXACT = frozenset({
    "/api/persona/revise", "/api/persona/freeze", "/api/persona/restore",
    "/api/persona/publish", "/api/persona/rollback",
    "/api/persona/adopt", "/api/persona/sync",
    "/api/evolution/settings", "/api/evolution/mode",
    "/api/evolution/evaluate", "/api/evolution/retry",
    "/api/evolution/propose",
    "/api/cleanup/correct", "/api/cleanup/delete", "/api/demo/reset",
    "/api/chat/send", "/api/chat/clear", "/api/chat/edit", "/api/chat/regenerate",
    "/api/model/config", "/api/model/test",
})


def _is_get_route(path: str) -> bool:
    return path in _GET_EXACT or path.startswith("/api/events/") or path.startswith("/designs/")


def _is_post_route(path: str) -> bool:
    if path in _POST_EXACT:
        return True
    parts = path.strip("/").split("/")
    return (
        len(parts) == 4
        and parts[:2] == ["api", "evolution"]
        and parts[3] in ("accept", "reject", "withdraw")
    )


class RinHTTPServer(ExclusiveAddrMixin, ThreadingHTTPServer):
    daemon_threads = True


# -- startup -----------------------------------------------------------------


def _reject_nekodata(path_text: str, label: str) -> None:
    if "nekodata" in str(path_text).lower():
        raise StartupError(
            f"refusing {label} path containing 'nekodata': {path_text}", code=2,
        )


def _resolve_character(root: Path, requested: str) -> str:
    """Pick the character directory to read under a live memory root.

    An explicit --character wins (and must exist). Otherwise a root with exactly
    one character directory uses it, so a real root such as nekodata/memory with
    a single 「小铃」 works without extra flags; anything ambiguous falls back to
    the demo character constant and fails loudly on the first read.
    """
    wanted = str(requested or "").strip()
    if wanted:
        if not (root / wanted).is_dir():
            raise StartupError(
                f"character {wanted!r} not found under memory root {root}", code=2,
            )
        return wanted
    try:
        candidates = sorted(
            child.name for child in root.iterdir()
            if child.is_dir() and not child.name.startswith(".")
            and child.name != "local_state"
        )
    except OSError:
        candidates = []
    if len(candidates) == 1:
        return candidates[0]
    if CHARACTER in candidates:
        return CHARACTER
    return CHARACTER


def _model_settings(args: argparse.Namespace):
    """Model configuration from the command line, or None when absent.

    The credential is never a flag value: `--model-api-key-env` names the
    environment variable, exactly like the service's own configuration.
    """
    from companion_memory.adapters.model_http import (
        HttpModelInvoker, ModelSettings,
    )

    base_url = str(getattr(args, "model_base_url", "") or "").strip()
    key_env = str(getattr(args, "model_api_key_env", "") or "").strip()
    models: dict[str, str] = {}
    for spec in getattr(args, "model", None) or ():
        if "=" not in str(spec):
            raise StartupError(f"--model expects tier=model, got {spec!r}", code=2)
        tier, name = str(spec).split("=", 1)
        if tier.strip() and name.strip():
            models[tier.strip()] = name.strip()
    if not base_url and not models:
        return None
    settings = ModelSettings(
        base_url=base_url, api_key_env=key_env, models=models,
        timeout_s=float(getattr(args, "model_timeout_s", 60.0) or 60.0),
    )
    return HttpModelInvoker(settings)


def _build_chat(core, *, character: str, root: Path, writable: bool, demo: bool,
                model=None):
    """One conversation bridge over the console's own core (never a 2nd core).

    `model` is the real invoker when the operator configured one; demo roots
    fall back to the labelled offline stand-in so the page can be exercised
    without a provider. A read-only console still gets a bridge: it serves the
    committed transcript and reports why sending is refused.
    """
    from companion_memory.service.chat import ChatBridge, DemoChatModel
    from companion_memory.service.memory_service import ServiceSettings

    demo_model = model is None and demo
    if demo_model:
        model = DemoChatModel()
    settings = ServiceSettings(
        memory_root=root, character=character, host="127.0.0.1", port=0,
    )
    return ChatBridge(
        core=core, character=character, model=model, settings=settings,
        memory_root=root, writable=writable, demo_model=demo_model,
    )


def build_app(args: argparse.Namespace) -> RinApp:
    if args.memory_root:
        root = Path(args.memory_root).expanduser().resolve()
        if not root.exists():
            raise StartupError(f"memory root does not exist: {root}", code=2)
        character = _resolve_character(root, args.character)
        core = seed_data.build_live_core(
            root, writable=bool(args.allow_write), character=character,
        )
        writable = bool(args.allow_write)
        return RinApp(
            core=core, mode="live", root=root,
            read_only=not writable, demo=False,
            character=character,
            chat=_build_chat(
                core, character=character, root=root, writable=writable,
                demo=False, model=_model_settings(args),
            ),
        )
    if args.demo_root:
        _reject_nekodata(args.demo_root, "--demo-root")
        root = Path(args.demo_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        core = seed_data.build_memory_core(root)
        return RinApp(
            core=core, mode="demo", root=root,
            read_only=False, demo=True,
            chat=_build_chat(
                core, character=CHARACTER, root=root, writable=True,
                demo=True, model=_model_settings(args),
            ),
        )
    root = Path(tempfile.mkdtemp(prefix="rin-webui-demo-"))
    core = seed_data.build_memory_core(root)
    return RinApp(
        core=core, mode="demo", root=root,
        read_only=False, demo=True, temporary_root=True,
        chat=_build_chat(
            core, character=CHARACTER, root=root, writable=True,
            demo=True, model=_model_settings(args),
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="铃芽 RIN local WebUI server (stdlib only, loopback by default)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    parser.add_argument("--demo", action="store_true", help="use a throwaway demo root")
    parser.add_argument("--memory-root", default="", help="explicit live/drill memory root")
    parser.add_argument("--demo-root", default="", help="reuse this demo root")
    parser.add_argument(
        "--allow-write", action="store_true",
        help="allow writes on --memory-root (default: read-only)",
    )
    parser.add_argument(
        "--character", default="",
        help="character directory under --memory-root (default: auto-detect)",
    )
    parser.add_argument(
        "--model", action="append", metavar="TIER=MODEL",
        help="map a model tier, e.g. conversation=your-model (repeatable)",
    )
    parser.add_argument(
        "--model-base-url", default="",
        help="OpenAI-compatible base URL for direct console conversation",
    )
    parser.add_argument(
        "--model-api-key-env", default="",
        help="environment variable holding the API key (never the key itself)",
    )
    parser.add_argument(
        "--model-timeout-s", type=float, default=60.0,
        help="per-call conversation timeout in seconds (default 60)",
    )
    parser.add_argument("--open", action="store_true", help="open the browser after start")
    args = parser.parse_args(argv)
    if args.memory_root and (args.demo or args.demo_root):
        parser.error("--memory-root cannot be combined with --demo/--demo-root")
    try:
        app = build_app(args)
    except StartupError as exc:
        print(f"[rin] {exc}", file=sys.stderr)
        return exc.code
    except Exception as exc:  # noqa: BLE001 - startup failure is a clean exit
        print(f"[rin] startup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    try:
        server = RinHTTPServer((args.host, int(args.port)), RinHandler)
    except OSError as exc:
        app.close()
        print(f"[rin] cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    server.app = app  # type: ignore[attr-defined]
    port = server.server_address[1]
    open_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{open_host}:{port}/"
    print(
        f"[rin] mode={app.mode} read_only={app.read_only} root={app.root}",
        flush=True,
    )
    print(f"[rin] listening on {url}", flush=True)
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n[rin] shutting down", file=sys.stderr)
    finally:
        server.server_close()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
