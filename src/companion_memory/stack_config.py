"""One explicit TOML configuration; paths are relative to its own directory."""
from __future__ import annotations

import os
import math
import re
import sys
import tomllib
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .adapters.model_http import ModelSettings
from .service.memory_service import ServiceSettings
from .service.memory_service import _with_console_model_config


class StackConfigError(ValueError):
    pass


@dataclass(frozen=True)
class StackConfig:
    path: Path
    roots: dict[str, Path]
    service: ServiceSettings
    python: Path
    display_name: str
    subject: str
    request_capacity: int
    startup_timeout_s: float
    host: dict[str, Any]

    @property
    def url(self) -> str:
        return f"http://{self.service.host}:{self.service.port}"

    def missing(self) -> list[str]:
        model = _with_console_model_config(self.service).model
        fields = []
        for tier in ("summary", "correction"):
            if not model.models.get(tier):
                fields.append(f"models.service.tiers.{tier}")
        if not model.base_url:
            fields.append("models.service.base_url")
        if not model.api_key_stored and (not model.api_key_env or not os.environ.get(model.api_key_env)):
            fields.append("models.service.api_key_env (environment variable not set)")
        return fields


def _is_source_tree(python: Path) -> bool:
    """Whether this interpreter resolves `companion_memory` from a checkout.

    Used only to relax the version pin: a checkout's version changes with every
    edit, and the interpreter is running exactly that code. An installed wheel
    keeps the strict check, because there the pin is what guarantees the launcher
    and the package agree.
    """
    try:
        import subprocess

        completed = subprocess.run(
            [str(python), "-c",
             "import companion_memory, pathlib;"
             "print(pathlib.Path(companion_memory.__file__).resolve())"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    module_path = completed.stdout.strip()
    if not module_path:
        return False
    # `<repo>/src/companion_memory/__init__.py` lives beside a `pyproject.toml`
    # in `<repo>`; a wheel install does not.
    package_dir = Path(module_path).parent
    source_root = package_dir.parent
    return (source_root / "pyproject.toml").is_file() and (source_root / "src").is_dir()


def _path(base: Path, value: Any, field: str, *, resolve_symlinks: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise StackConfigError(f"{field}: an explicit path is required")
    path = Path(value).expanduser()
    absolute = path if path.is_absolute() else base / path
    # A POSIX venv selects its environment through the invoked symlink path.
    return absolute.resolve() if resolve_symlinks else absolute.absolute()


def _number(table: dict, key: str, default: float, *, integer=False):
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise StackConfigError(f"service.{key}: must be positive")
    if integer and not isinstance(value, int):
        raise StackConfigError(f"service.{key}: must be an integer")
    return value


def _table(raw: dict, key: str, prefix: str = "") -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise StackConfigError(f"{prefix}{key}: expected a TOML table")
    return value


def load_config(path: Path | str) -> StackConfig:
    path = Path(path).expanduser().resolve()
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise StackConfigError(f"{path}: {exc}") from exc
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise StackConfigError("schema_version: expected 1")
    paths = _table(raw, "paths")
    roots = {key: _path(path.parent, paths.get(key), f"paths.{key}")
             for key in ("memory", "host", "logs", "state")}
    for name, root in roots.items():
        if root == Path(root.anchor) or root == path or path.is_relative_to(root):
            raise StackConfigError(f"paths.{name}: must be a dedicated runtime directory")
        for other, other_root in roots.items():
            if name != other and root.is_relative_to(other_root):
                raise StackConfigError(f"paths.{name}: overlaps paths.{other}")
        for parent in (root, *root.parents):
            if (parent / ".git").exists():
                raise StackConfigError(f"paths.{name}: runtime must be outside a source repository")
    character = _table(raw, "character")
    character_id = character.get("character_id", "")
    if not isinstance(character_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", character_id):
        raise StackConfigError("character.character_id: use 1 to 64 letters, digits, _ or -")
    subject = character.get("subject", "private")
    display = character.get("display_name", character_id)
    if not isinstance(subject, str) or not subject.strip():
        raise StackConfigError("character.subject: a nonempty identity is required")
    if not isinstance(display, str) or not display.strip() or len(display) > 80:
        raise StackConfigError("character.display_name: use 1 to 80 characters")
    runtime = _table(raw, "runtime")
    python = _path(path.parent, runtime.get("python"), "runtime.python", resolve_symlinks=False)
    if not python.is_file():
        raise StackConfigError(f"runtime.python: interpreter does not exist: {python}")
    # A pinned build and the installed one must agree; otherwise the launcher is
    # running code the config was not written for. A source checkout is the
    # exception: its version moves on every edit, and the code being run *is*
    # the configured tree, so a mismatch there is expected rather than an error.
    configured_version = runtime.get("package_version")
    installed_version = version("rin-memory")
    if configured_version != installed_version and not _is_source_tree(python):
        raise StackConfigError(
            "runtime.package_version: install the configured build with "
            f"runtime.python (configured {configured_version!r}, "
            f"installed {installed_version!r})",
        )
    cfg = _table(raw, "service")
    host = cfg.get("host", "127.0.0.1")
    if host != "127.0.0.1":
        raise StackConfigError("service.host: this release listens on 127.0.0.1")
    port = _number(cfg, "port", 48931, integer=True)
    if port > 65535:
        raise StackConfigError("service.port: must be at most 65535")
    model = _table(_table(raw, "models"), "service", "models.")
    if "api_key" in model:
        raise StackConfigError("models.service.api_key_env: store an environment variable name, not api_key")
    tiers = model.get("tiers", {})
    if not isinstance(tiers, dict) or any(not isinstance(v, str) for v in tiers.values()):
        raise StackConfigError("models.service.tiers: model names must be strings")
    tiers = {key: value.strip() for key, value in tiers.items() if value.strip()}
    base_url = model.get("base_url", "")
    key_env = model.get("api_key_env", "")
    if not isinstance(base_url, str) or (base_url and urlsplit(base_url).scheme not in ("http", "https")):
        raise StackConfigError("models.service.base_url: expected an HTTP(S) URL")
    if not isinstance(key_env, str) or (key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env)):
        raise StackConfigError("models.service.api_key_env: expected an environment variable name")
    clients = _table(raw, "clients")
    for client, spec in clients.items():
        if client == "console" or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", client) or not isinstance(spec, dict):
            raise StackConfigError("clients: use named client tables; console is reserved")
        if not isinstance(spec.get("token_env"), str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", spec["token_env"]):
            raise StackConfigError(f"clients.{client}.token_env: expected an environment variable name")
        subjects = spec.get("subjects", [subject])
        if not isinstance(subjects, list) or not subjects or any(not isinstance(item, str) or not item.strip() for item in subjects):
            raise StackConfigError(f"clients.{client}.subjects: expected a nonempty list of identities")
        if not isinstance(spec.get("plugin_id", ""), str):
            raise StackConfigError(f"clients.{client}.plugin_id: expected a string")
        if not isinstance(spec.get("host_persona", False), bool):
            raise StackConfigError(f"clients.{client}.host_persona: expected a boolean")
        if not isinstance(spec.get("memory_tools", False), bool):
            raise StackConfigError(f"clients.{client}.memory_tools: expected a boolean")
        if not isinstance(spec.get("persona_teach", False), bool):
            raise StackConfigError(f"clients.{client}.persona_teach: expected a boolean")
        if not isinstance(spec.get("memory_outputs", False), bool):
            raise StackConfigError(f"clients.{client}.memory_outputs: expected a boolean")
        # M6-A: the plugin ids this client may cite as a saved-message source when
        # it submits an explicit teaching. Empty (the default) trusts no plugin,
        # so an upgrade alone never widens the QQ path.
        trusted = spec.get("trusted_plugins", [])
        if not isinstance(trusted, list) or any(
            not isinstance(item, str) or not item.strip() for item in trusted
        ):
            raise StackConfigError(
                f"clients.{client}.trusted_plugins: expected a list of plugin ids",
            )
    settings = ServiceSettings(
        memory_root=roots["memory"], local_state_root=roots["state"] / "memory-service",
        character=character_id, host=host, port=port,
        maintenance_interval_s=_number(cfg, "maintenance_interval_s", 60),
        chat_timeout_s=_number(cfg, "chat_timeout_s", 60),
        context_budget_ms=_number(cfg, "context_budget_ms", 1000, integer=True),
        max_body_bytes=_number(cfg, "max_body_bytes", 2 * 1024 * 1024, integer=True),
        max_in_flight_chat=_number(cfg, "max_in_flight_chat", 4, integer=True),
        shutdown_timeout_s=_number(cfg, "shutdown_timeout_s", 30),
        require_published_persona=True,
        clients=clients,
        max_pending_events=_number(cfg, "max_pending_events", 1024, integer=True),
        model=ModelSettings(
            base_url=base_url, api_key_env=key_env, models=tiers,
            timeout_s=_number(cfg, "model_timeout_s", 60),
            max_concurrency=_number(cfg, "model_concurrency", 4, integer=True),
            proxy=model.get("proxy", ""),
        ),
    )
    host_config = _table(raw, "host")
    host_config["model_settings"] = _table(_table(raw, "models"), "host", "models.")
    presentation = _table(raw, "presentation")
    host_config["live2d_model"] = presentation.get("live2d_model", "")
    host_config["voice_id"] = presentation.get("voice_id", "")
    if not isinstance(host_config.get("enabled", False), bool):
        raise StackConfigError("host.enabled: expected a boolean")
    for key in ("source", "python", "shell", "plugin_source"):
        if host_config.get(key):
            resource = _path(path.parent, host_config[key], f"host.{key}", resolve_symlinks=key != "python")
            if not resource.exists():
                raise StackConfigError(f"host.{key}: path does not exist: {resource}")
            host_config[key] = str(resource)
    if _table(raw, "evolution").get("initial_mode", "off") != "off":
        raise StackConfigError("evolution.initial_mode: initialize as off; select propose/auto in the console")
    return StackConfig(
        path, roots, settings, python, display.strip(), subject.strip(),
        _number(cfg, "request_capacity", 16, integer=True),
        _number(cfg, "startup_timeout_s", 20), host_config,
    )


def initialize(path: Path | str) -> StackConfig:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # TOML literal strings preserve Windows backslashes without interpolation.
        template = f'''schema_version = 1

[runtime]
python = '{sys.executable}'
package_version = '{version("rin-memory")}'

[paths]
memory = '../memory'
host = '../host'
logs = '../logs'
state = '../state'

[character]
character_id = 'companion'
display_name = '新伙伴'
subject = 'private'

[service]
host = '127.0.0.1'
port = 48931
context_budget_ms = 1000
chat_timeout_s = 60
model_timeout_s = 60
model_concurrency = 4
request_capacity = 16
max_in_flight_chat = 4
max_pending_events = 1024
max_body_bytes = 2097152
startup_timeout_s = 20
shutdown_timeout_s = 30
maintenance_interval_s = 60

[models.service]
base_url = ''
api_key_env = 'COMPANION_MODEL_API_KEY'
proxy = ''

[models.service.tiers]
summary = ''
correction = ''
conversation = ''

[host]
# Install the companion_host.py adapter, then set source/python and enable.
enabled = false
client_id = 'neko-desktop'
source = ''
python = ''
shell = ''
plugin_source = ''
port = 48911
memory_port = 48912
tool_port = 48915
plugin_port = 48916
startup_timeout_s = 120

[clients.neko-desktop]
token_env = 'COMPANION_NEKO_TOKEN'
subjects = ['private']
# The desktop host may read, revise and publish the semantic persona card.
# Off by default; enabling it is an explicit grant of that capability only.
host_persona = false
# A chat client may let the user correct or forget their own facts from inside
# the conversation. Independent of host_persona and limited to the subjects
# above; off by default.
memory_tools = false
# A chat client may submit an explicit teaching ("以后别每次都总结") that points
# at one of its own saved messages, and take one back. It cannot post rule
# text or edit any other section. Independent of the two grants above; off by
# default.
persona_teach = false
# A chat client may record assistant output (a proactive message or a task
# result) and report which parts the platform actually delivered. Only the
# delivered parts become shared conversation, so this grant cannot fabricate an
# experience; it can only state what really arrived. Off by default.
memory_outputs = false
# Plugin ids whose saved messages this client may cite when it submits an
# explicit teaching. Only consulted together with persona_teach: a plugin id
# never grants the capability, and it never widens the subject or character
# scope. Empty means "trust no plugin", which keeps the previous behaviour
# (plugin sources refused) until a specific plugin is granted on purpose.
trusted_plugins = []

[models.host]
base_url = ''
api_key_env = 'COMPANION_HOST_API_KEY'
conversation = ''
realtime = ''

[presentation]
live2d_model = ''
voice_provider = ''
voice_id = ''
enabled_plugins = []

[evolution]
# The service persists off/propose/auto; change it in the management console.
initial_mode = 'off'
'''
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(template)
        except FileExistsError:
            pass
    config = load_config(path)
    for root in config.roots.values():
        root.mkdir(parents=True, exist_ok=True)
    return config
