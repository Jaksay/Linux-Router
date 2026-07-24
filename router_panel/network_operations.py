from __future__ import annotations

import time
from configparser import ConfigParser
from io import StringIO
from pathlib import Path
from typing import Callable

from .core import (
    CommandResult,
    HOTSPOT_CONNECTION_NAME,
    NETWORKMANAGER_CONFIG_PATH,
    NETWORKMANAGER_CONF_DIR,
    atomic_write_text,
    clear_timed_cache,
    file_update_lock,
    format_hotspot_error,
    get_hotspot_virtual_interface_name,
    get_network_interface_hardware,
    is_hotspot_virtual_interface,
    load_network_config,
    normalize_mac_address,
    run_command,
)
from .contracts import WifiConnectResult
from .network import (
    get_active_wifi_connection,
    get_device_status_item,
    get_device_details,
    get_hotspot_active_connection_for_parent,
    get_hotspot_profile,
    get_wifi_connection_profiles,
    get_wireless_interface_phy_map,
    get_wireless_phy_capabilities,
)
from .network_parsers import parse_csv_values, parse_nmcli_lines

ProgressCallback = Callable[[str], None]


def delete_inactive_hotspot_profiles() -> str | None:
    result = run_command(
        ["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show"]
    )
    if not result.ok:
        return result.output or "无法读取热点连接配置"

    profiles = parse_nmcli_lines(result.output, ["name", "uuid", "type", "device"])
    for profile in profiles:
        if (
            profile.get("name") != HOTSPOT_CONNECTION_NAME
            or profile.get("type") != "802-11-wireless"
            or profile.get("device")
        ):
            continue
        profile_uuid = profile.get("uuid", "").strip()
        if not profile_uuid:
            continue
        deleted = run_command(
            ["nmcli", "connection", "delete", "uuid", profile_uuid], timeout=15
        )
        if not deleted.ok:
            return deleted.output or "清理旧热点配置失败"
    return None


def ensure_hotspot_virtual_interface(parent_ifname: str, phy_name: str) -> tuple[str, str | None]:
    ap_ifname = get_hotspot_virtual_interface_name(parent_ifname)
    if Path(f"/sys/class/net/{ap_ifname}").exists():
        clear_timed_cache("wireless:")
        return ap_ifname, None
    if not phy_name:
        return "", "无法确定无线 PHY，不能创建并发热点接口"

    created = run_command(
        ["iw", "phy", phy_name, "interface", "add", ap_ifname, "type", "__ap"],
        timeout=10,
    )
    if not created.ok:
        return "", created.output or f"创建并发热点接口 {ap_ifname} 失败"
    run_command(["nmcli", "general", "reload"], timeout=15)
    run_command(["nmcli", "device", "set", ap_ifname, "managed", "yes"], timeout=15)
    clear_timed_cache("wireless:")
    return ap_ifname, None


def delete_hotspot_virtual_interface(parent_ifname: str) -> None:
    ap_ifname = get_hotspot_virtual_interface_name(parent_ifname)
    if not Path(f"/sys/class/net/{ap_ifname}").exists():
        return
    run_command(["iw", "dev", ap_ifname, "del"], timeout=10)
    run_command(["nmcli", "general", "reload"], timeout=15)
    clear_timed_cache("wireless:")


def cleanup_hotspot_virtual_interfaces(except_parent_ifname: str = "") -> None:
    changed = False
    for interface_dir in Path("/sys/class/net").iterdir():
        ifname = interface_dir.name
        if not is_hotspot_virtual_interface(ifname):
            continue
        if except_parent_ifname and ifname == get_hotspot_virtual_interface_name(except_parent_ifname):
            continue
        run_command(["iw", "dev", ifname, "del"], timeout=10)
        changed = True
    if changed:
        run_command(["nmcli", "general", "reload"], timeout=15)
        clear_timed_cache("wireless:")


def get_interface_permanent_mac(ifname: str) -> str:
    hardware = next(
        (item for item in get_network_interface_hardware() if item.get("name") == ifname),
        {},
    )
    return normalize_mac_address(hardware.get("permanent_mac_address", ""))


