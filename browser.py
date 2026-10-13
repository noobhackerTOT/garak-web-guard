"""Playwright lives in one actor thread; HTTP handlers never touch its objects."""
import queue
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from browser_launch import launch_standard
from response_reader import AUTO_SCRIPT, fresh_candidate
from page_discovery import DISCOVER_SCRIPT


SELECTOR_SCRIPT = r"""(el, kind) => {
  const root = el.getRootNode();
  const unique = s => {try {return root.querySelectorAll(s).length === 1} catch {return false}};
  let result = '';
  for (let attr of ['data-message-author-role','data-role','data-testid','aria-label','name']) {
    let v=el.getAttribute(attr); if(!v) continue;
    let s=el.tagName.toLowerCase()+'['+attr+'="'+CSS.escape(v)+'"]';
    if(unique(s) || (kind==='response' && (attr==='data-message-author-role'||attr==='data-role'))) {result=s;break;}
  }
  if(!result && kind==='response' && el.classList.length) result=el.tagName.toLowerCase()+[...el.classList].map(c=>'.'+CSS.escape(c)).join('');
  if(!result && el.id) {let s='#'+CSS.escape(el.id); if(unique(s)) result=s;}
  if(!result) {
    let path=[], item=el;
    while(item && item.nodeType===1) {
      let part=item.tagName.toLowerCase();
      if(item.id) {path.unshift('#'+CSS.escape(item.id));break;}
      let siblings=item.parentElement ? [...item.parentElement.children].filter(x=>x.tagName===item.tagName) : [];
      if(siblings.length>1) part+=':nth-of-type('+(siblings.indexOf(item)+1)+')';
      path.unshift(part); if(unique(path.join(' > '))) break; item=item.parentElement;
    }
    result=path.join(' > ');
  }
  if(root instanceof ShadowRoot) return window.__wgSelector(root.host,'host')+' '+result;
  return result;
}"""


PICK_SCRIPT = r"""(kind) => {
  if (window.__wgPickerCleanup) window.__wgPickerCleanup();
  window.__wgPick = null;
  let banner = document.createElement('div');
  banner.textContent = 'Garak Web Guard · 点击要选择的元素（Esc 取消）';
  banner.style.cssText = 'position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:2147483647;padding:14px 22px;background:#122723;color:#7ef4cc;border-radius:12px;font:14px sans-serif;pointer-events:none';
  document.body.appendChild(banner);
  let prior = null, outline = '';
  const restore = () => {if (prior) prior.style.outline = outline; prior = null;};
  const choose = el => {
    if (kind === 'input') return el.closest('textarea,input,[contenteditable="true"]') || el;
    if (kind === 'send' || kind === 'new_chat' || kind === 'launcher') return el.closest('button,[role="button"],a') || el;
    if (kind === 'response') return el.closest('[data-message-author-role="assistant"],[data-role="assistant"],[class*="markdown" i],[class*="assistant" i],[class*="answer" i],[class*="response" i]') || el;
    return el;
  };
  const target = e => e.composedPath()[0];
  const move = e => {restore(); prior=choose(target(e)); outline=prior.style.outline; prior.style.outline='3px solid #21d49b';};
  const cleanup = () => {restore();banner.remove();document.removeEventListener('mouseover',move,true);document.removeEventListener('click',click,true);document.removeEventListener('keydown',key,true);window.__wgPickerCleanup=null;};
  const click = e => {e.preventDefault();e.stopImmediatePropagation();let el=choose(target(e));window.__wgPick={kind,selector:window.__wgSelector(el,kind),tag:el.tagName};cleanup();};
  const key = e => {if(e.key==='Escape'){window.__wgPick={cancelled:true};cleanup();}};
  document.addEventListener('mouseover',move,true);document.addEventListener('click',click,true);document.addEventListener('keydown',key,true);
  window.__wgPickerCleanup=cleanup;
  return {kind, pending:true};
}"""

SNAPSHOT_SCRIPT = r"""els => {
  if (!window.__wgIds) {window.__wgIds = new WeakMap(); window.__wgId = 0;}
  if (!window.__wgDocument) window.__wgDocument=Math.random().toString(36).slice(2);
  return els.filter(el => {let r=el.getBoundingClientRect();let s=getComputedStyle(el);return r.width && r.height && s.display!=='none' && s.visibility!=='hidden';}).map((el, order) => {
    if(!window.__wgIds.has(el)) window.__wgIds.set(el, ++window.__wgId);
    return {id:window.__wgIds.get(el), document:window.__wgDocument, text:el.innerText || el.textContent || '', score:120, order};
  });
}"""


