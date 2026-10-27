"""Start the NEKO adapter with explicit roots, credentials and process ownership."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
import uuid
from pathlib import Path

from .persistence.atomic import FileSystemAtomicStore
from .service.memory_service import _with_console_model_config

STORE = FileSystemAtomicStore()


def client_environment(config):
    path = config.roots["state"] / "client_tokens.json"
    tokens = STORE.load(path) or {}
    for spec in config.service.clients.values():
        key = spec["token_env"]
        if not os.environ.get(key):
            tokens.setdefault(key, secrets.token_urlsafe(32))
    STORE.save(path, tokens)
    path.chmod(0o600)
    return {key: os.environ.get(key, value) for key, value in tokens.items()}


def status(config):
    from .stack import _alive, _request
    record = STORE.load(config.roots["state"] / "neko_host.json") or {}
    if not record:
        return {"status": "not_connected"}
    result = {key: record[key] for key in ("pid", "url", "config")}
    try:
        health = _request(record["url"] + "/api/rin-memory/status")
        if health.get("character_id") == config.service.character and Path(health.get("host_root", "")).resolve() == config.roots["host"]:
            return dict(result, status="running", memory=health)
        return dict(result, status="port_conflict")
    except (OSError, ValueError):
        return dict(result, status="starting" if _alive(record["pid"]) else "stopped")


def start(config):
    from .stack import StackError, _port_held, _request
    current = status(config)
    if current["status"] == "running":
        return dict(current, already_running=True)
    if current["status"] in ("starting", "port_conflict"):
        raise StackError("host is already starting or its port is occupied")
    host = config.host
    for key in ("source", "python", "client_id"):
        if not host.get(key):
            raise StackError(f"host.{key}: required when host.enabled=true")
    source = Path(host["source"])
    if not (source / "companion_host.py").is_file():
        raise StackError("host.source: install the standalone-memory host adapter")
    ports = {"MAIN_SERVER_PORT": int(host.get("port", 48911)),
             "MEMORY_SERVER_PORT": int(host.get("memory_port", 48912)),
             "TOOL_SERVER_PORT": int(host.get("tool_port", 48915)),
             "USER_PLUGIN_SERVER_PORT": int(host.get("plugin_port", 48916))}
    for port in ports.values():
        if _port_held("127.0.0.1", port):
            raise StackError(f"host port {port} is occupied")
    client_id = host["client_id"]
    spec = config.service.clients.get(client_id)
    if not spec:
        raise StackError(f"clients.{client_id}: configure the host client identity")
    env = dict(os.environ, **client_environment(config))
    model = _with_console_model_config(config.service).model
    model_key = model.api_key_stored or env.get(model.api_key_env, "")
    host_model = host.get("model_settings", {})
    dedicated = bool(host_model.get("base_url") or host_model.get("conversation"))
    model_name = host_model.get("conversation") or model.models.get("conversation")
    model_url = host_model.get("base_url") or model.base_url
    if dedicated:
        model_key = env.get(host_model.get("api_key_env", ""), "")
    if not model_name or not model_key:
        raise StackError("configure a conversation model and credential before starting NEKO")
    path = config.roots["host"] / "companion_host.json"
    previous = STORE.load(path) or {}
    display_name = _request(config.url + "/v1/bootstrap")["initialization"]["display_name"]
    settings = {"url": config.url, "client_id": client_id, "token": env[spec["token_env"]],
        "character_id": config.service.character, "subject": config.subject,
        "session_id": previous.get("session_id") or "neko-" + uuid.uuid4().hex,
        "host_root": str(config.roots["host"]), "state_root": str(config.roots["host"] / "state"),
        "display_name": display_name, "ports": ports,
        "model": model_name, "model_url": model_url, "model_key": model_key,
        "live2d_model": host.get("live2d_model", ""), "voice_id": host.get("voice_id", ""),
        # The host editor only renders the service card when this client was
        # granted the capability; the service re-checks it per request.
        "host_persona": bool(spec.get("host_persona", False))}
    STORE.save(path, settings)
    path.chmod(0o600)
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONPATH", None)
    command = [host["python"], str(source / "companion_host.py"), "--config", str(path)]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    log_path = config.roots["logs"] / "neko-host.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(command, cwd=source, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, creationflags=flags,
                                   start_new_session=os.name != "nt")
    record = {"pid": process.pid, "url": f"http://127.0.0.1:{ports['MAIN_SERVER_PORT']}", "config": str(path)}
    STORE.save(config.roots["state"] / "neko_host.json", record)
    deadline = time.monotonic() + float(host.get("startup_timeout_s", 120))
    while time.monotonic() < deadline:
        current = status(config)
        if current["status"] == "running":
            if host.get("shell"):
                shell = subprocess.Popen([host["shell"]], env=env, creationflags=flags)
                record["shell_pid"] = shell.pid
                STORE.save(config.roots["state"] / "neko_host.json", record)
            return current
        if process.poll() is not None:
            raise StackError(f"NEKO exited ({process.returncode}); see {log_path}")
        time.sleep(.2)
    from .stack import _kill_tree
    _kill_tree(process.pid)
    raise StackError(f"NEKO startup deadline exceeded; see {log_path}")


def stop(config):
    from .stack import StackError, _kill_tree, _request
    path = config.roots["state"] / "neko_host.json"
    record = STORE.load(path) or {}
    if not record:
        return
    if record.get("status") == "stopped":
        return
    current = status(config)
    if current["status"] == "stopped":
        record["status"] = "stopped"
        STORE.save(path, record)
        return
    if current["status"] != "running":
        raise StackError("host identity is unavailable; inspect the host process before stopping it")
    try:
        _request(record["url"] + "/api/rin-memory/flush", payload={}, timeout=8)
    except (OSError, ValueError):
        pass  # The host queue was persisted before generation and completion.
    # Flush is performed by the host shutdown handler where possible; pending
    # uploads are durable before this supervisor stops its own process tree.
    _kill_tree(record.get("shell_pid"))
    _kill_tree(record.get("pid"))
    record["status"] = "stopped"
    STORE.save(path, record)