def configure_hotspot_keepalive(enabled: bool) -> CommandResult:
    return run_command(
        [
            "nmcli", "connection", "modify", "id", HOTSPOT_CONNECTION_NAME,
            "connection.autoconnect", "yes",
            "connection.autoconnect-priority", "999" if enabled else "100",
            "connection.autoconnect-retries", "0" if enabled else "-1",
        ],
        timeout=15,
    )


def resolve_hotspot_keepalive_parent(config: dict[str, object]) -> tuple[str, str]:
    expected_mac = normalize_mac_address(str(config.get("parent_mac", "")))
    phy_map = get_wireless_interface_phy_map()
    for hardware in get_network_interface_hardware():
        if normalize_mac_address(hardware.get("permanent_mac_address", "")) != expected_mac:
            continue
        ifname = hardware.get("name", "")
        if ifname in phy_map:
            return ifname, phy_map.get(ifname, "")
    parent_ifname = str(config.get("parent_ifname", ""))
    if parent_ifname not in phy_map:
        return "", ""
    return parent_ifname, phy_map.get(parent_ifname, str(config.get("phy_name", "")))


def hotspot_keepalive_is_online(config: dict[str, object]) -> bool:
    parent_ifname, _ = resolve_hotspot_keepalive_parent(config)
    if not parent_ifname:
        return False
    active = get_hotspot_active_connection_for_parent(parent_ifname)
    hotspot_ifname = active.get("device", "")
    if not hotspot_ifname:
        return False
    return bool(get_device_details(hotspot_ifname).get("ipv4"))


def recover_hotspot_keepalive(config: dict[str, object]) -> CommandResult:
    parent_ifname, phy_name = resolve_hotspot_keepalive_parent(config)
    if not parent_ifname:
        return CommandResult(False, "找不到保活热点对应的无线网卡")
    profile = get_hotspot_profile()
    if not profile.get("password"):
        return CommandResult(False, "找不到可恢复的热点配置")

    configured = configure_hotspot_keepalive(True)
    if not configured.ok:
        return configured

    profile_ifname = profile.get("interface_name", "")
    if is_hotspot_virtual_interface(profile_ifname):
        ap_ifname, interface_error = ensure_hotspot_virtual_interface(parent_ifname, phy_name)
        if not interface_error:
            run_command(
                ["nmcli", "connection", "modify", "id", HOTSPOT_CONNECTION_NAME,
                 "connection.interface-name", ap_ifname],
                timeout=15,
            )
            concurrent_up = run_command(
                ["nmcli", "connection", "up", "id", HOTSPOT_CONNECTION_NAME, "ifname", ap_ifname],
                timeout=40,
            )
            if concurrent_up.ok:
                return concurrent_up
        delete_hotspot_virtual_interface(parent_ifname)

    # A protected AP wins when this radio cannot run STA and AP together.
    disconnect_wifi(parent_ifname)
    permanent_mac = get_interface_permanent_mac(parent_ifname)
    if not permanent_mac:
        return CommandResult(False, f"无法读取 {parent_ifname} 的永久 MAC 地址")
    rebound = run_command(
        [
            "nmcli", "connection", "modify", "id", HOTSPOT_CONNECTION_NAME,
            "connection.interface-name", parent_ifname,
            "802-11-wireless.mac-address", permanent_mac,
            "802-11-wireless.cloned-mac-address", "permanent",
        ],
        timeout=15,
    )
    if not rebound.ok:
        return rebound
    up = run_command(
        ["nmcli", "connection", "up", "id", HOTSPOT_CONNECTION_NAME, "ifname", parent_ifname],
        timeout=40,
    )
    return up


def bind_wifi_profile_to_hardware(profile_uuid: str, ifname: str, cloned_mac: str) -> str | None:
    permanent_mac = get_interface_permanent_mac(ifname)
    if not permanent_mac:
        return f"无法读取 {ifname} 的永久 MAC 地址"
    result = run_command(
        [
            "nmcli", "connection", "modify", "uuid", profile_uuid,
            "connection.interface-name", "", "802-11-wireless.mac-address", permanent_mac,
            "802-11-wireless.cloned-mac-address", cloned_mac or "permanent",
        ],
        timeout=15,
    )
    if not result.ok:
        return result.output or "绑定 Wi-Fi 配置到物理网卡失败"
    return None


