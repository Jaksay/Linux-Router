from __future__ import annotations

import json
import os
import socket
from typing import Any


AGENT_SOCKET_PATH = os.environ.get(
    "LINUX_ROUTER_AGENT_SOCKET",
    "/run/linux-router/agent.sock",
)


class AgentError(RuntimeError):
    pass


def _request(payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(AGENT_SOCKET_PATH)
            client.sendall(encoded)
            client.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except (OSError, TimeoutError) as exc:
        raise AgentError(f"系统控制服务不可用：{exc}") from exc

    try:
        response = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentError("系统控制服务返回了无效响应") from exc
    if not isinstance(response, dict):
        raise AgentError("系统控制服务返回了无效响应")
    if not response.get("ok", False):
        raise AgentError(str(response.get("error", "系统控制服务请求失败")))
    return response


def query_agent(name: str, params: dict[str, Any] | None = None) -> Any:
    return _request({"method": "query", "name": name, "params": params or {}}).get("result")


def submit_operation(
    action: str,
    params: dict[str, Any],
    *,
    scope: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = _request(
        {
            "method": "submit",
            "action": action,
            "params": params,
            "scope": scope,
            "context": context or {},
        }
    )
    return response["operation"]


def get_operation(operation_id: str) -> dict[str, Any]:
    response = _request(
        {"method": "operation", "operation_id": operation_id},
        timeout=5.0,
    )
    return response["operation"]


__all__ = [
    "AGENT_SOCKET_PATH",
    "AgentError",
    "query_agent",
    "submit_operation",
    "get_operation",
]
