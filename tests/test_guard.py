import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Console, GuardServer, Handler, host_matches_bind, origin_matches_bind, serve
from settings import DEFAULT_TARGET


class LoopbackPolicyTests(unittest.TestCase):
    def test_host_and_origin_allow_only_this_http_listener(self):
        self.assertTrue(host_matches_bind("127.0.0.1:8787", 8787))
        self.assertTrue(host_matches_bind(" LocalHost:8787 ", 8787))
        self.assertTrue(host_matches_bind("localhost", 80))
        self.assertTrue(host_matches_bind("127.0.0.1:80", 80))
        self.assertFalse(host_matches_bind("localhost", 8787))
        self.assertFalse(host_matches_bind("evil.example", 80))
        self.assertFalse(host_matches_bind(None, 8787))
        self.assertTrue(origin_matches_bind(None, 8787))
        self.assertTrue(origin_matches_bind("", 8787))
        self.assertTrue(origin_matches_bind("http://LOCALHOST:8787", 8787))
        self.assertTrue(origin_matches_bind("http://127.0.0.1", 80))
        self.assertFalse(origin_matches_bind("https://127.0.0.1:8787", 8787))
        self.assertFalse(origin_matches_bind("http://evil.example", 8787))
        self.assertFalse(origin_matches_bind("null", 8787))


class ConsoleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def open_console(self, storage):
        console = Console(headless=True, port=9, storage=storage)
        self.addCleanup(lambda c=console: c.browser.call("close", timeout=10))
        return console

    def test_restart_interrupts_incomplete_jobs_and_skips_corrupt_files(self):
        storage = Path(self.tmp.name) / "restart"
        running_id = "12345678-1234-1234-1234-123456789abc"
        partial_id = "abcdefab-cdef-abcd-efab-cdefabcdefab"
        running = storage / "runs" / running_id
        running.mkdir(parents=True)
        (running / "job.json").write_text(json.dumps({
            "id": running_id, "status": "running", "created_at": "2026-01-02T00:00:00+00:00",
            "config": {"target": {"name": "目标", "url": "https://example.test"}},
        }), encoding="utf-8")
        partial = storage / "runs" / partial_id
        partial.mkdir(parents=True)
        (partial / "job.json").write_text(json.dumps({"id": partial_id, "status": "completed"}), encoding="utf-8")
        corrupt = storage / "runs" / "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        corrupt.mkdir(parents=True)
        (corrupt / "job.json").write_text("[1, 2, 3]", encoding="utf-8")
        console = self.open_console(storage)
        self.assertEqual(console.jobs[running_id]["status"], "interrupted")
        self.assertIn("未完成", console.jobs[running_id]["error"])
        self.assertNotIn(partial_id, console.jobs)
        self.assertEqual(console.state()["jobs"][0]["id"], running_id)
        disk = json.loads((running / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(disk["status"], "interrupted")

    def job(self, job_id, status):
        return {
            "id": job_id, "status": status, "created_at": "2026-01-01T00:00:00+00:00", "finished_at": None,
            "config": {"target": {**DEFAULT_TARGET, "url": "https://example.test", "name": "示例"},
                       "probes": ["dan.Dan_11_0"], "max_per_probe": 1},
            "error": "", "logs": [], "cancel_requested": False, "demo": False,
            "garak_version": "0.17.0", "errors": 0,
        }

    def test_finish_job_writes_artifacts_and_releases_the_run(self):
        console = self.open_console(Path(self.tmp.name) / "finish-ok")
        job_id = "12345678-1234-1234-1234-123456789abc"
        (console.runs / job_id).mkdir(parents=True)
        job = self.job(job_id, "completed")
        console.jobs[job_id] = job
        console.active = job_id
        console._finish_job(job_id)
        self.assertIsNone(console.active)
        self.assertTrue((console.runs / job_id / "summary.json").is_file())
        html = (console.runs / job_id / "report.html").read_text(encoding="utf-8")
        self.assertIn("网页 AI 安全检测", html)
        self.assertIn("示例", html)
        saved = json.loads((console.runs / job_id / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "completed")
        self.assertTrue(saved["finished_at"])

    def test_finish_job_releases_active_when_report_rendering_fails(self):
        console = self.open_console(Path(self.tmp.name) / "finish-bad")
        job_id = "abcdefab-cdef-abcd-efab-cdefabcdefab"
        (console.runs / job_id).mkdir(parents=True)
        job = self.job(job_id, "running")
        console.jobs[job_id] = job
        console.active = job_id
        with mock.patch("app.build_html", side_effect=RuntimeError("boom")):
            console._finish_job(job_id)
        self.assertIsNone(console.active)
        self.assertEqual(job["status"], "failed")
        self.assertIn("结果整理失败", job["error"])
        self.assertIn("boom", job["error"])
        saved = json.loads((console.runs / job_id / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "failed")


class ApiGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = GuardServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.console = Console(headless=True, port=self.server.server_port, storage=self.tmp.name)
        self.addCleanup(lambda: self.console.browser.call("close", timeout=10))
        self.server.console = self.console
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, data=None, headers=None, token=True):
        extra = dict(headers or {})
        body = None if data is None else json.dumps(data).encode()
        if body is not None and "Content-Type" not in extra:
            extra["Content-Type"] = "application/json"
        return self._open(path, body, extra, token)

    def raw(self, path, body, headers):
        return self._open(path, body, headers, token=True)

    def _open(self, path, body, headers, token):
        last = None
        for _ in range(30):
            hdr = dict(headers)
            if token:
                hdr.setdefault("X-Console-Token", self.console.token)
            request = urllib.request.Request(self.base + path, data=body, headers=hdr, method="POST" if body is not None else "GET")
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = response.read()
                    if "json" in response.headers.get("Content-Type", ""):
                        return response.status, json.loads(payload.decode())
                    return response.status, payload
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                try:
                    parsed = json.loads(payload.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    parsed = payload
                return exc.code, parsed
            except urllib.error.URLError as exc:
                last = exc
                time.sleep(0.05)
        self.fail(f"服务未响应：{last}")

    def test_api_rejects_foreign_host_origin_and_missing_token(self):
        status, payload = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("garak", payload)
        self.assertIsNone(payload["active_job"])

        status, payload = self.request("/api/state", token=False)
        self.assertEqual(status, 403)

        status, payload = self.request("/api/state", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("本机", payload["error"])

        status, payload = self.request("/api/state", headers={"Host": f"LocalHost:{self.server.server_port}"})
        self.assertEqual(status, 200)

        status, _ = self.request("/", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        status, page = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(self.console.token.encode(), page)

        status, payload = self.request("/api/target", {"url": "https://example.test"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, payload = self.request("/api/target", {"name": "x"}, headers={"Origin": f"http://127.0.0.1:{self.server.server_port}"})
        self.assertEqual(status, 400)

        status, payload = self.raw("/api/target", b"{", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        status, payload = self.raw("/api/target", b"[]", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)


class ServeTests(unittest.TestCase):
    def test_invalid_port_is_rejected(self):
        for port in (0, 70000, True):
            with self.subTest(port=port):
                with self.assertRaises(SystemExit) as caught:
                    serve(port)
                self.assertIn("端口", str(caught.exception))

    def test_busy_port_explains_the_failure(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        self.addCleanup(sock.close)
        with self.assertRaises(SystemExit) as caught:
            serve(sock.getsockname()[1])
        self.assertIn("已被占用", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