def activate_hotspot_profile(
    hotspot_ifname: str,
    ssid: str,
    password: str,
    band: str,
    channel: str,
    mode: str,
    progress: ProgressCallback | None = None,
) -> CommandResult:
    lan_address = load_network_config()["lan_address"]
    permanent_mac = ""
    if mode == "exclusive":
        permanent_mac = get_interface_permanent_mac(hotspot_ifname)
        if not permanent_mac:
            return CommandResult(False, f"无法读取 {hotspot_ifname} 的永久 MAC 地址")

    deleted = run_command(
        ["nmcli", "connection", "delete", "id", HOTSPOT_CONNECTION_NAME], timeout=15
    )
    if not deleted.ok and "unknown connection" not in (deleted.output or "").lower():
        return deleted

    if progress:
        progress("正在写入热点配置")
    added = run_command(
        [
            "nmcli", "connection", "add", "type", "wifi", "ifname", hotspot_ifname,
            "con-name", HOTSPOT_CONNECTION_NAME, "ssid", ssid,
        ],
        timeout=20,
    )
    if not added.ok:
        return added

    modify_command = [
        "nmcli", "connection", "modify", HOTSPOT_CONNECTION_NAME,
        "connection.interface-name", "" if mode == "exclusive" else hotspot_ifname,
        "connection.autoconnect", "yes",
        "connection.autoconnect-priority", "100", "802-11-wireless.mode", "ap",
        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
        "ipv4.method", "shared", "ipv4.addresses", lan_address,
        "ipv4.link-local", "disabled", "ipv4.gateway", "", "ipv4.dns", "",
    ]
    if permanent_mac:
        modify_command.extend(
            [
                "802-11-wireless.mac-address", permanent_mac,
                "802-11-wireless.cloned-mac-address", "permanent",
            ]
        )
    if band:
        modify_command.extend(["802-11-wireless.band", band])
    if channel:
        modify_command.extend(["802-11-wireless.channel", channel])
    modified = run_command(modify_command, timeout=20)
    if not modified.ok:
        return modified

    if progress:
        progress("正在启动热点")
    up_command = ["nmcli", "connection", "up", HOTSPOT_CONNECTION_NAME]
    if mode == "exclusive":
        up_command.extend(["ifname", hotspot_ifname])
    first_attempt = run_command(up_command, timeout=40)
    if first_attempt.ok:
        return first_attempt

    if progress:
        progress("正在应用 WPA2 兼容配置")
    compatibility = run_command(
        [
            "nmcli", "connection", "modify", HOTSPOT_CONNECTION_NAME,
            "wifi-sec.key-mgmt", "wpa-psk",
            "wifi-sec.proto", "rsn",
            "wifi-sec.pairwise", "ccmp",
            "wifi-sec.group", "ccmp",
            "wifi-sec.pmf", "disable",
        ],
        timeout=20,
    )
    if not compatibility.ok:
        return CommandResult(
            False,
            f"{first_attempt.output or '热点首次激活失败'}；应用 WPA2 兼容配置失败："
            f"{compatibility.output or '未知错误'}",
        )

    if mode == "concurrent":
        time.sleep(2)
    if progress:
        progress("正在重试启动热点")
    fallback_attempt = run_command(up_command, timeout=40)
    if fallback_attempt.ok:
        return fallback_attempt
    return CommandResult(
        False,
        f"{first_attempt.output or '热点首次激活失败'}；WPA2 兼容模式重试失败："
        f"{fallback_attempt.output or '未知错误'}",
    )


def get_networkmanager_config_paths() -> list[Path]:
    paths: list[Path] = []
    if NETWORKMANAGER_CONFIG_PATH.exists():
        paths.append(NETWORKMANAGER_CONFIG_PATH)
    if NETWORKMANAGER_CONF_DIR.exists():
        paths.extend(sorted(NETWORKMANAGER_CONF_DIR.glob("*.conf")))
    return paths


