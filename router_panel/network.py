from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from configparser import ConfigParser
from pathlib import Path
from typing import Any

from .core import (
    HOTSPOT_CONNECTION_NAME,
    HOTSPOT_DEFAULT_SSID,
    WIRELESS_PHY_CACHE_TTL,
    get_network_interface_hardware,
    get_timed_cache,
    is_hotspot_virtual_interface,
    is_service_active,
    normalize_mac_address,
    read_text,
    run_command,
    set_timed_cache,
)
from .contracts import HotspotClientsStatus, HotspotStatus, WirelessStatus
from .hotspot_keepalive import get_hotspot_keepalive_runtime, load_hotspot_keepalive

from .network_parsers import (
    parse_nmcli_lines,
    translate_device_state,
    translate_connection_type,
    translate_wifi_security,
    format_wifi_band,
    hotspot_band_code_from_wifi_band,
    hotspot_band_label,
    channel_sort_key,
    parse_iw_frequency_line,
    parse_iw_valid_interface_combinations,
    parse_iw_station_dump,
    parse_csv_values,
    normalize_nmcli_general_state,
)

def get_wireless_interface_phy_map() -> dict[str, str]:
    cached = get_timed_cache("wireless:phy-map", WIRELESS_PHY_CACHE_TTL)
    if cached is not None:
        return cached.copy()

    result = run_command(["iw", "dev"], timeout=5)
    if not result.ok or not result.output:
        return {}

    mapping: dict[str, str] = {}
    current_phy = ""
    for raw_line in result.output.splitlines():
        line = raw_line.strip()
        if line.startswith("phy#"):
            current_phy = f"phy{line.split('#', 1)[1].strip()}"
            continue
        if line.startswith("Interface ") and current_phy:
            ifname = line.split(" ", 1)[1].strip()
            if ifname:
                mapping[ifname] = current_phy
    set_timed_cache("wireless:phy-map", WIRELESS_PHY_CACHE_TTL, mapping.copy())
    return mapping


def get_wireless_phy_capabilities() -> dict[str, dict[str, Any]]:
    cached = get_timed_cache("wireless:phy-capabilities", WIRELESS_PHY_CACHE_TTL)
    if cached is not None:
        return json.loads(json.dumps(cached))

    result = run_command(["iw", "phy"], timeout=8)
    if not result.ok or not result.output:
        return {}

    capabilities: dict[str, dict[str, Any]] = {}
    current_phy = ""
    section = ""
    current_band: dict[str, Any] | None = None
    combination_lines: list[str] = []

    for raw_line in result.output.splitlines():
        stripped = raw_line.strip()
        if raw_line.startswith("Wiphy "):
            if current_phy:
                capabilities[current_phy]["concurrency"] = parse_iw_valid_interface_combinations(combination_lines)
            current_phy = raw_line.split()[1]
            capabilities[current_phy] = {
                "supported_modes": [],
                "bands": [],
                "concurrency": {
                    "declared": False,
                    "supports_ap_sta": None,
                    "max_channels": None,
                    "same_channel_only": False,
                    "ap_sta_mode": "unknown",
                    "ap_sta_label": "未知",
                    "ap_sta_description": "驱动未提供足够的 AP 与 STA 并发限制信息",
                },
            }
            section = ""
            current_band = None
            combination_lines = []
            continue

        if not current_phy:
            continue

        if stripped == "Supported interface modes:":
            section = "modes"
            continue

        if stripped.startswith("Band "):
            section = ""
            current_band = {"channels": []}
            capabilities[current_phy]["bands"].append(current_band)
            continue

        if stripped == "Frequencies:" and current_band is not None:
            section = "frequencies"
            continue

        if stripped == "valid interface combinations:":
            section = "combinations"
            combination_lines = []
            continue

        if stripped == "interface combinations are not supported":
            capabilities[current_phy]["concurrency"] = {
                "declared": False,
                "supports_ap_sta": None,
                "max_channels": None,
                "same_channel_only": False,
                "ap_sta_mode": "unknown",
                "ap_sta_label": "未知",
                "ap_sta_description": "驱动未提供足够的 AP 与 STA 并发限制信息",
            }
            section = ""
            continue

        if section == "modes":
            if stripped.startswith("* "):
                capabilities[current_phy]["supported_modes"].append(stripped[2:].strip())
                continue
            section = ""

        if section == "frequencies" and current_band is not None:
            if stripped.startswith("* "):
                item = parse_iw_frequency_line(stripped)
                if item:
                    current_band["channels"].append(item)
                continue
            section = ""

        if section == "combinations":
            if stripped.startswith("* "):
                combination_lines.append(stripped[2:].strip())
                continue
            if combination_lines and stripped and not stripped.endswith(":"):
                combination_lines[-1] = f"{combination_lines[-1]} {stripped}"
                continue
            section = ""

    if current_phy:
        capabilities[current_phy]["concurrency"] = parse_iw_valid_interface_combinations(combination_lines)

    for capability in capabilities.values():
        for band in capability.get("bands", []):
            channels = band.get("channels", [])
            if not channels:
                continue
            band_label = channels[0].get("band_label", "未知")
            band["label"] = band_label
            band["nmcli_band"] = hotspot_band_code_from_wifi_band(band_label)

    set_timed_cache("wireless:phy-capabilities", WIRELESS_PHY_CACHE_TTL, json.loads(json.dumps(capabilities)))
    return capabilities


