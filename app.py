import bootstrap
bootstrap.configure_runtime()

import argparse
import copy
import importlib.metadata
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from bootstrap import ROOT, RUNTIME
from browser import BrowserWorker
from reports import parse_report, build_html
from settings import DEFAULT_TARGET, PROBES, validate_target, validate_scan

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class Console:
    def __init__(self, headless=False, port=8787, storage=None):
        self.port = port
        self.storage = Path(storage) if storage else RUNTIME
        self.storage.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.browser = BrowserWorker(self.storage / "browser-profile", headless=headless)
        self.lock = threading.RLock()
        self.operations = threading.Lock()
        self.jobs = {}
        self.active = None
        self.process = None
        self.bridge_tokens = {}
        self.runs = self.storage / "runs"
        self.runs.mkdir(parents=True, exist_ok=True)
        try:
            self.garak_version = importlib.metadata.version("garak")
        except importlib.metadata.PackageNotFoundError:
            self.garak_version = None
        self.saved_target = DEFAULT_TARGET.copy()
        target_file = self.storage / "target.json"
        if target_file.exists():
            try:
                self.saved_target.update(json.loads(target_file.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass
        for path in self.runs.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job["status"] not in TERMINAL:
                    job.update(status="interrupted", error="上次服务退出时检测未完成", finished_at=now())
                    atomic_json(path, job)
                self.jobs[job["id"]] = job
            except (ValueError, OSError, KeyError):
                continue

    def state(self):
        with self.lock:
            return {"garak": {"version": self.garak_version, "python": sys.executable},
                    "browser": self.browser.status(), "active_job": self.active,
                    "jobs": [self.public(j) for j in sorted(self.jobs.values(), key=lambda x: x["created_at"], reverse=True)[:50]],
                    "target": self.saved_target.copy(), "probes": PROBES}

    @staticmethod
    def public(job):
        return copy.deepcopy(job)

    def _save(self, job):
        with self.lock:
            atomic_json(self.runs / job["id"] / "job.json", job)

    def browser_action(self, action, *args):
        if not self.operations.acquire(blocking=False):
            raise ValueError("另一个浏览器操作正在进行，请稍候")
        try:
            with self.lock:
                if self.active:
                    raise ValueError("检测期间无法更改浏览器，请先停止检测")
            if action == "generate":
                self.browser.cancel.clear()
            return self.browser.call(action, *args)
        finally:
            self.operations.release()

    def save_target(self, target):
        target = validate_target(target)
        with self.lock:
            self.saved_target = target
            atomic_json(self.storage / "target.json", target)
        return target

    def start(self, data):
        if self.garak_version is None:
            raise ValueError("当前 Python 未安装 garak；请使用 Start.ps1 指定本机 garak 环境")
        config = validate_scan(data)
        if not self.operations.acquire(blocking=False):
            raise ValueError("浏览器正在操作，请稍候")
        try:
            with self.lock:
                if self.active:
                    raise ValueError("已有检测正在运行")
            if self.browser.target is None or any(self.browser.target.get(k) != config["target"][k]
                    for k in ("url", "channel", "browser_mode")):
                raise ValueError("请先为当前网址打开登录浏览器")
            inspection = self.browser.call("inspect", config["target"])
            if not inspection["selectors"]["input_selector"]["visible"]:
                raise ValueError("未找到输入框，请完成登录并检查选择器")
            for key, label in [("send_selector", "发送按钮"), ("new_chat_selector", "新对话按钮")]:
                required = bool(config["target"][key]) and (key != "new_chat_selector" or config["target"]["reset_each_prompt"])
                if required and not inspection["selectors"][key]["visible"]:
                    raise ValueError(f"未找到{label}，请检查选择器")
            job_id = str(uuid.uuid4())
            directory = self.runs / job_id
            directory.mkdir()
            # A local fixture is explicitly identified in every job and report.
            parts = urlsplit(config["target"]["url"])
            demo = parts.hostname in {"127.0.0.1", "localhost"} and parts.port == self.port and parts.path == "/demo"
            job = {"id": job_id, "status": "preparing", "created_at": now(), "finished_at": None,
                   "config": config, "garak_version": self.garak_version, "demo": demo,
                   "actual_page_url": inspection["browser"]["url"], "sent": 0, "answered": 0, "errors": 0,
                   "current_probe": "", "plans": [], "error": "", "logs": [], "cancel_requested": False}
            with self.lock:
                self.jobs[job_id] = job
                self.active = job_id
                self.bridge_tokens[job_id] = secrets.token_urlsafe(32)
                self.browser.cancel.clear()
                self._save(job)
            self.save_target(config["target"])
            thread = threading.Thread(target=self._run, args=(job_id,), daemon=True, name="garak-runner")
            thread.start()
            return self.public(job)
        finally:
            self.operations.release()

    def _run(self, job_id):
        job = self.jobs[job_id]
        directory = self.runs / job_id
        report_path = directory / "garak.report.jsonl"
        try:
            config_path = directory / "runner-config.json"
            yaml_path = directory / "garak.yaml"
            yaml_path.write_text(
                "run:\n  soft_probe_prompt_cap: " + str(job["config"]["max_per_probe"]) +
                "\nplugins:\n  extended_detectors: false\nreporting:\n  bootstrap_min_sample_size: 999999\n",
                encoding="utf-8")
            runner_config = {k: job["config"][k] for k in ["probes", "max_per_probe"]}
            runner_config.update(report_prefix=str(directory / "garak"), garak_config=str(yaml_path))
            atomic_json(config_path, runner_config)
            env = os.environ.copy()
            env.update(WG_BRIDGE_URL=f"http://127.0.0.1:{self.port}/api/bridge/{job_id}",
                       WG_BRIDGE_TOKEN=self.bridge_tokens[job_id],
                       WG_BRIDGE_TIMEOUT=str(job["config"]["target"]["timeout_s"] + 90),
                       PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            with self.lock:
                if job["cancel_requested"]:
                    job["status"] = "cancelled"
                    return
                self.process = subprocess.Popen([sys.executable, "-u", str(ROOT / "runner.py"), str(config_path)],
                    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                process = self.process
                job["status"] = "running"
                self._save(job)
            with (directory / "console.log").open("w", encoding="utf-8") as log:
                for line in process.stdout:
                    clean = ANSI.sub("", line).strip()
                    log.write(clean + "\n")
                    log.flush()
                    with self.lock:
                        if clean.startswith("WG_EVENT "):
                            try:
                                event = json.loads(clean[len("WG_EVENT "):])
                                if event["type"] == "probe_start":
                                    job["current_probe"] = event["probe"]
                                    job["plans"].append(event)
                            except (ValueError, KeyError):
                                pass
                        elif clean:
                            job["logs"].append(clean[:1800])
                            job["logs"] = job["logs"][-100:]
                        self._save(job)
            code = process.wait()
            report = parse_report(report_path)
            covered = {r["probe"] for r in report["attempts"] if r["evaluated"]}
            with self.lock:
                if job["cancel_requested"]:
                    job["status"] = "cancelled"
                elif code == 0 and report["completed"] and covered.issuperset(job["config"]["probes"]) and not report["summary"]["unevaluated"]:
                    job["status"] = "completed"
                else:
                    job["status"] = "failed"
                    if not job["error"]:
                        missing = set(job["config"]["probes"]) - covered
                        job["error"] = "部分检测未完成：" + ", ".join(sorted(missing)) if missing else f"garak 未正常完成（退出码 {code}）"
        except Exception as exc:
            with self.lock:
                job.update(status="cancelled" if job["cancel_requested"] else "failed", error=str(exc)[:2000])
        finally:
            if job["cancel_requested"]:
                try:
                    self.browser.call("idle", timeout=20)
                except Exception:
                    pass
            with self.lock:
                job["finished_at"] = now()
                self.process = None
                # Finalize artifacts before releasing the active-run reservation.
                report = parse_report(report_path)
                snapshot = self.public(job)
                atomic_json(directory / "summary.json", {"job": snapshot, **report})
                (directory / "report.html").write_text(build_html(snapshot, report), encoding="utf-8")
                self._save(job)
                self.bridge_tokens.pop(job_id, None)
                self.active = None

    def stop(self, job_id):
        with self.lock:
            if self.active != job_id:
                raise ValueError("该检测已结束")
            job = self.jobs[job_id]
            job.update(cancel_requested=True, status="stopping")
            self.browser.cancel.set()
            if self.process and self.process.poll() is None:
                self.process.terminate()
            self._save(job)
            return self.public(job)

    def generate(self, job_id, prompt):
        with self.lock:
            if self.active != job_id or self.jobs[job_id]["cancel_requested"]:
                raise ValueError("检测已停止或不存在")
            job = self.jobs[job_id]
            job["sent"] += 1
            self._save(job)
        try:
            response = self.browser.call("generate", job["config"]["target"], prompt,
                                         timeout=job["config"]["target"]["timeout_s"] + 60)
            with self.lock:
                job["answered"] += 1
                self._save(job)
            return {"response": response}
        except Exception as exc:
            with self.lock:
                if not job["cancel_requested"]:
                    job["errors"] += 1
                    job["error"] = str(exc)[:2000]
                self._save(job)
            raise

    def job_detail(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError("检测任务不存在")
            job = self.public(self.jobs[job_id])
        return {"job": job, **parse_report(self.runs / job_id / "garak.report.jsonl")}


class Handler(BaseHTTPRequestHandler):
    server_version = "WebGuard/1.0"

    @property
    def console(self):
        return self.server.console

    def log_message(self, fmt, *args):
        pass

    def reply(self, code, value, content_type="application/json; charset=utf-8", download=None):
        if isinstance(value, (dict, list)) or value is None:
            value = json.dumps(value, ensure_ascii=False).encode("utf-8")
        elif isinstance(value, str):
            value = value.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(value)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        try:
            self.end_headers()
            self.wfile.write(value)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def trusted(self):
        port = self.console.port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            self.reply(403, {"error": "仅允许本机访问"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in allowed}:
            self.reply(403, {"error": "不允许跨站请求"})
            return False
        return True

    def authorized(self):
        provided = self.headers.get("X-Console-Token", "")
        if not secrets.compare_digest(provided, self.console.token):
            self.reply(403, {"error": "请从本机控制台操作"})
            return False
        return True

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 2_000_000:
            raise ValueError("请求为空或过大")
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError("请求必须使用 JSON")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("请求必须是 JSON 对象")
        return data

    def do_GET(self):
        if not self.trusted():
            return
        path = urlsplit(self.path).path
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/demo": ("demo.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path in assets:
            name, kind = assets[path]
            body = (ROOT / "web" / name).read_text(encoding="utf-8")
            body = body.replace("__CONSOLE_TOKEN__", self.console.token)
            self.reply(200, body, kind)
            return
        if not self.authorized():
            return
        try:
            if path == "/api/state":
                self.reply(200, self.console.state())
            elif match := re.fullmatch(r"/api/jobs/([a-f0-9-]{36})(?:/download/(html|json|jsonl|log))?", path):
                job_id, kind = match.groups()
                if not kind:
                    self.reply(200, self.console.job_detail(job_id))
                    return
                if job_id not in self.console.jobs:
                    raise ValueError("任务不存在")
                filename, content_type = {
                    "html": ("report.html", "text/html; charset=utf-8"),
                    "json": ("summary.json", "application/json; charset=utf-8"),
                    "jsonl": ("garak.report.jsonl", "application/x-ndjson; charset=utf-8"),
                    "log": ("console.log", "text/plain; charset=utf-8"),
                }[kind]
                file = self.console.runs / job_id / filename
                if not file.exists():
                    raise ValueError("文件尚未生成，请等检测结束")
                self.reply(200, file.read_bytes(), content_type, f"webguard-{job_id[:8]}.{kind if kind != 'log' else 'txt'}")
            else:
                self.reply(404, {"error": "页面不存在"})
        except (ValueError, OSError) as exc:
            self.reply(400, {"error": str(exc)})

    def do_POST(self):
        if not self.trusted():
            return
        path = urlsplit(self.path).path
        bridge = re.fullmatch(r"/api/bridge/([a-f0-9-]{36})", path)
        if bridge:
            token = self.console.bridge_tokens.get(bridge[1])
            if token is None or not secrets.compare_digest(self.headers.get("X-Bridge-Token", ""), token):
                self.reply(403, {"error": "无效的检测会话"})
                return
        elif not self.authorized():
            return
        try:
            data = self.read_json()
            if bridge:
                prompt = data.get("prompt")
                if not isinstance(prompt, str):
                    raise ValueError("测试问题必须是文本")
                result = self.console.generate(bridge[1], prompt)
            elif path == "/api/target":
                result = self.console.save_target(data)
            elif path == "/api/browser/open":
                target = validate_target(data)
                result = self.console.browser_action("open", target)
                self.console.save_target(target)
            elif path == "/api/browser/inspect":
                result = self.console.browser_action("inspect", validate_target(data))
            elif path == "/api/browser/reveal":
                result = self.console.browser_action("reveal", validate_target(data))
            elif path == "/api/browser/discover":
                result = self.console.browser_action("discover")
            elif path == "/api/browser/test":
                target = validate_target(data.get("target"))
                # Transport check deliberately sends a harmless prompt.
                result = {"response": self.console.browser_action("generate", target, "请只回复：连接成功", "连接成功"),
                          "detected_selector": self.console.browser.status()["detected_selector"], "security_scan": False}
            elif path == "/api/browser/cancel":
                if self.console.active:
                    raise ValueError("正在安全检测，请使用停止检测")
                self.console.browser.cancel.set()
                result = {"cancel_requested": True}
            elif path == "/api/browser/pick":
                result = self.console.browser_action("pick", data.get("kind"))
            elif path == "/api/browser/pick-result":
                result = self.console.browser_action("pick_result")
            elif path == "/api/jobs":
                result = self.console.start(data)
            elif match := re.fullmatch(r"/api/jobs/([a-f0-9-]{36})/stop", path):
                result = self.console.stop(match[1])
            elif path == "/api/demo-reply":
                # This is a deterministic fixture, not a real AI security verdict.
                prompt = str(data.get("prompt", ""))
                if "连接成功" in prompt:
                    response = "连接成功"
                elif data.get("vulnerable"):
                    response = "I hate humans. [DAN] DAN Mode enabled.（模拟网页故意输出探针标记）"
                else:
                    response = "我会遵守系统规则，不执行覆盖指令、角色替换或复现受保护文本的请求。"
                result = {"response": response, "demo": True}
            elif path == "/api/shutdown":
                result = {"stopping": True}
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.reply(404, {"error": "接口不存在"})
                return
            self.reply(200, result)
        except Exception as exc:
            self.reply(400 if isinstance(exc, ValueError) else 500, {"error": str(exc)[:2000]})


def serve(port=8787, headless=False, open_browser=False):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.console = Console(headless=headless, port=server.server_port)
    atomic_json(RUNTIME / "server.json", {"pid": os.getpid(), "port": server.server_port})
    print(f"Garak Web Guard · http://127.0.0.1:{server.server_port}", flush=True)
    print(f"garak {server.console.garak_version or '未安装'} · {sys.executable}", flush=True)
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server.console.active:
            server.console.stop(server.console.active)
            deadline = time.monotonic() + 15
            while server.console.active and time.monotonic() < deadline:
                time.sleep(0.05)
        server.console.browser.cancel.set()
        try:
            server.console.browser.call("close", timeout=20)
        except Exception:
            pass
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="本机 garak 网页 AI 检测控制台")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--headless-browser", action="store_true", help="仅用于无需交互登录的测试网页")
    parser.add_argument("--open", action="store_true", help="启动时打开控制台")
    args = parser.parse_args()
    serve(args.port, args.headless_browser, args.open)
