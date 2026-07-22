from __future__ import annotations

import json
import re
from ipaddress import ip_network
from typing import Any

from .core import DATA_DIR, CommandResult, atomic_write_text, command_exists, is_service_active, run_command


TAILSCALE_CONFIG_PATH = DATA_DIR / "tailscale.json"
TAILSCALE_LOGIN_URL_RE = re.compile(r"https://login\.tailscale\.com/[^\s]+")
DEFAULT_TAILSCALE_CONFIG = {
    "accept_routes": True,
    "advertise_routes": "",
}


def describe_tailscale_state(
    *,
    installed: bool,
    service_active: bool,
    backend_state: str = "",
    logged_in: bool = False,
    status_error: bool = False,
) -> dict[str, str]:
    if not installed:
        return {"state_label": "未安装", "state_level": "warning"}
    if not service_active:
        return {"state_label": "未运行", "state_level": "error"}
    if status_error:
        return {"state_label": "状态异常", "state_level": "error"}
    if logged_in:
        return {"state_label": "已连接", "state_level": "ok"}

    state_labels = {
        "NeedsLogin": "待登录",
        "Stopped": "已停止",
        "Starting": "启动中",
        "NoState": "未初始化",
    }
    return {
        "state_label": state_labels.get(backend_state) or backend_state or "未登录",
        "state_level": "warning",
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def normalize_advertise_routes(value: str) -> tuple[str, str | None]:
    raw_routes = [
        item.strip()
        for item in value.replace("\n", ",").replace(";", ",").split(",")
        if item.strip()
    ]
    if not raw_routes:
        return "", None

    normalized_routes: list[str] = []
    for route in raw_routes:
        try:
            network = ip_network(route, strict=False)
        except ValueError:
            return "", f"发布路由无效：{route}"
        normalized_routes.append(str(network))
    return ",".join(dict.fromkeys(normalized_routes)), None


def normalize_tailscale_config(payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    accept_routes = bool(payload.get("accept_routes", False))
    advertise_routes, error = normalize_advertise_routes(str(payload.get("advertise_routes", "")))
    if error:
        return DEFAULT_TAILSCALE_CONFIG.copy(), error
    return {
        "accept_routes": accept_routes,
        "advertise_routes": advertise_routes,
    }, None


def load_tailscale_config() -> dict[str, Any]:
    try:
        payload = json.loads(TAILSCALE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return DEFAULT_TAILSCALE_CONFIG.copy()
    if not isinstance(payload, dict):
        return DEFAULT_TAILSCALE_CONFIG.copy()
    config, error = normalize_tailscale_config(payload)
    return DEFAULT_TAILSCALE_CONFIG.copy() if error else config


def save_tailscale_config(config: dict[str, Any]) -> None:
    atomic_write_text(
        TAILSCALE_CONFIG_PATH,
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )


def build_tailscale_up_command(config: dict[str, Any]) -> list[str]:
    command = ["tailscale", "up"]
    if config.get("accept_routes"):
        command.append("--accept-routes")
    advertise_routes = str(config.get("advertise_routes", "")).strip()
    if advertise_routes:
        command.append(f"--advertise-routes={advertise_routes}")
    return command


def extract_login_url(output: str) -> str:
    match = TAILSCALE_LOGIN_URL_RE.search(output or "")
    return match.group(0) if match else ""


def gather_tailscale_status() -> dict[str, Any]:
    if not command_exists("tailscale"):
        return {
            "agent_available": True,
            "installed": False,
            "service_active": False,
            "logged_in": False,
            **describe_tailscale_state(installed=False, service_active=False),
            "backend_state": "",
            "tailscale_ips": [],
            "self_name": "",
            "user": "",
            "errors": ["未安装 Tailscale，请先自行安装后再使用"],
        }

    service_active = is_service_active("tailscaled.service")
    result = run_command(["tailscale", "status", "--json"], timeout=8)
    if not result.ok or not result.output:
        return {
            "agent_available": True,
            "installed": True,
            "service_active": service_active,
            "logged_in": False,
            **describe_tailscale_state(
                installed=True,
                service_active=service_active,
                status_error=True,
            ),
            "backend_state": "",
            "tailscale_ips": [],
            "self_name": "",
            "user": "",
            "errors": [result.output or "无法读取 Tailscale 状态"],
        }

    try:
        payload = json.loads(result.output)
    except json.JSONDecodeError:
        return {
            "agent_available": True,
            "installed": True,
            "service_active": service_active,
            "logged_in": False,
            **describe_tailscale_state(
                installed=True,
                service_active=service_active,
                status_error=True,
            ),
            "backend_state": "",
            "tailscale_ips": [],
            "self_name": "",
            "user": "",
            "errors": ["Tailscale 返回了无效状态"],
        }

    self_node = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    backend_state = str(payload.get("BackendState", ""))
    tailscale_ips = _string_list(self_node.get("TailscaleIPs"))
    if not tailscale_ips:
        tailscale_ips = _string_list(payload.get("TailscaleIPs"))
    user_map = payload.get("User") if isinstance(payload.get("User"), dict) else {}
    user_id = str(self_node.get("UserID", ""))
    user = user_map.get(user_id, {}) if isinstance(user_map.get(user_id), dict) else {}
    login_name = str(user.get("LoginName", ""))
    logged_in = bool(tailscale_ips) and backend_state not in {"NeedsLogin", "Stopped"}

    return {
        "agent_available": True,
        "installed": True,
        "service_active": service_active,
        "logged_in": logged_in,
        **describe_tailscale_state(
            installed=True,
            service_active=service_active,
            backend_state=backend_state,
            logged_in=logged_in,
        ),
        "backend_state": backend_state,
        "tailscale_ips": tailscale_ips,
        "self_name": str(self_node.get("DNSName") or self_node.get("HostName") or ""),
        "user": login_name,
        "errors": [],
    }


def start_tailscale_login(config: dict[str, Any]) -> dict[str, Any]:
    if not command_exists("tailscale"):
        return {
            "ok": False,
            "message": "未安装 Tailscale，请先自行安装后再使用",
            "login_url": "",
        }

    service = run_command(["systemctl", "enable", "--now", "tailscaled.service"], timeout=20)
    if not service.ok:
        return {
            "ok": False,
            "message": service.output or "启动 tailscaled.service 失败",
            "login_url": "",
        }

    command = build_tailscale_up_command(config)
    result = run_command(command, timeout=8)
    login_url = extract_login_url("\n".join([result.stdout, result.stderr, result.output]))
    if login_url:
        return {
            "ok": True,
            "message": "请在 Tailscale 官方页面完成登录",
            "login_url": login_url,
        }
    if result.ok:
        return {
            "ok": True,
            "message": "Tailscale 已启动",
            "login_url": "",
        }
    return {
        "ok": False,
        "message": result.output or "启动 Tailscale 失败",
        "login_url": "",
    }


def logout_tailscale() -> CommandResult:
    if not command_exists("tailscale"):
        return CommandResult(False, "未安装 Tailscale，请先自行安装后再使用")
    return run_command(["tailscale", "logout"], timeout=20)


__all__ = [
    "TAILSCALE_CONFIG_PATH",
    "DEFAULT_TAILSCALE_CONFIG",
    "normalize_advertise_routes",
    "normalize_tailscale_config",
    "load_tailscale_config",
    "save_tailscale_config",
    "build_tailscale_up_command",
    "extract_login_url",
    "gather_tailscale_status",
    "start_tailscale_login",
    "logout_tailscale",
]