def build_hotspot_frequency_settings(
    device: dict[str, Any],
    phy_capability: dict[str, Any],
    hotspot_profile: dict[str, str],
) -> dict[str, Any]:
    concurrency = phy_capability.get("concurrency", {})
    concurrency_info = {
        "mode": concurrency.get("ap_sta_mode", "unknown"),
        "label": concurrency.get("ap_sta_label", "未知"),
        "description": concurrency.get("ap_sta_description", "驱动未提供足够的 AP 与 STA 并发限制信息"),
    }
    available_modes = [
        {
            "value": "exclusive",
            "label": "独占 AP",
            "description": "直接切换当前无线接口为热点，当前 Wi-Fi 连接会断开",
        }
    ]
    if concurrency_info["mode"] in {"same_frequency", "cross_frequency"}:
        available_modes.append(
            {
                "value": "concurrent",
                "label": "并发 AP+STA",
                "description": (
                    "保留当前 Wi-Fi 连接并额外创建热点"
                    if concurrency_info["mode"] == "cross_frequency"
                    else "保留当前 Wi-Fi 连接，但热点必须与 STA 共用同一信道"
                ),
            }
        )
    selected_mode = hotspot_profile.get("mode", "").strip()
    mode_values = {item["value"] for item in available_modes}
    if selected_mode not in mode_values:
        selected_mode = "concurrent" if "concurrent" in mode_values and device.get("wifi_link") else "exclusive"
    supported_modes = set(phy_capability.get("supported_modes", []))
    if "AP" not in supported_modes:
        return {
            "available": False,
            "reason": "该无线网卡不支持 AP 模式",
            "bands": [],
            "selected_band": "",
            "selected_channel": "",
            "locked": False,
            "note": "",
            "concurrency": concurrency_info,
            "available_modes": available_modes,
            "selected_mode": selected_mode,
            "mode_notes": {},
        }

    bands_by_code: dict[str, dict[str, Any]] = {}
    for band in phy_capability.get("bands", []):
        band_code = band.get("nmcli_band", "")
        if not band_code:
            continue
        band_entry = bands_by_code.setdefault(
            band_code,
            {"value": band_code, "label": hotspot_band_label(band_code), "channels": []},
        )
        known_channels = {
            item.get("value", "")
            for item in band_entry["channels"]
        }
        for channel in band.get("channels", []):
            channel_value = channel.get("channel", "")
            if not channel_value or channel.get("disabled") or channel.get("no_ir"):
                continue
            if channel_value in known_channels:
                continue
            band_entry["channels"].append(
                {
                    "value": channel_value,
                    "label": f"信道 {channel_value}",
                    "frequency": channel.get("frequency_label", ""),
                }
            )
            known_channels.add(channel_value)

    bands = list(bands_by_code.values())
    bands.sort(key=lambda item: (0 if item["value"] == "bg" else 1, item["label"]))
    for band in bands:
        band["channels"].sort(key=lambda item: channel_sort_key(item.get("value", "")))

    if not bands:
        return {
            "available": False,
            "reason": "当前监管域下没有可用于开启热点的频段",
            "bands": [],
            "selected_band": "",
            "selected_channel": "",
            "locked": False,
            "note": "",
            "concurrency": concurrency_info,
            "available_modes": available_modes,
            "selected_mode": selected_mode,
            "mode_notes": {},
        }

    selected_band = hotspot_profile.get("band", "").strip()
    if selected_band not in bands_by_code:
        selected_band = bands[0]["value"]
    selected_channel = hotspot_profile.get("channel", "").strip()
    if selected_channel and selected_channel not in {
        item.get("value", "")
        for item in bands_by_code[selected_band]["channels"]
    }:
        selected_channel = ""

    locked = False
    note = ""
    mode_notes = {
        "exclusive": "独占 AP 模式会断开当前无线连接，并将当前网卡直接切换为热点",
        "concurrent": "",
    }
    current_link = device.get("wifi_link") or {}
    current_ssid = current_link.get("ssid", "").strip()

    if current_ssid:
        if concurrency_info["mode"] == "unsupported":
            note = "该网卡不支持 AP 与 STA 并发，开启热点会中断当前无线连接"
            mode_notes["concurrent"] = "该网卡不支持并发热点"
        elif concurrency_info["mode"] == "same_frequency":
            current_band = hotspot_band_code_from_wifi_band(current_link.get("band", ""))
            current_channel = current_link.get("channel", "").strip()
            if current_band and current_channel:
                band_entry = bands_by_code.get(current_band)
                if band_entry is None:
                    band_entry = {
                        "value": current_band,
                        "label": hotspot_band_label(current_band),
                        "channels": [],
                    }
                    bands_by_code[current_band] = band_entry
                    bands.append(band_entry)
                if current_channel not in {
                    item.get("value", "")
                    for item in band_entry["channels"]
                }:
                    band_entry["channels"].append(
                        {
                            "value": current_channel,
                            "label": f"信道 {current_channel}",
                            "frequency": current_link.get("frequency", ""),
                        }
                    )
                    band_entry["channels"].sort(key=lambda item: channel_sort_key(item.get("value", "")))
                selected_band = current_band
                selected_channel = current_channel
                locked = True
                mode_notes["concurrent"] = f"并发模式将固定使用 {current_link.get('band', '当前频段')} 信道 {current_channel}"
                if selected_mode == "concurrent":
                    note = mode_notes["concurrent"]
        elif concurrency_info["mode"] == "cross_frequency":
            mode_notes["concurrent"] = "并发模式可保留当前 Wi-Fi 连接，并单独启动热点"
    elif concurrency_info["mode"] == "same_frequency":
        mode_notes["concurrent"] = "并发模式仅支持同频同信道运行；若之后接入上游 Wi-Fi，热点信道可能需要随之锁定"
    elif concurrency_info["mode"] == "cross_frequency":
        mode_notes["concurrent"] = "并发模式支持在保留 STA 的同时额外启动热点"
    elif concurrency_info["mode"] == "unsupported":
        mode_notes["concurrent"] = "该网卡不支持并发热点"

    bands.sort(key=lambda item: (0 if item["value"] == "bg" else 1, item["label"]))
    return {
        "available": True,
        "reason": "",
        "bands": bands,
        "selected_band": selected_band,
        "selected_channel": selected_channel,
        "locked": locked,
        "note": note,
        "concurrency": concurrency_info,
        "available_modes": available_modes,
        "selected_mode": selected_mode,
        "mode_notes": mode_notes,
    }


