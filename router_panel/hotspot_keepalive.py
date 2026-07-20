from __future__ import annotations

import json
import threading
from typing import Any

from .core import DATA_DIR, HOTSPOT_CONNECTION_NAME, atomic_write_text, normalize_mac_address


HOTSPOT_KEEPALIVE_PATH = DATA_DIR / "hotspot_keepalive.json"
_runtime_lock = threading.Lock()
_runtime_status: dict[str, Any] = {
    "recovering": False,
    "last_error": "",
}


def load_hotspot_keepalive() -> dict[str, Any]:
    try:
        payload = json.loads(HOTSPOT_KEEPALIVE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("enabled") is not True:
        return {}
    parent_ifname = payload.get("parent_ifname", "")
    parent_mac = normalize_mac_address(payload.get("parent_mac", ""))
    phy_name = payload.get("phy_name", "")
    if not isinstance(parent_ifname, str) or not isinstance(phy_name, str):
        return {}
    if not parent_ifname or not parent_mac:
        return {}
    return {
        "enabled": True,
        "parent_ifname": parent_ifname,
        "parent_mac": parent_mac,
        "phy_name": phy_name,
        "connection_name": HOTSPOT_CONNECTION_NAME,
    }


def save_hotspot_keepalive(parent_ifname: str, parent_mac: str, phy_name: str) -> dict[str, Any]:
    config = {
        "enabled": True,
        "parent_ifname": parent_ifname,
        "parent_mac": normalize_mac_address(parent_mac),
        "phy_name": phy_name,
        "connection_name": HOTSPOT_CONNECTION_NAME,
    }
    atomic_write_text(
        HOTSPOT_KEEPALIVE_PATH,
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )
    set_hotspot_keepalive_runtime(recovering=False, last_error="")
    return config


def clear_hotspot_keepalive() -> None:
    HOTSPOT_KEEPALIVE_PATH.unlink(missing_ok=True)
    set_hotspot_keepalive_runtime(recovering=False, last_error="")


def get_hotspot_keepalive_runtime() -> dict[str, Any]:
    with _runtime_lock:
        return dict(_runtime_status)


def set_hotspot_keepalive_runtime(*, recovering: bool, last_error: str = "") -> None:
    with _runtime_lock:
        _runtime_status["recovering"] = recovering
        _runtime_status["last_error"] = last_error


__all__ = [
    "HOTSPOT_KEEPALIVE_PATH",
    "clear_hotspot_keepalive",
    "get_hotspot_keepalive_runtime",
    "load_hotspot_keepalive",
    "save_hotspot_keepalive",
    "set_hotspot_keepalive_runtime",
]
