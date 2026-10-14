"""Loopback-only local UI, configuration storage and diagnostic reports."""
from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
import signal
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .diagnostics import CA_KEYS, DEFAULT_CONFIG, exception_summary, report_markdown, run_probe, validate_config, validate_real_environment
from .tool_session import ToolSession
from .runtime import default_data_dir, own_child_processes, open_browser

WEB = Path(__file__).with_name("web")


def write_json(path: Path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class FieldApp:
    def __init__(self, data_dir: Path, *, write_pvs=()):
        self.data_dir = data_dir
        self.reports_dir = data_dir / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.jobs = {}
        self.active_job = None
        self.workers = []
        self.tool_session = ToolSession(self.reports_dir / "manual.stderr.log", write_pvs=write_pvs)

    def config(self):
        path = self.data_dir / "config.json"
        if path.exists():
            return validate_config(json.loads(path.read_text(encoding="utf-8")))
        return dict(DEFAULT_CONFIG)

    def save_config(self, value):
        config = validate_config(value)
        with self.lock:
            write_json(self.data_dir / "config.json", config)
        return config

    def start_job(self, config, kind):
        config = validate_config(config)
        if kind not in {"environment", "simulation", "real"}:
            raise ValueError("未知测试类型。")
        if kind == "real" and not config["pv_name"]:
            raise ValueError("请先填写现场提供的 PV 名称。")
        if kind == "real" and config["connection_mode"] == "explicit" and not config["address_list"] and not config["auto_address"]:
            raise ValueError("请填写 IOC／网关地址，或明确启用自动搜索。")
        if kind == "real":
            validate_real_environment(config)
        with self.lock:
            if self.active_job:
                raise RuntimeError("已有测试正在运行，请等待完成后再试。")
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {"id": job_id, "state": "running", "test_kind": kind}
            self.active_job = job_id

        def work():
            try:
                report = run_probe(config, kind, self.reports_dir / f"{job_id}.stderr.log")
                write_json(self.reports_dir / f"{job_id}.json", report)
                (self.reports_dir / f"{job_id}.md").write_text(report_markdown(report), encoding="utf-8")
                outcome = {"id": job_id, "state": "done", "report": report}
            except Exception as exc:
                outcome = {"id": job_id, "state": "error", "error": f"{type(exc).__name__}: {exc}"}
            with self.lock:
                self.jobs[job_id] = outcome
                self.active_job = None
                # Keep bounded UI history; persisted evidence remains on disk.
                for old_id in list(self.jobs)[:-30]:
                    self.jobs.pop(old_id)
        worker = threading.Thread(target=work, name="field-probe", daemon=True)
        self.workers = [w for w in self.workers if w.is_alive()]
        self.workers.append(worker)
        worker.start()
        return {"id": job_id, "state": "running"}

    def start_tool_job(self, action, payload):
        if not isinstance(payload, dict):
            raise ValueError("工具请求必须是对象。")
        if action == "connect":
            if set(payload) != {"config"}:
                raise ValueError("连接请求格式不正确。")
            value = validate_config(payload["config"])
        elif action == "call":
            if set(payload) != {"catalog_id", "name", "arguments", "confirmed"}:
                raise ValueError("工具调用格式不正确。")
            if not isinstance(payload["name"], str) or not isinstance(payload["catalog_id"], str) or type(payload["confirmed"]) is not bool:
                raise ValueError("工具名称、连接编号或确认字段不正确。")
            value = payload
        else:
            raise ValueError("未知工具操作。")
        with self.lock:
            if self.active_job:
                raise RuntimeError("已有操作正在运行，请等待完成。")
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {"id": job_id, "state": "running", "test_kind": "tool_" + action}
            self.active_job = job_id

        def work():
            try:
                result = self.tool_session.request(action, value)
                if action == "call":
                    write_json(self.reports_dir / f"{job_id}.json", result)
                outcome = {"id": job_id, "state": "done", "result": result}
            except Exception as exc:
                outcome = {"id": job_id, "state": "error", "error": exception_summary(exc)}
            with self.lock:
                self.jobs[job_id] = outcome
                self.active_job = None
                for old_id in list(self.jobs)[:-30]:
                    self.jobs.pop(old_id)

        worker = threading.Thread(target=work, name="manual-tool", daemon=True)
        self.workers = [w for w in self.workers if w.is_alive()]
        self.workers.append(worker)
        worker.start()
        return {"id": job_id, "state": "running"}


def make_server(app: FieldApp, port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, content, content_type="application/json; charset=utf-8", filename=None):
            if not isinstance(content, bytes):
                content = json.dumps(content, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(content)

        def allowed(self, api=False):
            expected_host = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            if self.headers.get("Host") != expected_host or (origin and origin != f"http://{expected_host}"):
                self.send(403, {"error": "只接受本地页面的请求。"})
                return False
            if api and not secrets.compare_digest(self.headers.get("X-Field-Token", ""), app.token):
                self.send(403, {"error": "缺少本地会话凭据，请刷新页面。"})
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self.allowed(api=path.startswith("/api/")):
                return
            if path == "/":
                page = (WEB / "index.html").read_text(encoding="utf-8").replace("__FIELD_TOKEN__", app.token)
                self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif path in {"/app.js", "/style.css"}:
                mime = "text/javascript" if path.endswith(".js") else "text/css"
                self.send(200, (WEB / path[1:]).read_bytes(), mime + "; charset=utf-8")
            elif path == "/api/config":
                try:
                    self.send(200, app.config())
                except (ValueError, OSError) as exc:
                    self.send(400, {"error": f"配置文件无法读取：{exc}"})
            elif path == "/api/bootstrap":
                import os
                try:
                    saved = app.config()
                    inherited = {key: os.environ[key] for key in CA_KEYS if key in os.environ}
                    self.send(200, {"defaults": DEFAULT_CONFIG, "saved_config": saved,
                                    "detected_ca_environment": inherited,
                                    "name_server_configured": bool(os.environ.get("EPICS_CA_NAME_SERVERS")),
                                    "write_pvs": list(app.tool_session.write_pvs)})
                except (ValueError, OSError) as exc:
                    self.send(400, {"error": f"无法读取连接信息：{exc}"})
            elif path.startswith("/api/jobs/"):
                job_id = path.removeprefix("/api/jobs/")
                with app.lock:
                    job = app.jobs.get(job_id)
                self.send(200 if job else 404, job or {"error": "找不到测试记录。"})
            elif path.startswith("/api/reports/"):
                filename = path.removeprefix("/api/reports/")
                import re
                if not re.fullmatch(r"[0-9a-f]{32}\.(json|md)", filename):
                    self.send(404, {"error": "无效报告名称。"})
                    return
                with app.lock:
                    job = app.jobs.get(filename.split(".")[0])
                if not job or job["state"] != "done":
                    self.send(404, {"error": "只能导出本次运行生成的报告。"})
                    return
                report_path = app.reports_dir / filename
                if not report_path.is_file():
                    self.send(404, {"error": "报告尚未生成。"})
                    return
                mime = "application/json" if filename.endswith(".json") else "text/markdown"
                self.send(200, report_path.read_bytes(), mime + "; charset=utf-8", filename)
            else:
                self.send(404, {"error": "找不到页面。"})

        def do_POST(self):
            if not self.allowed(api=True):
                return
            try:
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("请求必须使用 JSON。")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32768:
                    raise ValueError("请求大小不正确。")
                body = json.loads(self.rfile.read(size))
                path = urlsplit(self.path).path
                if path == "/api/config":
                    self.send(200, app.save_config(body))
                elif path == "/api/jobs":
                    if not isinstance(body, dict) or set(body) != {"config", "kind"}:
                        raise ValueError("测试请求格式不正确。")
                    self.send(202, app.start_job(body["config"], body["kind"]))
                elif path in {"/api/tools/connect", "/api/tools/call"}:
                    self.send(202, app.start_tool_job(path.rsplit("/", 1)[1], body))
                else:
                    self.send(404, {"error": "找不到操作。"})
            except RuntimeError as exc:
                self.send(409, {"error": str(exc)})
            except (ValueError, TypeError, OSError) as exc:
                self.send(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    own_child_processes()
    parser = argparse.ArgumentParser(description="EPICS 现场测试客户端：本地、无模型、只读。")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--write-pv", action="append", default=[], metavar="PV", help="允许写入的精确 PV 名称，可重复；未指定时只读")
    args = parser.parse_args()
    app = FieldApp(args.data_dir.resolve(), write_pvs=args.write_pv)
    try:
        server = make_server(app, args.port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE and args.port == 8765:
            server = make_server(app, 0)
        else:
            parser.exit(1, f"无法启动本地页面：{exc}。可用 --port 更换端口。\n")

    def stop(signum, *_):
        if signum == getattr(signal, "SIGHUP", None):
            # A closed PTY rejects even the final status message with EIO.
            sink = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(sink, 1)
                os.dup2(sink, 2)
            finally:
                os.close(sink)
        raise KeyboardInterrupt

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"EPICS 现场测试客户端：{url}\n配置与报告：{app.data_dir}\n关闭此终端或按 Ctrl+C，检测服务会自动停止。", flush=True)
    try:
        if not args.no_browser:
            if not open_browser(url):
                print(f"未能自动打开浏览器，请打开：{url}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Terminal close must stop promptly, even during a slow device operation.
        deadline = threading.Timer(5, lambda: os._exit(0))
        deadline.daemon = True
        deadline.start()
        server.server_close()
        if app.active_job:
            print("等待当前只读测试退出并关闭 LabMCP…", flush=True)
        for worker in app.workers:
            worker.join()
        app.tool_session.close()
        deadline.cancel()
        print("检测服务已停止。", flush=True)


if __name__ == "__main__":
    main()
