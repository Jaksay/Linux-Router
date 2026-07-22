from __future__ import annotations

from typing import Any, TypedDict

from .core import CommandResult


class WirelessStatus(TypedDict):
    nmcli_available: bool
    wireless_devices: list[dict[str, Any]]
    saved_wifi_networks: list[dict[str, str]]
    saved_wifi_networks_unbound: list[dict[str, str]]
    wifi_scan_device: str
    wifi_networks: list[dict[str, Any]]
    errors: list[str]


class HotspotSummary(TypedDict):
    active: bool
    conflict: bool
    connection_name: str
    ssid: str
    password: str
    band: str
    band_label: str
    channel: str
    ifname: str
    ip: str
    frequency: str
    mode: str


class HotspotKeepaliveStatus(TypedDict):
    enabled: bool
    online: bool
    recovering: bool
    last_error: str
    parent_ifname: str


class HotspotStatus(TypedDict):
    nmcli_available: bool
    wireless_devices: list[dict[str, Any]]
    hotspot: HotspotSummary
    keepalive: HotspotKeepaliveStatus
    errors: list[str]


class HotspotClientsStatus(TypedDict):
    hotspots: list[dict[str, Any]]
    total_clients: int
    errors: list[str]


class SystemInfo(TypedDict, total=False):
    hostname: str
    os_name: str
    kernel: str
    architecture: str
    uptime: str
    load_average: str
    cpu_model: str
    cpu_temperature: str
    memory_total: str
    memory_available: str
    disk_total: str
    disk_free: str
    ip_addresses: list[str]
    active_connections: list[dict[str, Any]]
    network_status: dict[str, bool]
    network_interfaces: list[dict[str, Any]]


class WifiConnectResult(TypedDict):
    result: CommandResult
    binding_error: str | None
    current_state: dict[str, str]
    concurrency_mode: str
    hotspot_is_concurrent: bool
    hotspot_profile: dict[str, str]


__all__ = [
    "WirelessStatus",
    "HotspotSummary",
    "HotspotKeepaliveStatus",
    "HotspotStatus",
    "HotspotClientsStatus",
    "SystemInfo",
    "WifiConnectResult",
]
