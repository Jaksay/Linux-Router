from __future__ import annotations

import grp
import copy
import json
import logging
import os
import queue
import re
import socketserver
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .core import (
    CommandResult,
    HOTSPOT_CONNECTION_NAME,
    command_exists,
    is_hotspot_virtual_interface,
    is_service_active,
    normalize_mac_address,
    request_system_reboot,
)
from .dependencies import gather_dependency_status, run_dependency_action
from .network import (
    gather_hotspot_clients_status,
    gather_hotspot_status,
    gather_wired_network_info,
    gather_wireless_network_status,
    get_device_status_item,
    get_hotspot_device_settings,
    get_hotspot_active_connection_for_parent,
    get_hotspot_profile,
    get_wifi_scan_unavailable_reason,
    hotspot_band_label,
    select_hotspot_auto_channel,
)
from .network_operations import (
    configure_hotspot_keepalive,
    connect_wifi_profile,
    delete_inactive_hotspot_profiles,
    disconnect_wifi,
    forget_wifi_profile,
    get_interface_permanent_mac,
    hotspot_keepalive_is_online,
    manage_networkmanager_interface,
    recover_hotspot_keepalive,
    rescan_wifi,
    start_hotspot_profile,
    stop_hotspot_profile,
)
from .hotspot_keepalive import (
    clear_hotspot_keepalive,
    load_hotspot_keepalive,
    save_hotspot_keepalive,
    set_hotspot_keepalive_runtime,
)
from .system import gather_system_info
from .service_monitor import gather_service_monitor_status, run_service_action
from .tailscale import (
    gather_tailscale_status,
    logout_tailscale,
    normalize_tailscale_config,
    start_tailscale_login,
)


SOCKET_PATH = Path(os.environ.get("LINUX_ROUTER_AGENT_SOCKET", "/run/linux-router/agent.sock"))
MAX_REQUEST_BYTES = 1024 * 1024
MAX_QUEUED_OPERATIONS = 32
MAX_OPERATION_HISTORY = 500
_IFNAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
_CHANNEL_RE = re.compile(r"^[0-9]{1,4}$")
ProgressCallback = Callable[[str], None]


class ValidationError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OperationRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._operations: dict[str, dict[str, Any]] = {}

    def create(self, action: str, scope: str, context: dict[str, Any]) -> dict[str, Any]:
        operation_id = str(uuid.uuid4())
        operation = {
            "id": operation_id,
            "action": action,
            "scope": scope,
            "context": copy.deepcopy(context),
            "status": "queued",
            "progress_message": "",
            "result": None,
            "created_at": _utc_now(),
            "started_at": None,
            "finished_at": None,
        }
        with self._lock:
            completed = [
                item
                for item in self._operations.values()
                if item["status"] in {"succeeded", "failed"}
            ]
            completed.sort(key=lambda item: item["finished_at"] or "", reverse=True)
            for item in completed[MAX_OPERATION_HISTORY - 1 :]:
                self._operations.pop(item["id"], None)
            self._operations[operation_id] = operation
        return self.get(operation_id)

    def mark_running(self, operation_id: str) -> None:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise ValidationError("找不到该操作")
            operation["status"] = "running"
            operation["started_at"] = _utc_now()

    def update_progress(self, operation_id: str, message: str) -> None:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise ValidationError("找不到该操作")
            if operation["status"] not in {"queued", "running"}:
                return
            operation["progress_message"] = message.strip()[:120]

    def finish(self, operation_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise ValidationError("找不到该操作")
            operation["status"] = "succeeded" if result.get("ok") else "failed"
            operation["result"] = copy.deepcopy(result)
            operation["progress_message"] = ""
            operation["finished_at"] = _utc_now()

    def get(self, operation_id: str) -> dict[str, Any]:
        try:
            normalized_id = str(uuid.UUID(operation_id))
        except ValueError as exc:
            raise ValidationError("无效的操作编号") from exc
        with self._lock:
            operation = self._operations.get(normalized_id)
            if operation is None:
                raise ValidationError("找不到该操作")
            return copy.deepcopy(operation)


def _require_string(
    params: dict[str, Any],
    name: str,
    *,
    maximum: int = 256,
    strip: bool = True,
) -> str:
    value = params.get(name, "")
    if not isinstance(value, str):
        raise ValidationError(f"{name} 参数无效")
    if len(value) > maximum:
        raise ValidationError(f"{name} 参数过长")
    return value.strip() if strip else value


def _require_wireless_interface(params: dict[str, Any]) -> str:
    ifname = _require_string(params, "ifname", maximum=15)
    if not _IFNAME_RE.fullmatch(ifname):
        raise ValidationError("无线接口名称无效")
    device = get_device_status_item(ifname)
    if (
        device.get("type") != "wifi"
        or device.get("device") != ifname
        or is_hotspot_virtual_interface(ifname)
    ):
        raise ValidationError(f"找不到无线接口 {ifname}")
    return ifname


def _validate_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 8:
        raise ValidationError("操作上下文无效")
    context: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 40:
            raise ValidationError("操作上下文无效")
        if not isinstance(item, (str, bool, int)) or isinstance(item, str) and len(item) > 256:
            raise ValidationError("操作上下文无效")
        context[key] = item
    return context


def _result(result: CommandResult, success_message: str, fallback_error: str) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "message": success_message if result.ok else (result.output or fallback_error),
    }