def get_hotspot_device_settings(ifname: str) -> dict[str, Any]:
    device: dict[str, Any] = get_device_status_item(ifname)
    if (
        device.get("type") != "wifi"
        or is_hotspot_virtual_interface(device.get("device", ""))
    ):
        return {}

    phy_name = get_wireless_interface_phy_map().get(ifname, "")
    phy_capability = get_wireless_phy_capabilities().get(phy_name, {})
    hotspot_profile = get_hotspot_profile()
    device["phy_name"] = phy_name
    device["wifi_link"] = get_wifi_client_link(device)
    device["frequency_settings"] = build_hotspot_frequency_settings(
        device,
        phy_capability,
        hotspot_profile,
    )
    return device


def get_hotspot_profile() -> dict[str, str]:
    details = run_command(
        [
            "nmcli",
            "--show-secrets",
            "-g",
            "802-11-wireless.ssid,802-11-wireless-security.psk,802-11-wireless.band,802-11-wireless.channel,connection.interface-name",
            "connection",
            "show",
            HOTSPOT_CONNECTION_NAME,
        ]
    )
    if not details.ok or not details.output:
        return {
            "ssid": HOTSPOT_DEFAULT_SSID,
            "password": "",
            "band": "",
            "channel": "",
            "interface_name": "",
            "mode": "exclusive",
        }

    lines = details.output.splitlines()
    ssid = lines[0].strip() if lines else HOTSPOT_DEFAULT_SSID
    password = lines[1].strip() if len(lines) > 1 else ""
    band = lines[2].strip() if len(lines) > 2 else ""
    channel = lines[3].strip() if len(lines) > 3 else ""
    interface_name = lines[4].strip() if len(lines) > 4 else ""
    return {
        "ssid": ssid or HOTSPOT_DEFAULT_SSID,
        "password": password,
        "band": band,
        "channel": channel,
        "interface_name": interface_name,
        "mode": "concurrent" if is_hotspot_virtual_interface(interface_name) else "exclusive",
    }


def get_active_connections() -> list[dict[str, str]]:
    active_connections = run_command(
        [
            "nmcli",
            "-t",
            "-f",
            "NAME,TYPE,DEVICE",
            "connection",
            "show",
            "--active",
        ]
    )
    active_items = (
        parse_nmcli_lines(active_connections.output, ["name", "type", "device"])
        if active_connections.ok and active_connections.output
        else []
    )
    return [
        {
            **item,
            "type_label": (
                "热点"
                if item.get("name") == HOTSPOT_CONNECTION_NAME
                and item.get("type") == "802-11-wireless"
                else translate_connection_type(item.get("type", ""))
            ),
        }
        for item in active_items
        if item.get("device") != "lo"
    ]


def get_hotspot_active_connections_by_phy(
    active_items: list[dict[str, str]],
    wireless_phy_map: dict[str, str],
) -> dict[str, dict[str, str]]:
    connections_by_phy: dict[str, dict[str, str]] = {}
    for connection in active_items:
        if connection.get("name") != HOTSPOT_CONNECTION_NAME:
            continue
        device = connection.get("device", "").strip()
        phy_name = wireless_phy_map.get(device, "")
        if not device or not phy_name:
            continue
        connections_by_phy[phy_name] = connection
    return connections_by_phy


def get_hotspot_active_connection_for_parent(ifname: str) -> dict[str, str]:
    phy_name = get_wireless_interface_phy_map().get(ifname, "")
    if not phy_name:
        return {}
    active_items = get_active_connections()
    return get_hotspot_active_connections_by_phy(active_items, get_wireless_interface_phy_map()).get(phy_name, {})


def get_device_status_item(ifname: str) -> dict[str, str]:
    if not ifname:
        return {}

    result = run_command(
        [
            "nmcli",
            "-t",
            "-f",
            "DEVICE,TYPE,STATE,CONNECTION",
            "device",
            "status",
        ]
    )
    if not result.ok or not result.output:
        return {}

    for item in parse_nmcli_lines(result.output, ["device", "type", "state", "connection"]):
        if item.get("device") == ifname:
            return item
    return {}


def get_filtered_device_status(allowed_types: set[str]) -> tuple[list[dict[str, str]], list[str]]:
    result = run_command(
        [
            "nmcli",
            "-t",
            "-f",
            "DEVICE,TYPE,STATE,CONNECTION",
            "device",
            "status",
        ]
    )
    if not result.ok or not result.output:
        return [], [result.output or "无法读取网络状态"]

    devices: list[dict[str, str]] = []
    for item in parse_nmcli_lines(result.output, ["device", "type", "state", "connection"]):
        device_type = item.get("type", "").strip()
        device_name = item.get("device", "").strip()
        if device_type not in allowed_types:
            continue
        if (
            not device_name
            or device_name == "lo"
            or device_name.startswith("p2p-dev-")
            or is_hotspot_virtual_interface(device_name)
        ):
            continue
        devices.append(item)
    return devices, []


