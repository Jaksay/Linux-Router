from __future__ import annotations

import json
import fcntl
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from ipaddress import IPv4Network, ip_network
from pathlib import Path
from typing import Any

from werkzeug.security import generate_password_hash


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LINUX_ROUTER_DATA_DIR", BASE_DIR / "data")).expanduser()
AUTH_PATH = DATA_DIR / "auth.json"
NETWORK_CONFIG_PATH = DATA_DIR / "network.json"
PASSWORD_HINT_PATH = DATA_DIR / "initial_password.txt"
SECRET_KEY_PATH = DATA_DIR / "secret_key"
BUILD_INFO_PATH = BASE_DIR / "BUILD_INFO"
HOTSPOT_CONNECTION_NAME = "DebianRouterHotspot"
HOTSPOT_DEFAULT_SSID = "DebianRouter"
HOTSPOT_VIRTUAL_INTERFACE_PREFIX = "ap-"
DEFAULT_LAN_NETWORK = "192.168.50.0/24"
NETWORKMANAGER_CONFIG_PATH = Path("/etc/NetworkManager/NetworkManager.conf")
NETWORKMANAGER_CONF_DIR = Path("/etc/NetworkManager/conf.d")
NETPLAN_DIR = Path("/etc/netplan")
ROUTER_PANEL_SYSCTL_PATH = Path("/etc/sysctl.d/90-router-panel.conf")
HARDWARE_INFO_CACHE_TTL = 15
WIRELESS_PHY_CACHE_TTL = 15
SYSTEM_STATIC_CACHE_TTL = 3600
APT_TIMEOUT = 300
SYSTEM_COMMAND_TIMEOUT = 30
REQUIRED_PACKAGES = (
    "python3-flask",
    "gunicorn",
    "network-manager",
    "dnsmasq-base",
    "iproute2",
    "iptables",
    "iw",
    "udev",
    "wpasupplicant",
)


@dataclass
class CommandResult:
    ok: bool
    output: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    command: tuple[str, ...] = ()
    timed_out: bool = False


class KeyedLockRegistry:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    @contextmanager
    def acquire(self, key: str):
        with self._guard:
            lock = self._locks.setdefault(key, threading.RLock())
        acquired = lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()


_timed_cache: dict[str, tuple[float, Any]] = {}
_file_update_locks = KeyedLockRegistry()
_held_file_locks = threading.local()


@contextmanager
def file_update_lock(path: Path):
    resolved = path.resolve()
    key = str(resolved)
    held = getattr(_held_file_locks, "paths", {})
    if key in held:
        held[key] += 1
        try:
            yield True
        finally:
            held[key] -= 1
        return

    with _file_update_locks.acquire(key) as acquired:
        if not acquired:
            yield False
            return
        resolved.parent.mkdir(parents=True, exist_ok=True)
        lock_path = resolved.with_name(f"{resolved.name}.router-panel.lock")
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            yield False
            return
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            held[key] = 1
            _held_file_locks.paths = held
            try:
                yield True
            finally:
                held.pop(key, None)
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def atomic_write_text(
    path: Path,
    content: str,
    *,
    mode: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_update_lock(path) as acquired:
        if not acquired:
            raise OSError(f"file is already being updated: {path}")

        target_mode = mode
        if target_mode is None:
            try:
                target_mode = path.stat().st_mode & 0o777
            except FileNotFoundError:
                target_mode = 0o644

        fd, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, target_mode)
            os.replace(temporary_path, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary_path.unlink(missing_ok=True)


def run_command(
    command: list[str],
    timeout: int = 8,
    env: dict[str, str] | None = None,
) -> CommandResult:
    started_at = time.monotonic()
    command_tuple = tuple(command)
    command_env = dict(env) if env is not None else os.environ.copy()
    command_env["LC_ALL"] = "C"
    command_env.setdefault("LANG", "C")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=command_env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return CommandResult(
            False,
            str(exc),
            stdout=stdout.strip(),
            stderr=stderr.strip(),
            duration=time.monotonic() - started_at,
            command=command_tuple,
            timed_out=True,
        )
    except OSError as exc:
        return CommandResult(
            False,
            str(exc),
            stderr=str(exc),
            duration=time.monotonic() - started_at,
            command=command_tuple,
        )

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    output = stdout or stderr
    return CommandResult(
        completed.returncode == 0,
        output,
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        duration=time.monotonic() - started_at,
        command=command_tuple,
    )


def request_system_reboot() -> CommandResult:
    return run_command(["systemctl", "reboot", "--no-block"], timeout=5)


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def package_installed(package_name: str) -> bool:
    result = run_command(
        ["dpkg-query", "-W", "-f=${Status}", package_name],
        timeout=5,
    )
    return result.ok and "install ok installed" in result.output


def python_module_available(module_name: str) -> bool:
    try:
        __import__(module_name)
    except ImportError:
        return False
    return True


def is_hotspot_virtual_interface(ifname: str) -> bool:
    return ifname.startswith(HOTSPOT_VIRTUAL_INTERFACE_PREFIX)


def get_hotspot_virtual_interface_name(parent_ifname: str) -> str:
    return f"{HOTSPOT_VIRTUAL_INTERFACE_PREFIX}{parent_ifname}"[:15]


def format_hotspot_error(message: str, ifname: str) -> str:
    normalized = (message or "").strip()
    if not normalized:
        return "开启热点失败"
    if any(
        marker in normalized
        for marker in (
            "；应用 WPA2 兼容配置失败：",
            "；WPA2 兼容模式重试失败：",
        )
    ):
        return normalized

    lowered = normalized.lower()
    timeout_markers = (
        "802.1x supplicant took too long to authenticate",
        "hotspot network creation took too long",
        "supplicant-timeout",
    )
    if any(marker in lowered for marker in timeout_markers):
        return (
            f"NetworkManager 启动 {ifname} 热点超时；这通常是网卡或驱动切换到 AP 模式失败。"
            "请稍后重试，必要时改用 2.4 GHz 频段或另一张无线网卡"
        )
    return normalized


def get_timed_cache(key: str, ttl: int) -> Any | None:
    cached = _timed_cache.get(key)
    if not cached:
        return None

    expires_at, value = cached
    if time.time() >= expires_at:
        _timed_cache.pop(key, None)
        return None
    return value


def set_timed_cache(key: str, ttl: int, value: Any) -> Any:
    _timed_cache[key] = (time.time() + ttl, value)
    return value


def clear_timed_cache(prefix: str = "") -> None:
    if not prefix:
        _timed_cache.clear()
        return

    for key in list(_timed_cache):
        if key.startswith(prefix):
            _timed_cache.pop(key, None)


def load_os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key] = value.strip().strip('"')
    except OSError:
        return data
    return data