def _execute_wifi_rescan(params: dict[str, Any]) -> dict[str, Any]:
    ifname = _require_wireless_interface(params)
    unavailable_reason = get_wifi_scan_unavailable_reason(get_device_status_item(ifname))
    if unavailable_reason:
        raise ValidationError(unavailable_reason)
    return _result(rescan_wifi(ifname), f"已刷新 {ifname} 的 Wi-Fi 列表", "Wi-Fi 扫描失败")


def _execute_wifi_connect(params: dict[str, Any]) -> dict[str, Any]:
    ifname = _require_wireless_interface(params)
    _reject_protected_radio(ifname)
    unavailable_reason = get_wifi_scan_unavailable_reason(get_device_status_item(ifname))
    if unavailable_reason:
        raise ValidationError(unavailable_reason)
    ssid = _require_string(params, "ssid", maximum=32)
    password = _require_string(params, "password", maximum=128, strip=False)
    bssid_raw = _require_string(params, "bssid", maximum=17)
    cloned_raw = _require_string(params, "cloned_mac", maximum=17)
    if not ssid or len(ssid.encode("utf-8")) > 32:
        raise ValidationError("Wi-Fi 名称必须是 1 到 32 字节")
    bssid = normalize_mac_address(bssid_raw) if bssid_raw else ""
    cloned_mac = normalize_mac_address(cloned_raw) if cloned_raw else ""
    if bssid_raw and not bssid:
        raise ValidationError("BSSID 无效")
    if cloned_raw and not cloned_mac:
        raise ValidationError("指定 MAC 必须是有效的单播地址")

    action = connect_wifi_profile(ifname, ssid, password, bssid, cloned_mac)
    command_result = action["result"]
    final_state = action["current_state"].get("state", "")
    binding_error = action["binding_error"]
    if command_result.ok and binding_error:
        message = f"已连接到 {ssid}，但{binding_error}"
        ok = False
    elif command_result.ok and final_state == "connected":
        message = f"已连接到 {ssid}"
        ok = True
    elif command_result.ok and final_state == "connecting":
        message = f"正在连接 {ssid}"
        ok = True
    elif command_result.ok:
        message = command_result.output or f"连接 {ssid} 后当前仍未建立连接"
        ok = False
    else:
        message = command_result.output or f"连接 {ssid} 失败"
        if (
            action["hotspot_is_concurrent"]
            and action["concurrency_mode"] == "same_frequency"
            and "The Wi-Fi network could not be found" in message
        ):
            hotspot_profile = action["hotspot_profile"]
            hotspot_band = hotspot_band_label(hotspot_profile.get("band", ""))
            hotspot_channel = hotspot_profile.get("channel", "").strip() or "当前信道"
            message = (
                f"连接 {ssid} 失败：当前并发热点固定在 {hotspot_band} 信道 {hotspot_channel}。"
                "这张网卡仅支持同频同信道的 AP+STA 并发；要连接其他信道的 Wi-Fi，请先关闭热点，"
                "连上 Wi-Fi 后再重新开启并发热点"
            )
        if "802-11-wireless-security.key-mgmt: property is missing" in message:
            message = (
                f"连接 {ssid} 失败，已尝试清理旧配置并重试"
                if password
                else f"{ssid} 的已保存 Wi-Fi 配置不完整，请重新输入密码后再试"
            )
        ok = False
    return {
        "ok": ok,
        "connected": final_state == "connected" and not binding_error,
        "message": message,
        "selected_ssid": ssid,
        "selected_bssid": bssid,
        "selected_cloned_mac": cloned_mac,
    }