def get_device_details(ifname: str) -> dict[str, Any]:
    if not ifname:
        return {"mac": "", "ipv4": [], "gateway": "", "dns": []}

    details = run_command(
        [
            "nmcli",
            "-t",
            "-f",
            "GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS",
            "device",
            "show",
            ifname,
        ]
    )
    detail_map: dict[str, Any] = {"mac": "", "ipv4": [], "gateway": "", "dns": []}
    if not details.ok or not details.output:
        return detail_map

    for line in details.output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if key == "GENERAL.HWADDR":
            detail_map["mac"] = value
        elif key == "IP4.ADDRESS[1]":
            detail_map["ipv4"].append(value)
        elif key.startswith("IP4.ADDRESS"):
            detail_map["ipv4"].append(value)
        elif key == "IP4.GATEWAY":
            detail_map["gateway"] = value
        elif key.startswith("IP4.DNS"):
            detail_map["dns"].append(value)
    return detail_map


def get_wifi_connection_profiles(ssid: str) -> list[dict[str, str]]:
    if not ssid:
        return []

    result = run_command(["nmcli", "-t", "-f", "NAME,UUID,TYPE", "connection", "show"])
    if not result.ok or not result.output:
        return []

    profiles: list[dict[str, str]] = []
    for item in parse_nmcli_lines(result.output, ["name", "uuid", "type"]):
        if item.get("type") != "802-11-wireless":
            continue
        if item.get("name", "").strip() != ssid:
            continue
        profiles.append(item)
    return profiles


def get_active_wifi_connection(ifname: str) -> dict[str, str]:
    result = run_command(
        ["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show", "--active"]
    )
    if not result.ok or not result.output:
        return {}
    for item in parse_nmcli_lines(result.output, ["name", "uuid", "type", "device"]):
        if item.get("type") == "802-11-wireless" and item.get("device") == ifname:
            return item
    return {}


def get_saved_wifi_networks(active_items: list[dict[str, str]]) -> list[dict[str, str]]:
    result = run_command(["nmcli", "-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT,DEVICE,FILENAME", "connection", "show"])
    if not result.ok or not result.output:
        return []

    active_wifi_devices = {
        item.get("device", "").strip(): item.get("name", "").strip()
        for item in active_items
        if item.get("type") == "802-11-wireless" and item.get("device")
    }

    networks: list[dict[str, str]] = []
    for item in parse_nmcli_lines(result.output, ["name", "uuid", "type", "autoconnect", "device", "filename"]):
        if item.get("type") != "802-11-wireless":
            continue

        name = item.get("name", "").strip()
        if not name or name == HOTSPOT_CONNECTION_NAME:
            continue

        profile_uuid = item.get("uuid", "").strip()
        interface_name = ""
        permanent_mac_address = ""
        cloned_mac_address = ""
        filename = item.get("filename", "").strip()
        if filename:
            parser = ConfigParser(interpolation=None)
            try:
                with Path(filename).open(encoding="utf-8") as handle:
                    parser.read_file(handle)
                interface_name = parser.get("connection", "interface-name", fallback="").strip()
                permanent_mac_address = normalize_mac_address(
                    parser.get("wifi", "mac-address", fallback="")
                )
                cloned_mac_address = parser.get("wifi", "cloned-mac-address", fallback="").strip()
            except OSError:
                interface_name = ""

        active_device = item.get("device", "").strip()
        is_active = active_wifi_devices.get(active_device) == name

        networks.append(
            {
                "name": name,
                "uuid": profile_uuid,
                "device": active_device,
                "interface_name": interface_name,
                "permanent_mac_address": permanent_mac_address,
                "cloned_mac_address": cloned_mac_address,
                "autoconnect": "是" if item.get("autoconnect", "").strip() == "yes" else "否",
                "is_active": "1" if is_active else "0",
                "state_label": "已连接" if is_active else "已保存",
            }
        )

    networks.sort(key=lambda item: item.get("name", "").lower())
    return networks


def get_wifi_client_link(device: dict[str, Any]) -> dict[str, str]:
    if device.get("state") != "connected":
        return {}
    if device.get("connection", "").strip() == HOTSPOT_CONNECTION_NAME:
        return {}
    ifname = device.get("device", "")
    link = get_current_wifi_link(ifname)
    if link:
        details = device.get("details")
        if not isinstance(details, dict):
            details = get_device_details(ifname)
        ipv4_addresses = details.get("ipv4", [])
        link["ip_address"] = (
            ipv4_addresses[0].split("/", 1)[0]
            if ipv4_addresses
            else "无"
        )
        link["mac_address"] = read_text(f"/sys/class/net/{ifname}/address") or "未知"
    return link


def enrich_wireless_devices(devices: list[dict[str, str]]) -> list[dict[str, Any]]:
    hardware_map = {
        item.get("name", ""): item
        for item in get_network_interface_hardware()
    }
    enriched_devices: list[dict[str, Any]] = []
    for item in sorted(devices, key=lambda value: value.get("device", "")):
        hardware_info = hardware_map.get(item.get("device", ""), {})
        enriched_devices.append(
            {
                **item,
                "state_label": translate_device_state(item.get("state", "")),
                "hardware": {
                    "bus_label": hardware_info.get("bus_label", ""),
                    "vendor": hardware_info.get("vendor", ""),
                    "model": hardware_info.get("model", ""),
                    "driver": hardware_info.get("driver", ""),
                    "permanent_mac_address": hardware_info.get("permanent_mac_address", ""),
                },
                "wifi_link": get_wifi_client_link(item),
            }
        )
    return enriched_devices


