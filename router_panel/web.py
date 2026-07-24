from __future__ import annotations

import secrets
from functools import wraps
from typing import Any

from flask import abort, redirect, render_template, request, session, url_for

from .agent_client import AgentError, get_operation, query_agent
from .core import PASSWORD_HINT_PATH, get_build_info
from .web_general import register_general_routes
from .web_network import register_network_routes
from .web_tools import register_tools_routes


def register_routes(app) -> None:
    def login_required(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped_view

    def get_csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    @app.before_request
    def validate_csrf_token() -> None:
        if request.method != "POST":
            return

        expected_token = session.get("csrf_token", "")
        submitted_token = request.form.get("csrf_token", "") or request.headers.get(
            "X-CSRF-Token", ""
        )
        if not expected_token or not submitted_token or not secrets.compare_digest(
            expected_token, submitted_token
        ):
            abort(400, description="CSRF token validation failed")

    def is_async_request() -> bool:
        return request.headers.get("X-Requested-With", "").lower() == "fetch"

    @app.route("/operations/<operation_id>")
    @login_required
    def operation_status(operation_id):
        try:
            operation = get_operation(operation_id)
        except AgentError as exc:
            return {"ok": False, "pending": False, "message": str(exc)}, 503

        if operation["status"] in {"queued", "running"}:
            return {
                "ok": True,
                "pending": True,
                "operation_id": operation["id"],
                "operation_status": operation["status"],
                "progress_message": operation.get("progress_message", ""),
            }

        response = dict(operation.get("result") or {})
        response.update(
            {
                "pending": False,
                "operation_id": operation["id"],
                "operation_status": operation["status"],
            }
        )
        scope = operation.get("scope")
        context = operation.get("context") or {}
        try:
            if scope == "network":
                status = query_agent(
                    "wireless_status",
                    {
                        "include_wifi_networks": bool(context.get("include_wifi_networks")),
                        "wifi_scan_device_override": str(context.get("ifname", "")),
                    },
                )
                response["status"] = status
                response["fragments"] = {
                    "wireless": render_template("partials/network_devices.html", status=status),
                    "errors": render_template("partials/network_errors.html", status=status),
                    "wifi_networks": render_template("partials/wifi_networks.html", status=status),
                }
            elif scope == "hotspot":
                status = query_agent("hotspot_status")
                response["status"] = status
                response["fragments"] = {
                    "wireless": render_template("partials/hotspot_devices.html", status=status),
                    "errors": render_template("partials/hotspot_errors.html", status=status),
                }
        except AgentError as exc:
            response["ok"] = False
            response["message"] = f"{response.get('message', '操作已结束')}，但无法刷新状态：{exc}"
        return response

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        return {
            "current_path": request.path,
            "initial_password_file": str(PASSWORD_HINT_PATH),
            "build_info": get_build_info(),
            "csrf_token": get_csrf_token(),
        }

    register_general_routes(app, login_required, is_async_request)
    register_network_routes(app, login_required, is_async_request)
    register_tools_routes(app, login_required, is_async_request)
