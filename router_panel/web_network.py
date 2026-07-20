from __future__ import annotations

from typing import Any

from flask import flash, redirect, render_template, request, url_for

from .agent_client import AgentError, query_agent, submit_operation
from .core import normalize_mac_address


def register_network_routes(app, login_required, is_async_request) -> None:
    def wireless_status(
        include_wifi_networks: bool = False,
        ifname: str = "",
    ) -> dict[str, Any]:
        return query_agent(
            "wireless_status",
            {
                "include_wifi_networks": include_wifi_networks,
                "wifi_scan_device_override": ifname,
            },
        )

    def hotspot_status() -> dict[str, Any]:
        return query_agent("hotspot_status")

    def with_fragments(response: dict[str, Any], scope: str) -> dict[str, Any]:
        status = response.get("status", {})
        if scope == "hotspot":
            response["fragments"] = {
                "wireless": render_template("partials/hotspot_devices.html", status=status),
                "errors": render_template("partials/hotspot_errors.html", status=status),
            }
        else:
            response["fragments"] = {
                "wireless": render_template("partials/network_devices.html", status=status),
                "errors": render_template("partials/network_errors.html", status=status),
                "wifi_networks": render_template("partials/wifi_networks.html", status=status),
            }
        return response

    def failed(message: str, status: dict[str, Any], scope: str, redirect_endpoint: str):
        if is_async_request():
            return with_fragments(
                {"ok": False, "pending": False, "message": message, "status": status},
                scope,
            )
        flash(message, "error")
        return redirect(url_for(redirect_endpoint))

    def submit(
        action: str,
        params: dict[str, Any],
        *,
        scope: str,
        context: dict[str, Any],
        redirect_endpoint: str,
    ):
        try:
            operation = submit_operation(action, params, scope=scope, context=context)
        except AgentError as exc:
            try:
                status = hotspot_status() if scope == "hotspot" else wireless_status(
                    bool(context.get("include_wifi_networks")),
                    str(context.get("ifname", "")),
                )
            except AgentError:
                status = {"wireless_devices": [], "wifi_networks": [], "errors": [str(exc)]}
            return failed(str(exc), status, scope, redirect_endpoint)

        message = "操作已加入队列"
        if is_async_request():
            return {
                "ok": True,
                "pending": True,
                "message": message,
                "operation_id": operation["id"],
                "operation_url": url_for("operation_status", operation_id=operation["id"]),
            }, 202
        flash(message, "success")
        return redirect(url_for(redirect_endpoint))

    @app.route("/wifi")
    @login_required
    def wifi_page():
        try:
            status = wireless_status()
        except AgentError as exc:
            status = {
                "wireless_devices": [],
                "saved_wifi_networks": [],
                "saved_wifi_networks_unbound": [],
                "wifi_scan_device": "",
                "wifi_networks": [],
                "errors": [str(exc)],
            }
        return render_template("network.html", status=status)

    @app.route("/hotspot")
    @login_required
    def hotspot_page():
        try:
            status = hotspot_status()
        except AgentError as exc:
            status = {"wireless_devices": [], "hotspot": {}, "errors": [str(exc)]}
        return render_template("hotspot.html", status=status)

    @app.route("/hotspot/clients")
    @login_required
    def hotspot_clients_page():
        try:
            status = query_agent("hotspot_clients_status")
        except AgentError as exc:
            status = {"hotspots": [], "total_clients": 0, "errors": [str(exc)]}
        return render_template("clients.html", status=status)

    @app.route("/wifi/rescan", methods=["POST"])
    @login_required
    def wifi_rescan():
        ifname = request.form.get("ifname", "").strip()
        try:
            status = wireless_status()
        except AgentError as exc:
            return failed(str(exc), {"wireless_devices": [], "wifi_networks": [], "errors": [str(exc)]}, "network", "wifi_page")
        if not ifname:
            ifname = status.get("wifi_scan_device", "")
        if not ifname:
            return failed("没有可用的无线接口", status, "network", "wifi_page")
        return submit(
            "wifi_rescan",
            {"ifname": ifname},
            scope="network",
            context={"ifname": ifname, "include_wifi_networks": True},
            redirect_endpoint="wifi_page",
        )

    @app.route("/wifi/connect", methods=["POST"])
    @login_required
    def wifi_connect():
        ifname = request.form.get("ifname", "").strip()
        ssid = request.form.get("ssid", "").strip()
        password = request.form.get("password", "")
        bssid = request.form.get("bssid", "").strip()
        requested_mac = request.form.get("cloned_mac", "").strip()
        cloned_mac = normalize_mac_address(requested_mac)
        try:
            status = wireless_status(True, ifname)
        except AgentError as exc:
            return failed(str(exc), {"wireless_devices": [], "wifi_networks": [], "errors": [str(exc)]}, "network", "wifi_page")
        if not ifname:
            ifname = status.get("wifi_scan_device", "")
        if not ifname:
            return failed("没有可用的无线接口", status, "network", "wifi_page")
        if not ssid:
            return failed("请输入要连接的 Wi-Fi 名称", status, "network", "wifi_page")
        if requested_mac and not cloned_mac:
            response = failed("指定 MAC 必须是有效的单播地址", status, "network", "wifi_page")
            if isinstance(response, dict):
                response.update(
                    {
                        "connected": False,
                        "selected_ssid": ssid,
                        "selected_bssid": bssid,
                        "selected_cloned_mac": requested_mac,
                    }
                )
            return response
        return submit(
            "wifi_connect",
            {
                "ifname": ifname,
                "ssid": ssid,
                "password": password,
                "bssid": bssid,
                "cloned_mac": cloned_mac,
            },
            scope="network",
            context={"ifname": ifname, "include_wifi_networks": True},
            redirect_endpoint="wifi_page",
        )

    @app.route("/wifi/disconnect", methods=["POST"])
    @login_required
    def wifi_disconnect():
        ifname = request.form.get("ifname", "").strip()
        include = request.form.get("include_wifi_networks", "").strip() == "1"
        if not ifname:
            try:
                status = wireless_status(include)
            except AgentError as exc:
                status = {"wireless_devices": [], "wifi_networks": [], "errors": [str(exc)]}
            return failed("没有可用的无线接口", status, "network", "wifi_page")
        return submit(
            "wifi_disconnect",
            {"ifname": ifname},
            scope="network",
            context={"ifname": ifname, "include_wifi_networks": include},
            redirect_endpoint="wifi_page",
        )

    @app.route("/network/interfaces/manage", methods=["POST"])
    @login_required
    def network_interface_manage():
        ifname = request.form.get("ifname", "").strip()
        include = request.form.get("include_wifi_networks", "").strip() == "1"
        scope = "hotspot" if request.form.get("scope", "").strip() == "hotspot" else "network"
        redirect_endpoint = "hotspot_page" if scope == "hotspot" else "wifi_page"
        try:
            status = hotspot_status() if scope == "hotspot" else wireless_status(include)
        except AgentError as exc:
            return failed(str(exc), {"wireless_devices": [], "wifi_networks": [], "errors": [str(exc)]}, scope, redirect_endpoint)
        if not ifname:
            return failed("没有指定无线接口", status, scope, redirect_endpoint)
        device = next((item for item in status.get("wireless_devices", []) if item.get("device") == ifname), None)
        if not device:
            return failed(f"找不到无线接口 {ifname}", status, scope, redirect_endpoint)
        if device.get("state") != "unmanaged":
            if is_async_request():
                return with_fragments(
                    {"ok": True, "pending": False, "message": f"{ifname} 已由 NetworkManager 管理", "status": status},
                    scope,
                )
            flash(f"{ifname} 已由 NetworkManager 管理", "success")
            return redirect(url_for(redirect_endpoint))
        return submit(
            "interface_manage",
            {"ifname": ifname},
            scope=scope,
            context={"ifname": ifname, "include_wifi_networks": include},
            redirect_endpoint=redirect_endpoint,
        )

    @app.route("/wifi/forget", methods=["POST"])
    @login_required
    def wifi_forget():
        profile_uuid = request.form.get("uuid", "").strip()
        include = request.form.get("include_wifi_networks", "").strip() == "1"
        try:
            status = wireless_status(include)
        except AgentError as exc:
            return failed(str(exc), {"wireless_devices": [], "wifi_networks": [], "errors": [str(exc)]}, "network", "wifi_page")
        profile = next(
            (item for item in status.get("saved_wifi_networks", []) if item.get("uuid") == profile_uuid),
            None,
        )
        if not profile:
            return failed("找不到已保存网络", status, "network", "wifi_page")
        return submit(
            "wifi_forget",
            {"uuid": profile_uuid, "name": profile.get("name", "该网络")},
            scope="network",
            context={"ifname": "", "include_wifi_networks": include},
            redirect_endpoint="wifi_page",
        )

    @app.route("/hotspot/start", methods=["POST"])
    @login_required
    def hotspot_start():
        ifname = request.form.get("ifname", "").strip()
        ssid = request.form.get("ssid", "").strip()
        password = request.form.get("password", "")
        band = request.form.get("band", "").strip()
        channel = request.form.get("channel", "").strip()
        mode = request.form.get("mode", "").strip()
        try:
            status = hotspot_status()
        except AgentError as exc:
            return failed(str(exc), {"wireless_devices": [], "hotspot": {}, "errors": [str(exc)]}, "hotspot", "hotspot_page")
        if not ifname:
            return failed("没有可用的无线接口", status, "hotspot", "hotspot_page")
        if not ssid:
            return failed("请输入热点名称", status, "hotspot", "hotspot_page")
        if not 8 <= len(password) <= 63:
            return failed("热点密码长度必须在 8 到 63 个字符之间", status, "hotspot", "hotspot_page")
        device = next((item for item in status.get("wireless_devices", []) if item.get("device") == ifname), None)
        if not device:
            return failed(f"找不到无线接口 {ifname}", status, "hotspot", "hotspot_page")
        settings = device.get("hotspot", {}).get("frequency_settings", {})
        if not settings.get("available"):
            return failed(settings.get("reason") or f"{ifname} 当前不能开启热点", status, "hotspot", "hotspot_page")
        modes = {item.get("value") for item in settings.get("available_modes", [])}
        if not mode:
            mode = settings.get("selected_mode", "") or "exclusive"
        if mode not in modes:
            return failed("请选择受支持的热点模式", status, "hotspot", "hotspot_page")
        if settings.get("locked"):
            band = settings.get("selected_band", "")
            channel = settings.get("selected_channel", "")
        else:
            bands = {item.get("value"): item for item in settings.get("bands", [])}
            if not band:
                band = settings.get("selected_band", "")
            if band not in bands:
                return failed("请选择受支持的热点频段", status, "hotspot", "hotspot_page")
            channels = {item.get("value") for item in bands[band].get("channels", [])}
            if channel and channel not in channels:
                return failed("请选择受支持的热点信道", status, "hotspot", "hotspot_page")
        return submit(
            "hotspot_start",
            {
                "ifname": ifname,
                "ssid": ssid,
                "password": password,
                "band": band,
                "channel": channel,
                "mode": mode,
            },
            scope="hotspot",
            context={"ifname": ifname},
            redirect_endpoint="hotspot_page",
        )

    @app.route("/hotspot/stop", methods=["POST"])
    @login_required
    def hotspot_stop():
        ifname = request.form.get("ifname", "").strip()
        if not ifname:
            try:
                status = hotspot_status()
            except AgentError as exc:
                status = {"wireless_devices": [], "hotspot": {}, "errors": [str(exc)]}
            return failed("没有指定无线接口", status, "hotspot", "hotspot_page")
        return submit(
            "hotspot_stop",
            {"ifname": ifname},
            scope="hotspot",
            context={"ifname": ifname},
            redirect_endpoint="hotspot_page",
        )

    @app.route("/hotspot/keepalive/enable", methods=["POST"])
    @login_required
    def hotspot_keepalive_enable():
        ifname = request.form.get("ifname", "").strip()
        if not ifname:
            try:
                status = hotspot_status()
            except AgentError as exc:
                status = {"wireless_devices": [], "hotspot": {}, "errors": [str(exc)]}
            return failed("没有指定无线接口", status, "hotspot", "hotspot_page")
        return submit(
            "hotspot_keepalive_enable",
            {"ifname": ifname},
            scope="hotspot",
            context={"ifname": ifname},
            redirect_endpoint="hotspot_page",
        )

    @app.route("/hotspot/keepalive/disable", methods=["POST"])
    @login_required
    def hotspot_keepalive_disable():
        ifname = request.form.get("ifname", "").strip()
        if not ifname:
            try:
                status = hotspot_status()
            except AgentError as exc:
                status = {"wireless_devices": [], "hotspot": {}, "errors": [str(exc)]}
            return failed("没有指定无线接口", status, "hotspot", "hotspot_page")
        return submit(
            "hotspot_keepalive_disable",
            {"ifname": ifname},
            scope="hotspot",
            context={"ifname": ifname},
            redirect_endpoint="hotspot_page",
        )


__all__ = ["register_network_routes"]