def get_wireless_scan_device(
    wireless_devices: list[dict[str, Any]],
    wifi_scan_device_override: str = "",
) -> dict[str, Any] | None:
    if not wireless_devices:
        return None
    if wifi_scan_device_override:
        for device in wireless_devices:
            if device.get("device") == wifi_scan_device_override:
                return device
    return wireless_devices[0]


def attach_saved_wifi_networks(
    wireless_devices: list[dict[str, Any]],
    saved_wifi_networks: list[dict[str, str]],
) -> list[dict[str, str]]:
    saved_networks_by_interface: dict[str, list[dict[str, str]]] = {}
    unbound_saved_networks: list[dict[str, str]] = []
    devices_by_permanent_mac = {
        normalize_mac_address(device.get("hardware", {}).get("permanent_mac_address", "")): device.get("device", "")
        for device in wireless_devices
        if normalize_mac_address(device.get("hardware", {}).get("permanent_mac_address", ""))
    }

    for network in saved_wifi_networks:
        interface_name = network.get("device", "").strip()
        if not interface_name:
            permanent_mac = normalize_mac_address(network.get("permanent_mac_address", ""))
            interface_name = devices_by_permanent_mac.get(permanent_mac, "")
        if not interface_name:
            interface_name = network.get("interface_name", "").strip()
        if interface_name:
            saved_networks_by_interface.setdefault(interface_name, []).append(network)
        else:
            unbound_saved_networks.append(network)

    for device in wireless_devices:
        device["saved_networks"] = list(saved_networks_by_interface.get(device.get("device", ""), []))

    if wireless_devices and unbound_saved_networks:
        wireless_devices[0]["saved_networks"].extend(
            [
                {
                    **network,
                    "binding_label": "未绑定接口",
                }
                for network in unbound_saved_networks
            ]
        )
        unbound_saved_networks = []

    return unbound_saved_networks


def gather_wireless_network_status(
    include_wifi_networks: bool = False,
    wifi_scan_device_override: str = "",
) -> WirelessStatus:
    base_devices, errors = get_filtered_device_status({"wifi"})
    with ThreadPoolExecutor(max_workers=2) as executor:
        wireless_devices_future = executor.submit(enrich_wireless_devices, base_devices)
        active_items_future = executor.submit(get_active_connections)
        wireless_devices = wireless_devices_future.result()
        active_items = active_items_future.result()

    saved_wifi_networks = get_saved_wifi_networks(active_items)
    unbound_saved_networks = attach_saved_wifi_networks(wireless_devices, saved_wifi_networks)

    wifi_device = get_wireless_scan_device(wireless_devices, wifi_scan_device_override)
    wifi_networks: list[dict[str, Any]] = []
    wifi_scan_error: str | None = None
    if include_wifi_networks and wifi_device:
        wifi_networks, wifi_scan_error = get_wifi_networks(wifi_device.get("device", ""))
    if wifi_scan_error:
        errors.append(wifi_scan_error)

    return {
        "nmcli_available": True,
        "wireless_devices": wireless_devices,
        "saved_wifi_networks": saved_wifi_networks,
        "saved_wifi_networks_unbound": unbound_saved_networks,
        "wifi_scan_device": wifi_device.get("device", "") if wifi_device else "",
        "wifi_networks": wifi_networks,
        "errors": errors,
    }


def get_hotspot_radio_status(
    ifname: str,
    hotspot_profile: dict[str, str],
    phy_capability: dict[str, Any],
) -> dict[str, str]:
    link = get_current_wifi_link(ifname)
    channel = link.get("channel", "").strip() or hotspot_profile.get("channel", "").strip()
    frequency = link.get("frequency", "").strip()

    if not frequency and channel:
        for band in phy_capability.get("bands", []):
            channel_info = next(
                (
                    item
                    for item in band.get("channels", [])
                    if item.get("channel", "").strip() == channel
                ),
                None,
            )
            if channel_info:
                frequency = channel_info.get("frequency_label", "").strip()
                break

    return {
        "frequency": frequency or "未知",
        "channel": channel or "自动",
        "band_label": link.get("band", "").strip() or hotspot_band_label(hotspot_profile.get("band", "")),
    }


def _get_cached_device_details(
    cache: dict[str, dict[str, Any]],
    ifname: str,
) -> dict[str, Any]:
    if ifname not in cache:
        cache[ifname] = get_device_details(ifname)
    return cache[ifname]


