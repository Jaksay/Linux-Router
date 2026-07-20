from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for

from .agent_client import AgentError, query_agent, submit_operation
from .service_monitor import (
    load_service_monitor_config,
    normalize_service_monitor_config,
    save_service_monitor_config,
)
from .tailscale import load_tailscale_config, normalize_tailscale_config, save_tailscale_config


def register_tools_routes(app, login_required, is_async_request) -> None:
    def tailscale_context() -> dict[str, object]:
        config = load_tailscale_config()
        try:
            status = query_agent("tailscale_status")
        except AgentError as exc:
            status = {
                "agent_available": False,
                "installed": False,
                "service_active": False,
                "logged_in": False,
                "errors": [str(exc)],
            }
        return {"config": config, "status": status}

    def service_monitor_context() -> dict[str, object]:
        config = load_service_monitor_config()
        try:
            status = query_agent("service_monitor_status")
        except AgentError as exc:
            status = {"services": [], "errors": [str(exc)]}
        return {"config": config, "status": status}

    def queued_response(operation, message):
        if is_async_request():
            return {
                "ok": True,
                "pending": True,
                "message": message,
                "operation_id": operation["id"],
                "operation_url": url_for("operation_status", operation_id=operation["id"]),
            }, 202
        flash(message, "success")
        return redirect(url_for("tools_page"))

    @app.route("/tools")
    @login_required
    def tools_page():
        return render_template(
            "tools.html",
            tailscale=tailscale_context(),
            service_monitor=service_monitor_context(),
        )

    @app.route("/tools/tailscale/status")
    @login_required
    def tailscale_status():
        return tailscale_context()["status"]

    @app.route("/tools/tailscale/login", methods=["POST"])
    @login_required
    def tailscale_login():
        config, error = normalize_tailscale_config(
            {
                "accept_routes": request.form.get("accept_routes", "") == "1",
                "advertise_routes": request.form.get("advertise_routes", ""),
            }
        )
        if error:
            if is_async_request():
                return {"ok": False, "pending": False, "message": error}, 400
            flash(error, "error")
            return redirect(url_for("tools_page"))
        save_tailscale_config(config)
        try:
            operation = submit_operation("tailscale_login", config, scope="tools")
        except AgentError as exc:
            if is_async_request():
                return {"ok": False, "pending": False, "message": str(exc)}, 503
            flash(str(exc), "error")
            return redirect(url_for("tools_page"))
        return queued_response(operation, "Tailscale 登录已加入队列")

    @app.route("/tools/tailscale/logout", methods=["POST"])
    @login_required
    def tailscale_logout():
        try:
            operation = submit_operation("tailscale_logout", {}, scope="tools")
        except AgentError as exc:
            if is_async_request():
                return {"ok": False, "pending": False, "message": str(exc)}, 503
            flash(str(exc), "error")
            return redirect(url_for("tools_page"))
        return queued_response(operation, "Tailscale 退出登录已加入队列")

    @app.route("/tools/services/save", methods=["POST"])
    @login_required
    def service_monitor_save():
        config, error = normalize_service_monitor_config(
            {"services": request.form.get("services", "")}
        )
        if error:
            if is_async_request():
                return {"ok": False, "pending": False, "message": error}, 400
            flash(error, "error")
            return redirect(url_for("tools_page"))
        save_service_monitor_config(config)
        if is_async_request():
            return {"ok": True, "pending": False, "message": "服务监控已保存"}
        flash("服务监控已保存", "success")
        return redirect(url_for("tools_page"))

    @app.route("/tools/services/action", methods=["POST"])
    @login_required
    def service_monitor_action():
        service = request.form.get("service", "")
        action = request.form.get("action", "")
        try:
            operation = submit_operation(
                "service_monitor_action",
                {"service": service, "action": action},
                scope="tools",
            )
        except AgentError as exc:
            if is_async_request():
                return {"ok": False, "pending": False, "message": str(exc)}, 503
            flash(str(exc), "error")
            return redirect(url_for("tools_page"))
        return queued_response(operation, "服务操作已加入队列")


__all__ = ["register_tools_routes"]
