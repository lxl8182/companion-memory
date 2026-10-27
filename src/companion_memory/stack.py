"""Initialize and supervise a standalone stack from one explicit config."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any

from .persistence.atomic import FileSystemAtomicStore
from .persistence.ownership import acquire_root_lock
from .stack_config import StackConfig, StackConfigError, initialize, load_config

STORE = FileSystemAtomicStore()
_CHILDREN: dict[str, subprocess.Popen] = {}


class StackError(RuntimeError):
    pass


def _state_path(config: StackConfig) -> Path:
    return config.roots["state"] / "stack.json"


def _request(url: str, *, payload=None, token="", timeout=2):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "X-Companion-Client": "console",
        "X-Companion-Token": token,
    })
    # Loopback traffic must not inherit an HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denied means it is alive, but we do not own it.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def status(config: StackConfig) -> dict:
    record = STORE.load(_state_path(config)) or {}
    result = {
        "config": str(config.path), "character": config.service.character,
        "roots": {name: str(root) for name, root in config.roots.items()},
        "instance_id": record.get("instance_id"), "pid": record.get("pid"),
        "status": "stopped", "url": record.get("url", config.url),
        "missing": config.missing(), "host": {"status": "not_connected"},
    }
    if record.get("status") == "stopped":
        result["exit"] = record.get("exit")
        return result
    try:
        health = _request(result["url"] + "/v1/health")
    except (OSError, urllib.error.URLError, ValueError):
        if record.get("pid") and _alive(record["pid"]):
            result["status"] = "unreachable"
        elif record:
            result["status"] = "exited"
        return result
    if not record or health.get("instance_id") != record.get("instance_id"):
        result["status"] = "port_conflict"
        return result
    result.update(status="running", capabilities=health)
    if config.host.get("enabled"):
        from .host import status as host_status
        result["host"] = host_status(config)
    return result


def start(config: StackConfig, *, open_browser=True) -> dict:
    for root in config.roots.values():
        root.mkdir(parents=True, exist_ok=True)
    with acquire_root_lock(config.roots["state"], mode="cli"):
        current = status(config)
        if current["status"] == "running":
            if config.host.get("enabled"):
                from .host import start as start_host
                current["host"] = start_host(config)
            return dict(current, already_running=True)
        if current["status"] in ("unreachable", "port_conflict"):
            raise StackError(f"existing instance is {current['status']}; inspect status and logs")
        try:
            with socket.socket() as probe:
                if os.name != "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((config.service.host, config.service.port))
        except OSError as exc:
            raise StackError(f"service.port: {config.service.port} is occupied") from exc
        instance = uuid.uuid4().hex
        log_path = config.roots["logs"] / "memory-service.log"
        # Child imports the installed build, including its UI, from outside the repository.
        env = dict(os.environ)
        from .host import client_environment
        env.update(client_environment(config))
        env.pop("PYTHONPATH", None)
        env["PYTHONUTF8"] = "1"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                [str(config.python), "-m", "companion_memory.stack", "run-service",
                 "--config", str(config.path), "--instance", instance],
                cwd=config.roots["state"], env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, creationflags=flags,
                start_new_session=os.name != "nt",
            )
        _CHILDREN[instance] = process
        STORE.save(_state_path(config), {
            "instance_id": instance, "pid": process.pid, "status": "starting",
            "config": str(config.path), "url": config.url,
            "python": str(config.python), "log": str(log_path),
        })
        deadline = time.monotonic() + config.startup_timeout_s
        while time.monotonic() < deadline:
            if process.poll() is not None:
                _CHILDREN.pop(instance, None)
                raise StackError(f"service exited ({process.returncode}); see {log_path}")
            current = status(config)
            if current["status"] == "running" and current["capabilities"]["storage"]["status"] == "ready":
                if config.host.get("enabled"):
                    from .host import start as start_host
                    current["host"] = start_host(config)
                if open_browser:
                    webbrowser.open(config.url)
                return current
            time.sleep(0.1)
        # The Popen object identifies only the child created in this start attempt.
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise StackError(f"startup timed out and child did not exit; see {log_path}") from exc
        _CHILDREN.pop(instance, None)
        raise StackError(f"service startup timed out; see {log_path}")


def stop(config: StackConfig) -> dict:
    config.roots["state"].mkdir(parents=True, exist_ok=True)
    with acquire_root_lock(config.roots["state"], mode="cli"):
        if config.host.get("enabled"):
            from .host import stop as stop_host
            stop_host(config)
        current = status(config)
        if current["status"] in ("stopped", "exited"):
            return current
        if current["status"] != "running":
            raise StackError(f"instance is {current['status']}; inspect {config.roots['logs']}")
        bootstrap = _request(current["url"] + "/v1/bootstrap")
        _request(current["url"] + "/v1/stack/stop", payload={}, token=bootstrap["write_token"])
        deadline = time.monotonic() + config.service.shutdown_timeout_s + 5
        while time.monotonic() < deadline:
            record = STORE.load(_state_path(config)) or {}
            if record.get("instance_id") != current["instance_id"]:
                raise StackError("instance changed during stop")
            if record.get("status") == "stopped":
                child = _CHILDREN.pop(current["instance_id"], None)
                if child is not None:
                    try:
                        child.wait(timeout=max(.1, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        pass
                if not _alive(current["pid"]) and not _port_held(
                    config.service.host, config.service.port,
                ):
                    return status(config)
            time.sleep(0.1)
        # The service said it stopped but the endpoint is still bound: its
        # launcher's child outlived it (see `_port_held`). Killing the tree is
        # the only reliable exit on Windows, and leaving it would block the next
        # start with a port conflict that names an unrelated PID.
        if _port_held(config.service.host, config.service.port) and record.get(
            "status",
        ) == "stopped":
            _kill_tree(record.get("pid"))
            for _ in range(50):
                if not _port_held(config.service.host, config.service.port):
                    return status(config)
                time.sleep(0.1)
        raise StackError(f"shutdown deadline exceeded; see {config.roots['logs'] / 'memory-service.log'}")


def _kill_tree(pid: Any) -> None:
    """Best-effort kill of one process and its children (Windows: taskkill /T)."""
    if not isinstance(pid, int) or pid <= 0:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass


def _port_held(host: str, port: int) -> bool:
    """Whether something still listens on this endpoint.

    `_alive(pid)` is not enough on Windows: a venv `python.exe` is a launcher that
    re-executes the interpreter, so the PID recorded by `Popen` is the *parent*
    shim. It exits while its child keeps the socket -- `stop` then reported
    success and the port stayed bound, which is exactly how orphan instances
    accumulated. The socket itself is the ground truth.
    """
    with socket.socket() as probe:
        if os.name != "nt":
            # POSIX bind can fail on TIME_WAIT sockets after the listener exits.
            probe.settimeout(.2)
            return probe.connect_ex((host, int(port))) == 0
        try:
            # `SO_EXCLUSIVEADDRUSE` (Windows default on a fresh socket) makes
            # this bind fail while anyone listens, which is the answer we want.
            probe.bind((host, int(port)))
        except OSError:
            return True
    return False


async def run_service(config: StackConfig, instance: str) -> int:
    from .host import client_environment
    os.environ.update(client_environment(config))
    from importlib.resources import files
    from .cli import _install_stop_handlers
    from .service.management import ManagementApi
    from .service.memory_service import MemoryService
    from .service.server import MemoryApi, build_server

    # A source checkout carries the page in `webui/designs/tidebell` instead of
    # the packaged location; `_ui_resource` resolves both, so one-click launch
    # from the repository works while a broken *install* still fails loudly.
    from .service.server import _ui_resource

    missing = [
        resource for resource in ("index-watercolor.html", "live.js", "motion.js", "motion.css")
        if _ui_resource(resource) is None
    ]
    if missing:
        raise StackConfigError(
            "runtime.python: install the wheel with its management UI resources "
            f"(missing: {', '.join(missing)})",
        )
    service = MemoryService.start(config.service)
    server = None
    thread = None
    exit_info = {"status": "failed"}
    try:
        management = ManagementApi(service, display_name=config.display_name, subject=config.subject,
                                   config_missing=config.missing())
        api = MemoryApi(service, max_in_flight_chat=config.service.max_in_flight_chat)
        api.instance_id = instance
        server = build_server(
            api, host=config.service.host, port=config.service.port,
            max_body_bytes=config.service.max_body_bytes, management=management,
            request_capacity=config.request_capacity,
            request_timeout_s=config.service.chat_timeout_s + 5,
        )
        await service.start_background()
        stop_signal = asyncio.Event()
        _install_stop_handlers(stop_signal)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        while not stop_signal.is_set() and not server.stop_event.is_set():
            await asyncio.sleep(0.1)
        await asyncio.to_thread(server.shutdown)
        await service.stop()
        exit_info = {"status": "stopped", "drain": service._last_drain}
        return 0
    finally:
        if server is not None:
            if thread is not None:
                await asyncio.to_thread(server.shutdown)
            server.server_close()
        if thread is not None:
            thread.join(timeout=1)
        await service.stop()
        # Startup writes the record before probing readiness. Never overwrite another instance.
        record = STORE.load(_state_path(config)) or {}
        if record.get("instance_id") == instance:
            record.update(status="stopped", exit=exit_info)
            STORE.save(_state_path(config), record)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="New companion stack: init/start/stop/status")
    parser.add_argument("action", choices=("init", "start", "stop", "status", "run-service"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--instance", default="")
    args = parser.parse_args(argv)
    try:
        config = initialize(args.config) if args.action == "init" else load_config(args.config)
        if args.action == "init":
            result = {"config": str(config.path), "status": "initialized", "missing": config.missing()}
        elif args.action == "start":
            result = start(config, open_browser=not args.no_open)
        elif args.action == "stop":
            result = stop(config)
        elif args.action == "status":
            result = status(config)
        else:
            if not args.instance:
                raise StackError("run-service requires a launcher instance identity")
            return asyncio.run(run_service(config, args.instance))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (StackConfigError, StackError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
