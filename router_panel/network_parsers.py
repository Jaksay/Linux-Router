from __future__ import annotations

from typing import Any

def split_escaped(line: str, separator: str = ":") -> list[str]:
    values: list[str] = []
    current: list[str] = []
    escaped = False

    for char in line:
        if escaped:
            current.append(char)
            escaped = False
            continue

        if char == "\\":
            escaped = True
            continue

        if char == separator:
            values.append("".join(current))
            current = []
            continue

        current.append(char)

    values.append("".join(current))
    return values


def parse_nmcli_lines(raw: str, fields: list[str], separator: str = ":") -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in raw.splitlines():
        values = split_escaped(line, separator)
        padded = values + [""] * (len(fields) - len(values))
        rows.append(dict(zip(fields, padded, strict=False)))
    return rows


def translate_device_state(value: str) -> str:
    mapping = {
        "connected": "已连接",
        "connected (externally)": "外部已连接",
        "disconnected": "未连接",
        "connecting": "连接中",
        "unavailable": "不可用",
        "unmanaged": "未托管",
    }
    return mapping.get(value, value or "未知")


def translate_connection_type(value: str) -> str:
    mapping = {
        "802-3-ethernet": "有线",
        "802-11-wireless": "无线",
        "wireguard": "WireGuard",
        "vpn": "VPN",
    }
    return mapping.get(value, value or "未知")


def translate_wifi_security(value: str) -> str:
    return value or "开放网络"


def parse_wifi_frequency_mhz(value: str) -> int:
    if not value:
        return 0
    first = value.strip().split()[0]
    try:
        return int(float(first))
    except ValueError:
        return 0


def format_wifi_band(value: str) -> str:
    mhz = parse_wifi_frequency_mhz(value)
    if 2400 <= mhz < 2500:
        return "2.4 GHz"
    if 4900 <= mhz < 5925:
        return "5 GHz"
    if 5925 <= mhz < 7125:
        return "6 GHz"
    return "未知"


def hotspot_band_code_from_wifi_band(value: str) -> str:
    mapping = {
        "2.4 GHz": "bg",
        "5 GHz": "a",
    }
    return mapping.get(value, "")


def hotspot_band_label(value: str) -> str:
    mapping = {
        "bg": "2.4 GHz",
        "a": "5 GHz",
    }
    return mapping.get(value, "自动")


def channel_sort_key(value: str) -> tuple[int, str]:
    try:
        return int(value), value
    except ValueError:
        return 9999, value


def parse_iw_frequency_line(line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped.startswith("* "):
        return None

    body = stripped[2:].strip()
    if not body:
        return None

    frequency_part = body.split()[0]
    frequency_mhz = parse_wifi_frequency_mhz(frequency_part)
    if not frequency_mhz:
        return None

    channel = ""
    if "[" in body and "]" in body:
        channel = body.split("[", 1)[1].split("]", 1)[0].strip()

    return {
        "channel": channel,
        "frequency_mhz": frequency_mhz,
        "frequency_label": f"{frequency_mhz} MHz",
        "band_label": format_wifi_band(str(frequency_mhz)),
        "disabled": "(disabled)" in body,
        "no_ir": "(no IR)" in body,
        "radar": "radar detection" in body,
    }


def parse_iw_valid_interface_combinations(lines: list[str]) -> dict[str, Any]:
    supports_ap_sta: bool | None = None
    max_channels: int | None = None

    for line in lines:
        has_managed = "managed" in line
        has_ap = "#{ AP }" in line
        if has_managed and has_ap:
            supports_ap_sta = True

        if has_managed and has_ap and "#channels <=" in line:
            value = line.split("#channels <=", 1)[1].split(",", 1)[0].strip()
            try:
                parsed_channels = int(value)
                max_channels = parsed_channels if max_channels is None else max(max_channels, parsed_channels)
            except ValueError:
                pass

    same_channel_only = bool(max_channels == 1 and supports_ap_sta)
    if supports_ap_sta is None and lines:
        supports_ap_sta = False

    if supports_ap_sta is False:
        ap_sta_mode = "unsupported"
        ap_sta_label = "完全不支持"
        ap_sta_description = "驱动未声明 AP 与 STA 可并发运行"
    elif same_channel_only:
        ap_sta_mode = "same_frequency"
        ap_sta_label = "同频支持"
        ap_sta_description = "AP 与 STA 只能共用同一信道，不能跨频/跨信道并发"
    elif supports_ap_sta is True and max_channels and max_channels >= 2:
        ap_sta_mode = "cross_frequency"
        ap_sta_label = "跨频支持"
        ap_sta_description = "驱动声明 AP 与 STA 可跨信道并发运行"
    else:
        ap_sta_mode = "unknown"
        ap_sta_label = "未知"
        ap_sta_description = "驱动未提供足够的 AP 与 STA 并发限制信息"

    return {
        "declared": bool(lines),
        "supports_ap_sta": supports_ap_sta,
        "max_channels": max_channels,
        "same_channel_only": same_channel_only,
        "ap_sta_mode": ap_sta_mode,
        "ap_sta_label": ap_sta_label,
        "ap_sta_description": ap_sta_description,
    }


def parse_integer_prefix(value: str) -> int:
    try:
        return int(value.strip().split()[0])
    except (ValueError, IndexError):
        return 0


def format_elapsed_time(total_seconds: int) -> str:
    if total_seconds < 60:
        return f"{total_seconds} 秒"

    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes} 分钟")
    return " ".join(parts) or "不足 1 分钟"


