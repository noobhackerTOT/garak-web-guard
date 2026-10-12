"""Real Edge + real installed garak against a local, deterministic chat fixture."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bootstrap
bootstrap.configure_runtime()
import json
import threading
import time
import uuid
import urllib.request
import urllib.error

from app import Console, Handler, ThreadingHTTPServer, TERMINAL
from settings import DEFAULT_TARGET
from playwright.sync_api import sync_playwright


def main():
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    server.daemon_threads=True
    qa_storage=bootstrap.RUNTIME / 'qa' / ('state-'+str(uuid.uuid4()))
    console=Console(headless=True,port=server.server_port,storage=qa_storage)
    # Give QA its own browser profile and do not replace the user's saved target.
    console.browser.profile=bootstrap.RUNTIME / "qa" / "browser-profile"
    old_target=console.saved_target.copy()
    server.console=console
    threading.Thread(target=server.serve_forever,daemon=True).start()
    base=f"http://127.0.0.1:{server.server_port}"
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path,data=None,headers=None):
        req=urllib.request.Request(base+path,data=None if data is None else json.dumps(data).encode(),
            headers={"X-Console-Token":console.token,"Content-Type":"application/json",**(headers or {})})
        with opener.open(req,timeout=180) as response:
            return json.load(response)
    def wait(job_id,timeout=120):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            result=request('/api/jobs/'+job_id)
            if result['job']['status'] in TERMINAL and console.active is None:
                return result
            time.sleep(.3)
        raise AssertionError('Job timeout: '+json.dumps(result,ensure_ascii=False))
    def scan(target,probes,cap=1):
        job=request('/api/jobs',{"target":target,"probes":probes,"max_per_probe":cap})
        result=wait(job['id'])
        print(json.dumps({"status":result['job']['status'],"id":job['id'],"summary":result['summary'],"errors":result['job']['errors']},ensure_ascii=False),flush=True)
        if result['job']['status']=='failed':
            print('\n'.join(result['job']['logs'][-12:]),flush=True)
        return result
    target={**DEFAULT_TARGET,"name":"QA 本地模拟网页","url":base+'/demo?vulnerable=1',
        "input_selector":"#chat-input","response_selector":".assistant-message","send_selector":"#send-button",
        "new_chat_selector":"#new-chat","busy_selector":"#stop-button","reset_each_prompt":True,
        "timeout_s":15,"stable_ms":500,"min_response_ms":700,"delay_s":0}
    artifacts=bootstrap.RUNTIME / "qa"
    artifacts.mkdir(exist_ok=True,parents=True)
    errors=[]
    try:
        with sync_playwright() as p:
            qa=p.chromium.launch(channel='msedge',headless=True)
            page=qa.new_page(viewport={"width":1440,"height":1100},device_scale_factor=1)
            page.on('pageerror',lambda exc:errors.append(str(exc)))
            page.goto(base)
            page.wait_for_function("document.querySelector('#engine-version').textContent.includes('0.17')")
            page.screenshot(path=str(artifacts/'dashboard-desktop.png'),full_page=True)
            page.set_viewport_size({"width":390,"height":844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile overflow'
            page.screenshot(path=str(artifacts/'dashboard-mobile.png'),full_page=True)
            page.set_viewport_size({"width":1440,"height":1100})
            request('/api/browser/open',target)
            test=request('/api/browser/test',{"target":target})
            assert test['response']=='连接成功',test
            # Exercise the page element picker without sending a chat message.
            request('/api/browser/pick',{"kind":"input"})
            console.browser.call('inspect',target)
            # Actor method for QA only: schedule a click through the actor thread.
            console.browser._qa_click=lambda: console.browser.page.locator('#chat-input').click()
            console.browser.call('qa_click')
            picked=request('/api/browser/pick-result',{})
            assert picked['selector']=='#chat-input',picked
            print('PASS login browser, streamed transport, picker, desktop/mobile UI',flush=True)

            probes=["promptinject.HijackHateHumans","dan.Dan_11_0","encoding.InjectBase64","leakreplay.LiteratureComplete"]
            vulnerable=scan(target,probes)
            assert vulnerable['job']['status']=='completed',vulnerable['job']['error']
            assert vulnerable['summary']['evaluated']==4,vulnerable['summary']
            assert vulnerable['summary']['flagged']>=2,vulnerable['summary']
            assert vulnerable['job']['demo'] is True
            assert all(plan['planned']<=1 for plan in vulnerable['job']['plans'])
            page.reload();page.wait_for_function("document.querySelector('#metric-tested').textContent==='4'")
            page.locator('.result-row').first.click()
            page.screenshot(path=str(artifacts/'scan-results.png'),full_page=True)
            doc=opener.open(urllib.request.Request(base+'/api/jobs/'+vulnerable['job']['id']+'/download/html',headers={'X-Console-Token':console.token})).read().decode()
            assert '模拟网页验证' in doc and '[DAN]' in doc and '疑似风险' in doc
            (artifacts/'sample-report.html').write_text(doc,encoding='utf-8')

            target={**target,"url":base+'/demo?vulnerable=0',"reset_each_prompt":False}
            request('/api/browser/open',target)
            safe=scan(target,["promptinject.HijackHateHumans"],2)
            assert safe['job']['status']=='completed' and safe['summary']['evaluated']==2 and safe['summary']['flagged']==0,safe
            # Repeated identical answers must be read as fresh messages.
            assert safe['job']['answered']==2
            print('PASS real garak signals, cap, clean fixture, repeated identical responses, report download',flush=True)

            bad={**target,"response_selector":".missing-answer","timeout_s":10}
            failed=scan(bad,["dan.Dan_11_0"])
            assert failed['job']['status']=='failed' and failed['job']['errors']==1,failed
            assert failed['summary']['evaluated']==0 and failed['summary']['no_signal_rate'] is None
            print('PASS timeout cannot become a passing safety result',flush=True)

            job=request('/api/jobs',{"target":target,"probes":["promptinject.HijackHateHumans"],"max_per_probe":5})
            until=time.monotonic()+30
            while console.jobs[job['id']]['sent']==0 and time.monotonic()<until:time.sleep(.1)
            try:
                request('/api/browser/open',target)
                raise AssertionError('Browser mutations accepted during scan')
            except urllib.error.HTTPError as exc:
                assert exc.code==400
            request('/api/jobs/'+job['id']+'/stop',{})
            stopped=wait(job['id'])
            assert stopped['job']['status']=='cancelled',stopped
            assert console.process is None
            for headers in [{'Origin':'https://foreign.example'},{'X-Console-Token':'invalid'}]:
                try:
                    request('/api/target',target,headers)
                    raise AssertionError('Unauthorized write accepted')
                except urllib.error.HTTPError as exc:
                    assert exc.code==403
            print('PASS cancellation, scan exclusivity, cross-origin/token rejection',flush=True)

            # Verify front-end form actions through the visible control surface.
            page.reload()
            page.wait_for_function("!document.querySelector('#start-scan').disabled")
            page.locator('#target-url').fill(target['url'])
            page.locator('#input-selector').fill('#chat-input')
            page.locator('#response-selector').fill('.assistant-message')
            page.locator('#send-selector').fill('#send-button')
            page.locator('details.advanced > summary').click()
            page.locator('#busy-selector').fill('#stop-button')
            page.locator('#reset-each').uncheck()
            page.locator('#stable').fill('500');page.locator('#min-response').fill('700');page.locator('#delay').fill('0')
            page.locator('#test-button').click()
            page.wait_for_function("document.querySelector('#connection-result').textContent.includes('收到网页回答')",timeout=30000)
            page.locator('[data-view=history]').click()
            assert page.locator('.history-row').count()>=4
            page.locator('.open-job').first.click()
            assert page.locator('#workspace-view').is_visible()
            assert not errors,errors
            qa.close()
            (artifacts/'verification.json').write_text(json.dumps({"passed":True,"garak_version":console.garak_version,"browser":"Microsoft Edge","vulnerable_job":vulnerable['job']['id'],"safe_job":safe['job']['id'],"failed_job":failed['job']['id'],"cancelled_job":stopped['job']['id'],"javascript_errors":errors},ensure_ascii=False,indent=2),encoding='utf-8')
            print('PASS frontend connection action/history and zero JavaScript errors',flush=True)
    finally:
        if console.active:
            console.stop(console.active)
            time.sleep(1)
        console.browser.cancel.set()
        console.browser.call('close',timeout=30)
        # Restore the prior configuration so test localhost ports are not the default.
        from app import atomic_json
        atomic_json(qa_storage/'target.json',old_target)
        server.shutdown();server.server_close()


if __name__=='__main__':
    main()
