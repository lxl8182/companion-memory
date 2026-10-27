"""Local model configuration saved from the console (S05 settings page).

The console page needs a place to type the provider URL, the model name and the
credential, because a first run has no `stack.toml` yet and editing an
environment variable from a browser page is not a workflow. This module owns
that one file.

Boundaries this module keeps, because they are the reason the plan says
"凭据由环境或本机受控配置提供，不进入命令行参数、前端响应、普通日志和导出":

* the file lives under the memory root's `local_state`, never in the memory
  data itself and never in the repository;
* the file is created `0o600` on POSIX and is never served as a static asset or
  included in `/v1/export`;
* `public()` reports *whether* a credential is stored and where it came from,
  never the value, not even a masked prefix -- a masked prefix of a short key is
  most of the key;
* `load()` fails closed: an unreadable or malformed file yields empty settings
  and a reason, so a broken file closes chat instead of falling back to a
  surprising provider.

Only the fields the page can set are stored. Everything else (concurrency,
timeout) stays in `ServiceSettings`, so this file never becomes a second
configuration system.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from ..adapters.model_http import REASONING_EFFORTS, ModelSettings

#: File name inside the memory root's local-state directory.
MODEL_CONFIG_FILENAME = "model_config.json"
SCHEMA_VERSION = 1
#: Longest credential we accept; anything longer is a pasted document, not a key.
MAX_API_KEY_CHARS = 4096
#: Tiers the console can name. `conversation` drives chat; the others are the
#: core's own background purposes (extraction, reflections, evolution).
CONFIGURABLE_TIERS = ("conversation", "summary", "correction")
EFFORT_LABELS = {
    "": "跟随服务端默认",
    "low": "低 · 更快",
    "medium": "中 · 平衡",
    "high": "高 · 更深",
}
_LOCK = threading.Lock()


class ModelConfigError(ValueError):
    """A rejected configuration value; `field` names the form input."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def config_path(local_state_root: Path | str) -> Path:
    return Path(local_state_root) / MODEL_CONFIG_FILENAME


def load_model_config(local_state_root: Path | str) -> dict[str, Any]:
    """Stored fields, or empty ones when absent/unreadable.

    A malformed file is reported through `problem` rather than raising: the
    console must still render and explain what is wrong with the file.
    """
    path = config_path(local_state_root)
    empty = {
        "base_url": "", "api_key": "", "models": {}, "reasoning_effort": "",
    }
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(empty, problem="配置文件无法读取，已按未配置处理")
    if not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION:
        return dict(empty, problem="配置版本不支持，已按未配置处理")
    models = raw.get("models")
    models = {
        str(key): str(value).strip()
        for key, value in (models or {}).items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    } if isinstance(models, Mapping) else {}
    return {
        "base_url": str(raw.get("base_url") or "").strip(),
        "api_key": str(raw.get("api_key") or ""),
        "models": models,
        "reasoning_effort": str(raw.get("reasoning_effort") or "").strip().lower(),
    }


def save_model_config(
    local_state_root: Path | str, values: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and persist one full configuration; returns the stored fields.

    Validation lives here so the console and any future transport reject the
    same inputs with the same message. `api_key` is written verbatim (never
    transformed) so a key the provider accepts is not silently altered.
    """
    base_url = str(values.get("base_url") or "").strip()
    if base_url and not base_url.lower().startswith(("http://", "https://")):
        raise ModelConfigError("base_url", "接口地址需要以 http:// 或 https:// 开头")
    if base_url.endswith("/chat/completions"):
        # Accept the URL people copy from the provider docs, but store the base
        # so the adapter's own suffix is not doubled.
        base_url = base_url[: -len("/chat/completions")].rstrip("/")

    api_key = str(values.get("api_key") or "")
    if len(api_key) > MAX_API_KEY_CHARS:
        raise ModelConfigError("api_key", "Key 太长，请确认粘贴的是密钥本身")
    if api_key != api_key.strip():
        # Providers reject padded keys and the trimmed value is what was meant.
        api_key = api_key.strip()
    if "\n" in api_key or "\r" in api_key:
        raise ModelConfigError("api_key", "Key 不能包含换行")

    effort = str(values.get("reasoning_effort") or "").strip().lower()
    if effort not in REASONING_EFFORTS:
        raise ModelConfigError("reasoning_effort", "思考强度只能是默认/低/中/高")

    raw_models = values.get("models")
    if raw_models is None:
        raw_models = {}
    if not isinstance(raw_models, Mapping):
        raise ModelConfigError("models", "模型项必须是「档位: 模型名」的映射")
    models: dict[str, str] = {}
    for tier, name in raw_models.items():
        tier = str(tier).strip()
        name = str(name or "").strip()
        if tier not in CONFIGURABLE_TIERS:
            raise ModelConfigError("models", f"未知档位 {tier!r}")
        if name:
            models[tier] = name

    document = {
        "schema_version": SCHEMA_VERSION,
        "base_url": base_url,
        "api_key": api_key,
        "models": models,
        "reasoning_effort": effort,
    }
    path = config_path(local_state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.tmp-{os.getpid()}"
    with _LOCK:
        try:
            with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp, 0o600)
            except OSError:  # pragma: no cover - Windows ACLs differ
                pass
            os.replace(temp, path)
        finally:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass
    return {
        "base_url": base_url, "api_key": api_key,
        "models": models, "reasoning_effort": effort,
    }


def apply_model_config(
    settings: ModelSettings, stored: Mapping[str, Any],
) -> ModelSettings:
    """Overlay stored console values onto the configured settings.

    The service/stack file stays authoritative for everything the page does not
    own (tiers it did not mention, timeout, concurrency, proxy). A stored value
    replaces a configured one only when it is non-empty, so a page that never
    touched `summary` cannot silently drop the background tier.
    """
    base_url = str(stored.get("base_url") or "").strip() or settings.base_url
    models = dict(settings.models)
    for tier, name in (stored.get("models") or {}).items():
        if str(name or "").strip():
            models[str(tier)] = str(name).strip()
    return replace(
        settings,
        base_url=base_url,
        models=models,
        api_key_stored=str(stored.get("api_key") or ""),
        reasoning_effort=str(stored.get("reasoning_effort") or ""),
    )


def public_model_config(
    settings: ModelSettings, stored: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The page's view of the configuration: no credential, ever."""
    stored = stored or {}
    return {
        "base_url": settings.base_url,
        "models": dict(settings.models),
        "tiers": list(CONFIGURABLE_TIERS),
        "reasoning_effort": settings.reasoning_effort,
        "efforts": [
            {"value": value, "label": label}
            for value, label in EFFORT_LABELS.items()
        ],
        "api_key": {
            "stored": settings.api_key_source() == "console",
            "source": settings.api_key_source(),
            "present": bool(settings.api_key()),
            "env_name": settings.api_key_env,
        },
        "configured": settings.configured,
        "editable": True,
        "problem": stored.get("problem"),
    }


__all__ = [
    "CONFIGURABLE_TIERS",
    "MAX_API_KEY_CHARS",
    "MODEL_CONFIG_FILENAME",
    "ModelConfigError",
    "apply_model_config",
    "config_path",
    "load_model_config",
    "public_model_config",
    "save_model_config",
]
