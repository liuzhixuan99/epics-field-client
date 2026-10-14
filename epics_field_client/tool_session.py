"""Persistent local LabMCP session with schema validation and explicit PV write limits."""
from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import uuid
from concurrent.futures import Future
from contextlib import AsyncExitStack
from pathlib import Path

from .diagnostics import child_environment, exception_summary, unpack_result, validate_config, validate_real_environment
from .runtime import worker_arguments

QUERY_TOOLS = frozenset({"get_connection_info", "get_command_log", "get_pv", "get_pvs", "pv_info", "monitor_pv", "reconnect"})
WRITE_TOOLS = frozenset({"put_pv", "put_pvs"})


def validate_arguments(tool, arguments, *, write_pvs=(), confirmed=False):
    from jsonschema import Draft202012Validator

    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是 JSON 对象。")
    json.dumps(arguments, allow_nan=False)
    errors = list(Draft202012Validator(tool["input_schema"]).iter_errors(arguments))
    if errors:
        error = errors[0]
        path = ".".join(str(x) for x in error.absolute_path) or "参数"
        raise ValueError(f"{path}：{error.message}")
    name = tool["name"]
    if name not in QUERY_TOOLS | WRITE_TOOLS:
        raise ValueError("此工具未开放执行；安全动作需要由现场控制系统单独配置。")
    if name in WRITE_TOOLS:
        if not write_pvs:
            raise ValueError("当前只读模式不允许写入。")
        if confirmed is not True:
            raise ValueError("请明确确认本次设备写入。")
        names = [arguments["name"]] if name == "put_pv" else [row["name"] for row in arguments["writes"]]
        if any(pv not in write_pvs for pv in names):
            raise ValueError("写入 PV 不在启动时指定的允许清单中。")
        if len(names) > 10 or arguments.get("timeout_s", 10) > 60:
            raise ValueError("写入最多 10 个 PV，每项等待最多 60 秒。")
    if name == "monitor_pv" and arguments.get("duration_s", 5) > 60:
        raise ValueError("单次监测最多 60 秒。")
    return arguments


