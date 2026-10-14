import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from epics_field_client.app import FieldApp, make_server
from epics_field_client.diagnostics import (
    DEFAULT_CONFIG, call_read_tool, child_environment, conclusion, run_probe, validate_config, validate_real_environment,
    linux_network_snapshot, collect_environment, report_markdown,
)


class ConfigurationTests(unittest.TestCase):
    def test_existing_small_array_limit_is_not_silently_replaced(self):
        config = validate_config({"max_array_bytes": 1024})
        self.assertEqual(child_environment(config, {"USER": "field-test"})["EPICS_CA_MAX_ARRAY_BYTES"], "1024")
    def test_linux_network_data_and_command_failure_are_preserved(self):
        evidence = [{"command": ["ip", "-j", "address"], "returncode": 0, "stdout": json.dumps([
            {"ifname": "eth0", "operstate": "UP", "addr_info": [{"local": "192.0.2.10", "prefixlen": 24}]}])},
            {"command": ["ip", "-j", "route"], "returncode": 0, "stdout": json.dumps([
                {"dst": "default", "gateway": "192.0.2.1", "dev": "eth0"}])}]
        snapshot = linux_network_snapshot(evidence)
        self.assertEqual(snapshot["interfaces"][0]["addresses"], ["192.0.2.10/24"])
        self.assertEqual(snapshot["routes"][0]["gateway"], "192.0.2.1")
        self.assertEqual(snapshot["errors"], [])
        bad = linux_network_snapshot([{"command": ["ip", "-j", "address"], "error": "ip not installed"}])
        self.assertEqual(bad["interfaces"], [])
        self.assertEqual(bad["errors"], ["ip not installed"])
        malformed = linux_network_snapshot([{"command": ["ip", "-j", "address"], "returncode": 0, "stdout": "truncated JSON"}])
        self.assertTrue(malformed["errors"])

    def test_linux_collection_uses_local_read_only_ip_commands(self):
        command_result = {"command": ["ip", "-j", "address"], "returncode": 0, "stdout": "[]"}
        with patch("epics_field_client.diagnostics.sys.platform", "linux"), patch("epics_field_client.diagnostics.linux_distribution", return_value="Test Linux"), patch("epics_field_client.diagnostics.command_evidence", return_value=command_result) as run:
            environment = collect_environment({**DEFAULT_CONFIG, "collect_network": True})
        self.assertEqual([call.args[0] for call in run.call_args_list], [["ip", "-j", "address"], ["ip", "-j", "route"]])
        self.assertEqual(environment["distribution"], "Test Linux")
        self.assertEqual(environment["network_snapshot"]["interfaces"], [])
    def test_explicit_does_not_inherit_simulation_writes_or_ca(self):
        source = {"USERNAME": "field-test", "PATH": "bin", "LABMCP_SIMULATE": "1",
                  "LABMCP_OPTIONS": 'safe_state=TEST:STOP=1', "EPICS_CA_ADDR_LIST": "old-gateway",
                  "EPICS_CA_NAME_SERVERS": "old-nameserver", "EPICS_CAS_INTF_ADDR_LIST": "0.0.0.0"}
        original = source.copy()
        config = validate_config({"address_list": "gw.local, 127.0.0.1:5064"})
        env = child_environment(config, source)
        self.assertEqual(source, original)
        self.assertEqual(env["EPICS_CA_ADDR_LIST"], "gw.local 127.0.0.1:5064")
        self.assertEqual(env["EPICS_CA_AUTO_ADDR_LIST"], "NO")
        self.assertEqual(env["USERNAME"], "field-test")
        self.assertEqual(env["FASTMCP_CHECK_FOR_UPDATES"], "off")
        self.assertEqual(env["FASTMCP_TELEMETRY_MODE"], "off")
        self.assertNotIn("EPICS_CA_NAME_SERVERS", env)
        self.assertFalse(any(k.startswith(("LABMCP_", "EPICS_CAS_")) for k in env))

    def test_inherit_uses_approved_ca_environment(self):
        env = child_environment(validate_config({"connection_mode": "inherit"}), {
            "USERNAME": "field-test", "EPICS_CA_ADDR_LIST": "existing", "EPICS_CA_AUTO_ADDR_LIST": "YES",
            "EPICS_CA_NAME_SERVERS": "nameserver", "EPICS_CA_UNKNOWN": "unexpected"})
        self.assertEqual(env["EPICS_CA_ADDR_LIST"], "existing")
        self.assertNotIn("EPICS_CA_NAME_SERVERS", env)
        self.assertNotIn("EPICS_CA_UNKNOWN", env)

    def test_name_servers_and_empty_no_broadcast_configuration_rejected(self):
        config = validate_config({"connection_mode": "inherit"})
        for source in ({"USERNAME": "field-test", "EPICS_CA_NAME_SERVERS": "ns.local"},
                       {"USERNAME": "field-test", "EPICS_CA_AUTO_ADDR_LIST": "NO"}):
            with self.subTest(source=source), self.assertRaises(ValueError):
                validate_real_environment(config, source)

    def test_invalid_input_is_rejected(self):
        for raw in ({"timeout_s": float("nan")}, {"auto_address": "false"},
                    {"pv_name": "-h"}, {"pv_name": "PV; WITH SPACE"},
                    {"server_port": True}, {"max_array_bytes": 0}, {"address_list": "http://localhost"},
                    {"address_list": "localhost:99999"}, {"command": "caput"}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_config(raw)

    def test_unsafe_tool_never_reaches_session(self):
        for name in ("apply_safe_state", "put_pv", "put_pvs", "reconnect"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                asyncio.run(call_read_tool(None, name, {}, 1))

    def test_success_requires_verified_identity(self):
        base = {"test_kind": "real", "stages": [{"stage": "get_pv", "ok": True, "data": {"value": 22}}]}
        self.assertFalse(conclusion(base)["real_connection_verified"])
        base["stages"].append({"stage": "get_connection_info", "data": {"simulated": True}})
        self.assertEqual(conclusion(base)["status"], "simulation_passed")
        base["stages"][-1]["data"]["simulated"] = False
        self.assertTrue(conclusion(base)["real_connection_verified"])
        self.assertFalse(conclusion(base)["baseline_verified"])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = FieldApp(Path(self.temp.name))
        self.server = make_server(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        all_headers = {"X-Field-Token": self.app.token, "Content-Type": "application/json"}
        all_headers.update(headers or {})
        return urlopen(Request(self.url + path, headers=all_headers,
                               data=None if body is None else json.dumps(body).encode()), timeout=10)

    def test_api_rejects_cross_origin_and_missing_token(self):
        for headers in ({"X-Field-Token": ""}, {"Origin": "https://other.example"}, {"Host": "other.example"}):
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as caught:
                self.request("/api/config", headers=headers)
            self.assertEqual(caught.exception.code, 403)

    def test_config_roundtrip(self):
        values = {"address_list": "ioc.local", "pv_name": "BL:CURRENT", "notes": "现场待核对"}
        with self.request("/api/config", values) as response:
            self.assertEqual(response.status, 200)
        with self.request("/api/config") as response:
            saved = json.load(response)
        self.assertEqual(saved["notes"], "现场待核对")
        self.assertEqual(saved["address_list"], "ioc.local")

    def test_bootstrap_only_exposes_approved_ca_environment(self):
        with patch.dict(os.environ, {"EPICS_CA_ADDR_LIST": "ioc.local", "PRIVATE_TEST_SECRET": "do-not-export"}):
            with self.request("/api/bootstrap") as response:
                bootstrap = json.load(response)
        self.assertEqual(bootstrap["detected_ca_environment"]["EPICS_CA_ADDR_LIST"], "ioc.local")
        self.assertNotIn("do-not-export", json.dumps(bootstrap))
        self.assertEqual(bootstrap["defaults"], DEFAULT_CONFIG)

    def test_generic_tool_execution_has_no_endpoint(self):
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/tools/call", {"name": "put_pv"})
        self.assertEqual(caught.exception.code, 404)

    def test_previous_reports_are_not_downloadable_in_current_session(self):
        old_id = "a" * 32
        for extension in ("json", "md"):
            (self.app.reports_dir / f"{old_id}.{extension}").write_text("private fixture")
            with self.assertRaises(HTTPError) as caught:
                self.request(f"/api/reports/{old_id}.{extension}")
            self.assertEqual(caught.exception.code, 404)

    def test_environment_job_exports_evidence_without_device_calls(self):
        import time
        with self.request("/api/jobs", {"kind": "environment", "config": DEFAULT_CONFIG}) as response:
            job = json.load(response)
        for _ in range(30):
            with self.request("/api/jobs/" + job["id"]) as response:
                state = json.load(response)
            if state["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["report"]["stages"], [])
        self.assertEqual(state["report"]["conclusion"]["status"], "environment_only")
        markdown = report_markdown(state["report"])
        self.assertIn("## 电脑环境", markdown)
        self.assertIn("## 检测到的 EPICS 配置", markdown)
        self.assertIn("## 网络信息", markdown)
        for extension in ("json", "md"):
            with self.request(f"/api/reports/{job['id']}.{extension}") as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 100)


@unittest.skipUnless(os.environ.get("FIELD_INTEGRATION") == "1", "Set FIELD_INTEGRATION=1 for local CA/MCP integration")
class MCPIntegrationTests(unittest.TestCase):
    def test_actual_stdio_mcp_and_simulated_ioc(self):
        with tempfile.TemporaryDirectory() as folder:
            report = run_probe(DEFAULT_CONFIG, "simulation", Path(folder) / "stderr.log")
        self.assertEqual(report["conclusion"]["status"], "simulation_passed", json.dumps(report, ensure_ascii=False))
        self.assertFalse(report["conclusion"]["real_connection_verified"])
        self.assertTrue(all(s["ok"] for s in report["stages"]))
        self.assertEqual([s["stage"] for s in report["stages"]], ["mcp_initialize", "get_connection_info", "pv_info", "get_pv"])
        self.assertFalse(any(t["client_enabled"] for t in report["tools"] if t["name"] == "apply_safe_state"))
        self.assertNotIn("effective_ca_environment", report["environment"])
        info = next(s["data"] for s in report["stages"] if s["stage"] == "pv_info")
        self.assertTrue(info["server"].startswith("127.0.0.1:"))
        self.assertEqual(report["runtime_ca_environment"]["EPICS_CA_ADDR_LIST"], info["server"])
        self.assertEqual(report["runtime_ca_environment"]["EPICS_CA_AUTO_ADDR_LIST"], "NO")

    def test_real_mode_to_local_reference_ioc_using_environment(self):
        from labmcp_epics.simulator import SimulatedIOC
        ioc = SimulatedIOC("FIELDTEST:").start()
        try:
            config = {**DEFAULT_CONFIG, "connection_mode": "inherit", "pv_name": "FIELDTEST:TEMP"}
            with patch.dict(os.environ, ioc.client_env), tempfile.TemporaryDirectory() as folder:
                report = run_probe(config, "real", Path(folder) / "stderr.log")
            self.assertEqual(report["conclusion"]["status"], "real_pv_read", json.dumps(report, ensure_ascii=False))
            self.assertNotIn("--simulate", report["launch"]["args"])
            # This verifies real-mode transport against a local reference IOC, not facility hardware.
        finally:
            ioc.stop()

    def test_unknown_pv_is_failure_despite_successful_mcp_start(self):
        from labmcp_epics.simulator import SimulatedIOC
        ioc = SimulatedIOC("FIELDTEST:").start()
        try:
            config = {**DEFAULT_CONFIG, "address_list": ioc.client_env["EPICS_CA_ADDR_LIST"],
                      "repeater_port": ioc.repeater_port, "timeout_s": 0.5, "pv_name": "FIELDTEST:DOES_NOT_EXIST"}
            with tempfile.TemporaryDirectory() as folder:
                report = run_probe(config, "real", Path(folder) / "stderr.log")
            self.assertEqual(report["conclusion"]["status"], "failed")
            self.assertTrue(report["stages"][0]["ok"])
            self.assertFalse(report["conclusion"]["real_connection_verified"])
        finally:
            ioc.stop()

    def test_real_mode_to_local_reference_ioc_using_explicit_address(self):
        from labmcp_epics.simulator import SimulatedIOC
        ioc = SimulatedIOC("FIELDTEST:").start()
        try:
            config = {**DEFAULT_CONFIG, "address_list": ioc.client_env["EPICS_CA_ADDR_LIST"],
                      "repeater_port": ioc.repeater_port, "pv_name": "FIELDTEST:TEMP"}
            with tempfile.TemporaryDirectory() as folder:
                report = run_probe(config, "real", Path(folder) / "stderr.log")
            self.assertEqual(report["conclusion"]["status"], "real_pv_read", json.dumps(report, ensure_ascii=False))
            self.assertTrue(all(s["ok"] for s in report["stages"]))
            self.assertNotIn("--simulate", report["launch"]["args"])
        finally:
            ioc.stop()


if __name__ == "__main__":
    unittest.main()