def remove_interface_from_unmanaged_devices(ifname: str) -> bool:
    if not ifname:
        return False
    target = f"interface-name:{ifname}"
    changed = False
    for path in get_networkmanager_config_paths():
        with file_update_lock(path) as acquired:
            if not acquired:
                continue
            parser = ConfigParser(interpolation=None)
            try:
                with path.open(encoding="utf-8") as handle:
                    parser.read_file(handle)
            except OSError:
                continue
            file_changed = False
            for section in parser.sections():
                if not parser.has_option(section, "unmanaged-devices"):
                    continue
                current_values = parse_csv_values(parser.get(section, "unmanaged-devices"))
                updated_values = [value for value in current_values if value != target]
                if updated_values == current_values:
                    continue
                file_changed = True
                changed = True
                if updated_values:
                    parser.set(section, "unmanaged-devices", ",".join(updated_values))
                else:
                    parser.remove_option(section, "unmanaged-devices")
            if not file_changed:
                continue
            output = StringIO()
            parser.write(output)
            atomic_write_text(path, output.getvalue())
    return changed


def _rescan_wifi(ifname: str) -> CommandResult:
    return run_command(["nmcli", "device", "wifi", "rescan", "ifname", ifname], timeout=15)


def _connect_wifi_profile(
    ifname: str,
    ssid: str,
    password: str,
    bssid: str,
    cloned_mac: str,
) -> WifiConnectResult:
    phy_name = get_wireless_interface_phy_map().get(ifname, "")
    phy_capability = get_wireless_phy_capabilities().get(phy_name, {})
    concurrency_mode = phy_capability.get("concurrency", {}).get("ap_sta_mode", "unknown")
    hotspot_connection = get_hotspot_active_connection_for_parent(ifname)
    hotspot_is_concurrent = bool(
        hotspot_connection
        and is_hotspot_virtual_interface(hotspot_connection.get("device", ""))
    )
    command = ["nmcli", "device", "wifi", "connect", ssid]
    if password:
        command.extend(["password", password])
    command.extend(["ifname", ifname])
    if bssid:
        command.extend(["bssid", bssid])

    result = run_command(command, timeout=30)
    if (
        not result.ok
        and (
            "802-11-wireless-security.key-mgmt: property is missing" in result.output
            or "passwords or encryption keys are required" in result.output.lower()
        )
        and password
    ):
        for profile in get_wifi_connection_profiles(ssid):
            profile_uuid = profile.get("uuid", "").strip()
            if profile_uuid:
                run_command(["nmcli", "connection", "delete", "uuid", profile_uuid], timeout=15)
        result = run_command(command, timeout=30)

    binding_error = ""
    current_state: dict[str, str] = {}
    if result.ok:
        profile_uuid = get_active_wifi_connection(ifname).get("uuid", "").strip()
        binding_error = (
            bind_wifi_profile_to_hardware(profile_uuid, ifname, cloned_mac)
            if profile_uuid
            else "找不到刚建立的 Wi-Fi 配置"
        )
        if not binding_error and cloned_mac:
            down_result = run_command(
                ["nmcli", "connection", "down", "uuid", profile_uuid], timeout=20
            )
            if not down_result.ok:
                binding_error = down_result.output or "应用指定 MAC 时无法断开当前连接"
            else:
                up_result = run_command(
                    ["nmcli", "connection", "up", "uuid", profile_uuid, "ifname", ifname],
                    timeout=40,
                )
                if not up_result.ok:
                    binding_error = up_result.output or "使用指定 MAC 重新连接失败"

        for _ in range(12):
            current_state = get_device_status_item(ifname)
            state = current_state.get("state", "")
            if state == "connected" or state not in {"connecting", "disconnected"}:
                break
            time.sleep(1)

    return {
        "result": result,
        "binding_error": binding_error,
        "current_state": current_state,
        "concurrency_mode": concurrency_mode,
        "hotspot_is_concurrent": hotspot_is_concurrent,
        "hotspot_profile": get_hotspot_profile(),
    }