def _execute_wifi_disconnect(params: dict[str, Any]) -> dict[str, Any]:
    ifname = _require_wireless_interface(params)
    _reject_protected_radio(ifname)
    return _result(disconnect_wifi(ifname), f"已断开 {ifname} 的 Wi-Fi 连接", f"断开 {ifname} 失败")


def _execute_interface_manage(params: dict[str, Any]) -> dict[str, Any]:
    ifname = _require_wireless_interface(params)
    results = manage_networkmanager_interface(ifname)
    failed = next((item for item in results if not item.ok), None)
    if failed:
        return {"ok": False, "message": failed.output or f"{ifname} 接管失败"}
    return {"ok": True, "message": f"已将 {ifname} 交给 NetworkManager 接管"}


def _execute_wifi_forget(params: dict[str, Any]) -> dict[str, Any]:
    profile_uuid = _require_string(params, "uuid", maximum=36)
    try:
        profile_uuid = str(uuid.UUID(profile_uuid))
    except ValueError as exc:
        raise ValidationError("Wi-Fi 配置编号无效") from exc
    name = _require_string(params, "name", maximum=128) or "该网络"
    return _result(forget_wifi_profile(profile_uuid), f"已忘记 {name}", f"忘记 {name} 失败")


def _execute_hotspot_start(
    params: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    if progress:
        progress("正在检查热点参数")
    ifname = _require_string(params, "ifname", maximum=15)
    if not _IFNAME_RE.fullmatch(ifname):
        raise ValidationError("无线接口名称无效")
    if load_hotspot_keepalive():
        raise ValidationError("已有 AP 设为保活，请先取消保活")
    ssid = _require_string(params, "ssid", maximum=32)
    password = _require_string(params, "password", maximum=63, strip=False)
    band = _require_string(params, "band", maximum=8)
    channel = _require_string(params, "channel", maximum=4)
    mode = _require_string(params, "mode", maximum=16)
    if not ssid or len(ssid.encode("utf-8")) > 32:
        raise ValidationError("热点名称必须是 1 到 32 字节")
    if not 8 <= len(password) <= 63:
        raise ValidationError("热点密码长度必须在 8 到 63 个字符之间")
    if channel and not _CHANNEL_RE.fullmatch(channel):
        raise ValidationError("热点信道无效")

    device = get_hotspot_device_settings(ifname)
    if not device:
        raise ValidationError(f"找不到无线接口 {ifname}")
    settings = device.get("frequency_settings", {})
    if not settings.get("available"):
        raise ValidationError(settings.get("reason") or f"{ifname} 当前不能开启热点")
    modes = {item.get("value") for item in settings.get("available_modes", [])}
    if mode not in modes:
        raise ValidationError("请选择受支持的热点模式")
    if settings.get("locked"):
        band = settings.get("selected_band", "")
        channel = settings.get("selected_channel", "")
    else:
        bands = {item.get("value"): item for item in settings.get("bands", [])}
        if band not in bands:
            raise ValidationError("请选择受支持的热点频段")
        selected_band_entry = bands[band]
        channels = {item.get("value") for item in selected_band_entry.get("channels", [])}
        if channel and channel not in channels:
            raise ValidationError("请选择受支持的热点信道")
        if not channel:
            channel = select_hotspot_auto_channel(selected_band_entry)
            if not channel:
                raise ValidationError("当前频段没有可自动选择的非 DFS 信道，请手动指定信道")
    if is_service_active("hostapd"):
        raise ValidationError("检测到 hostapd/RaspAP 正在占用无线网卡，请先停用后再开启热点")
    if not command_exists("dnsmasq"):
        raise ValidationError("系统缺少 dnsmasq，请先安装 dnsmasq-base")
    if progress:
        progress("正在清理旧热点配置")
    cleanup_error = delete_inactive_hotspot_profiles()
    if cleanup_error:
        raise ValidationError(cleanup_error)

    result = start_hotspot_profile(
        ifname,
        device.get("phy_name", ""),
        ssid,
        password,
        band,
        channel,
        mode,
        progress=progress,
    )
    success = f"已开启并发热点：{ssid}" if mode == "concurrent" else f"已开启热点：{ssid}"
    return _result(result, success, "开启热点失败")


def _execute_hotspot_stop(params: dict[str, Any]) -> dict[str, Any]:
    ifname = _require_wireless_interface(params)
    _reject_protected_radio(ifname)
    return _result(stop_hotspot_profile(ifname), "已关闭热点", "关闭热点失败")


def _protected_radio_matches(ifname: str) -> bool:
    config = load_hotspot_keepalive()
    if not config:
        return False
    if ifname == config.get("parent_ifname"):
        return True
    return get_interface_permanent_mac(ifname) == config.get("parent_mac")


def _reject_protected_radio(ifname: str) -> None:
    if _protected_radio_matches(ifname):
        raise ValidationError("该 AP 已设为保活，请先取消保活")


def _execute_hotspot_keepalive_enable(params: dict[str, Any]) -> dict[str, Any]:
    ifname = _require_wireless_interface(params)
    current = load_hotspot_keepalive()
    if current:
        if _protected_radio_matches(ifname):
            return {"ok": True, "message": "该 AP 已处于保活状态"}
        raise ValidationError("已有 AP 设为保活，请先取消保活")
    active = get_hotspot_active_connection_for_parent(ifname)
    if active.get("name") != HOTSPOT_CONNECTION_NAME:
        raise ValidationError("只能将当前在线的 AP 设为保活")
    parent_mac = get_interface_permanent_mac(ifname)
    if not parent_mac:
        raise ValidationError(f"无法读取 {ifname} 的永久 MAC 地址")
    phy_name = get_hotspot_device_settings(ifname).get("phy_name", "")
    configured = configure_hotspot_keepalive(True)
    if not configured.ok:
        return _result(configured, "已设为保活", "设置 AP 保活失败")
    try:
        save_hotspot_keepalive(ifname, parent_mac, phy_name)
    except OSError as exc:
        configure_hotspot_keepalive(False)
        return {"ok": False, "message": f"保存 AP 保活配置失败：{exc}"}
    return {"ok": True, "message": "已将该 AP 设为保活"}


def _execute_hotspot_keepalive_disable(params: dict[str, Any]) -> dict[str, Any]:
    ifname = _require_wireless_interface(params)
    if not load_hotspot_keepalive():
        return {"ok": True, "message": "AP 保活已取消"}
    if not _protected_radio_matches(ifname):
        raise ValidationError("该网卡不是当前保活 AP")
    clear_hotspot_keepalive()
    configured = configure_hotspot_keepalive(False)
    return _result(configured, "已取消 AP 保活", "已取消保活，但恢复普通优先级失败")


def _execute_hotspot_keepalive_recover(params: dict[str, Any]) -> dict[str, Any]:
    config = load_hotspot_keepalive()
    if not config or config.get("parent_mac") != params.get("parent_mac"):
        return {"ok": True, "message": "AP 保活已取消"}
    return _result(recover_hotspot_keepalive(config), "保活 AP 已恢复", "保活 AP 恢复失败")


def _execute_dependency(params: dict[str, Any]) -> dict[str, Any]:
    action = _require_string(params, "action", maximum=64)
    return _result(run_dependency_action(action), "处置完成", "处置失败")


def _execute_reboot(params: dict[str, Any]) -> dict[str, Any]:
    return _result(request_system_reboot(), "设备正在重启", "设备重启失败")


def _execute_tailscale_login(params: dict[str, Any]) -> dict[str, Any]:
    config, error = normalize_tailscale_config(params)
    if error:
        raise ValidationError(error)
    return start_tailscale_login(config)


def _execute_tailscale_logout(params: dict[str, Any]) -> dict[str, Any]:
    return _result(logout_tailscale(), "已退出 Tailscale 登录", "退出 Tailscale 登录失败")


def _execute_service_monitor_action(params: dict[str, Any]) -> dict[str, Any]:
    service = _require_string(params, "service", maximum=128)
    action = _require_string(params, "action", maximum=16)
    labels = {
        "start": "服务已启动",
        "stop": "服务已停止",
        "restart": "服务已重启",
    }
    return _result(run_service_action(service, action), labels.get(action, "服务操作已完成"), "服务操作失败")


OPERATIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "wifi_rescan": _execute_wifi_rescan,
    "wifi_connect": _execute_wifi_connect,
    "wifi_disconnect": _execute_wifi_disconnect,
    "interface_manage": _execute_interface_manage,
    "wifi_forget": _execute_wifi_forget,
    "hotspot_start": _execute_hotspot_start,
    "hotspot_stop": _execute_hotspot_stop,
    "hotspot_keepalive_enable": _execute_hotspot_keepalive_enable,
    "hotspot_keepalive_disable": _execute_hotspot_keepalive_disable,
    "dependency_fix": _execute_dependency,
    "system_reboot": _execute_reboot,
    "tailscale_login": _execute_tailscale_login,
    "tailscale_logout": _execute_tailscale_logout,
    "service_monitor_action": _execute_service_monitor_action,
}