def gather_hotspot_status() -> HotspotStatus:
    keepalive_config = load_hotspot_keepalive()
    keepalive_runtime = get_hotspot_keepalive_runtime()
    base_devices, errors = get_filtered_device_status({"wifi"})
    with ThreadPoolExecutor(max_workers=6) as executor:
        hardware_future = executor.submit(get_network_interface_hardware)
        phy_map_future = executor.submit(get_wireless_interface_phy_map)
        phy_capabilities_future = executor.submit(get_wireless_phy_capabilities)
        active_items_future = executor.submit(get_active_connections)
        hotspot_profile_future = executor.submit(get_hotspot_profile)
        hotspot_conflict_future = executor.submit(is_service_active, "hostapd")

        hardware_items = hardware_future.result()
        wireless_phy_map = phy_map_future.result()
        wireless_phy_capabilities = phy_capabilities_future.result()
        active_items = active_items_future.result()
        hotspot_profile = hotspot_profile_future.result()
        hotspot_conflict = hotspot_conflict_future.result()

    hardware_map = {
        item.get("name", ""): item
        for item in hardware_items
    }
    hotspot_connections_by_phy = get_hotspot_active_connections_by_phy(active_items, wireless_phy_map)
    hotspot_device_details: dict[str, dict[str, Any]] = {}
    hotspot_radio_statuses: dict[str, dict[str, str]] = {}

    wireless_devices: list[dict[str, Any]] = []
    keepalive_online = False
    for item in sorted(base_devices, key=lambda value: value.get("device", "")):
        hardware_info = hardware_map.get(item.get("device", ""), {})
        details = get_device_details(item.get("device", ""))
        device = {
            **item,
            "details": details,
            "state_label": translate_device_state(item.get("state", "")),
                "hardware": {
                    "bus_label": hardware_info.get("bus_label", ""),
                    "vendor": hardware_info.get("vendor", ""),
                    "model": hardware_info.get("model", ""),
                    "driver": hardware_info.get("driver", ""),
                    "permanent_mac_address": hardware_info.get("permanent_mac_address", ""),
                },
            "phy_name": wireless_phy_map.get(item.get("device", ""), ""),
            "wifi_link": get_wifi_client_link({**item, "details": details}),
        }
        phy_capability = wireless_phy_capabilities.get(device.get("phy_name", ""), {})
        frequency_settings = build_hotspot_frequency_settings(device, phy_capability, hotspot_profile)
        hotspot_connection = hotspot_connections_by_phy.get(device.get("phy_name", ""))
        hotspot_active = hotspot_connection is not None
        hotspot_ifname = hotspot_connection.get("device", "") if hotspot_connection else ""
        if hotspot_active and hotspot_ifname:
            hotspot_detail = _get_cached_device_details(hotspot_device_details, hotspot_ifname)
            hotspot_ip = hotspot_detail["ipv4"][0] if hotspot_detail["ipv4"] else "无"
            if hotspot_ifname not in hotspot_radio_statuses:
                hotspot_radio_statuses[hotspot_ifname] = get_hotspot_radio_status(
                    hotspot_ifname,
                    hotspot_profile,
                    phy_capability,
                )
            hotspot_radio_status = hotspot_radio_statuses[hotspot_ifname]
        else:
            hotspot_ip = "无"
            hotspot_radio_status = {
                "frequency": "未知",
                "channel": hotspot_profile["channel"] or "自动",
                "band_label": hotspot_band_label(hotspot_profile["band"]),
            }
        device_mac = normalize_mac_address(hardware_info.get("permanent_mac_address", ""))
        keepalive_protected = bool(
            keepalive_config
            and (
                device.get("device") == keepalive_config.get("parent_ifname")
                or device_mac == keepalive_config.get("parent_mac")
            )
        )
        if keepalive_protected and hotspot_active and hotspot_ip != "无":
            keepalive_online = True
        device["hotspot"] = {
            "active": hotspot_active,
            "conflict": hotspot_conflict and not hotspot_active,
            "connection_name": HOTSPOT_CONNECTION_NAME,
            "ssid": hotspot_profile["ssid"],
            "password": hotspot_profile["password"],
            "band": hotspot_profile["band"],
            "band_label": hotspot_radio_status["band_label"],
            "channel": hotspot_radio_status["channel"],
            "ifname": hotspot_ifname or device.get("device", ""),
            "ip": hotspot_ip,
            "frequency": hotspot_radio_status["frequency"],
            "phy_name": device.get("phy_name", ""),
            "frequency_settings": frequency_settings,
            "mode": "concurrent" if hotspot_ifname and is_hotspot_virtual_interface(hotspot_ifname) else "exclusive",
            "keepalive": keepalive_protected,
        }
        wireless_devices.append(device)

    wifi_device = get_wireless_scan_device(wireless_devices)
    hotspot_connection = (
        hotspot_connections_by_phy.get(wifi_device.get("phy_name", ""))
        if wifi_device
        else None
    )
    hotspot_active = hotspot_connection is not None
    hotspot_ifname = hotspot_connection.get("device", "") if hotspot_connection else ""
    if hotspot_active and hotspot_ifname:
        hotspot_detail = _get_cached_device_details(hotspot_device_details, hotspot_ifname)
        hotspot_ip = hotspot_detail["ipv4"][0] if hotspot_detail["ipv4"] else "无"
        hotspot_radio_status = hotspot_radio_statuses.get(
            hotspot_ifname,
            {
                "frequency": "未知",
                "channel": hotspot_profile["channel"] or "自动",
                "band_label": hotspot_band_label(hotspot_profile["band"]),
            },
        )
    else:
        hotspot_ip = "无"
        hotspot_radio_status = {
            "frequency": "未知",
            "channel": hotspot_profile["channel"] or "自动",
            "band_label": hotspot_band_label(hotspot_profile["band"]),
        }

    return {
        "nmcli_available": True,
        "wireless_devices": wireless_devices,
        "hotspot": {
            "active": hotspot_active,
            "conflict": hotspot_conflict and not hotspot_active,
            "connection_name": HOTSPOT_CONNECTION_NAME,
            "ssid": hotspot_profile["ssid"],
            "password": hotspot_profile["password"],
            "band": hotspot_profile["band"],
            "band_label": hotspot_radio_status["band_label"],
            "channel": hotspot_radio_status["channel"],
            "ifname": hotspot_ifname or (wifi_device.get("device", "") if wifi_device else ""),
            "ip": hotspot_ip,
            "frequency": hotspot_radio_status["frequency"],
            "mode": "concurrent" if hotspot_ifname and is_hotspot_virtual_interface(hotspot_ifname) else "exclusive",
        },
        "keepalive": {
            "enabled": bool(keepalive_config),
            "online": keepalive_online,
            "recovering": bool(keepalive_runtime.get("recovering")),
            "last_error": str(keepalive_runtime.get("last_error", "")),
            "parent_ifname": keepalive_config.get("parent_ifname", "") if keepalive_config else "",
        },
        "errors": errors,
    }


