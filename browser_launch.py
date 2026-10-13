"""Launch an owned browser with its own profile and loopback-only CDP connection."""
import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path


def executable(channel, chromium_path):
    if channel == "chromium":
        return chromium_path
    relative = {"msedge": "Microsoft/Edge/Application/msedge.exe",
                "chrome": "Google/Chrome/Application/chrome.exe"}[channel]
    for root in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                 os.environ.get("LOCALAPPDATA")):
        if root and (path := Path(root) / relative).is_file():
            return str(path)
    raise ValueError("未找到所选浏览器，请安装后重试或切换浏览器")


def launch_standard(playwright, channel, profile, headless=False, background=False):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    args = [executable(channel, playwright.chromium.executable_path),
            "--user-data-dir=" + str(profile.resolve()), "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=" + str(port), "--no-first-run", "--no-default-browser-check",
            "--start-maximized", "about:blank"]
    if headless:
        args.insert(-1, "--headless=new")
    if background:
        args[-1:-1] = ["--disable-background-timer-throttling", "--disable-renderer-backgrounding"]
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    endpoint = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 25
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValueError("浏览器启动失败；请关闭本工具遗留的独立浏览器窗口后重试")
            try:
                with opener.open(endpoint + "/json/version", timeout=1) as response:
                    version = json.load(response)
                if not version.get("webSocketDebuggerUrl", "").startswith(f"ws://127.0.0.1:{port}/"):
                    raise ValueError("浏览器返回了非本机连接地址")
                browser = playwright.chromium.connect_over_cdp(endpoint, timeout=15000)
                if not browser.contexts:
                    raise ValueError("浏览器未提供可用的页面环境")
                return browser, browser.contexts[0], process
            except (OSError, ValueError) as exc:
                if isinstance(exc, ValueError):
                    raise
                time.sleep(0.15)
        raise TimeoutError("常规浏览器连接超时，可切换自动化浏览器模式重试")
    except Exception:
        if process.poll() is None:
            process.terminate()
        raise