def _disconnect_wifi(ifname: str) -> CommandResult:
    return run_command(["nmcli", "device", "disconnect", ifname], timeout=20)


def _manage_networkmanager_interface(ifname: str) -> list[CommandResult]:
    remove_interface_from_unmanaged_devices(ifname)
    return [
        run_command(["nmcli", "general", "reload"], timeout=15),
        run_command(["nmcli", "device", "set", ifname, "managed", "yes"], timeout=15),
    ]


def _forget_wifi_profile(profile_uuid: str) -> CommandResult:
    return run_command(["nmcli", "connection", "delete", "uuid", profile_uuid], timeout=20)


def _start_hotspot_profile(
    ifname: str,
    phy_name: str,
    ssid: str,
    password: str,
    band: str,
    channel: str,
    mode: str,
    progress: ProgressCallback | None = None,
) -> CommandResult:
    hotspot_ifname = ifname
    if progress:
        progress("正在准备 AP 接口")
    if mode == "concurrent":
        ap_ifname, interface_error = ensure_hotspot_virtual_interface(ifname, phy_name)
        if interface_error:
            return CommandResult(False, interface_error)
        hotspot_ifname = ap_ifname
    else:
        cleanup_hotspot_virtual_interfaces()

    result = activate_hotspot_profile(
        hotspot_ifname,
        ssid,
        password,
        band,
        channel,
        mode,
        progress=progress,
    )
    if result.ok:
        return result
    run_command(["nmcli", "connection", "down", "id", HOTSPOT_CONNECTION_NAME], timeout=20)
    delete_inactive_hotspot_profiles()
    if mode == "concurrent":
        delete_hotspot_virtual_interface(ifname)
    return CommandResult(False, format_hotspot_error(result.output or "开启热点失败", ifname))


def _stop_hotspot_profile(ifname: str) -> CommandResult:
    disable_autoconnect = run_command(
        ["nmcli", "connection", "modify", "id", HOTSPOT_CONNECTION_NAME, "connection.autoconnect", "no"],
        timeout=15,
    )
    result = (
        run_command(["nmcli", "connection", "down", "id", HOTSPOT_CONNECTION_NAME], timeout=20)
        if disable_autoconnect.ok
        else disable_autoconnect
    )
    if ifname:
        delete_hotspot_virtual_interface(ifname)
    else:
        cleanup_hotspot_virtual_interfaces()
    return result


def rescan_wifi(ifname: str) -> CommandResult:
    return _rescan_wifi(ifname)


def connect_wifi_profile(
    ifname: str,
    ssid: str,
    password: str,
    bssid: str,
    cloned_mac: str,
) -> WifiConnectResult:
    return _connect_wifi_profile(ifname, ssid, password, bssid, cloned_mac)


def disconnect_wifi(ifname: str) -> CommandResult:
    return _disconnect_wifi(ifname)


def manage_networkmanager_interface(ifname: str) -> list[CommandResult]:
    return _manage_networkmanager_interface(ifname)


def forget_wifi_profile(profile_uuid: str) -> CommandResult:
    return _forget_wifi_profile(profile_uuid)


def start_hotspot_profile(
    ifname: str,
    phy_name: str,
    ssid: str,
    password: str,
    band: str,
    channel: str,
    mode: str,
    progress: ProgressCallback | None = None,
) -> CommandResult:
    return _start_hotspot_profile(
        ifname,
        phy_name,
        ssid,
        password,
        band,
        channel,
        mode,
        progress=progress,
    )


def stop_hotspot_profile(ifname: str) -> CommandResult:
    return _stop_hotspot_profile(ifname)


__all__ = [
    "rescan_wifi",
    "connect_wifi_profile",
    "disconnect_wifi",
    "manage_networkmanager_interface",
    "forget_wifi_profile",
    "start_hotspot_profile",
    "stop_hotspot_profile",
    "configure_hotspot_keepalive",
    "get_interface_permanent_mac",
    "hotspot_keepalive_is_online",
    "recover_hotspot_keepalive",
    "resolve_hotspot_keepalive_parent",
]