def get_current_wifi_link(ifname: str) -> dict[str, str]:
    wifi_list = run_command(
        [
            "nmcli",
            "-t",
            "-f",
            "IN-USE,SSID,BSSID,CHAN,FREQ,RATE",
            "device",
            "wifi",
            "list",
            "--rescan",
            "no",
            "ifname",
            ifname,
        ]
    )
    if not wifi_list.ok or not wifi_list.output:
        return {}

    for item in parse_nmcli_lines(
        wifi_list.output,
        ["in_use", "ssid", "bssid", "channel", "frequency", "rate"],
    ):
        if item.get("in_use", "").strip() != "*":
            continue
        frequency = item.get("frequency", "").strip()
        return {
            "ssid": item.get("ssid", "").strip() or "隐藏网络",
            "bssid": item.get("bssid", "").strip() or "未知",
            "channel": item.get("channel", "").strip() or "未知",
            "frequency": frequency or "未知",
            "band": format_wifi_band(frequency),
            "rate": item.get("rate", "").strip() or "未知",
        }

    return {}


def get_interface_ipv4_neighbors(ifname: str) -> dict[str, str]:
    result = run_command(["ip", "-4", "neighbor", "show", "dev", ifname], timeout=5)
    if not result.ok or not result.output:
        return {}

    addresses: dict[str, str] = {}
    for line in result.output.splitlines():
        parts = line.split()
        if not parts or "lladdr" not in parts:
            continue
        mac_index = parts.index("lladdr") + 1
        if mac_index >= len(parts):
            continue
        addresses[normalize_mac_address(parts[mac_index])] = parts[0]
    return addresses


def get_hotspot_dhcp_leases(ifname: str) -> dict[str, dict[str, str]]:
    lease_path = Path(f"/var/lib/NetworkManager/dnsmasq-{ifname}.leases")
    try:
        lines = lease_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    leases: dict[str, dict[str, str]] = {}
    for line in lines:
        fields = line.split(maxsplit=4)
        if len(fields) < 4:
            continue
        _, mac_address, ip_address, hostname = fields[:4]
        normalized_mac = normalize_mac_address(mac_address)
        if not normalized_mac:
            continue
        leases[normalized_mac] = {
            "ip_address": ip_address.strip() or "未知",
            "device_name": hostname.strip() if hostname.strip() not in {"", "*"} else "未知设备",
        }
    return leases


def get_hotspot_station_clients(ifname: str) -> tuple[list[dict[str, Any]], str | None]:
    result = run_command(["iw", "dev", ifname, "station", "dump"], timeout=8)
    if not result.ok:
        return [], result.output or f"无法读取 {ifname} 的热点客户端"

    clients = parse_iw_station_dump(result.output)
    ipv4_neighbors = get_interface_ipv4_neighbors(ifname)
    dhcp_leases = get_hotspot_dhcp_leases(ifname)
    for client in clients:
        normalized_mac = normalize_mac_address(client.get("mac_address", ""))
        lease = dhcp_leases.get(normalized_mac, {})
        client["device_name"] = lease.get("device_name", "未知设备")
        client["ip_address"] = lease.get("ip_address", "") or ipv4_neighbors.get(normalized_mac, "未知")
    clients.sort(key=lambda item: item.get("mac_address", ""))
    return clients, None


def gather_hotspot_clients_status() -> HotspotClientsStatus:
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        active_items_future = executor.submit(get_active_connections)
        hotspot_profile_future = executor.submit(get_hotspot_profile)

        active_items = active_items_future.result()
        hotspot_profile = hotspot_profile_future.result()

    hotspot_connections = [
        item
        for item in active_items
        if item.get("name") == HOTSPOT_CONNECTION_NAME
        and item.get("type") == "802-11-wireless"
        and item.get("device")
    ]

    hotspots: list[dict[str, Any]] = []
    for connection in hotspot_connections:
        hotspot_ifname = connection.get("device", "").strip()
        clients, client_error = get_hotspot_station_clients(hotspot_ifname)
        if client_error:
            errors.append(client_error)
        hotspots.append(
            {
                "ssid": hotspot_profile.get("ssid", "") or HOTSPOT_DEFAULT_SSID,
                "clients": clients,
                "client_count": len(clients),
            }
        )

    return {
        "hotspots": hotspots,
        "total_clients": sum(item["client_count"] for item in hotspots),
        "errors": errors,
    }