class ToolSession:
    def __init__(self, log_path: Path, *, write_pvs=()):
        self.log_path = log_path
        self.write_pvs = tuple(dict.fromkeys(write_pvs))
        for name in self.write_pvs:
            validate_config({"pv_name": name})
            if not name:
                raise ValueError("允许写入的 PV 名称不能为空。")
        self.start_lock = threading.Lock()
        self.ready = threading.Event()
        self.thread = None
        self.closed = False
        self.catalog = None
        self.pending = {}

    def request(self, action, payload):
        with self.start_lock:
            if self.closed:
                raise RuntimeError("工具连接已关闭。")
            if self.thread is None or not self.thread.is_alive():
                self.ready.clear()
                self.thread = threading.Thread(target=self._run, name="labmcp-session", daemon=True)
                self.thread.start()
            future = Future()
            self.pending[future] = (action, payload)
        if not self.ready.wait(5):
            with self.start_lock:
                self.pending.pop(future, None)
            raise RuntimeError("工具线程无法启动。")
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, (action, payload, future))
            return future.result()
        finally:
            with self.start_lock:
                self.pending.pop(future, None)

    def close(self):
        with self.start_lock:
            self.closed = True
            thread = self.thread
        if thread and thread.is_alive():
            self.ready.wait()
            future = Future()
            self.loop.call_soon_threadsafe(self.queue.put_nowait, ("close", {}, future))
            thread.join()

    def _run(self):
        message = "工具会话已关闭，请重新加载连接。"
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            message = "工具会话中断，请重新加载连接：" + exception_summary(exc)
        finally:
            connection = self.catalog.get("connection") if self.catalog else None
            self.catalog = None
            with self.start_lock:
                waiting = list(self.pending.items())
            for future, (action, payload) in waiting:
                if not future.done():
                    if action == "call" and payload.get("name") in WRITE_TOOLS:
                        future.set_result({"ok": False, "tool": payload["name"], "arguments": payload.get("arguments"),
                                           "connection": connection, "write": True, "outcome_uncertain": True,
                                           "connection_lost": True, "error": message})
                    else:
                        future.set_exception(RuntimeError(message))

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.ready.set()
        self.catalog = None
        stack = AsyncExitStack()
        session = None
        connected_config = None
        try:
            while True:
                action, payload, future = await self.queue.get()
                if action == "close":
                    break
                try:
                    if action == "connect":
                        config = validate_config(payload)
                        validate_real_environment(config)
                        if not config["pv_name"]:
                            raise ValueError("请先填写已知可读的 PV。")
                        if session is None or config != connected_config:
                            await stack.aclose()
                            stack = AsyncExitStack()
                            session = None
                            self.catalog = None
                            from mcp import ClientSession, StdioServerParameters
                            from mcp.client.stdio import stdio_client

                            allowlist = "(?:" + "|".join(re.escape(pv) for pv in self.write_pvs) + ")" if self.write_pvs else "(?!)"
                            args = worker_arguments(["--transport", "stdio", "--timeout", str(config["timeout_s"]),
                                    "--option", f"put_allowlist={allowlist}"])
                            if not self.write_pvs:
                                args.append("--read-only")
                            log = stack.enter_context(self.log_path.open("a", encoding="utf-8"))
                            params = StdioServerParameters(command=sys.executable, args=args, env=child_environment(config))
                            streams = await stack.enter_async_context(stdio_client(params, errlog=log))
                            session = await stack.enter_async_context(ClientSession(*streams, read_timeout_seconds=60))
                            await asyncio.wait_for(session.initialize(), 60)
                            identity = unpack_result(await session.call_tool("get_connection_info", {}))
                            info = unpack_result(await session.call_tool("pv_info", {"name": config["pv_name"]}))
                            if not identity["ok"] or not info["ok"] or not info.get("data", {}).get("connected"):
                                raise ValueError("未确认指定 PV 连接，请先检查设备地址和 PV。")
                            self.catalog = {"id": uuid.uuid4().hex, "config": config,
                                            "connection": info["data"], "identity": identity.get("data"),
                                            "write_pvs": list(self.write_pvs)}
                            connected_config = config
                        listed = await asyncio.wait_for(session.list_tools(), 60)
                        self.catalog["tools"] = [self._descriptor(tool) for tool in listed.tools]
                        result = self.catalog
                    elif action == "call":
                        if not self.catalog or payload.get("catalog_id") != self.catalog["id"]:
                            raise ValueError("连接已变化或尚未建立，请重新加载工具。")
                        tool = next((t for t in self.catalog["tools"] if t["name"] == payload.get("name")), None)
                        if not tool or not tool["enabled"]:
                            raise ValueError("当前连接不允许执行此工具。")
                        arguments = validate_arguments(tool, payload.get("arguments"), write_pvs=self.write_pvs,
                                                       confirmed=payload.get("confirmed", False))
                        if tool["name"] in WRITE_TOOLS:
                            timeout = 3710
                        elif tool["name"] == "monitor_pv":
                            timeout = arguments.get("duration_s", 5) + connected_config["timeout_s"] + 15
                        elif tool["name"] == "get_pvs":
                            timeout = len(arguments["names"]) * connected_config["timeout_s"] + 30
                        else:
                            timeout = connected_config["timeout_s"] + 15
                        connection = self.catalog["connection"]
                        try:
                            reply = await session.call_tool(tool["name"], arguments, read_timeout_seconds=timeout)
                            result = unpack_result(reply)
                            if tool["name"] == "reconnect" and result["ok"]:
                                identity = unpack_result(await session.call_tool("get_connection_info", {}))
                                info = unpack_result(await session.call_tool("pv_info", {"name": connected_config["pv_name"]}))
                                if not identity["ok"] or not info["ok"] or not info.get("data", {}).get("connected"):
                                    raise ValueError("重连后无法核验目标 PV，请重新加载工具。")
                                self.catalog = {**self.catalog, "id": uuid.uuid4().hex, "connection": info["data"], "identity": identity.get("data")}
                                connection = self.catalog["connection"]
                                result["catalog"] = self.catalog
                        except Exception as exc:
                            result = {"ok": False, "error": exception_summary(exc),
                                      "outcome_uncertain": tool["name"] in WRITE_TOOLS, "connection_lost": True}
                            self.catalog = None
                            session = None
                            connected_config = None
                            try:
                                await stack.aclose()
                            except Exception as cleanup:
                                result["error"] += "; " + exception_summary(cleanup)
                            stack = AsyncExitStack()
                        result = {"tool": tool["name"], "arguments": arguments, "connection": connection,
                                  "write": tool["name"] in WRITE_TOOLS, **result}
                    else:
                        raise ValueError("未知工具操作。")
                    future.set_result(result)
                except Exception as exc:
                    if action == "connect":
                        self.catalog = None
                        session = None
                        connected_config = None
                        await stack.aclose()
                        stack = AsyncExitStack()
                    future.set_exception(ValueError(exception_summary(exc)))
        finally:
            await stack.aclose()

    def _descriptor(self, tool):
        enabled = tool.name in QUERY_TOOLS or (tool.name in WRITE_TOOLS and bool(self.write_pvs))
        return {"name": tool.name, "description": tool.description or "", "input_schema": tool.input_schema,
                "annotations": tool.annotations.model_dump(mode="json") if tool.annotations else {},
                "enabled": enabled, "write": tool.name in WRITE_TOOLS or tool.name == "apply_safe_state",
                "disabled_reason": ("未配置现场安全动作，此工具不开放执行。" if tool.name == "apply_safe_state"
                                    else "当前版本未支持此工具执行。") if not enabled else ""}
