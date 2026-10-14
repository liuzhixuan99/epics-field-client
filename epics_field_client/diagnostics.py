"""Collect bounded local evidence and invoke only fixed, read-only MCP tools."""
from __future__ import annotations

import asyncio
import getpass
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runtime import worker_arguments

CA_KEYS = (
    "EPICS_CA_ADDR_LIST", "EPICS_CA_AUTO_ADDR_LIST", "EPICS_CA_SERVER_PORT",
    "EPICS_CA_REPEATER_PORT", "EPICS_CA_MAX_ARRAY_BYTES",
)
PACKAGES = ("labmcp-epics", "labmcp", "mcp", "fastmcp", "caproto", "numpy")
READ_TOOLS = frozenset({"get_connection_info", "pv_info", "get_pv"})
DEFAULT_CONFIG = {
    "connection_mode": "explicit", "address_list": "", "auto_address": False,
    "server_port": 5064, "repeater_port": 5065, "max_array_bytes": 16384,
    "timeout_s": 3, "pv_name": "", "site": "", "device": "",
    "network_layer": "", "notes": "", "baseline": "", "run_caget": False,
    "collect_network": False,
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def validate_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - set(DEFAULT_CONFIG):
        raise ValueError("配置必须是对象，且只能包含界面支持的字段。")
    config = {**DEFAULT_CONFIG, **raw}
    if config["connection_mode"] not in {"explicit", "inherit"}:
        raise ValueError("连接方式只能是 explicit 或 inherit。")
    for key in ("auto_address", "run_caget", "collect_network"):
        if not isinstance(config[key], bool):
            raise ValueError(f"{key} 必须为布尔值。")
    for key, low, high in (("server_port", 1, 65535), ("repeater_port", 1, 65535),
                           ("max_array_bytes", 1, 16777216)):
        value = config[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} 必须为 {low}–{high} 之间的整数。")
    timeout = config["timeout_s"]
    if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0.5 <= timeout <= 10:
        raise ValueError("读取超时必须为 0.5–10 秒。")
    for key in ("address_list", "pv_name", "site", "device", "network_layer", "notes", "baseline"):
        if not isinstance(config[key], str) or len(config[key]) > (4000 if key in {"notes", "baseline"} else 1024):
            raise ValueError(f"{key} 内容类型或长度不正确。")
        config[key] = config[key].strip()
        if "\x00" in config[key]:
            raise ValueError("配置不允许含空字符。")
    addresses = config["address_list"].replace(",", " ").split()
    for address in addresses:
        match = re.fullmatch(r"([a-zA-Z0-9_.-]+)(?::([0-9]{1,5}))?", address)
        if not match or (match[2] and not 1 <= int(match[2]) <= 65535):
            raise ValueError("地址请填 IPv4／主机名，可带 :端口；多个地址用空格分隔。不要填 URL。")
    config["address_list"] = " ".join(addresses)
    pv = config["pv_name"]
    if pv and (pv.startswith("-") or len(pv) > 255 or any(c.isspace() or ord(c) < 32 or c in "\"'" for c in pv)):
        raise ValueError("PV 名称不允许空白、引号或控制字符，长度不能超过 255。")
    return config


def child_environment(config: dict[str, Any], environ=None) -> dict[str, str]:
    # Inherited LabMCP options can silently enable simulation or safe-state writes.
    env = dict(os.environ if environ is None else environ)
    for key in list(env):
        if key.startswith("LABMCP_") or key.startswith("EPICS_CAS_"):
            env.pop(key)
        elif key.startswith("EPICS_CA_") and (config["connection_mode"] == "explicit" or key not in CA_KEYS):
            env.pop(key)
    if config["connection_mode"] == "explicit":
        env.update({
            "EPICS_CA_ADDR_LIST": config["address_list"],
            "EPICS_CA_AUTO_ADDR_LIST": "YES" if config["auto_address"] else "NO",
            "EPICS_CA_SERVER_PORT": str(config["server_port"]),
            "EPICS_CA_REPEATER_PORT": str(config["repeater_port"]),
            "EPICS_CA_MAX_ARRAY_BYTES": str(config["max_array_bytes"]),
        })
    # Preserve Windows identity: caproto/getpass otherwise falls back to Unix pwd.
    if not any(env.get(k) for k in ("LOGNAME", "USER", "LNAME", "USERNAME")):
        env["USERNAME"] = getpass.getuser()
    env["PYTHONUTF8"] = "1"
    env["FASTMCP_CHECK_FOR_UPDATES"] = "off"
    env["FASTMCP_TELEMETRY_MODE"] = "off"
    return env


def validate_real_environment(config: dict, environ=None):
    source = os.environ if environ is None else environ
    env = child_environment(config, source)
    if config["connection_mode"] == "inherit" and source.get("EPICS_CA_NAME_SERVERS"):
        raise ValueError("当前 LabMCP/caproto 不支持 EPICS_CA_NAME_SERVERS。请向现场同事获取 IOC／CA 网关地址，并改用显式配置。")
    if not env.get("EPICS_CA_ADDR_LIST", "").strip() and env.get("EPICS_CA_AUTO_ADDR_LIST", "YES").upper() not in {"YES", "1", "TRUE"}:
        raise ValueError("没有 CA 搜索地址且已关闭自动搜索，无法安全启动。请提供地址或明确启用自动搜索。")


def command_evidence(command: list[str], *, env=None, timeout=5) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {"command": command}
    try:
        completed = subprocess.run(command, env=env, capture_output=True, timeout=timeout,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        encoding = "gbk" if sys.platform == "win32" else "utf-8"
        result.update(returncode=completed.returncode,
                      stdout=completed.stdout.decode(encoding, errors="replace")[:32000],
                      stderr=completed.stderr.decode(encoding, errors="replace")[:8000])
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["error"] = str(exc)
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    return result


def linux_network_snapshot(evidence: list[dict]) -> dict:
    snapshot = {"interfaces": [], "routes": [], "errors": []}
    for item in evidence:
        if item.get("returncode") != 0:
            snapshot["errors"].append(item.get("error") or item.get("stderr") or "网络命令未成功执行。")
            continue
        try:
            records = json.loads(item["stdout"])
            if not isinstance(records, list):
                raise ValueError("ip 命令没有返回列表。")
            if "address" in item["command"]:
                for row in records:
                    snapshot["interfaces"].append({
                        "name": row.get("ifname"), "state": row.get("operstate"),
                        "addresses": [f"{entry.get('local', '')}/{entry.get('prefixlen', '')}" for entry in row.get("addr_info", [])],
                    })
            else:
                snapshot["routes"] = [{"destination": row.get("dst", "default"),
                                         "gateway": row.get("gateway"), "interface": row.get("dev"),
                                         "source": row.get("prefsrc"), "metric": row.get("metric")} for row in records]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            snapshot["errors"].append(f"网络信息解析失败：{exc}")
    return snapshot


def linux_distribution() -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        values = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines() if "=" in line and not line.startswith("#"))
        return values.get("PRETTY_NAME", values.get("NAME", "Linux")).strip('"')
    except OSError:
        return "Linux（发行版未识别）"


def collect_environment(config: dict[str, Any]) -> dict[str, Any]:
    versions = {}
    for package in PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    result = {
        "collected_at": now(), "client_version": "0.1.0", "system": platform.system(),
        "release": platform.release(), "machine": platform.machine(), "hostname": platform.node(),
        "python": sys.version, "python_executable": sys.executable, "packages": versions,
        "distribution": linux_distribution(), "cpu_count": os.cpu_count(),
        "inherited_ca_environment": {k: os.environ[k] for k in CA_KEYS if k in os.environ},
        "launch_ca_environment": {k: child_environment(config).get(k) for k in CA_KEYS},
        "unsupported_name_server_configured": bool(os.environ.get("EPICS_CA_NAME_SERVERS")),
        "ca_name_servers": os.environ.get("EPICS_CA_NAME_SERVERS"),
        "cli_paths": {k: shutil.which(k) for k in ("caget", "cainfo", "camonitor")},
        "network_details": [],
    }
    if config["collect_network"]:
        if sys.platform == "win32":
            commands = [["ipconfig", "/all"], ["route", "print", "-4"]]
        elif sys.platform == "darwin":
            commands = [["ifconfig"], ["netstat", "-rn", "-f", "inet"]]
        else:
            commands = [["ip", "-j", "address"], ["ip", "-j", "route"]]
        result["network_details"] = [command_evidence(c) for c in commands]
        if sys.platform.startswith("linux"):
            result["network_snapshot"] = linux_network_snapshot(result["network_details"])
    return result


def unpack_result(result) -> dict[str, Any]:
    raw = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    data = raw.get("structuredContent")
    if data is None:
        for block in raw.get("content", []):
            if block.get("type") == "text":
                try:
                    data = json.loads(block["text"])
                    break
                except (ValueError, KeyError):
                    pass
    return {"ok": not raw.get("isError", False), "data": data, "raw": raw}


def exception_summary(exc: BaseException) -> str:
    nested = getattr(exc, "exceptions", None)
    if nested:
        return "; ".join(exception_summary(item) for item in nested)
    return f"{type(exc).__name__}: {str(exc) or '超过等待时间／会话被中止'}"


async def call_read_tool(session, name: str, arguments: dict, timeout: float):
    if name not in READ_TOOLS:
        raise ValueError("第一版仅允许固定的只读工具。")
    return unpack_result(await asyncio.wait_for(session.call_tool(name, arguments), timeout))


async def _mcp_probe(report: dict, config: dict, simulated: bool, log_path: Path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    args = worker_arguments(["--transport", "stdio", "--read-only",
            "--timeout", str(config["timeout_s"]), "--option", "put_allowlist=(?!)"])
    if simulated:
        args.append("--simulate")
    report["launch"] = {"command": sys.executable, "args": args, "transport": "stdio"}
    params = StdioServerParameters(command=sys.executable, args=args, env=child_environment(config))
    with log_path.open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as streams:
            async with ClientSession(*streams, read_timeout_seconds=60) as session:
                report["last_mcp_phase"] = "启动读取服务"
                started = time.monotonic()
                initialized = await asyncio.wait_for(session.initialize(), 60)
                report["stages"].append({"stage": "mcp_initialize", "ok": True,
                                          "server": initialized.server_info.model_dump(mode="json"),
                                          "elapsed_s": round(time.monotonic() - started, 3)})
                report["last_mcp_phase"] = "取得读取工具清单"
                started = time.monotonic()
                tools = await asyncio.wait_for(session.list_tools(), 60)
                report["tool_discovery_elapsed_s"] = round(time.monotonic() - started, 3)
                report["tools"] = [{"name": t.name, "description": t.description,
                                     "input_schema": t.input_schema,
                                     "client_enabled": t.name in READ_TOOLS} for t in tools.tools]
                # No generic tool-call API: even SAFETY tools may write despite --read-only.
                calls = [("get_connection_info", {}), ("pv_info", {"name": report["tested_pv"]}),
                         ("get_pv", {"name": report["tested_pv"], "max_elements": 100})]
                for name, arguments in calls:
                    report["last_mcp_phase"] = name
                    started = time.monotonic()
                    try:
                        value = await call_read_tool(session, name, arguments, config["timeout_s"] + 8)
                        report["stages"].append({"stage": name, **value,
                                                  "elapsed_s": round(time.monotonic() - started, 3)})
                        if name == "get_connection_info" and isinstance(value.get("data"), dict):
                            instrument = value["data"].get("instrument", {})
                            if isinstance(instrument, dict):
                                report["runtime_ca_environment"] = instrument.get("ca_environment")
                    except Exception as exc:
                        report["stages"].append({"stage": name, "ok": False,
                                                  "error": f"{type(exc).__name__}: {exc}",
                                                  "traceback": "".join(traceback.format_exception(exc)),
                                                  "elapsed_s": round(time.monotonic() - started, 3)})


def conclusion(report: dict) -> dict:
    readings = [s for s in report["stages"] if s["stage"] == "get_pv"]
    infos = [s for s in report["stages"] if s["stage"] == "get_connection_info"]
    successful = bool(readings and readings[0].get("ok") and
                      isinstance(readings[0].get("data"), dict) and "value" in readings[0]["data"])
    identity = infos[0].get("data") if infos else None
    verified_real = isinstance(identity, dict) and identity.get("simulated") is False
    simulated = report["test_kind"] == "simulation" or (isinstance(identity, dict) and identity.get("simulated") is True)
    if successful and simulated:
        status, text = "simulation_passed", "本机仿真读取通过；未验证现场设备或跨网段连接。"
    elif successful and verified_real:
        status, text = "real_pv_read", "已通过真实连接读取指定 PV；请与现场 caget／基准读数核对。"
    elif successful:
        status, text = "identity_unverified", "读取有结果，但真实／仿真身份未核实，不能判定现场接入通过。"
    elif report["test_kind"] == "environment":
        status, text = "environment_only", "已收集本机环境；未发起设备读取。"
    else:
        status, text = "failed", "未取得有效 PV 读数；请查看各阶段结果，不能据此直接断定网络故障。"
    return {"status": status, "message": text, "real_connection_verified": successful and verified_real and not simulated,
            "baseline_verified": False}


def run_probe(config: dict, kind: str, log_path: Path) -> dict:
    config = validate_config(config)
    if kind not in {"environment", "simulation", "real"}:
        raise ValueError("未知测试类型。")
    if kind == "real" and not config["pv_name"]:
        raise ValueError("真实测试需要填写现场提供的 PV 名称。")
    if kind == "real" and config["connection_mode"] == "explicit" and not config["address_list"] and not config["auto_address"]:
        raise ValueError("请填写 IOC／网关地址，或明确启用自动搜索。")
    if kind == "real":
        validate_real_environment(config)
    report = {"schema_version": 1, "started_at": now(), "test_kind": kind,
              "config": config, "tested_pv": "SIM:TEMP" if kind == "simulation" else config["pv_name"],
              "environment": collect_environment(config), "stages": [],
              "scope": "本地、无模型、CA、固定只读工具；不扫描设备、不执行写入。"}
    report["environment"]["ca_configuration_note"] = (
        "launch_ca_environment 是传入子进程的配置；仿真模式会将搜索地址替换为随机回环端口。实际连接信息见 get_connection_info 和 pv_info。"
        if kind == "simulation" else
        "launch_ca_environment 是传入子进程的配置；null 表示未设置，由驱动取默认值。实际 PV 服务地址见 pv_info。"
    )
    if kind != "environment":
        try:
            asyncio.run(asyncio.wait_for(_mcp_probe(report, config, kind == "simulation", log_path), 180))
        except Exception as exc:
            report["stages"].append({"stage": "mcp_session", "ok": False,
                                      "error": f"{report.get('last_mcp_phase', '读取会话')}：{exception_summary(exc)}",
                                      "traceback": "".join(traceback.format_exception(exc))})
        if log_path.exists():
            report["mcp_stderr"] = log_path.read_text(encoding="utf-8", errors="replace")[-16000:]
        if kind == "real" and config["run_caget"]:
            caget = report["environment"]["cli_paths"]["caget"]
            report["caget_baseline"] = command_evidence(
                [caget, "-w", str(config["timeout_s"]), config["pv_name"]],
                env=child_environment(config), timeout=config["timeout_s"] + 3,
            ) if caget else {"skipped": True, "reason": "本机未找到 caget；无需安装 CLI 也可进行 LabMCP 测试。"}
    report["conclusion"] = conclusion(report)
    report["finished_at"] = now()
    return report


def report_markdown(report: dict) -> str:
    summary = report["conclusion"]["message"]
    environment = report["environment"]
    def cell(value):
        if value is None or value == "":
            return "未检测到"
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    rows = [("系统", environment.get("distribution") or environment.get("system")),
            ("内核／系统版本", environment.get("release")), ("架构", environment.get("machine")),
            ("主机名", environment.get("hostname")), ("Python", environment.get("python")),
            ("Python 路径", environment.get("python_executable")),
            ("caget", environment.get("cli_paths", {}).get("caget"))]
    rows.extend((key, value) for key, value in environment.get("packages", {}).items())
    env_table = "\n".join(f"| {cell(k)} | {cell(v)} |" for k, v in rows)
    ca_table = "\n".join(f"| {cell(k)} | {cell(v)} |" for k, v in environment.get("inherited_ca_environment", {}).items()) or "| EPICS 环境变量 | 未检测到 |"
    network = environment.get("network_snapshot", {})
    net_table = "\n".join(f"| {cell(row.get('name'))} | {cell(row.get('state'))} | {cell(', '.join(row.get('addresses', [])))} |" for row in network.get("interfaces", [])) or "| 网卡 | — | 请查看完整证据中的本机网络输出 |"
    reading = next((s.get("data") for s in report["stages"] if s["stage"] == "get_pv" and s.get("ok")), None)
    read_summary = f"\n读取结果：`{cell(reading.get('value'))}` {cell(reading.get('units'))}；告警：{cell(reading.get('severity'))}。\n" if isinstance(reading, dict) else ""
    return (f"# EPICS 现场测试报告\n\n{summary}\n\n"
            f"- 测试类型：{report['test_kind']}\n- 时间（UTC）：{report['started_at']}\n"
            f"- 测试 PV：{report['tested_pv']}\n"
            "- 读数是否与现场基准一致：待现场人员核对，程序不自动判定。\n"
            "- 本报告可能包含内部地址、PV 名称、主机名和设备读数；分享前由现场人员检查。\n\n"
            + "## 电脑环境\n\n| 项目 | 检测结果 |\n| --- | --- |\n" + env_table + "\n\n"
            + "## 检测到的 EPICS 配置\n\n| 项目 | 检测结果 |\n| --- | --- |\n" + ca_table + "\n\n"
            + "## 网络信息\n\n| 网卡 | 状态 | 地址 |\n| --- | --- | --- |\n" + net_table + "\n" + read_summary + "\n"
            + "## 完整证据\n\n```json\n" + json.dumps(report, ensure_ascii=False, indent=2).replace("```", "` ` `") + "\n```\n")
