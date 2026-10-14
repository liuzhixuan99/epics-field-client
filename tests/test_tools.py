import json
import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import asynccontextmanager
from types import SimpleNamespace

from epics_field_client.app import FieldApp
from epics_field_client.diagnostics import DEFAULT_CONFIG
from epics_field_client.tool_session import ToolSession, validate_arguments


def descriptor(name, properties, required):
    return {"name": name, "input_schema": {"type": "object", "properties": properties,
                                            "required": required, "additionalProperties": False}}


class ToolValidationTests(unittest.TestCase):
    def test_session_fatal_exit_releases_waiters_and_marks_writes_uncertain(self):
        class CrashingSession(ToolSession):
            async def _serve(self):
                self.loop = asyncio.get_running_loop()
                self.queue = asyncio.Queue()
                self.ready.set()
                await self.queue.get()
                raise asyncio.CancelledError("test connection terminated")
        with tempfile.TemporaryDirectory() as folder:
            bridge = CrashingSession(Path(folder) / "mcp.log")
            result = bridge.request("call", {"name": "put_pv", "arguments": {"name": "TEST:SETPOINT", "value": 2}})
            self.assertFalse(result["ok"])
            self.assertTrue(result["outcome_uncertain"])
            self.assertTrue(result["connection_lost"])
            bridge.thread.join(2)
            with self.assertRaises(RuntimeError):
                bridge.request("connect", DEFAULT_CONFIG)
            bridge.close()

    def test_bad_arguments_and_unknown_tools_rejected(self):
        tool = descriptor("get_pv", {"name": {"type": "string", "minLength": 1},
                                     "max_elements": {"type": "integer", "minimum": 1}}, ["name"])
        for arguments in ({}, {"name": ""}, {"name": "TEST:TEMP", "other": 1},
                          {"name": "TEST:TEMP", "max_elements": True},
                          {"name": "TEST:TEMP", "max_elements": 0}, {"name": "TEST:TEMP", "max_elements": float("nan")}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validate_arguments(tool, arguments)
        with self.assertRaises(ValueError):
            validate_arguments(descriptor("arbitrary", {}, []), {})

    def test_writes_require_exact_pv_allowlist_and_confirmation(self):
        tool = descriptor("put_pv", {"name": {"type": "string"}, "value": {"type": "number"}}, ["name", "value"])
        value = {"name": "TEST:SETPOINT", "value": 2}
        for allowed, confirmed in (((), True), (("TEST:OTHER",), True), (("TEST:SETPOINT",), False)):
            with self.subTest(allowed=allowed, confirmed=confirmed), self.assertRaises(ValueError):
                validate_arguments(tool, value, write_pvs=allowed, confirmed=confirmed)
        self.assertEqual(validate_arguments(tool, value, write_pvs=("TEST:SETPOINT",), confirmed=True), value)
        with self.assertRaises(ValueError):
            validate_arguments(descriptor("apply_safe_state", {}, []), {}, write_pvs=("TEST:SETPOINT",), confirmed=True)

    def test_manual_jobs_keep_actual_result_and_export_file(self):
        result = {"tool": "get_pv", "ok": True, "data": {"value": 25.125}}
        with tempfile.TemporaryDirectory() as folder:
            app = FieldApp(Path(folder))
            with patch.object(app.tool_session, "request", return_value=result) as request:
                payload = {"catalog_id": "example", "name": "get_pv", "arguments": {"name": "TEST:TEMP"}, "confirmed": False}
                job = app.start_tool_job("call", payload)
                for worker in app.workers:
                    worker.join(5)
            state = app.jobs[job["id"]]
            self.assertEqual(state["result"], result)
            self.assertEqual(json.loads((app.reports_dir / f"{job['id']}.json").read_text()), result)
            request.assert_called_once_with("call", payload)


@unittest.skipUnless(os.environ.get("FIELD_INTEGRATION") == "1", "Requires local MCP and CA dependencies")
class ToolIntegrationTests(unittest.TestCase):
    def test_transport_failure_recovers_on_one_reload_and_reconnect_updates_target(self):
        class Reply:
            def __init__(self, data):
                self.data = data
            def model_dump(self, **_):
                return {"structuredContent": self.data}
        class FakeSession:
            instances = []
            def __init__(self, *_args, **_kwargs):
                self.index = len(self.instances) + 1
                self.fail_read = self.index == 1
                self.instances.append(self)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                pass
            async def initialize(self):
                return None
            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name=name, description="test", input_schema=schema, annotations=None)
                                              for name, schema in [("get_pv", {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}), ("reconnect", {"type": "object", "properties": {}})]])
            async def call_tool(self, name, arguments, **_):
                if name == "get_connection_info":
                    return Reply({"simulated": False, "read_only": True})
                if name == "pv_info":
                    return Reply({"connected": True, "server": f"192.0.2.{self.index}:5064"})
                if name == "get_pv" and self.fail_read:
                    raise RuntimeError("transport closed")
                if name == "reconnect":
                    self.index += 1
                return Reply({"value": 25.125})
        @asynccontextmanager
        async def fake_stdio(*_, **__):
            yield (None, None)
        with tempfile.TemporaryDirectory() as folder, patch("mcp.ClientSession", FakeSession), patch("mcp.client.stdio.stdio_client", fake_stdio):
            bridge = ToolSession(Path(folder) / "mcp.log")
            try:
                config = {**DEFAULT_CONFIG, "address_list": "192.0.2.1", "pv_name": "TEST:TEMP"}
                initial = bridge.request("connect", config)
                failed = bridge.request("call", {"catalog_id": initial["id"], "name": "get_pv", "arguments": {"name": "TEST:TEMP"}})
                self.assertTrue(failed["connection_lost"])
                self.assertIsNone(bridge.catalog)
                recovered = bridge.request("connect", config)
                self.assertEqual(len(FakeSession.instances), 2)
                result = bridge.request("call", {"catalog_id": recovered["id"], "name": "reconnect", "arguments": {}})
                self.assertEqual(result["connection"]["server"], "192.0.2.3:5064")
                self.assertNotEqual(result["catalog"]["id"], recovered["id"])
                with self.assertRaises(ValueError):
                    bridge.request("call", {"catalog_id": recovered["id"], "name": "get_pv", "arguments": {"name": "TEST:TEMP"}})
            finally:
                bridge.close()

    def test_persistent_read_tools_monitor_logs_and_reconnect(self):
        from labmcp_epics.simulator import SimulatedIOC
        ioc = SimulatedIOC("TOOLTEST:").start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                bridge = ToolSession(Path(folder) / "mcp.log")
                try:
                    config = {**DEFAULT_CONFIG, "address_list": ioc.client_env["EPICS_CA_ADDR_LIST"],
                              "repeater_port": ioc.repeater_port, "pv_name": "TOOLTEST:TEMP"}
                    catalog = bridge.request("connect", config)
                    tools = {tool["name"]: tool for tool in catalog["tools"]}
                    self.assertIn("get_pvs", tools)
                    self.assertIn("monitor_pv", tools)
                    self.assertNotIn("put_pv", tools)
                    self.assertFalse(tools["apply_safe_state"]["enabled"])
                    def call(name, arguments):
                        return bridge.request("call", {"catalog_id": catalog["id"], "name": name,
                                                       "arguments": arguments, "confirmed": False})
                    bulk = call("get_pvs", {"names": ["TOOLTEST:TEMP"]})
                    self.assertTrue(bulk["ok"], bulk)
                    self.assertEqual(len(bulk["data"]["readings"]), 1)
                    monitored = call("monitor_pv", {"name": "TOOLTEST:TEMP", "duration_s": 1, "max_points": 4})
                    self.assertTrue(monitored["ok"], monitored)
                    self.assertGreater(monitored["data"]["n_updates"], 0)
                    logged = call("get_command_log", {"limit": 20})
                    self.assertIn("TOOLTEST:TEMP", json.dumps(logged))
                    reconnected = call("reconnect", {})
                    self.assertTrue(reconnected["ok"])
                    self.assertNotEqual(reconnected["catalog"]["id"], catalog["id"])
                    catalog = reconnected["catalog"]
                    self.assertTrue(call("get_pv", {"name": "TOOLTEST:TEMP"})["ok"])
                    with self.assertRaises(ValueError):
                        call("apply_safe_state", {})
                    with self.assertRaises(ValueError):
                        bridge.request("call", {"catalog_id": "old", "name": "get_pv", "arguments": {"name": "TOOLTEST:TEMP"}})
                finally:
                    bridge.close()
                self.assertFalse(bridge.thread.is_alive())
        finally:
            ioc.stop()

    def test_optional_write_tools_reject_other_pvs_and_read_back_allowed_write(self):
        from labmcp_epics.simulator import SimulatedIOC
        ioc = SimulatedIOC("WRITETEST:").start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                bridge = ToolSession(Path(folder) / "mcp.log", write_pvs=("WRITETEST:HEATER",))
                try:
                    config = {**DEFAULT_CONFIG, "address_list": ioc.client_env["EPICS_CA_ADDR_LIST"],
                              "repeater_port": ioc.repeater_port, "pv_name": "WRITETEST:TEMP"}
                    catalog = bridge.request("connect", config)
                    self.assertIn("put_pv", [t["name"] for t in catalog["tools"]])
                    payload = {"catalog_id": catalog["id"], "name": "put_pv", "arguments": {"name": "WRITETEST:TEMP", "value": 25}, "confirmed": True}
                    with self.assertRaises(ValueError):
                        bridge.request("call", payload)
                    payload["arguments"] = {"name": "WRITETEST:HEATER", "value": "On"}
                    payload["confirmed"] = False
                    with self.assertRaises(ValueError):
                        bridge.request("call", payload)
                    payload["confirmed"] = True
                    reply = bridge.request("call", payload)
                    self.assertTrue(reply["ok"], reply)
                    self.assertTrue(reply["data"]["completed"])
                    readback = bridge.request("call", {"catalog_id": catalog["id"], "name": "get_pv",
                                                       "arguments": {"name": "WRITETEST:HEATER"}, "confirmed": False})
                    self.assertEqual(readback["data"]["value"], "On")
                finally:
                    bridge.close()
        finally:
            ioc.stop()