def format_inactive_time(milliseconds: int) -> str:
    if milliseconds < 1000:
        return f"{milliseconds} ms"
    if milliseconds < 60000:
        return f"{milliseconds / 1000:.1f} 秒"
    return f"{milliseconds // 60000} 分钟"


def format_station_signal(value: str) -> tuple[str, str, str]:
    signal_text = value.split("[", 1)[0].strip()
    if not signal_text:
        return "未知", "warning", "未知"
    try:
        signal_dbm = int(signal_text.split()[0])
    except (ValueError, IndexError):
        return signal_text, "warning", "未知"
    if "dbm" not in signal_text.lower():
        signal_text = f"{signal_text} dBm"
    if signal_dbm >= -55:
        return signal_text, "ok", "优秀"
    if signal_dbm >= -67:
        return signal_text, "ok", "良好"
    if signal_dbm >= -75:
        return signal_text, "warning", "一般"
    return signal_text, "error", "较弱"


def split_station_bitrate(value: str) -> tuple[str, str]:
    parts = value.strip().split(maxsplit=2)
    if len(parts) < 2:
        return value.strip() or "未知", ""
    return " ".join(parts[:2]), parts[2] if len(parts) > 2 else ""


def parse_iw_station_dump(raw: str) -> list[dict[str, Any]]:
    stations: list[dict[str, Any]] = []
    current: dict[str, str] | None = None

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if line.startswith("Station "):
            if current:
                stations.append(current)
            station_parts = line.split()
            current = {
                "mac_address": station_parts[1].upper() if len(station_parts) > 1 else "未知",
            }
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        current[key.strip().lower()] = value.strip()

    if current:
        stations.append(current)

    parsed: list[dict[str, Any]] = []
    for station in stations:
        signal, signal_level, signal_label = format_station_signal(station.get("signal", ""))
        downlink_rate, downlink_rate_detail = split_station_bitrate(station.get("tx bitrate", ""))
        uplink_rate, uplink_rate_detail = split_station_bitrate(station.get("rx bitrate", ""))
        connected_seconds = parse_integer_prefix(station.get("connected time", ""))
        inactive_milliseconds = parse_integer_prefix(station.get("inactive time", ""))
        parsed.append(
            {
                "mac_address": station.get("mac_address", "未知"),
                "ip_address": "未知",
                "signal": signal,
                "signal_level": signal_level,
                "signal_label": signal_label,
                "downlink_rate": downlink_rate,
                "downlink_rate_detail": downlink_rate_detail,
                "uplink_rate": uplink_rate,
                "uplink_rate_detail": uplink_rate_detail,
                "connected_time": format_elapsed_time(connected_seconds),
                "inactive_time": format_inactive_time(inactive_milliseconds),
            }
        )
    return parsed


def parse_csv_values(raw: str) -> list[str]:
    if not raw:
        return []
    values = raw.replace(";", ",").split(",")
    return [value.strip() for value in values if value.strip()]


def normalize_nmcli_general_state(value: str) -> str:
    try:
        state_code = int(value.split(" ", 1)[0])
    except (ValueError, IndexError):
        return value.strip()

    if state_code == 100:
        return "connected"
    if state_code == 30:
        return "disconnected"
    if state_code == 20:
        return "unavailable"
    if state_code == 10:
        return "unmanaged"
    if 40 <= state_code <= 110:
        return "connecting"
    return "unavailable" if state_code == 120 else value.strip()


__all__ = [
    "split_escaped",
    "parse_nmcli_lines",
    "translate_device_state",
    "translate_connection_type",
    "translate_wifi_security",
    "parse_wifi_frequency_mhz",
    "format_wifi_band",
    "hotspot_band_code_from_wifi_band",
    "hotspot_band_label",
    "channel_sort_key",
    "parse_iw_frequency_line",
    "parse_iw_valid_interface_combinations",
    "parse_integer_prefix",
    "format_elapsed_time",
    "format_inactive_time",
    "format_station_signal",
    "split_station_bitrate",
    "parse_iw_station_dump",
    "parse_csv_values",
    "normalize_nmcli_general_state",
]
