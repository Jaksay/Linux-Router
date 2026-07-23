import atexit
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


TEST_DATA_DIRECTORY = tempfile.TemporaryDirectory(prefix="router-panel-tests-")
atexit.register(TEST_DATA_DIRECTORY.cleanup)
os.environ["LINUX_ROUTER_DATA_DIR"] = TEST_DATA_DIRECTORY.name

import app as application
from router_panel import core
from router_panel import contracts
from router_panel.core import CommandResult
from router_panel import dependencies
from router_panel import network
from router_panel import network_operations
from router_panel import network_parsers
from router_panel import web_network
from router_panel import agent_server
from router_panel import hotspot_keepalive
from router_panel import service_monitor
from router_panel import tailscale
from router_panel import web
from router_panel import system
from router_panel import web_tools


EXPECTED_ROUTES = {
    "/",
    "/dependencies",
    "/dependencies/fix",
    "/healthz",
    "/hotspot",
    "/hotspot/clients",
    "/hotspot/keepalive/disable",
    "/hotspot/keepalive/enable",
    "/hotspot/start",
    "/hotspot/stop",
    "/login",
    "/logout",
    "/network/interfaces/manage",
    "/operations/<operation_id>",
    "/settings",
    "/settings/lan",
    "/settings/password",
    "/settings/reboot",
    "/static/<path:filename>",
    "/system",
    "/tools",
    "/tools/services/action",
    "/tools/services/save",
    "/tools/tailscale/login",
    "/tools/tailscale/logout",
    "/tools/tailscale/status",
    "/wifi",
    "/wifi/connect",
    "/wifi/disconnect",
    "/wifi/forget",
    "/wifi/rescan",
    "/wired",
}

FIXTURES = Path(__file__).parent / "fixtures"
INSTALL_SCRIPT = Path(__file__).parent.parent / "install.sh"
AGENT_SERVICE_UNIT = Path(__file__).parent.parent / "router-panel-agent.service"