INTERNAL_OPERATIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "hotspot_keepalive_recover": _execute_hotspot_keepalive_recover,
}


def execute_query(name: str, params: dict[str, Any]) -> Any:
    if name == "health":
        return {"status": "ok"}
    if name == "system_info":
        return gather_system_info()
    if name == "dependency_status":
        return gather_dependency_status()
    if name == "wired_status":
        return gather_wired_network_info()
    if name == "wireless_status":
        include = params.get("include_wifi_networks", False)
        override = params.get("wifi_scan_device_override", "")
        if not isinstance(include, bool) or not isinstance(override, str) or len(override) > 15:
            raise ValidationError("无线状态参数无效")
        return gather_wireless_network_status(include, override)
    if name == "hotspot_status":
        return gather_hotspot_status()
    if name == "hotspot_clients_status":
        return gather_hotspot_clients_status()
    if name == "hotspot_profile":
        return get_hotspot_profile()
    if name == "tailscale_status":
        return gather_tailscale_status()
    if name == "service_monitor_status":
        return gather_service_monitor_status()
    raise ValidationError("不支持的查询")


class AgentRuntime:
    def __init__(
        self,
        registry: OperationRegistry | None = None,
        *,
        monitor_interval: float = 12.0,
        monitor_initial_delay: float = 3.0,
    ) -> None:
        self.store = registry or OperationRegistry()
        self.queue: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue(MAX_QUEUED_OPERATIONS)
        self.submit_lock = threading.Lock()
        self._monitor_interval = monitor_interval
        self._monitor_initial_delay = monitor_initial_delay
        self._monitor_stop = threading.Event()
        self._recovery_pending = False
        self._recovery_attempt = 0
        self._next_recovery_at = 0.0
        self.worker = threading.Thread(target=self._run_worker, name="router-agent-worker", daemon=True)
        self.worker.start()
        self.monitor = threading.Thread(target=self._run_keepalive_monitor, name="router-agent-ap-keepalive", daemon=True)
        self.monitor.start()

    def submit(
        self,
        action: str,
        params: dict[str, Any],
        scope: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if action not in OPERATIONS:
            raise ValidationError("不支持的系统操作")
        if not isinstance(params, dict) or len(params) > 16:
            raise ValidationError("操作参数无效")
        if scope not in {"network", "hotspot", "dependencies", "system", "tools"}:
            raise ValidationError("操作范围无效")
        with self.submit_lock:
            if self.queue.full():
                raise ValidationError("系统操作队列已满，请稍后再试")
            operation = self.store.create(action, scope, _validate_context(context))
            self.queue.put_nowait((operation["id"], action, params.copy()))
        return operation

    def _queue_keepalive_recovery(self, config: dict[str, Any]) -> bool:
        with self.submit_lock:
            if self._recovery_pending or self.queue.full():
                return False
            operation = self.store.create(
                "hotspot_keepalive_recover",
                "hotspot",
                {"ifname": config.get("parent_ifname", ""), "automatic": True},
            )
            self._recovery_pending = True
            set_hotspot_keepalive_runtime(recovering=True, last_error="")
            self.queue.put_nowait(
                (operation["id"], "hotspot_keepalive_recover", {"parent_mac": config["parent_mac"]})
            )
        return True

    def _run_keepalive_monitor(self) -> None:
        if self._monitor_stop.wait(self._monitor_initial_delay):
            return
        while not self._monitor_stop.is_set():
            try:
                config = load_hotspot_keepalive()
                if not config:
                    self._recovery_attempt = 0
                    self._next_recovery_at = 0.0
                    set_hotspot_keepalive_runtime(recovering=False, last_error="")
                elif hotspot_keepalive_is_online(config):
                    self._recovery_attempt = 0
                    self._next_recovery_at = 0.0
                    if not self._recovery_pending:
                        set_hotspot_keepalive_runtime(recovering=False, last_error="")
                elif time.monotonic() >= self._next_recovery_at:
                    self._queue_keepalive_recovery(config)
            except Exception:
                logging.exception("AP keepalive monitor failed")
            self._monitor_stop.wait(self._monitor_interval)

    def _execute_operation(
        self,
        operation_id: str,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if action == "hotspot_start":
            def progress(message: str) -> None:
                self.store.update_progress(operation_id, message)

            return _execute_hotspot_start(params, progress)

        operation = OPERATIONS.get(action) or INTERNAL_OPERATIONS[action]
        return operation(params)

    def _run_worker(self) -> None:
        while True:
            operation_id, action, params = self.queue.get()
            try:
                self.store.mark_running(operation_id)
                logging.info("operation started id=%s action=%s", operation_id, action)
                try:
                    result = self._execute_operation(operation_id, action, params)
                except ValidationError as exc:
                    result = {"ok": False, "message": str(exc)}
                except Exception as exc:
                    result = {"ok": False, "message": f"系统操作异常：{exc}"}
                self.store.finish(operation_id, result)
                logging.info(
                    "operation finished id=%s action=%s ok=%s",
                    operation_id,
                    action,
                    bool(result.get("ok")),
                )
                if action == "hotspot_keepalive_recover":
                    self._recovery_pending = False
                    if result.get("ok"):
                        self._recovery_attempt = 0
                        self._next_recovery_at = 0.0
                        set_hotspot_keepalive_runtime(recovering=False, last_error="")
                    else:
                        delays = (5, 15, 30, 60)
                        delay = delays[min(self._recovery_attempt, len(delays) - 1)]
                        self._recovery_attempt += 1
                        self._next_recovery_at = time.monotonic() + delay
                        set_hotspot_keepalive_runtime(
                            recovering=False,
                            last_error=str(result.get("message", "保活 AP 恢复失败")),
                        )
            except Exception:
                logging.exception("operation state update failed id=%s action=%s", operation_id, action)
            finally:
                self.queue.task_done()


class AgentRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            self._respond({"ok": False, "error": "请求过大"})
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValidationError("请求格式无效")
            method = payload.get("method")
            runtime: AgentRuntime = self.server.runtime  # type: ignore[attr-defined]
            if method == "query":
                name = payload.get("name", "")
                params = payload.get("params", {})
                if not isinstance(name, str) or not isinstance(params, dict):
                    raise ValidationError("查询参数无效")
                response = {"ok": True, "result": execute_query(name, params)}
            elif method == "submit":
                response = {
                    "ok": True,
                    "operation": runtime.submit(
                        payload.get("action", ""),
                        payload.get("params", {}),
                        payload.get("scope", ""),
                        payload.get("context", {}),
                    ),
                }
            elif method == "operation":
                operation_id = payload.get("operation_id", "")
                if not isinstance(operation_id, str):
                    raise ValidationError("无效的操作编号")
                response = {"ok": True, "operation": runtime.store.get(operation_id)}
            else:
                raise ValidationError("不支持的请求")
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = {"ok": False, "error": "请求格式无效"}
        except ValidationError as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception as exc:
            response = {"ok": False, "error": f"系统控制服务异常：{exc}"}
        self._respond(response)

    def _respond(self, response: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))


class AgentServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: Path, runtime: AgentRuntime) -> None:
        self.runtime = runtime
        super().__init__(str(socket_path), AgentRequestHandler)


def run_agent() -> None:
    if os.geteuid() != 0:
        raise SystemExit("router-panel-agent must run as root")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists() or SOCKET_PATH.is_socket():
        SOCKET_PATH.unlink()
    runtime = AgentRuntime()
    server = AgentServer(SOCKET_PATH, runtime)
    os.chmod(SOCKET_PATH, 0o660)
    group_name = os.environ.get("LINUX_ROUTER_AGENT_GROUP", "router-panel")
    try:
        os.chown(SOCKET_PATH, 0, grp.getgrnam(group_name).gr_gid)
    except KeyError:
        pass
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        SOCKET_PATH.unlink(missing_ok=True)


__all__ = [
    "AgentRuntime",
    "OperationRegistry",
    "ValidationError",
    "execute_query",
    "run_agent",
]