def read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def format_uptime(seconds_raw: str) -> str:
    try:
        total_seconds = int(float(seconds_raw.split()[0]))
    except (ValueError, IndexError):
        return "未知"

    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours or days:
        parts.append(f"{hours} 小时")
    parts.append(f"{minutes} 分钟")
    return " ".join(parts)


def format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


def get_cpu_temperature() -> str:
    thermal_root = Path("/sys/class/thermal")
    candidates: list[tuple[int, Path]] = []
    try:
        thermal_zones = list(thermal_root.glob("thermal_zone*"))
    except OSError:
        return "未知"

    for zone in thermal_zones:
        zone_type = read_text(str(zone / "type")).lower()
        priority = 0 if any(name in zone_type for name in ("cpu", "soc", "package")) else 1
        candidates.append((priority, zone))

    for _, zone in sorted(candidates, key=lambda item: (item[0], item[1].name)):
        raw_temperature = read_text(str(zone / "temp"))
        try:
            temperature = float(raw_temperature)
        except ValueError:
            continue
        if abs(temperature) >= 1000:
            temperature /= 1000
        if -50 <= temperature <= 200:
            return f"{temperature:.1f} °C"
    return "未知"


def parse_key_value_lines(raw: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value.strip()
    return data


def normalize_mac_address(value: str) -> str:
    compact = value.strip().replace(":", "").replace("-", "").lower()
    if len(compact) != 12:
        return ""
    try:
        octets = [int(compact[index:index + 2], 16) for index in range(0, 12, 2)]
    except ValueError:
        return ""
    if octets[0] & 1 or all(value == 0 for value in octets) or all(value == 255 for value in octets):
        return ""
    return ":".join(f"{value:02X}" for value in octets)


def translate_bus_label(value: str) -> str:
    mapping = {
        "pci": "PCIe",
        "usb": "USB",
        "platform": "板载",
        "sdio": "SDIO",
    }
    return mapping.get(value.lower(), value or "未知")


def format_vendor_name(value: str) -> str:
    if not value:
        return ""
    return value.replace(" Corp.", "").replace(" Corporation", "").strip().rstrip(",")


def get_udev_properties(device_path: Path) -> dict[str, str]:
    result = run_command(["udevadm", "info", "-q", "property", "-p", str(device_path)], timeout=5)
    if not result.ok or not result.output:
        return {}
    return parse_key_value_lines(result.output)


def get_dt_compatible_vendor_model(compatible: str) -> tuple[str, str]:
    if not compatible:
        return "", ""
    vendor, _, model = compatible.partition(",")
    return vendor.replace("-", " ").title(), model or compatible


def get_network_interface_hardware() -> list[dict[str, Any]]:
    cached = get_timed_cache("hardware:interfaces", HARDWARE_INFO_CACHE_TTL)
    if cached is not None:
        return [item.copy() for item in cached]

    interfaces: list[dict[str, Any]] = []
    sys_class_net = Path("/sys/class/net")
    if not sys_class_net.exists():
        return interfaces

    for interface_dir in sorted(sys_class_net.iterdir(), key=lambda item: item.name):
        ifname = interface_dir.name
        if ifname == "lo":
            continue

        device_link = interface_dir / "device"
        if not device_link.exists():
            continue

        device_path = device_link.resolve()
        props = get_udev_properties(device_path)
        interface_props = get_udev_properties(interface_dir)
        bus = props.get("SUBSYSTEM", "")
        compatible = props.get("OF_COMPATIBLE_0", "")
        dt_vendor, dt_model = get_dt_compatible_vendor_model(compatible)
        product_code = props.get("PRODUCT", "")
        pci_id = props.get("PCI_ID", "")

        vendor = (
            format_vendor_name(props.get("ID_VENDOR_FROM_DATABASE", ""))
            or dt_vendor
            or "未知"
        )
        model = (
            props.get("ID_MODEL_FROM_DATABASE", "")
            or props.get("ID_MODEL", "")
            or read_text(str(device_path / "interface"))
            or dt_model
            or "未知"
        )
        driver = (
            props.get("ID_NET_DRIVER", "")
            or props.get("ID_USB_DRIVER", "")
            or props.get("DRIVER", "")
            or "未知"
        )

        hardware_id = "未知"
        if pci_id:
            hardware_id = pci_id
        elif product_code:
            parts = product_code.split("/")
            if len(parts) >= 2:
                hardware_id = f"{parts[0].upper()}:{parts[1].upper()}"
            else:
                hardware_id = product_code
        elif compatible:
            hardware_id = compatible

        location = (
            props.get("PCI_SLOT_NAME", "")
            or props.get("ID_PATH", "")
            or props.get("DEVPATH", "")
            or str(device_path)
        )
        current_mac_address = read_text(str(interface_dir / "address")) or "无"
        permanent_mac_name = interface_props.get("ID_NET_NAME_MAC", "")
        permanent_mac_address = normalize_mac_address(permanent_mac_name[-12:])
        if not permanent_mac_address:
            permanent_mac_address = normalize_mac_address(current_mac_address)

        interfaces.append(
            {
                "name": ifname,
                "role_label": "无线网卡" if (interface_dir / "wireless").exists() else "有线网卡",
                "bus_label": translate_bus_label(bus),
                "vendor": vendor,
                "model": model,
                "driver": driver,
                "hardware_id": hardware_id,
                "location": location,
                "mac_address": current_mac_address,
                "permanent_mac_address": permanent_mac_address,
            }
        )

    set_timed_cache("hardware:interfaces", HARDWARE_INFO_CACHE_TTL, [item.copy() for item in interfaces])
    return interfaces


def get_cpu_model() -> str:
    lscpu = run_command(["lscpu"])
    if lscpu.ok and lscpu.output:
        model_name = ""
        cpu_count = ""
        for line in lscpu.output.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            if key == "model name":
                model_name = value
            elif key == "cpu(s)":
                cpu_count = value

        if model_name and cpu_count:
            return f"{model_name} ({cpu_count} 核)"
        if model_name:
            return model_name

    cpuinfo_values: dict[str, str] = {}
    processor_count = 0
    for line in read_text("/proc/cpuinfo").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "processor":
            processor_count += 1
        elif key in {"model name", "hardware", "processor"} and not value.isdigit():
            cpuinfo_values[key] = value

    for preferred_key in ["model name", "hardware", "processor"]:
        if cpuinfo_values.get(preferred_key):
            if processor_count:
                return f"{cpuinfo_values[preferred_key]} ({processor_count} 核)"
            return cpuinfo_values[preferred_key]

    if processor_count:
        return f"{processor_count} 核 CPU"

    return "未知"


def ensure_secret_key() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_text(encoding="utf-8").strip()

    secret_key = secrets.token_hex(32)
    atomic_write_text(SECRET_KEY_PATH, secret_key, mode=0o600)
    return secret_key


def ensure_auth_config() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if AUTH_PATH.exists():
        return

    initial_password = os.environ.get("LINUX_ROUTER_INITIAL_PASSWORD", "").strip() or "password"
    atomic_write_text(
        AUTH_PATH,
        json.dumps(
            {
                "username": "admin",
                "password_hash": generate_password_hash(initial_password),
            },
            ensure_ascii=False,
            indent=2,
        ),
        mode=0o600,
    )
    atomic_write_text(
        PASSWORD_HINT_PATH,
        (
            "首次登录凭据\n"
            "用户名: admin\n"
            f"密码: {initial_password}\n"
            "登录后建议尽快更换密码\n"
        ),
        mode=0o600,
    )


def load_auth_config() -> dict[str, str]:
    try:
        return json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"username": "admin", "password_hash": ""}