class ApplicationStructureTests(unittest.TestCase):
    def test_installer_writes_completion_marker_after_state_backups(self):
        installer = INSTALL_SCRIPT.read_text(encoding="utf-8")
        backup_loop = installer.index('for name in networkmanager.conf netplan.yaml sysctl.conf; do')
        completion_marker = installer.index("printf 'complete=1\\n'")
        self.assertGreater(completion_marker, backup_loop)

    def test_agent_service_restarts_as_keepalive_supervisor(self):
        unit = AGENT_SERVICE_UNIT.read_text(encoding="utf-8")
        self.assertIn("StartLimitIntervalSec=0", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_installer_exposes_uninstall_modes(self):
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("install", result.stdout)
        self.assertIn("upgrade", result.stdout)
        self.assertIn("uninstall", result.stdout)
        self.assertIn("--purge-data", result.stdout)

        missing_command = subprocess.run(
            ["bash", str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(missing_command.returncode, 0)
        self.assertIn("specify one command", missing_command.stderr)

        missing_installation = subprocess.run(
            [
                "bash",
                str(INSTALL_SCRIPT),
                "upgrade",
                "--install-dir",
                "/tmp/router-panel-missing-install",
                "--data-dir",
                "/tmp/router-panel-missing-data",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(missing_installation.returncode, 0)
        self.assertIn("no installation found", missing_installation.stderr)

    def test_installer_rejects_unsafe_uninstall_paths(self):
        traversal = subprocess.run(
            [
                "bash",
                str(INSTALL_SCRIPT),
                "uninstall",
                "--install-dir",
                "/home/../etc",
                "--data-dir",
                "/tmp/router-panel-test-data",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(traversal.returncode, 0)
        self.assertIn("must not contain", traversal.stderr)

        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "install-link"
            link.symlink_to("/etc")
            symlink = subprocess.run(
                [
                    "bash",
                    str(INSTALL_SCRIPT),
                    "uninstall",
                    "--install-dir",
                    str(link),
                    "--data-dir",
                    str(Path(directory) / "data"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(symlink.returncode, 0)
        self.assertIn("symbolic link", symlink.stderr)

    def test_route_contract_is_preserved(self):
        routes = {rule.rule for rule in application.app.url_map.iter_rules()}
        self.assertEqual(routes, EXPECTED_ROUTES)

    def test_templates_load(self):
        for template_name in application.app.jinja_env.list_templates():
            application.app.jinja_env.get_template(template_name)

    def test_settings_page_includes_build_footer(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"

        with tempfile.TemporaryDirectory() as directory:
            build_info_path = Path(directory) / "BUILD_INFO"
            build_info_path.write_text("branch=dev\nbuild=67c4ce5\n", encoding="utf-8")
            with patch.object(core, "BUILD_INFO_PATH", build_info_path):
                response = client.get("/settings")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Branch dev", response.data)
        self.assertIn(b"Build 67c4ce5", response.data)
        self.assertIn(b"https://github.com/Jaksay/Linux-Router", response.data)

    def test_csrf_rejects_missing_token_and_accepts_valid_token(self):
        client = application.app.test_client()
        page = client.get("/login")
        token_match = re.search(
            rb'name="csrf_token" value="([^"]+)"',
            page.data,
        )
        self.assertIsNotNone(token_match)

        self.assertEqual(client.post("/login", data={"password": "wrong"}).status_code, 400)
        response = client.post(
            "/login",
            data={
                "csrf_token": token_match.group(1).decode(),
                "password": "definitely-wrong",
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_nmcli_and_bitrate_parsers(self):
        self.assertEqual(
            network_parsers.split_escaped(r"one:two\:three"),
            ["one", "two:three"],
        )
        self.assertEqual(
            network_parsers.split_station_bitrate(
                "173.3 MBit/s VHT-MCS 8 short GI VHT-NSS 2"
            ),
            ("173.3 MBit/s", "VHT-MCS 8 short GI VHT-NSS 2"),
        )

    def test_command_result_preserves_process_details(self):
        completed = SimpleNamespace(returncode=7, stdout="partial output\n", stderr="failed\n")
        with patch.object(core.subprocess, "run", return_value=completed):
            result = core.run_command(["example", "--flag"])

        self.assertFalse(result.ok)
        self.assertEqual(result.output, "partial output")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "partial output")
        self.assertEqual(result.stderr, "failed")
        self.assertEqual(result.command, ("example", "--flag"))
        self.assertFalse(result.timed_out)

    def test_atomic_write_replaces_content_and_preserves_requested_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            core.atomic_write_text(path, "first", mode=0o600)
            core.atomic_write_text(path, "second", mode=0o600)

            self.assertEqual(path.read_text(), "second")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_atomic_write_failure_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            core.atomic_write_text(path, "original", mode=0o600)
            with patch.object(core.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    core.atomic_write_text(path, "replacement", mode=0o600)

            self.assertEqual(path.read_text(), "original")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_network_operations_rely_on_agent_single_writer(self):
        expected = CommandResult(True, "scanned")
        with patch.object(network_operations, "_rescan_wifi", return_value=expected) as rescan:
            result = network_operations.rescan_wifi("wlan0")
        self.assertIs(result, expected)
        rescan.assert_called_once_with("wlan0")

    def test_hotspot_validation_uses_single_device_query(self):
        device = {
            "device": "wlan0",
            "phy_name": "phy0",
            "frequency_settings": {
                "available": True,
                "available_modes": [{"value": "exclusive"}],
                "locked": False,
                "bands": [{"value": "bg", "channels": [{"value": "6"}]}],
            },
        }
        with (
            patch.object(agent_server, "get_hotspot_device_settings", return_value=device) as settings,
            patch.object(agent_server, "gather_hotspot_status") as full_status,
            patch.object(agent_server, "is_service_active", return_value=False),
            patch.object(agent_server, "command_exists", return_value=True),
            patch.object(agent_server, "delete_inactive_hotspot_profiles", return_value=None),
            patch.object(
                agent_server,
                "start_hotspot_profile",
                return_value=CommandResult(True, "started"),
            ),
        ):
            result = agent_server._execute_hotspot_start(
                {
                    "ifname": "wlan0",
                    "ssid": "Hotspot",
                    "password": "secret123",
                    "band": "bg",
                    "channel": "6",
                    "mode": "exclusive",
                }
            )
        self.assertTrue(result["ok"])
        settings.assert_called_once_with("wlan0")
        full_status.assert_not_called()

    def test_operation_polling_has_deadline(self):
        script = (Path(__file__).parent.parent / "static" / "router-common.js").read_text()
        self.assertIn("operationPollTimeoutMs", script)
        self.assertIn("Date.now() >= deadline", script)

    def test_common_operation_forms_read_action_attribute(self):
        script = (Path(__file__).parent.parent / "static" / "router-common.js").read_text()
        self.assertIn('form.getAttribute("action")', script)
        self.assertNotIn("postForm(form.action", script)

    def test_mobile_navigation_breakpoints_stay_in_sync(self):
        responsive = (Path(__file__).parent.parent / "static" / "responsive.css").read_text()
        base = (Path(__file__).parent.parent / "templates" / "base.html").read_text()
        self.assertIn("@media (max-width: 900px)", responsive)
        self.assertIn('matchMedia("(max-width: 900px)")', base)

    def test_disruptive_device_actions_require_confirmation(self):
        templates = Path(__file__).parent.parent / "templates"
        network_template = (templates / "network.html").read_text()
        hotspot_template = (templates / "hotspot.html").read_text()
        settings_template = (templates / "settings.html").read_text()
        self.assertIn("确定要断开", network_template)
        self.assertIn("确定要关闭", hotspot_template)
        self.assertIn("确定要重启设备", settings_template)

    def test_cross_module_contracts_keep_required_fields(self):
        self.assertTrue(
            {"wireless_devices", "wifi_scan_device", "wifi_networks", "errors"}
            <= contracts.WirelessStatus.__required_keys__
        )
        self.assertTrue(
            {"result", "binding_error", "current_state"}
            <= contracts.WifiConnectResult.__required_keys__
        )

    def test_network_overview_status_uses_active_connections(self):
        status = system.summarize_network_status(
            [
                {"name": "lan", "type": "802-3-ethernet"},
                {"name": "Home Wi-Fi", "type": "802-11-wireless"},
            ]
        )
        self.assertEqual(
            status,
            {"wired": True, "wireless": True, "hotspot": False},
        )

        hotspot_only = system.summarize_network_status(
            [{"name": core.HOTSPOT_CONNECTION_NAME, "type": "802-11-wireless"}]
        )
        self.assertEqual(
            hotspot_only,
            {"wired": False, "wireless": False, "hotspot": True},
        )

    def test_nmcli_fixture_preserves_escaped_connection_name(self):
        rows = network_parsers.parse_nmcli_lines(
            (FIXTURES / "nmcli_devices.txt").read_text(),
            ["device", "type", "state", "connection"],
        )
        self.assertEqual(rows[0]["connection"], "Home:Lab")
        self.assertEqual(rows[1]["connection"], "")

    def test_iw_phy_fixture_detects_same_channel_ap_sta(self):
        lines = (FIXTURES / "iw_phy.txt").read_text().splitlines()
        concurrency = network_parsers.parse_iw_valid_interface_combinations(lines)
        channels = [
            network_parsers.parse_iw_frequency_line(line)
            for line in lines
            if "MHz [" in line
        ]
        self.assertEqual(concurrency["ap_sta_mode"], "same_frequency")
        self.assertTrue(concurrency["same_channel_only"])
        self.assertEqual([channel["channel"] for channel in channels], ["1", "11", "149"])

    def test_station_fixture_preserves_rate_details(self):
        stations = network_parsers.parse_iw_station_dump(
            (FIXTURES / "iw_station_dump.txt").read_text()
        )
        self.assertEqual(len(stations), 1)
        station = stations[0]
        self.assertEqual(station["mac_address"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(station["uplink_rate"], "173.3 MBit/s")
        self.assertEqual(station["uplink_rate_detail"], "VHT-MCS 8 short GI VHT-NSS 2")
        self.assertEqual(station["connected_time"], "1 小时 1 分钟")

    def test_wifi_client_link_exposes_ipv4_without_prefix(self):
        with (
            patch.object(
                network,
                "get_current_wifi_link",
                return_value={"ssid": "Test", "bssid": "00:11:22:33:44:55"},
            ),
            patch.object(
                network,
                "get_device_details",
                return_value={"ipv4": ["192.168.3.204/24"]},
            ),
            patch.object(network, "read_text", return_value="AA:BB:CC:DD:EE:FF"),
        ):
            link = network.get_wifi_client_link(
                {"device": "wlan0", "state": "connected", "connection": "Test"}
            )

        self.assertEqual(link["ip_address"], "192.168.3.204")

    def test_hotspot_device_details_are_loaded_once_per_interface(self):
        cache = {}
        details = {"ipv4": ["192.168.50.1/24"]}
        with patch.object(network, "get_device_details", return_value=details) as load:
            first = network._get_cached_device_details(cache, "ap-wlan0")
            second = network._get_cached_device_details(cache, "ap-wlan0")

        self.assertIs(first, details)
        self.assertIs(second, details)
        load.assert_called_once_with("ap-wlan0")

    def test_wifi_connect_operation_reaches_connected_state(self):
        with (
            patch.object(network_operations, "get_wireless_interface_phy_map", return_value={"wlan0": "phy0"}),
            patch.object(
                network_operations,
                "get_wireless_phy_capabilities",
                return_value={"phy0": {"concurrency": {"ap_sta_mode": "same_frequency"}}},
            ),
            patch.object(network_operations, "get_hotspot_active_connection_for_parent", return_value={}),
            patch.object(network_operations, "run_command", return_value=CommandResult(True, "connected")),
            patch.object(network_operations, "get_active_wifi_connection", return_value={"uuid": "profile-id"}),
            patch.object(network_operations, "bind_wifi_profile_to_hardware", return_value=None),
            patch.object(network_operations, "get_device_status_item", return_value={"state": "connected"}),
            patch.object(network_operations, "get_hotspot_profile", return_value={}),
        ):
            result = network_operations.connect_wifi_profile("wlan0", "Test", "secret123", "", "")

        self.assertTrue(result["result"].ok)
        self.assertEqual(result["current_state"], {"state": "connected"})
        self.assertIsNone(result["binding_error"])

    def test_async_network_action_returns_operation_id(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"
            session["csrf_token"] = "test-token"

        status = {
            "wireless_devices": [],
            "wifi_networks": [],
            "errors": [],
            "wifi_scan_device": "wlan0",
        }
        with (
            patch.object(web_network, "query_agent", return_value=status),
            patch.object(
                web_network,
                "submit_operation",
                return_value={"id": "11111111-1111-4111-8111-111111111111"},
            ),
        ):
            response = client.post(
                "/wifi/rescan",
                data={"csrf_token": "test-token", "ifname": "wlan0"},
                headers={"X-Requested-With": "fetch"},
            )

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["pending"])
        self.assertEqual(payload["operation_id"], "11111111-1111-4111-8111-111111111111")
        self.assertIn("/operations/", payload["operation_url"])

    def test_completed_operation_uses_server_rendered_fragments(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"

        operation_id = "11111111-1111-4111-8111-111111111111"
        operation = {
            "id": operation_id,
            "action": "wifi_rescan",
            "scope": "network",
            "context": {"ifname": "wlan0", "include_wifi_networks": True},
            "status": "succeeded",
            "result": {"ok": True, "message": "扫描完成"},
        }
        status = {
            "wireless_devices": [],
            "wifi_networks": [],
            "errors": [],
            "wifi_scan_device": "wlan0",
        }
        with (
            patch.object(web, "get_operation", return_value=operation),
            patch.object(web, "query_agent", return_value=status),
        ):
            response = client.get(f"/operations/{operation_id}")

        payload = response.get_json()
        self.assertFalse(payload["pending"])
        self.assertTrue(payload["ok"])
        self.assertIn("wireless", payload["fragments"])
        self.assertIn("wifi_networks", payload["fragments"])

    def test_agent_registry_does_not_retain_operation_parameters(self):
        registry = agent_server.OperationRegistry()
        operation = registry.create("wifi_connect", "network", {"ifname": "wlan0"})
        registry.finish(operation["id"], {"ok": True, "message": "done"})

        stored = registry.get(operation["id"])
        self.assertNotIn("params", stored)
        self.assertNotIn("password", repr(stored))

    def test_agent_runtime_rejects_unknown_actions(self):
        runtime = agent_server.AgentRuntime()
        with self.assertRaises(agent_server.ValidationError):
            runtime.submit("arbitrary_command", {}, "system", {})

    def test_hotspot_keepalive_config_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hotspot_keepalive.json"
            with patch.object(hotspot_keepalive, "HOTSPOT_KEEPALIVE_PATH", path):
                saved = hotspot_keepalive.save_hotspot_keepalive(
                    "wlan0", "AA:BB:CC:DD:EE:FF", "phy0"
                )
                self.assertEqual(hotspot_keepalive.load_hotspot_keepalive(), saved)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                hotspot_keepalive.clear_hotspot_keepalive()
                self.assertEqual(hotspot_keepalive.load_hotspot_keepalive(), {})

    def test_tailscale_config_defaults_and_route_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tailscale.json"
            with patch.object(tailscale, "TAILSCALE_CONFIG_PATH", path):
                self.assertEqual(
                    tailscale.load_tailscale_config(),
                    {"accept_routes": True, "advertise_routes": ""},
                )
                config, error = tailscale.normalize_tailscale_config(
                    {
                        "accept_routes": True,
                        "advertise_routes": "192.168.50.2/24, 10.0.0.0/24",
                    }
                )
                self.assertIsNone(error)
                self.assertEqual(config["advertise_routes"], "192.168.50.0/24,10.0.0.0/24")
                tailscale.save_tailscale_config(config)
                self.assertEqual(tailscale.load_tailscale_config(), config)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_tailscale_rejects_invalid_advertise_routes(self):
        config, error = tailscale.normalize_tailscale_config(
            {"accept_routes": True, "advertise_routes": "not-a-cidr"}
        )
        self.assertEqual(config, tailscale.DEFAULT_TAILSCALE_CONFIG)
        self.assertIn("发布路由无效", error)

    def test_tailscale_up_command_uses_only_supported_settings(self):
        command = tailscale.build_tailscale_up_command(
            {"accept_routes": True, "advertise_routes": "192.168.50.0/24"}
        )
        self.assertEqual(
            command,
            ["tailscale", "up", "--accept-routes", "--advertise-routes=192.168.50.0/24"],
        )

    def test_tailscale_status_reports_not_installed(self):
        with patch.object(tailscale, "command_exists", return_value=False):
            status = tailscale.gather_tailscale_status()
        self.assertFalse(status["installed"])
        self.assertFalse(status["logged_in"])
        self.assertEqual(status["state_label"], "未安装")
        self.assertEqual(status["state_level"], "warning")
        self.assertIn("未安装", status["errors"][0])

    def test_tailscale_status_reports_inactive_service_separately(self):
        payload = {
            "BackendState": "NeedsLogin",
            "Self": {"TailscaleIPs": None},
            "User": {},
        }
        with (
            patch.object(tailscale, "command_exists", return_value=True),
            patch.object(tailscale, "is_service_active", return_value=False),
            patch.object(
                tailscale,
                "run_command",
                return_value=CommandResult(True, json.dumps(payload)),
            ),
        ):
            status = tailscale.gather_tailscale_status()
        self.assertFalse(status["service_active"])
        self.assertFalse(status["logged_in"])
        self.assertEqual(status["state_label"], "未运行")
        self.assertEqual(status["state_level"], "error")

    def test_tailscale_status_handles_missing_ip_list(self):
        payload = {
            "BackendState": "NeedsLogin",
            "Self": {"TailscaleIPs": None},
            "User": {},
        }
        with (
            patch.object(tailscale, "command_exists", return_value=True),
            patch.object(tailscale, "is_service_active", return_value=True),
            patch.object(
                tailscale,
                "run_command",
                return_value=CommandResult(True, json.dumps(payload)),
            ),
        ):
            status = tailscale.gather_tailscale_status()
        self.assertTrue(status["installed"])
        self.assertFalse(status["logged_in"])
        self.assertEqual(status["tailscale_ips"], [])
        self.assertEqual(status["state_label"], "待登录")
        self.assertEqual(status["state_level"], "warning")

    def test_tailscale_status_uses_top_level_ip_fallback(self):
        payload = {
            "BackendState": "Running",
            "Self": {"TailscaleIPs": None, "HostName": "router"},
            "TailscaleIPs": ["100.64.0.1"],
            "User": {},
        }
        with (
            patch.object(tailscale, "command_exists", return_value=True),
            patch.object(tailscale, "is_service_active", return_value=True),
            patch.object(
                tailscale,
                "run_command",
                return_value=CommandResult(True, json.dumps(payload)),
            ),
        ):
            status = tailscale.gather_tailscale_status()
        self.assertTrue(status["logged_in"])
        self.assertEqual(status["tailscale_ips"], ["100.64.0.1"])
        self.assertEqual(status["state_label"], "已连接")
        self.assertEqual(status["state_level"], "ok")

    def test_tailscale_login_requires_installed_binary(self):
        with patch.object(tailscale, "command_exists", return_value=False):
            result = tailscale.start_tailscale_login(
                {"accept_routes": True, "advertise_routes": ""}
            )
        self.assertFalse(result["ok"])
        self.assertIn("未安装", result["message"])

    def test_tailscale_login_returns_auth_url(self):
        with (
            patch.object(tailscale, "command_exists", return_value=True),
            patch.object(
                tailscale,
                "run_command",
                side_effect=[
                    CommandResult(True, "started"),
                    CommandResult(False, "https://login.tailscale.com/a/example"),
                ],
            ),
        ):
            result = tailscale.start_tailscale_login(
                {"accept_routes": True, "advertise_routes": "192.168.50.0/24"}
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["login_url"], "https://login.tailscale.com/a/example")

    def test_tailscale_agent_operations_are_allowlisted(self):
        with (
            patch.object(agent_server, "start_tailscale_login", return_value={"ok": True, "message": "login", "login_url": ""}) as login,
            patch.object(agent_server, "logout_tailscale", return_value=CommandResult(True, "logout")) as logout,
        ):
            login_result = agent_server._execute_tailscale_login(
                {"accept_routes": True, "advertise_routes": "192.168.50.0/24"}
            )
            logout_result = agent_server._execute_tailscale_logout({})
        self.assertTrue(login_result["ok"])
        self.assertTrue(logout_result["ok"])
        login.assert_called_once_with(
            {"accept_routes": True, "advertise_routes": "192.168.50.0/24"}
        )
        logout.assert_called_once_with()

    def test_service_monitor_normalizes_service_names(self):
        services, error = service_monitor.normalize_service_list(
            "nginx, cron.service, serial-getty@ttyS0.service, nginx.service"
        )
        self.assertIsNone(error)
        self.assertEqual(
            services,
            ["nginx.service", "cron.service", "serial-getty@ttyS0.service"],
        )

        services, error = service_monitor.normalize_service_list("bad/name.service")
        self.assertEqual(services, [])
        self.assertIn("服务名称无效", error)

        services, error = service_monitor.normalize_service_list("router-panel.service")
        self.assertEqual(services, [])
        self.assertIn("不能监控当前面板自身服务", error)

    def test_service_monitor_status_reads_configured_services(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                service_monitor,
                "SERVICE_MONITOR_CONFIG_PATH",
                Path(directory) / "service-monitor.json",
            ),
            patch.object(
                service_monitor,
                "run_command",
                side_effect=[
                    CommandResult(True, "active\n"),
                    CommandResult(False, "inactive\n"),
                ],
            ),
        ):
            service_monitor.save_service_monitor_config(
                {"services": ["nginx.service", "cron.service"]}
            )
            status = service_monitor.gather_service_monitor_status()

        self.assertEqual(
            status["services"],
            [
                {"name": "nginx.service", "status": "active", "level": "ok"},
                {"name": "cron.service", "status": "inactive", "level": "error"},
            ],
        )

    def test_service_monitor_agent_operation_is_allowlisted(self):
        with patch.object(
            agent_server,
            "run_service_action",
            return_value=CommandResult(True, "done"),
        ) as action:
            result = agent_server._execute_service_monitor_action(
                {"service": "nginx.service", "action": "restart"}
            )

        self.assertTrue(result["ok"])
        action.assert_called_once_with("nginx.service", "restart")

    def test_hotspot_keepalive_enable_requires_active_ap(self):
        device = {"device": "wlan0", "type": "wifi"}
        with (
            patch.object(agent_server, "get_device_status_item", return_value=device),
            patch.object(agent_server, "load_hotspot_keepalive", return_value={}),
            patch.object(agent_server, "get_hotspot_active_connection_for_parent", return_value={}),
        ):
            with self.assertRaisesRegex(agent_server.ValidationError, "当前在线"):
                agent_server._execute_hotspot_keepalive_enable({"ifname": "wlan0"})

    def test_exclusive_hotspot_cannot_scan(self):
        device = {
            "device": "wlan0",
            "type": "wifi",
            "connection": core.HOTSPOT_CONNECTION_NAME,
        }
        with (
            patch.object(agent_server, "get_device_status_item", return_value=device),
            patch.object(agent_server, "rescan_wifi") as rescan,
        ):
            with self.assertRaisesRegex(agent_server.ValidationError, "独占 AP"):
                agent_server._execute_wifi_rescan({"ifname": "wlan0"})
        rescan.assert_not_called()

    def test_protected_hotspot_cannot_be_stopped(self):
        device = {"device": "wlan0", "type": "wifi"}
        config = {
            "enabled": True,
            "parent_ifname": "wlan0",
            "parent_mac": "AA:BB:CC:DD:EE:FF",
        }
        with (
            patch.object(agent_server, "get_device_status_item", return_value=device),
            patch.object(agent_server, "load_hotspot_keepalive", return_value=config),
            patch.object(agent_server, "stop_hotspot_profile") as stop,
        ):
            with self.assertRaisesRegex(agent_server.ValidationError, "先取消保活"):
                agent_server._execute_hotspot_stop({"ifname": "wlan0"})
        stop.assert_not_called()

    def test_keepalive_recovery_is_queued_only_once(self):
        release = threading.Event()

        def recover(params):
            release.wait(1)
            return {"ok": True, "message": "done"}

        runtime = agent_server.AgentRuntime(monitor_initial_delay=3600)
        config = {"parent_ifname": "wlan0", "parent_mac": "AA:BB:CC:DD:EE:FF"}
        with patch.dict(
            agent_server.INTERNAL_OPERATIONS,
            {"hotspot_keepalive_recover": recover},
            clear=True,
        ):
            self.assertTrue(runtime._queue_keepalive_recovery(config))
            self.assertFalse(runtime._queue_keepalive_recovery(config))
            release.set()
            runtime.queue.join()
        runtime._monitor_stop.set()

    def test_keepalive_recovery_disconnects_sta_for_exclusive_ap(self):
        command_results = [
            CommandResult(True, "configured"),
            CommandResult(True, "rebound"),
            CommandResult(True, "started"),
        ]
        config = {
            "parent_ifname": "wlan0",
            "parent_mac": "AA:BB:CC:DD:EE:FF",
            "phy_name": "phy0",
        }
        with (
            patch.object(
                network_operations,
                "resolve_hotspot_keepalive_parent",
                return_value=("wlan0", "phy0"),
            ),
            patch.object(
                network_operations,
                "get_hotspot_profile",
                return_value={"password": "secret123", "interface_name": "wlan0"},
            ),
            patch.object(network_operations, "disconnect_wifi") as disconnect,
            patch.object(
                network_operations,
                "get_interface_permanent_mac",
                return_value="AA:BB:CC:DD:EE:FF",
            ),
            patch.object(
                network_operations,
                "run_command",
                side_effect=command_results,
            ),
        ):
            result = network_operations.recover_hotspot_keepalive(config)
        self.assertTrue(result.ok)
        disconnect.assert_called_once_with("wlan0")

    def test_network_stack_does_not_require_dhcpcd(self):
        installer = INSTALL_SCRIPT.read_text()
        operations = (
            Path(__file__).parent.parent / "router_panel" / "network_operations.py"
        ).read_text()
        self.assertNotIn("dhcpcd", core.REQUIRED_PACKAGES)
        self.assertNotRegex(installer, r"(?m)^\s+dhcpcd \\")
        self.assertNotIn("dhcpcd --release", operations)
        self.assertNotIn("release_interface_from_dhcpcd", operations)
        self.assertIn('"disable_dhcpcd"', Path(dependencies.__file__).read_text())

        with patch.object(
            dependencies,
            "run_command",
            return_value=CommandResult(True, "disabled"),
        ) as run:
            result = dependencies.run_dependency_action("disable_dhcpcd")
        self.assertTrue(result.ok)
        run.assert_called_once_with(
            ["systemctl", "disable", "--now", "dhcpcd.service"],
            timeout=core.SYSTEM_COMMAND_TIMEOUT,
        )

    def test_hotspot_template_exposes_keepalive_controls(self):
        template = (
            Path(__file__).parent.parent / "templates" / "partials" / "hotspot_devices.html"
        ).read_text()
        self.assertIn("data-enable-hotspot-keepalive-ifname", template)
        self.assertIn("data-disable-hotspot-keepalive-ifname", template)
        self.assertEqual(
            len(
                re.findall(
                    r'class="button-danger-outline"\s+'
                    r'data-disable-hotspot-keepalive-ifname',
                    template,
                )
            ),
            2,
        )
        self.assertRegex(
            template,
            r'class="button-info-strong"\s+data-enable-hotspot-keepalive-ifname',
        )
        self.assertIn("保活热点当前离线", template)

    def test_hotspot_clients_table_uses_mobile_detail_layout(self):
        template = (
            Path(__file__).parent.parent / "templates" / "clients.html"
        ).read_text()
        self.assertIn("hotspot-client-table responsive-detail-table", template)
        for label in ("设备", "信号", "下行速率", "上行速率", "在线时长", "最近活动"):
            self.assertIn(f'data-label="{label}"', template)

    def test_tailscale_logged_in_state_hides_login_form_copy(self):
        template = (
            Path(__file__).parent.parent / "templates" / "tools.html"
        ).read_text()
        self.assertNotIn("更新并重新登录", template)
        self.assertIn('id="tailscale-login-form"', template)
        self.assertIn('id="tailscale-logout-form"', template)

    def test_tailscale_status_endpoint_returns_current_status(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"

        status = {
            "agent_available": True,
            "installed": True,
            "service_active": True,
            "logged_in": True,
            "errors": [],
        }
        with patch.object(web_tools, "query_agent", return_value=status):
            response = client.get("/tools/tailscale/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), status)

    def test_service_monitor_save_endpoint_normalizes_config(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"
            session["csrf_token"] = "test-token"

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                service_monitor,
                "SERVICE_MONITOR_CONFIG_PATH",
                Path(directory) / "service-monitor.json",
            ),
        ):
            response = client.post(
                "/tools/services/save",
                data={"csrf_token": "test-token", "services": "nginx, cron.service"},
                headers={"X-Requested-With": "fetch"},
            )
            config = service_monitor.load_service_monitor_config()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(config["services"], ["nginx.service", "cron.service"])

    def test_wireless_template_disables_exclusive_hotspot_scan(self):
        template = (
            Path(__file__).parent.parent / "templates" / "partials" / "network_devices.html"
        ).read_text()
        network_page = (Path(__file__).parent.parent / "templates" / "network.html").read_text()
        self.assertIn("data-exclusive-hotspot-scan", template)
        self.assertIn("该网卡正在运行独占 AP，无法扫描 Wi-Fi 网络", network_page)

    def test_agent_runtime_executes_mutations_with_one_writer(self):
        active = 0
        maximum_active = 0
        guard = threading.Lock()

        def mutation(params):
            nonlocal active, maximum_active
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.05)
            with guard:
                active -= 1
            return {"ok": True, "message": params["message"]}

        registry = agent_server.OperationRegistry()
        runtime = agent_server.AgentRuntime(registry)
        with patch.dict(agent_server.OPERATIONS, {"test_mutation": mutation}, clear=True):
            first = runtime.submit("test_mutation", {"message": "first"}, "system", {})
            second = runtime.submit("test_mutation", {"message": "second"}, "system", {})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if registry.get(second["id"])["status"] == "succeeded":
                    break
                time.sleep(0.01)

        self.assertEqual(registry.get(first["id"])["status"], "succeeded")
        self.assertEqual(registry.get(second["id"])["status"], "succeeded")
        self.assertEqual(maximum_active, 1)

    def test_wifi_connect_retries_after_incomplete_saved_profile(self):
        command_results = [
            CommandResult(False, "802-11-wireless-security.key-mgmt: property is missing"),
            CommandResult(True, "deleted"),
            CommandResult(True, "connected"),
        ]
        with (
            patch.object(network_operations, "get_wireless_interface_phy_map", return_value={}),
            patch.object(network_operations, "get_wireless_phy_capabilities", return_value={}),
            patch.object(network_operations, "get_hotspot_active_connection_for_parent", return_value={}),
            patch.object(network_operations, "run_command", side_effect=command_results) as run,
            patch.object(network_operations, "get_wifi_connection_profiles", return_value=[{"uuid": "old-id"}]),
            patch.object(network_operations, "get_active_wifi_connection", return_value={"uuid": "new-id"}),
            patch.object(network_operations, "bind_wifi_profile_to_hardware", return_value=None),
            patch.object(network_operations, "get_device_status_item", return_value={"state": "connected"}),
            patch.object(network_operations, "get_hotspot_profile", return_value={}),
        ):
            result = network_operations.connect_wifi_profile("wlan0", "Test", "secret123", "", "")

        self.assertTrue(result["result"].ok)
        self.assertEqual(run.call_count, 3)
        self.assertIn("old-id", run.call_args_list[1].args[0])

    def test_wifi_connect_retries_after_missing_secrets_saved_profile(self):
        command_results = [
            CommandResult(
                False,
                "Passwords or encryption keys are required to access the wireless network 'HIWIFI_2G'.",
            ),
            CommandResult(True, "deleted"),
            CommandResult(True, "connected"),
        ]
        with (
            patch.object(network_operations, "get_wireless_interface_phy_map", return_value={}),
            patch.object(network_operations, "get_wireless_phy_capabilities", return_value={}),
            patch.object(network_operations, "get_hotspot_active_connection_for_parent", return_value={}),
            patch.object(network_operations, "run_command", side_effect=command_results) as run,
            patch.object(network_operations, "get_wifi_connection_profiles", return_value=[{"uuid": "old-id"}]),
            patch.object(network_operations, "get_active_wifi_connection", return_value={"uuid": "new-id"}),
            patch.object(network_operations, "bind_wifi_profile_to_hardware", return_value=None),
            patch.object(network_operations, "get_device_status_item", return_value={"state": "connected"}),
            patch.object(network_operations, "get_hotspot_profile", return_value={}),
        ):
            result = network_operations.connect_wifi_profile("wlan0", "HIWIFI_2G", "secret123", "", "")

        self.assertTrue(result["result"].ok)
        self.assertEqual(run.call_count, 3)
        self.assertIn("old-id", run.call_args_list[1].args[0])

    def test_exclusive_hotspot_rolls_back_when_lan_setup_fails(self):
        with (
            patch.object(network_operations, "cleanup_hotspot_virtual_interfaces"),
            patch.object(
                network_operations,
                "run_command",
                side_effect=[CommandResult(True, "started"), CommandResult(True, "stopped")],
            ) as run,
            patch.object(network_operations, "configure_active_hotspot_lan", return_value="LAN 配置失败"),
            patch.object(network_operations, "delete_inactive_hotspot_profiles") as cleanup,
        ):
            result = network_operations.start_hotspot_profile(
                "wlan0", "phy0", "Hotspot", "secret123", "a", "149", "exclusive"
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.output, "LAN 配置失败")
        self.assertEqual(run.call_count, 2)
        cleanup.assert_called_once()

    def test_networkmanager_takeover_runs_reload_and_manage(self):
        with (
            patch.object(network_operations, "remove_interface_from_unmanaged_devices") as remove,
            patch.object(network_operations, "run_command", return_value=CommandResult(True, "")) as run,
        ):
            results = network_operations.manage_networkmanager_interface("wlan0")

        remove.assert_called_once_with("wlan0")
        self.assertEqual(len(results), 2)
        self.assertEqual(run.call_count, 2)

    def test_hidden_bulk_dependency_action_is_rejected(self):
        result = dependencies.run_dependency_action("fix_all_dependencies")
        self.assertFalse(result.ok)
        self.assertEqual(result.output, "不支持的修复动作")

    def test_networkmanager_shared_hotspot_satisfies_nat_status(self):
        with (
            patch.object(
                dependencies,
                "get_active_hotspot_connection",
                return_value={"device": "wlp1s0"},
            ),
            patch.object(dependencies, "get_default_route_interface", return_value="end0"),
            patch.object(
                dependencies,
                "run_command",
                return_value=CommandResult(True, "shared"),
            ) as run,
        ):
            status = dependencies.get_hotspot_nat_status()

        self.assertEqual(status["level"], "ok")
        self.assertEqual(status["details"], "wlp1s0 -> end0")
        run.assert_called_once_with(
            [
                "nmcli",
                "-g",
                "ipv4.method",
                "connection",
                "show",
                "id",
                core.HOTSPOT_CONNECTION_NAME,
            ],
            timeout=5,
        )

    def test_non_shared_hotspot_reports_nat_error_without_legacy_repair(self):
        with (
            patch.object(
                dependencies,
                "get_active_hotspot_connection",
                return_value={"device": "wlp1s0"},
            ),
            patch.object(dependencies, "get_default_route_interface", return_value="end0"),
            patch.object(
                dependencies,
                "run_command",
                return_value=CommandResult(True, "manual"),
            ),
        ):
            status = dependencies.get_hotspot_nat_status()

        self.assertEqual(status["level"], "error")
        self.assertIn("NetworkManager", status["summary"])
        repair = dependencies.run_dependency_action("fix_hotspot_nat")
        self.assertFalse(repair.ok)
        self.assertEqual(repair.output, "不支持的修复动作")

    def test_dependency_details_are_short_descriptions(self):
        rows = dependencies.get_dependency_rows()
        self.assertTrue(rows)
        for row in rows:
            self.assertLessEqual(len(row["detail"]), 15, row["name"])

    def test_netplan_repair_only_sets_global_renderer(self):
        with tempfile.TemporaryDirectory() as directory:
            netplan_dir = Path(directory)
            with (
                patch.object(dependencies, "NETPLAN_DIR", netplan_dir),
                patch.object(dependencies, "command_exists", return_value=True),
            ):
                result = dependencies.ensure_netplan_networkmanager_renderer()

            self.assertTrue(result.ok)
            target = netplan_dir / "90-linux-router.yaml"
            self.assertTrue(target.exists())
            content = target.read_text()
            self.assertIn("renderer: NetworkManager", content)
            self.assertNotIn("ethernets:", content)
            self.assertNotIn("wifis:", content)

    def test_netplan_repair_updates_existing_renderer_files(self):
        with tempfile.TemporaryDirectory() as directory:
            netplan_dir = Path(directory)
            existing = netplan_dir / "10-dhcp-all-interfaces.yaml"
            existing.write_text(
                "network:\n"
                "  version: 2\n"
                "  renderer: networkd\n"
                "  ethernets:\n"
                "    all:\n"
                "      match:\n"
                "        name: '*'\n"
                "      dhcp4: true\n",
                encoding="utf-8",
            )

            with (
                patch.object(dependencies, "NETPLAN_DIR", netplan_dir),
                patch.object(dependencies, "command_exists", return_value=True),
            ):
                result = dependencies.ensure_netplan_networkmanager_renderer()
                summary = dependencies.get_netplan_renderer_summary()

            self.assertTrue(result.ok)
            self.assertIn("10-dhcp-all-interfaces.yaml", result.output)
            content = existing.read_text(encoding="utf-8")
            self.assertIn("renderer: NetworkManager", content)
            self.assertIn("ethernets:", content)
            self.assertEqual(summary["level"], "ok")

    def test_network_stack_repair_restores_files_when_apply_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            netplan_dir = root / "netplan"
            netplan_dir.mkdir()
            networkmanager_config = root / "NetworkManager.conf"
            networkmanager_config.write_text(
                "[ifupdown]\nmanaged=false\n",
                encoding="utf-8",
            )
            existing = netplan_dir / "10-network.yaml"
            existing.write_text(
                "network:\n  version: 2\n  renderer: networkd\n",
                encoding="utf-8",
            )

            command_results = [
                CommandResult(True, "generated"),
                CommandResult(False, "apply failed"),
                CommandResult(True, "regenerated"),
                CommandResult(True, "reapplied"),
                CommandResult(True, "restarted"),
                CommandResult(True, "disabled"),
                CommandResult(True, "stopped"),
            ]
            with (
                patch.object(dependencies, "NETPLAN_DIR", netplan_dir),
                patch.object(
                    dependencies,
                    "NETWORKMANAGER_CONFIG_PATH",
                    networkmanager_config,
                ),
                patch.object(dependencies, "is_service_enabled", return_value=False),
                patch.object(dependencies, "is_service_active", return_value=False),
                patch.object(dependencies, "command_exists", return_value=True),
                patch.object(
                    dependencies,
                    "run_command",
                    side_effect=command_results,
                ),
            ):
                result = dependencies.run_dependency_action("repair_network_stack")

            self.assertFalse(result.ok)
            self.assertEqual(result.output, "apply failed")
            self.assertEqual(
                networkmanager_config.read_text(encoding="utf-8"),
                "[ifupdown]\nmanaged=false\n",
            )
            self.assertEqual(
                existing.read_text(encoding="utf-8"),
                "network:\n  version: 2\n  renderer: networkd\n",
            )
            self.assertFalse((netplan_dir / "90-linux-router.yaml").exists())

    def test_networkmanager_repair_restores_config_when_restart_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            netplan_dir = root / "netplan"
            netplan_dir.mkdir()
            networkmanager_config = root / "NetworkManager.conf"
            original = "[ifupdown]\nmanaged=false\n"
            networkmanager_config.write_text(original, encoding="utf-8")
            with (
                patch.object(dependencies, "NETPLAN_DIR", netplan_dir),
                patch.object(
                    dependencies,
                    "NETWORKMANAGER_CONFIG_PATH",
                    networkmanager_config,
                ),
                patch.object(dependencies, "is_service_enabled", return_value=False),
                patch.object(dependencies, "is_service_active", return_value=False),
                patch.object(
                    dependencies,
                    "run_command",
                    side_effect=[
                        CommandResult(False, "restart failed"),
                        CommandResult(True, "restored"),
                        CommandResult(True, "disabled"),
                        CommandResult(True, "stopped"),
                    ],
                ),
            ):
                result = dependencies.run_dependency_action("fix_nm_managed")

            self.assertFalse(result.ok)
            self.assertEqual(result.output, "restart failed")
            self.assertEqual(networkmanager_config.read_text(encoding="utf-8"), original)

    def test_network_stack_repair_does_not_reload_on_first_step_failure(self):
        failure = CommandResult(False, "NetworkManager 配置正在更新")
        with (
            patch.object(dependencies, "ensure_networkmanager_managed_ifupdown", return_value=failure),
            patch.object(dependencies, "is_service_enabled", return_value=False),
            patch.object(dependencies, "is_service_active", return_value=False),
            patch.object(dependencies, "_snapshot_network_configuration", return_value={}),
            patch.object(dependencies, "run_command") as run,
        ):
            result = dependencies.run_dependency_action("repair_network_stack")

        self.assertIs(result, failure)
        run.assert_not_called()

    def test_netplan_missing_is_skipped_without_rollback_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "NetworkManager.conf"
            original = "[ifupdown]\nmanaged=false\n"
            path.write_text(original, encoding="utf-8")
            snapshot = {path: (original, 0o644)}

            with (
                patch.object(dependencies, "command_exists", return_value=False),
                patch.object(dependencies, "run_command") as run,
            ):
                summary = dependencies.get_netplan_renderer_summary()
                apply_result = dependencies.apply_netplan()
                rollback_error = dependencies._rollback_network_configuration(
                    snapshot,
                    apply_netplan_config=True,
                    restart_networkmanager=False,
                )

            self.assertTrue(summary["ok"])
            self.assertTrue(apply_result.ok)
            self.assertEqual(rollback_error, "")
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            run.assert_not_called()

    def test_ip_forward_repair_restores_file_and_runtime_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            sysctl_path = Path(directory) / "90-router-panel.conf"
            original = "net.ipv4.ip_forward=0\n"
            sysctl_path.write_text(original, encoding="utf-8")
            with (
                patch.object(dependencies, "ROUTER_PANEL_SYSCTL_PATH", sysctl_path),
                patch.object(
                    dependencies,
                    "run_command",
                    side_effect=[
                        CommandResult(True, "0"),
                        CommandResult(False, "sysctl failed"),
                        CommandResult(True, "restored"),
                    ],
                ) as run,
            ):
                result = dependencies.run_dependency_action("fix_ip_forward")

            self.assertFalse(result.ok)
            self.assertEqual(result.output, "sysctl failed")
            self.assertEqual(sysctl_path.read_text(encoding="utf-8"), original)
            self.assertEqual(run.call_count, 3)

    def test_removed_internal_routes_are_not_exposed(self):
        client = application.app.test_client()
        for path in (
            "/api/system",
            "/api/wifi",
            "/api/hotspot",
            "/api/hotspot/clients",
            "/wired/configure",
            "/wired/connect",
        ):
            self.assertEqual(client.get(path).status_code, 404, path)


if __name__ == "__main__":
    unittest.main()