def get_wifi_networks(ifname: str) -> tuple[list[dict[str, Any]], str | None]:
    device_status = get_device_status_item(ifname)
    interface_is_hotspot = (
        device_status.get("connection", "").strip() == HOTSPOT_CONNECTION_NAME
    )
    wifi_list = run_command(
        [
            "nmcli",
            "-t",
            "-f",
            "IN-USE,SSID,SIGNAL,SECURITY,BSSID,CHAN,FREQ,RATE",
            "device",
            "wifi",
            "list",
            "--rescan",
            "no",
            "ifname",
            ifname,
        ]
    )
    if not wifi_list.ok:
        return [], wifi_list.output or "无法读取 Wi-Fi 列表"

    networks_by_ssid: dict[str, dict[str, Any]] = {}
    for item in parse_nmcli_lines(
        wifi_list.output,
        ["in_use", "ssid", "signal", "security", "bssid", "channel", "frequency", "rate"],
    ):
        ssid = item.get("ssid", "").strip()
        bssid = item.get("bssid", "").strip()
        if not ssid or not bssid:
            continue

        try:
            signal_value = int(item.get("signal", "0") or "0")
        except ValueError:
            signal_value = 0

        frequency = item.get("frequency", "").strip()
        candidate = {
            "ssid": ssid,
            "bssid": bssid,
            "signal": signal_value,
            "security": translate_wifi_security(item.get("security", "").strip()),
            "in_use": item.get("in_use", "").strip() == "*",
            "channel": item.get("channel", "").strip() or "未知",
            "frequency": frequency or "未知",
            "band": format_wifi_band(frequency),
            "rate": item.get("rate", "").strip() or "未知",
        }
        if interface_is_hotspot and candidate["in_use"]:
            continue
        existing = networks_by_ssid.get(ssid)
        if not existing:
            networks_by_ssid[ssid] = candidate
            continue
        if candidate["in_use"] and not existing["in_use"]:
            networks_by_ssid[ssid] = candidate
            continue
        if candidate["signal"] > existing["signal"]:
            networks_by_ssid[ssid] = candidate

    networks = list(networks_by_ssid.values())
    networks.sort(key=lambda item: (not item["in_use"], -item["signal"], item["ssid"].lower()))
    return networks, None


def default_wired_profile(ifname: str) -> dict[str, Any]:
    return {
        "name": "",
        "active_device": ifname,
        "interface_name": ifname,
        "autoconnect": True,
        "ipv4_method": "auto",
        "ipv4_address": "",
        "ipv4_gateway": "",
        "ipv4_dns": "",
        "route_metric": "-1",
    }


def gather_wired_network_info() -> dict[str, Any]:
    result = run_command(
        [
            "nmcli",
            "-t",
            "-f",
            (
                "GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE,GENERAL.CONNECTION,"
                "GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS"
            ),
            "device",
            "show",
        ]
    )
    if not result.ok or not result.output:
        return {
            "devices": [],
            "errors": [result.output or "无法读取有线网络状态"],
        }

    raw_devices: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in result.output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()

        if key == "GENERAL.DEVICE":
            if current:
                raw_devices.append(current)
            current = {
                "device": value,
                "type": "",
                "state": "",
                "connection": "",
                "mac": "",
                "ipv4": [],
                "gateway": "",
                "dns": [],
            }
            continue
        if current is None:
            continue

        if key == "GENERAL.TYPE":
            current["type"] = value
        elif key == "GENERAL.STATE":
            current["state"] = normalize_nmcli_general_state(value)
        elif key == "GENERAL.CONNECTION":
            current["connection"] = "" if value == "--" else value
        elif key == "GENERAL.HWADDR":
            current["mac"] = value
        elif key.startswith("IP4.ADDRESS"):
            current["ipv4"].append(value)
        elif key == "IP4.GATEWAY":
            current["gateway"] = value
        elif key.startswith("IP4.DNS"):
            current["dns"].append(value)
    if current:
        raw_devices.append(current)

    devices: list[dict[str, Any]] = []
    for device in sorted(raw_devices, key=lambda value: value.get("device", "")):
        if device.get("type") != "ethernet":
            continue
        ifname = device.get("device", "")
        connection_name = device.get("connection", "")
        profile = default_wired_profile(ifname)
        profile["name"] = connection_name
        devices.append(
            {
                "device": ifname,
                "state": device.get("state", ""),
                "state_label": translate_device_state(device.get("state", "")),
                "connection": connection_name or "未连接",
                "details": {
                    "mac": device.get("mac", ""),
                    "ipv4": device.get("ipv4", []),
                    "gateway": device.get("gateway", ""),
                    "dns": device.get("dns", []),
                },
                "profile": profile,
            }
        )

    return {
        "devices": devices,
        "errors": [],
    }


__all__ = [
    "parse_nmcli_lines",
    "translate_device_state",
    "translate_connection_type",
    "translate_wifi_security",
    "format_wifi_band",
    "hotspot_band_code_from_wifi_band",
    "hotspot_band_label",
    "channel_sort_key",
    "parse_iw_frequency_line",
    "parse_iw_valid_interface_combinations",
    "parse_iw_station_dump",
    "parse_csv_values",
    "normalize_nmcli_general_state",
    "get_wireless_interface_phy_map",
    "get_wireless_phy_capabilities",
    "build_hotspot_frequency_settings",
    "get_hotspot_device_settings",
    "get_hotspot_profile",
    "get_active_connections",
    "get_hotspot_active_connections_by_phy",
    "get_hotspot_active_connection_for_parent",
    "get_device_status_item",
    "get_filtered_device_status",
    "get_device_details",
    "get_wifi_connection_profiles",
    "get_active_wifi_connection",
    "get_saved_wifi_networks",
    "get_wifi_client_link",
    "enrich_wireless_devices",
    "get_wireless_scan_device",
    "attach_saved_wifi_networks",
    "gather_wireless_network_status",
    "get_hotspot_radio_status",
    "gather_hotspot_status",
    "get_current_wifi_link",
    "get_interface_ipv4_neighbors",
    "get_hotspot_dhcp_leases",
    "get_hotspot_station_clients",
    "gather_hotspot_clients_status",
    "get_wifi_networks",
    "default_wired_profile",
    "gather_wired_network_info",
]