def save_auth_config(config: dict[str, str]) -> None:
    atomic_write_text(
        AUTH_PATH,
        json.dumps(config, ensure_ascii=False, indent=2),
        mode=0o600,
    )


def normalize_lan_network(value: str) -> tuple[IPv4Network | None, str | None]:
    try:
        network = ip_network(value.strip(), strict=False)
    except ValueError:
        return None, "请输入有效的 IPv4 网段，例如 192.168.50.0/24"

    if not isinstance(network, IPv4Network):
        return None, "LAN 网段仅支持 IPv4"
    if not 16 <= network.prefixlen <= 30:
        return None, "LAN 网段前缀长度必须在 /16 到 /30 之间"

    private_ranges = (
        IPv4Network("10.0.0.0/8"),
        IPv4Network("172.16.0.0/12"),
        IPv4Network("192.168.0.0/16"),
    )
    if not any(network.subnet_of(private_range) for private_range in private_ranges):
        return None, "LAN 必须使用 10/8、172.16/12 或 192.168/16 私有网段"
    return network, None


def load_network_config() -> dict[str, str]:
    try:
        config = json.loads(NETWORK_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        config = {}

    network, _ = normalize_lan_network(config.get("lan_network", DEFAULT_LAN_NETWORK))
    if network is None:
        network = IPv4Network(DEFAULT_LAN_NETWORK)
    gateway = next(network.hosts())
    return {
        "lan_network": str(network),
        "lan_gateway": str(gateway),
        "lan_address": f"{gateway}/{network.prefixlen}",
    }


def save_network_config(lan_network: IPv4Network) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        NETWORK_CONFIG_PATH,
        json.dumps({"lan_network": str(lan_network)}, ensure_ascii=False, indent=2),
        mode=0o600,
    )