class BrowserWorker:
    def __init__(self, profile: Path, headless=False):
        self.profile = profile
        self.headless = headless
        self.cancel = threading.Event()
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._status = {"open": False, "url": "", "title": "", "error": "", "busy": False,
                        "phase": "", "elapsed_s": 0, "answer_candidates": 0, "detected_selector": ""}
        self.context = self.page = self.playwright = None
        self.managed_browser = self.browser_process = None
        self._picker_kind = None
        self._picker_frames = set()
        self.target = None
        self.thread = threading.Thread(target=self._loop, daemon=True, name="browser-actor")
        self.thread.start()

    def status(self):
        with self._lock:
            return self._status.copy()

    def _update(self, **kwargs):
        with self._lock:
            self._status.update(kwargs)

    def call(self, action, *args, timeout=660):
        future = Future()
        self._queue.put((action, args, future))
        return future.result(timeout=timeout)

    def _loop(self):
        while True:
            action, args, future = self._queue.get()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                self._update(busy=True)
                value = getattr(self, "_" + action)(*args)
                self._update(error="")
                future.set_result(value)
            except Exception as exc:
                self._update(error=str(exc)[:1000])
                future.set_exception(exc)
            finally:
                self._update(busy=False, phase="")
            if action == "close":
                return

    def _open(self, target):
        from playwright.sync_api import sync_playwright
        if self.target and any(self.target.get(k) != target.get(k) for k in ("channel", "browser_mode")):
            self._release_browser()
        if self.playwright is None:
            self.playwright = sync_playwright().start()
        if self.context is None:
            self._release_browser()
            self.profile.mkdir(parents=True, exist_ok=True)
            self.managed_browser, self.context, self.browser_process = launch_standard(
                self.playwright, target["channel"], self.profile, self.headless,
                background=target["browser_mode"] == "background")
            self.context.on("close", self._context_closed)
            self.context.set_default_timeout(10000)
        self.target = target.copy()
        live = [p for p in self.context.pages if not p.is_closed()]
        self.page = live[0] if live else self.context.new_page()
        if self.headless or target["browser_mode"] == "background":
            self.page.set_viewport_size({"width": 1360, "height": 900})
        self.page.goto(target["url"], wait_until="domcontentloaded", timeout=60000)
        self.page.bring_to_front()
        self.cancel.clear()
        self._picker_kind = None
        self._reveal(target)
        self._update(open=True, url=self.page.url, title=self.page.title())
        return self.status()

    def _context_closed(self, *_):
        self.context = self.page = None
        self._picker_kind = None
        self._update(open=False)

    def _active_page(self):
        if self.context is None:
            raise ValueError("请先打开登录浏览器")
        pages = [p for p in self.context.pages if not p.is_closed()]
        if not pages:
            self._update(open=False)
            raise ValueError("登录浏览器已关闭，请重新打开")
        # Prefer a target-host tab over an SSO tab opened during login.
        from urllib.parse import urlsplit
        host = urlsplit(self.target["url"]).hostname if self.target else None
        matches = [p for p in pages if urlsplit(p.url).hostname == host]
        self.page = matches[-1] if matches else pages[-1]
        self._update(open=True, url=self.page.url, title=self.page.title())
        return self.page

    def _scope(self, target, launcher=False):
        page = self._active_page()
        scope = page
        selector = target["launcher_frame_selector"] if launcher else target["frame_selector"]
        for part in selector.split(" >>> ") if selector else []:
            scope = scope.frame_locator(part)
        return scope

    @staticmethod
    def _on_screen(item):
        if not item.is_visible():
            return False
        rect = item.bounding_box()
        viewport = item.page.evaluate("({width:innerWidth,height:innerHeight})")
        return bool(rect and rect["x"] < viewport["width"] and rect["x"] + rect["width"] > 0
                    and rect["y"] < viewport["height"] and rect["y"] + rect["height"] > 0)

    def _chat_visible(self, target):
        try:
            loc = self._scope(target).locator(target["input_selector"])
            return any(self._on_screen(loc.nth(i)) for i in range(loc.count()))
        except Exception:
            return False

    def _reveal(self, target):
        if not target["launcher_selector"] or self._chat_visible(target):
            return self.status()
        self._visible(self._scope(target, launcher=True).locator(target["launcher_selector"]), "助手入口").click(timeout=10000)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self._chat_visible(target):
                return self.status()
            self._pause(0.15)
        raise ValueError("已点击助手入口，但输入框仍不可见；请检查 iframe 配置或在浏览器完成登录")

    @staticmethod
    def _visible(locator, label, required=True):
        for i in range(locator.count()):
            item = locator.nth(i)
            if item.is_visible():
                return item
        if required:
            raise ValueError(f"找不到可见的{label}，请检查登录状态和选择器")
        return None

    def _inspect(self, target):
        self._reveal(target)
        scope = self._scope(target)
        results = {}
        for key in ["input_selector", "send_selector", "response_selector", "new_chat_selector", "busy_selector"]:
            value = target[key]
            if not value:
                results[key] = {"count": 0, "visible": 0, "configured": False}
                continue
            loc = scope.locator(value)
            count = loc.count()
            results[key] = {"count": count, "visible": sum(loc.nth(i).is_visible() for i in range(count)), "configured": True}
        return {"selectors": results, "browser": self.status(), "frames": self._frames(),
                "chat_visible": self._chat_visible(target),
                "answer_candidates": len(self._auto_snapshots(scope, target, "", None)) if target["response_mode"] == "auto" else None}

    def _frame_path(self, frame):
        parts = []
        while frame.parent_frame:
            element = frame.frame_element()
            try:
                parts.append(element.evaluate("(el) => {window.__wgSelector=" + SELECTOR_SCRIPT + ";return window.__wgSelector(el,'frame')}"))
            finally:
                element.dispose()
            frame = frame.parent_frame
        return " >>> ".join(reversed(parts))

    def _frames(self):
        results = []
        for frame in self.page.frames:
            if frame == self.page.main_frame or frame.is_detached():
                continue
            try:
                results.append({"selector": self._frame_path(frame), "url": frame.url})
            except Exception:
                continue
        return results

    def _discover(self):
        page = self._active_page()
        deadline = time.monotonic() + 3
        while True:
            result = self._discover_once(page)
            if result["candidates"] or time.monotonic() >= deadline:
                return result
            self._pause(0.2)

    def _discover_once(self, page):
        candidates, launchers = [], []
        for frame in page.frames:
            if frame.is_detached():
                continue
            try:
                frame.evaluate("() => {window.__wgSelector=" + SELECTOR_SCRIPT + ";}")
                found = frame.locator("body").evaluate(DISCOVER_SCRIPT, timeout=2000)
                path = self._frame_path(frame)
                answers = self._auto_snapshots(frame, {"response_selector": ""}, "", None)
                response = max(answers, key=lambda r: (r["score"], r["order"])) if answers else None
                for item in found["inputs"]:
                    if frame.locator(item["input_selector"]).count() != 1:
                        continue
                    item.update(frame_selector=path, visible=self._on_screen(frame.locator(item["input_selector"])),
                                response_selector=response["selector"] if response else "")
                    if response:
                        item["score"] += 20
                    candidates.append(item)
                for item in found["launchers"]:
                    item["launcher_frame_selector"] = path
                    launchers.append(item)
            except Exception:
                continue
        candidates.sort(key=lambda row: (row["visible"], row["score"]), reverse=True)
        return {"candidates": candidates, "launchers": launchers, "frames": self._frames()}

    def _install_picker(self, frame):
        frame.evaluate("() => {window.__wgSelector=" + SELECTOR_SCRIPT + ";}")
        frame.evaluate(PICK_SCRIPT, self._picker_kind)
        self._picker_frames.add(frame)

    def _cleanup_picker(self):
        for frame in self._picker_frames:
            try:
                frame.evaluate("if(window.__wgPickerCleanup) window.__wgPickerCleanup()")
            except Exception:
                pass
        self._picker_frames.clear()
        self._picker_kind = None

    def _pick(self, kind):
        if kind not in {"input", "send", "response", "new_chat", "busy", "launcher"}:
            raise ValueError("未知元素类型")
        page = self._active_page()
        page.bring_to_front()
        self._cleanup_picker()
        self._picker_kind = kind
        for frame in page.frames:
            try:
                self._install_picker(frame)
            except Exception:
                if frame == page.main_frame:
                    self._cleanup_picker()
                    raise
        return {"kind": kind, "pending": True}

    def _pick_result(self):
        page = self._active_page()
        if self._picker_kind is None:
            return None
        for frame in page.frames:
            if frame.is_detached():
                continue
            try:
                result = frame.evaluate("window.__wgPick || null")
                if result:
                    if not result.get("cancelled"):
                        result["frame_selector"] = self._frame_path(frame)
                    self._cleanup_picker()
                    return result
                if frame not in self._picker_frames or not frame.evaluate("Boolean(window.__wgPickerCleanup)"):
                    self._install_picker(frame)
            except Exception:
                continue
        return None

    def _idle(self):
        return self.status()

    def _snapshots(self, locator):
        return locator.evaluate_all(SNAPSHOT_SCRIPT)

    def _auto_snapshots(self, scope, target, prompt, expected):
        root = scope.locator("body")
        root.evaluate("body => {window.__wgSelector=" + SELECTOR_SCRIPT + ";}")
        return root.evaluate(AUTO_SCRIPT, {"prompt": prompt, "expected": expected,
                                          "configured": target["response_selector"]})

    def _pause(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.cancel.is_set():
                raise InterruptedError("检测已停止")
            self.page.wait_for_timeout(min(200, max(1, (deadline - time.monotonic()) * 1000)))

    def _generate(self, target, prompt, expected=None):
        if self._picker_kind:
            raise ValueError("请先完成元素点选或按 Esc 取消点选")
        self._reveal(target)
        scope = self._scope(target)
        page = self.page
        if self.cancel.is_set():
            raise InterruptedError("检测已停止")
        self._update(phase="正在填写问题", elapsed_s=0, answer_candidates=0, detected_selector="")
        if target["reset_each_prompt"]:
            self._visible(scope.locator(target["new_chat_selector"]), "新对话按钮").click(timeout=10000)
            self._pause(0.7)
            scope = self._scope(target)
        field = self._visible(scope.locator(target["input_selector"]), "输入框")
        automatic = target["response_mode"] == "auto"
        replies = scope.locator(target["response_selector"] or ":not(*)")
        before = self._auto_snapshots(scope, target, prompt, expected) if automatic else self._snapshots(replies)
        old_keys = {(r["document"], r["id"]) for r in before}
        field.fill(prompt, timeout=15000)
        if target["send_selector"]:
            self._visible(scope.locator(target["send_selector"]), "发送按钮").click(timeout=15000)
        else:
            field.press("Enter", timeout=15000)
        started = time.monotonic()
        last_text = None
        changed_at = started
        blank_ids = set()
        detected_selector = ""
        while time.monotonic() - started < target["timeout_s"]:
            if self.cancel.is_set():
                raise InterruptedError("检测已停止，当前网页生成可能仍在进行")
            if page.is_closed():
                raise ValueError("检测期间浏览器页面被关闭")
            try:
                snapshots = self._auto_snapshots(scope, target, prompt, expected) if automatic else self._snapshots(replies)
            except Exception as exc:
                if "Execution context was destroyed" not in str(exc) and "Cannot find context" not in str(exc):
                    raise
                self._pause(0.2)
                scope = self._scope(target)
                page = self.page
                replies = scope.locator(target["response_selector"] or ":not(*)")
                continue
            last = snapshots[-1] if snapshots else None
            if last and not last["text"].strip():
                blank_ids.add((last["document"], last["id"]))
            busy = False
            if target["busy_selector"]:
                busy = self._visible(scope.locator(target["busy_selector"]), "生成状态", required=False) is not None
            candidate = fresh_candidate(before, snapshots)
            if not automatic and not candidate and last:
                key = (last["document"], last["id"])
                if key in old_keys and key in blank_ids and last["text"].strip():
                    candidate = last
            last = candidate
            fresh = last is not None
            elapsed = round(time.monotonic() - started, 1)
            self._update(phase="网页正在生成" if busy else "等待新回答" if not fresh else "确认回答已完成",
                         elapsed_s=elapsed, answer_candidates=len(snapshots))
            if fresh and last["text"].strip():
                text = last["text"].strip()
                detected_selector = last.get("selector", target["response_selector"])
                if text != last_text:
                    last_text, changed_at = text, time.monotonic()
                now = time.monotonic()
                if (not busy and (now - changed_at) * 1000 >= target["stable_ms"]
                        and (now - started) * 1000 >= target["min_response_ms"]):
                    self._pause(target["delay_s"])
                    self._update(detected_selector=detected_selector)
                    return text
            self._pause(0.2)
        if not snapshots:
            raise TimeoutError("网页未找到可读取的 AI 回答区域。请展开助手，并点选完整回答气泡或切换自动识别；网页已回答但没有返回时，通常是回答选择器未匹配")
        raise TimeoutError("等待新的完整回答超时；请确认回答区域只匹配 AI 回答，并调整生成状态或等待时间")

    def _screenshot(self, path):
        self._active_page().screenshot(path=str(path), full_page=True)
        return str(path)

    def _release_browser(self):
        self._cleanup_picker()
        browser, context, process = self.managed_browser, self.context, self.browser_process
        self.managed_browser = self.browser_process = self.context = self.page = None
        try:
            if browser and browser.is_connected():
                try:
                    browser.new_browser_cdp_session().send("Browser.close")
                finally:
                    browser.close()
            elif context:
                context.close()
        finally:
            if process and process.poll() is None:
                try:
                    process.wait(timeout=5)
                except Exception:
                    process.terminate()
            self._update(open=False)

    def _close(self):
        self._release_browser()
        if self.playwright:
            self.playwright.stop()
        self._update(open=False)
        return True
