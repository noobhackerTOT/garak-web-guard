"""Playwright lives in one actor thread; HTTP handlers never touch its objects."""
import queue
import threading
import time
from concurrent.futures import Future
from pathlib import Path


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
    if (kind === 'send' || kind === 'new_chat') return el.closest('button,[role="button"],a') || el;
    if (kind === 'response') return el.closest('[data-message-author-role="assistant"],[data-role="assistant"],.assistant-message,.ds-markdown') || el;
    return el;
  };
  const selector = el => {
    const unique = s => {try {return document.querySelectorAll(s).length === 1} catch {return false}};
    if (el.id) {let s='#'+CSS.escape(el.id); if(unique(s)) return s;}
    for(let attr of ['data-message-author-role','data-role','data-testid','aria-label','name']) {
      let v=el.getAttribute(attr); if(!v) continue;
      let s=el.tagName.toLowerCase()+'['+attr+'="'+CSS.escape(v)+'"]';
      if(unique(s) || (kind==='response' && (attr==='data-message-author-role'||attr==='data-role'))) return s;
    }
    if(kind==='response') {
      for(let c of el.classList) {let s=el.tagName.toLowerCase()+'.'+CSS.escape(c); if(c.includes('markdown')||c.includes('assistant')) return s;}
    }
    let path=[];
    while(el && el.nodeType===1) {
      let part=el.tagName.toLowerCase();
      if(el.id) {path.unshift('#'+CSS.escape(el.id));break;}
      let siblings=el.parentElement ? [...el.parentElement.children].filter(x=>x.tagName===el.tagName) : [];
      if(siblings.length>1) part+=':nth-of-type('+(siblings.indexOf(el)+1)+')';
      path.unshift(part); if(unique(path.join(' > '))) break; el=el.parentElement;
    }
    return path.join(' > ');
  };
  const move = e => {restore(); prior=choose(e.target); outline=prior.style.outline; prior.style.outline='3px solid #21d49b';};
  const cleanup = () => {restore();banner.remove();document.removeEventListener('mouseover',move,true);document.removeEventListener('click',click,true);document.removeEventListener('keydown',key,true);window.__wgPickerCleanup=null;};
  const click = e => {e.preventDefault();e.stopImmediatePropagation();let el=choose(e.target);window.__wgPick={kind,selector:selector(el),tag:el.tagName};cleanup();};
  const key = e => {if(e.key==='Escape'){window.__wgPick={cancelled:true};cleanup();}};
  document.addEventListener('mouseover',move,true);document.addEventListener('click',click,true);document.addEventListener('keydown',key,true);
  window.__wgPickerCleanup=cleanup;
  return {kind, pending:true};
}"""

SNAPSHOT_SCRIPT = r"""els => {
  if (!window.__wgIds) {window.__wgIds = new WeakMap(); window.__wgId = 0;}
  return els.map(el => {
    if(!window.__wgIds.has(el)) window.__wgIds.set(el, ++window.__wgId);
    return {id:window.__wgIds.get(el), text:el.innerText || el.textContent || ''};
  });
}"""


class BrowserWorker:
    def __init__(self, profile: Path, headless=False):
        self.profile = profile
        self.headless = headless
        self.cancel = threading.Event()
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._status = {"open": False, "url": "", "title": "", "error": "", "busy": False}
        self.context = self.page = self.playwright = None
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
                self._update(busy=False)
            if action == "close":
                return

    def _open(self, target):
        from playwright.sync_api import sync_playwright
        if self.context is not None and self.target and self.target["channel"] != target["channel"]:
            self.context.close()
            self.context = self.page = None
        if self.playwright is None:
            self.playwright = sync_playwright().start()
        if self.context is None:
            self.profile.mkdir(parents=True, exist_ok=True)
            kwargs = {"user_data_dir": str(self.profile), "headless": self.headless,
                      "viewport": {"width": 1360, "height": 900}, "locale": "zh-CN"}
            if target["channel"] != "chromium":
                kwargs["channel"] = target["channel"]
            self.context = self.playwright.chromium.launch_persistent_context(**kwargs)
            self.context.on("close", self._context_closed)
        self.target = target.copy()
        live = [p for p in self.context.pages if not p.is_closed()]
        self.page = live[0] if live else self.context.new_page()
        self.page.goto(target["url"], wait_until="domcontentloaded", timeout=60000)
        self.page.bring_to_front()
        self.cancel.clear()
        self._update(open=True, url=self.page.url, title=self.page.title())
        return self.status()

    def _context_closed(self, *_):
        self.context = self.page = None
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

    def _scope(self, target):
        page = self._active_page()
        return page.frame_locator(target["frame_selector"]) if target["frame_selector"] else page

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
        return {"selectors": results, "browser": self.status()}

    def _pick(self, kind):
        if kind not in {"input", "send", "response", "new_chat", "busy"}:
            raise ValueError("未知元素类型")
        page = self._active_page()
        page.bring_to_front()
        return page.evaluate(PICK_SCRIPT, kind)

    def _pick_result(self):
        return self._active_page().evaluate("window.__wgPick || null")

    def _idle(self):
        return self.status()

    def _snapshots(self, locator):
        return locator.evaluate_all(SNAPSHOT_SCRIPT)

    def _pause(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.cancel.is_set():
                raise InterruptedError("检测已停止")
            self.page.wait_for_timeout(min(200, max(1, (deadline - time.monotonic()) * 1000)))

    def _generate(self, target, prompt):
        scope = self._scope(target)
        page = self.page
        if page.evaluate("Boolean(window.__wgPickerCleanup)"):
            raise ValueError("请先完成元素点选或按 Esc 取消点选")
        if self.cancel.is_set():
            raise InterruptedError("检测已停止")
        if target["reset_each_prompt"]:
            self._visible(scope.locator(target["new_chat_selector"]), "新对话按钮").click(timeout=10000)
            self._pause(0.7)
            scope = self._scope(target)
        field = self._visible(scope.locator(target["input_selector"]), "输入框")
        replies = scope.locator(target["response_selector"])
        before = self._snapshots(replies)
        old_ids = {r["id"] for r in before}
        old_last = before[-1] if before else None
        field.fill(prompt, timeout=15000)
        if target["send_selector"]:
            self._visible(scope.locator(target["send_selector"]), "发送按钮").click(timeout=15000)
        else:
            field.press("Enter", timeout=15000)
        started = time.monotonic()
        last_text = None
        changed_at = started
        saw_blank = False
        saw_busy = False
        while time.monotonic() - started < target["timeout_s"]:
            if self.cancel.is_set():
                raise InterruptedError("检测已停止，当前网页生成可能仍在进行")
            if page.is_closed():
                raise ValueError("检测期间浏览器页面被关闭")
            snapshots = self._snapshots(replies)
            last = snapshots[-1] if snapshots else None
            if last and not last["text"].strip():
                saw_blank = True
            busy = False
            if target["busy_selector"]:
                busy = self._visible(scope.locator(target["busy_selector"]), "生成状态", required=False) is not None
                saw_busy = saw_busy or busy
            fresh = last is not None and (
                last["id"] not in old_ids or len(snapshots) > len(before)
                or (old_last is not None and last["text"] != old_last["text"])
                or saw_blank or (saw_busy and not busy)
            )
            if fresh and last["text"].strip():
                text = last["text"].strip()
                if text != last_text:
                    last_text, changed_at = text, time.monotonic()
                now = time.monotonic()
                if (not busy and (now - changed_at) * 1000 >= target["stable_ms"]
                        and (now - started) * 1000 >= target["min_response_ms"]):
                    self._pause(target["delay_s"])
                    return text
            self._pause(0.2)
        raise TimeoutError("等待新的完整回答超时；请确认回答区域选择器只匹配 AI 回答，并调整生成状态或等待时间")

    def _screenshot(self, path):
        self._active_page().screenshot(path=str(path), full_page=True)
        return str(path)

    def _close(self):
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()
        self._update(open=False)
        return True