def get_build_info() -> dict[str, str]:
    info = {"branch": "unknown", "build": "unknown"}
    try:
        raw_lines = BUILD_INFO_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return info

    for line in raw_lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in info and value:
            info[key] = value
    return info


def is_service_active(service_name: str) -> bool:
    result = run_command(["systemctl", "is-active", service_name], timeout=5)
    return result.ok and result.output.strip() == "active"


def is_service_enabled(service_name: str) -> bool:
    result = run_command(["systemctl", "is-enabled", service_name], timeout=5)
    return result.ok and result.output.strip() == "enabled"


__all__ = [
    "BASE_DIR",
    "DATA_DIR",
    "AUTH_PATH",
    "NETWORK_CONFIG_PATH",
    "PASSWORD_HINT_PATH",
    "SECRET_KEY_PATH",
    "BUILD_INFO_PATH",
    "HOTSPOT_CONNECTION_NAME",
    "HOTSPOT_DEFAULT_SSID",
    "HOTSPOT_VIRTUAL_INTERFACE_PREFIX",
    "DEFAULT_LAN_NETWORK",
    "NETWORKMANAGER_CONFIG_PATH",
    "NETWORKMANAGER_CONF_DIR",
    "NETPLAN_DIR",
    "ROUTER_PANEL_SYSCTL_PATH",
    "HARDWARE_INFO_CACHE_TTL",
    "WIRELESS_PHY_CACHE_TTL",
    "SYSTEM_STATIC_CACHE_TTL",
    "APT_TIMEOUT",
    "SYSTEM_COMMAND_TIMEOUT",
    "REQUIRED_PACKAGES",
    "CommandResult",
    "KeyedLockRegistry",
    "file_update_lock",
    "atomic_write_text",
    "run_command",
    "request_system_reboot",
    "command_exists",
    "package_installed",
    "python_module_available",
    "is_hotspot_virtual_interface",
    "get_hotspot_virtual_interface_name",
    "format_hotspot_error",
    "get_timed_cache",
    "set_timed_cache",
    "clear_timed_cache",
    "load_os_release",
    "read_text",
    "format_uptime",
    "format_bytes",
    "get_cpu_temperature",
    "parse_key_value_lines",
    "normalize_mac_address",
    "translate_bus_label",
    "format_vendor_name",
    "get_udev_properties",
    "get_dt_compatible_vendor_model",
    "get_network_interface_hardware",
    "get_cpu_model",
    "ensure_secret_key",
    "ensure_auth_config",
    "load_auth_config",
    "save_auth_config",
    "normalize_lan_network",
    "load_network_config",
    "save_network_config",
    "get_build_info",
    "is_service_active",
    "is_service_enabled",
]
