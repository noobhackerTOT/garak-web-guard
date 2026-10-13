"""Real browser checks across independent origins, floating panels and open shadow roots."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
import bootstrap
bootstrap.configure_runtime()
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from browser import BrowserWorker
from settings import DEFAULT_TARGET

CHAT = '''<textarea id="input"></textarea><button id="send">Send</button><b id="busy" hidden>Generating</b><h1 id="question"></h1><div id="history"><div class="answer markdown">历史回答</div></div>
<script>send.onclick=()=>{let text=input.value;question.textContent=text;input.value='';busy.hidden=false;
let reply=document.createElement('div');reply.className=location.search.includes('unmarked')?'content-bubble':'answer markdown';document.getElementById('history').appendChild(reply);
let p=document.createElement('p');reply.appendChild(p);p.textContent='';setTimeout(()=>{p.textContent=text.includes('连接成功')?'连接成功':'第一段回答';},100);
setTimeout(()=>{if(!text.includes('连接成功')){let p2=document.createElement('p');p2.textContent='第二段回答';reply.appendChild(p2);}busy.hidden=true;},300);};</script>'''


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/plain':
            body = CHAT
        elif path == '/inner':
            body = CHAT
        elif path == '/entry':
            body = '<button id="launch" onclick="parent.postMessage(\'open\',\'*\')">AI</button>'
        elif path == '/embed':
            body = f'''<iframe id="entry" src="{SECOND}/entry"></iframe><iframe id="chat" style="position:fixed;right:-200%;width:500px;height:500px" src="{SECOND}/inner"></iframe><script>onmessage=e=>{{if(e.data==='open')chat.style.right='0'}};</script>'''
        elif path == '/nested':
            body = '<iframe id="outer" style="width:700px;height:650px" src="/shell"></iframe>'
        elif path == '/shell':
            body = f'<iframe id="inner" style="width:600px;height:600px" src="{SECOND}/inner"></iframe>'
        elif path == '/shadow':
            body = '<div id="host"></div><script>let root=host.attachShadow({mode:"open"});root.innerHTML=\'<textarea id="input"></textarea><button id="send">Send</button><div id="history"></div>\';root.querySelector("#send").onclick=()=>{let r=document.createElement("div");r.className="answer markdown";r.textContent="连接成功";root.querySelector("#history").appendChild(r)};</script>'
        else:
            body = 'fixture'
        content = ('<!doctype html><html><meta charset="utf-8"><body>' + body + '</body></html>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main():
    global SECOND
    servers = [ThreadingHTTPServer(('127.0.0.1', 0), Fixture) for _ in range(2)]
    base, SECOND = [f'http://127.0.0.1:{s.server_port}' for s in servers]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    worker = BrowserWorker(bootstrap.RUNTIME / 'qa' / ('embedded-' + str(uuid.uuid4())), headless=True)
    target = {**DEFAULT_TARGET, 'url': base + '/plain', 'input_selector': '#input',
              'send_selector': '#send', 'busy_selector': '#busy', 'timeout_s': 10,
              'stable_ms': 500, 'min_response_ms': 700, 'delay_s': 0}
    results = []

    def passed(label):
        results.append(label)
        print('PASS ' + label, flush=True)

    def actor(fn):
        worker._qa_action = fn
        return worker.call('qa_action', timeout=20)

    try:
        print('START regular Edge', flush=True)
        worker.call('open', target, timeout=40)
        discovery=worker.call('discover')
        assert len(discovery['candidates'])==1 and discovery['candidates'][0]['frame_selector']=='',discovery
        assert discovery['candidates'][0]['send_selector']=='#send',discovery
        passed('locate a chat input, send button and response region on the page')
        assert '--no-sandbox' not in worker.browser_process.args
        answer = worker.call('generate', target, '请只回复：连接成功', '连接成功', timeout=20)
        assert answer == '连接成功', answer
        passed('auto answer discovery with a nonmatching example selector; prompt heading excluded')
        assert worker.call('generate', target, 'multi paragraph', timeout=20) == '第一段回答\n\n第二段回答'
        passed('streamed complete multi-paragraph answer')
        actor(lambda: worker.page.locator('#send').evaluate("el=>el.onclick=()=>{busy.hidden=false;setTimeout(()=>busy.hidden=true,200)}"))
        try:
            worker.call('generate', target, 'no new answer', timeout=20)
            raise AssertionError('A spinner without an answer reused history')
        except TimeoutError:
            pass
        passed('a completed spinner without a new answer cannot reuse history')
        # A page without semantic model markup can learn a container from a known harmless reply.
        target['url'] = base + '/plain?unmarked'
        worker.call('open', target, timeout=20)
        assert worker.call('generate', target, '请只回复：连接成功', '连接成功', timeout=20) == '连接成功'
        learned = worker.status()['detected_selector']
        assert 'content-bubble' in learned, learned
        learned_target = {**target, 'response_selector': learned}
        assert worker.call('generate', learned_target, 'more paragraphs', timeout=20) == '第一段回答\n\n第二段回答'
        passed('learn an unmarked response container then read its later complete answer')
        target.update(url=base + '/embed', launcher_selector='#launch', launcher_frame_selector='#entry', frame_selector='#chat')
        worker.call('open', target, timeout=25)
        discovery=worker.call('discover')
        assert discovery['candidates'][0]['frame_selector']=='#chat',discovery
        assert any(c['launcher_frame_selector']=='#entry' for c in discovery['launchers']),discovery
        passed('locate chat and launcher in separate cross-origin iframes')
        assert worker.call('inspect', target)['chat_visible']
        assert worker.call('generate', target, '请只回复：连接成功', '连接成功', timeout=20) == '连接成功'
        actor(lambda: worker.page.locator('#chat').evaluate("el=>el.style.right='-200%'"))
        assert worker.call('generate', target, '请只回复：连接成功', '连接成功', timeout=20) == '连接成功'
        passed('separate cross-origin launcher/chat iframes; reopen a collapsed offscreen panel')
        worker.call('pick', 'input')
        actor(lambda: worker.page.frame_locator('#chat').locator('#input').click())
        picked = worker.call('pick_result')
        assert picked['frame_selector'] == '#chat' and picked['selector'] == '#input', picked
        worker.call('pick', 'response')
        actor(lambda: worker.page.frame_locator('#chat').locator('.answer').last.click())
        picked = worker.call('pick_result')
        assert 'nth-of-type' not in picked['selector'], picked
        passed('cross-origin picker and repeatable response selector')
        target.update(url=base + '/nested', launcher_selector='', launcher_frame_selector='', frame_selector='#outer >>> #inner')
        worker.call('open', target, timeout=20)
        assert worker.call('discover')['candidates'][0]['frame_selector']=='#outer >>> #inner'
        worker.call('pick', 'input')
        actor(lambda: worker.page.frame_locator('#outer').frame_locator('#inner').locator('#input').click())
        picked = worker.call('pick_result')
        assert picked['frame_selector'] == '#outer >>> #inner', picked
        assert worker.call('generate', target, '请只回复：连接成功', '连接成功', timeout=20) == '连接成功'
        passed('nested iframe picker and transport')
        target.update(url=base + '/shadow', frame_selector='', busy_selector='')
        worker.call('open', target, timeout=20)
        discovery=worker.call('discover')
        assert discovery['candidates'][0]['input_selector']=='#host #input',discovery
        assert discovery['candidates'][0]['send_selector']=='#host #send',discovery
        worker.call('pick', 'input')
        actor(lambda: worker.page.locator('#host #input').click())
        picked = worker.call('pick_result')
        assert picked['selector'] == '#host #input', picked
        assert worker.call('generate', target, '请只回复：连接成功', '连接成功', timeout=20) == '连接成功'
        passed('open Shadow DOM picker and automatic answer reading')
        # A wrong manual selector should remain an error and support cancellation.
        bad = {**target, 'response_mode': 'selector', 'response_selector': '.absent'}
        errors = []
        def pending():
            try:
                worker.call('generate', bad, 'test cancel', timeout=20)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=pending)
        thread.start()
        deadline = time.monotonic() + 5
        while not worker.status()['phase'] and time.monotonic() < deadline:
            time.sleep(.05)
        worker.cancel.set()
        thread.join(timeout=4)
        assert errors and isinstance(errors[0], InterruptedError), errors
        worker.cancel.clear()
        passed('cancel a waiting connection test without a passing result')
        target.update(url=base + '/plain', browser_mode='background', response_mode='auto', response_selector='', busy_selector='#busy')
        worker.call('open', target, timeout=30)
        assert '--no-sandbox' not in worker.browser_process.args
        assert '--disable-background-timer-throttling' in worker.browser_process.args
        assert worker.call('generate', target, '请只回复：连接成功', '连接成功', timeout=20) == '连接成功'
        passed('background mode relaunch with its own sandboxed browser and stable viewport')
        (bootstrap.RUNTIME / 'qa' / 'embedded-verification.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        worker.cancel.set()
        worker.call('close', timeout=20)
        for server in servers:
            server.shutdown()


if __name__ == '__main__':
    main()
