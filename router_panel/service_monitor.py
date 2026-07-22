from __future__ import annotations

import json
import re
from typing import Any

from .core import DATA_DIR, CommandResult, atomic_write_text, run_command


SERVICE_MONITOR_CONFIG_PATH = DATA_DIR / "service-monitor.json"
DEFAULT_SERVICE_MONITOR_CONFIG = {"services": []}
SERVICE_ACTIONS = {"start", "stop", "restart"}
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}\.service$")
PROTECTED_SERVICE_NAMES = {
    "router-panel.service",
    "router-panel-agent.service",
}


def normalize_service_name(value: str) -> tuple[str, str | None]:
    name = value.strip()
    if not name:
        return "", None
    if "." not in name:
        name = f"{name}.service"
    if not SERVICE_NAME_RE.fullmatch(name):
        return "", f"服务名称无效：{value.strip()}"
    if name in PROTECTED_SERVICE_NAMES:
        return "", f"不能监控当前面板自身服务：{name}"
    return name, None


def normalize_service_list(value: str | list[Any]) -> tuple[list[str], str | None]:
    if isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raw_items = str(value).replace("\n", ",").replace(";", ",").split(",")

    services: list[str] = []
    for raw_item in raw_items:
        service, error = normalize_service_name(raw_item)
        if error:
            return [], error
        if service:
            services.append(service)
    return list(dict.fromkeys(services)), None


def normalize_service_monitor_config(payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    services, error = normalize_service_list(payload.get("services", []))
    if error:
        return DEFAULT_SERVICE_MONITOR_CONFIG.copy(), error
    return {"services": services}, None


def load_service_monitor_config() -> dict[str, Any]:
    try:
        payload = json.loads(SERVICE_MONITOR_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return DEFAULT_SERVICE_MONITOR_CONFIG.copy()
    if not isinstance(payload, dict):
        return DEFAULT_SERVICE_MONITOR_CONFIG.copy()
    config, error = normalize_service_monitor_config(payload)
    return DEFAULT_SERVICE_MONITOR_CONFIG.copy() if error else config


def save_service_monitor_config(config: dict[str, Any]) -> None:
    atomic_write_text(
        SERVICE_MONITOR_CONFIG_PATH,
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )


def get_service_state(service: str) -> dict[str, str]:
    normalized, error = normalize_service_name(service)
    if error:
        return {"name": service, "status": "unknown", "level": "error"}

    result = run_command(["systemctl", "is-active", normalized], timeout=8)
    status = (result.output or result.stdout or "").strip().splitlines()
    state = status[0] if status else "unknown"
    level = "ok" if state == "active" else "error"
    return {"name": normalized, "status": state, "level": level}


def gather_service_monitor_status() -> dict[str, Any]:
    config = load_service_monitor_config()
    services = [get_service_state(service) for service in config["services"]]
    return {"services": services, "errors": []}


def run_service_action(service: str, action: str) -> CommandResult:
    normalized, error = normalize_service_name(service)
    if error:
        return CommandResult(False, error)
    if action not in SERVICE_ACTIONS:
        return CommandResult(False, "不支持的服务操作")
    return run_command(["systemctl", action, normalized], timeout=30)


__all__ = [
    "DEFAULT_SERVICE_MONITOR_CONFIG",
    "SERVICE_MONITOR_CONFIG_PATH",
    "gather_service_monitor_status",
    "load_service_monitor_config",
    "normalize_service_list",
    "normalize_service_monitor_config",
    "normalize_service_name",
    "run_service_action",
    "save_service_monitor_config",
]
