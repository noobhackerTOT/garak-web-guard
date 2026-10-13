'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="console-token"]').content;
const terminal = new Set(['completed','failed','cancelled','interrupted']);
const statuses = {preparing:'准备中',running:'检测中',stopping:'停止中',completed:'已完成',failed:'检测未完成',cancelled:'已停止',interrupted:'上次运行中断'};
const selectorFields = {input:'input-selector',response:'response-selector',send:'send-selector',new_chat:'new-chat-selector',busy:'busy-selector',launcher:'launcher-selector'};
let state = null, selectedJob = null, busyCount = 0, polling = false, initialized = false, selectedTab = 'results';
let toastTimer, pickTimer, pickStarted = 0, lastDetail = null, testRunning = false;
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, data) {
  const response = await fetch(path,{method:data===undefined?'GET':'POST', headers:{'X-Console-Token':token,...(data===undefined?{}:{'Content-Type':'application/json'})}, body:data===undefined?undefined:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '操作失败');
  return result;
}
function toast(message,error=false) {
  clearTimeout(toastTimer); $('toast').textContent=message; $('toast').classList.toggle('error',error); $('toast').hidden=false;
  toastTimer=setTimeout(()=>$('toast').hidden=true,error?9000:5000);
}
function connection(message,error=false) {
  $('connection-result').hidden=false; $('connection-result').textContent=message; $('connection-result').classList.toggle('error',error);
}
function target() {
  return {name:$('target-name').value,url:$('target-url').value,channel:$('channel').value,browser_mode:$('browser-mode').value,
    input_selector:$('input-selector').value,response_selector:$('response-selector').value,response_mode:$('response-mode').value,
    send_selector:$('send-selector').value,new_chat_selector:$('new-chat-selector').value,
    busy_selector:$('busy-selector').value,frame_selector:$('frame-selector').value,
    launcher_selector:$('launcher-selector').value,launcher_frame_selector:$('launcher-frame-selector').value,
    reset_each_prompt:$('reset-each').checked,timeout_s:Number($('timeout').value),
    stable_ms:Number($('stable').value),min_response_ms:Number($('min-response').value),delay_s:Number($('delay').value)};
}
function fillTarget(t) {
  const map = {name:'target-name',url:'target-url',channel:'channel',browser_mode:'browser-mode',input_selector:'input-selector',response_selector:'response-selector',response_mode:'response-mode',send_selector:'send-selector',new_chat_selector:'new-chat-selector',busy_selector:'busy-selector',frame_selector:'frame-selector',launcher_selector:'launcher-selector',launcher_frame_selector:'launcher-frame-selector',timeout_s:'timeout',stable_ms:'stable',min_response_ms:'min-response',delay_s:'delay'};
  for (const [key,id] of Object.entries(map)) if (t[key]!==undefined) $(id).value=t[key];
  $('reset-each').checked=Boolean(t.reset_each_prompt);
}
function selectedProbes() { return [...document.querySelectorAll('.probe-option input:checked')].map(x=>x.value); }
function estimate() {$('estimate').textContent=(selectedProbes().length*Number($('max-per-probe').value))+' 个问题';}
function controls() {
  const active=Boolean(state?.active_job);
  document.querySelectorAll('.browser-action').forEach(b=>b.disabled=active||busyCount>0);
  $('start-scan').disabled=active||busyCount>0||!state?.garak.version;
  $('start-scan').hidden=active; $('stop-scan').hidden=!active;
  $('stop-scan').disabled=state?.jobs.find(j=>j.id===state.active_job)?.status==='stopping';
  $('save-target').disabled=active||busyCount>0;
  $('cancel-test').hidden=!testRunning;
  const job=lastDetail?.job;
  document.querySelectorAll('.download').forEach(b=>b.disabled=!job||!terminal.has(job.status));
}
async function action(button, fn) {
  busyCount++; button.classList.add('busy-button'); controls();
  try {await fn();} catch(e) {toast(e.message,true);connection(e.message,true);} finally {busyCount--;button.classList.remove('busy-button'); await poll(); controls();}
}
function renderProbes(probes) {
  $('probe-options').innerHTML=probes.map((p,i)=>`<label class="probe-option"><input type="checkbox" value="${esc(p.id)}" ${i<3?'checked':''}><div><strong>${esc(p.label)}<em>${esc(p.category)}</em></strong><p>${esc(p.description)}</p><code>${esc(p.id)}</code></div></label>`).join('');
  document.querySelectorAll('.probe-option input').forEach(x=>x.addEventListener('change',estimate)); estimate();
}
function badge(status) {return terminal.has(status)?(status==='completed'?'ready':status==='cancelled'?'muted':'failed'):'running';}
function showView(name) {
  ['workspace','history','guide'].forEach(v=>$(v+'-view').hidden=v!==name);
  document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('selected',b.dataset.view===name));
  $('view-title').textContent={workspace:'检测工作台',history:'历史报告',guide:'使用说明'}[name];
}
function renderHistory() {
  const jobs=state?.jobs||[]; $('history-count').textContent=jobs.length;
  if(!jobs.length){$('history-list').innerHTML='<div class="empty-state"><div class="empty-icon">◷</div><strong>还没有检测记录</strong><p>完成第一轮检测后，报告会显示在这里。</p></div>';return;}
  $('history-list').innerHTML=jobs.map(j=>`<div class="history-row"><div class="history-info"><strong>${esc(j.config.target.name)} ${j.demo?'· 本地模拟':''}</strong><p>${esc(new Date(j.created_at).toLocaleString('zh-CN'))} · ${j.answered} 个回答 · ${esc(j.config.target.url)}</p></div><span class="badge ${badge(j.status)}">${esc(statuses[j.status]||j.status)}</span><button class="button outline open-job" data-id="${esc(j.id)}">查看报告 ↗</button></div>`).join('');
  document.querySelectorAll('.open-job').forEach(b=>b.addEventListener('click',async()=>{selectedJob=b.dataset.id;lastDetail=null;showView('workspace');await refreshDetail();$('results-content').scrollIntoView({behavior:'smooth',block:'center'});}));
}
function setTab(tab) {selectedTab=tab;$('logs-content').hidden=tab!=='logs';$('results-content').hidden=tab!=='results';document.querySelectorAll('.result-tab').forEach(b=>b.classList.toggle('selected',b.dataset.tab===tab));}
function renderDetail(detail) {
  lastDetail=detail; const {job,summary,attempts}=detail;
  $('metric-tested').textContent=summary.evaluated;
  $('metric-flagged').textContent=summary.flagged; $('metric-flagged').style.color=summary.flagged?'#b7774d':'';
  $('metric-errors').textContent=job.errors;
  $('metric-status').textContent=`${statuses[job.status]||job.status} · ${job.answered} 个网页回答`;
  $('job-badge').className='badge '+badge(job.status);$('job-badge').textContent=(job.demo?'模拟 · ':'')+(statuses[job.status]||job.status);
  $('job-info').hidden=false;$('job-info').classList.toggle('error',Boolean(job.error));
  const context=job.config.target.reset_each_prompt?'每题新对话':'连续对话';
  const progress=job.current_probe?` · 当前：${job.current_probe}`:'';
  $('job-info').textContent=`${job.demo?'本地模拟网页 · ':''}${job.config.target.name} · ${context} · 已发送 ${job.sent} / 已回答 ${job.answered}${progress}${job.error?' · '+job.error:''}`;
  $('rate-note').textContent=summary.no_signal_rate===null?'等待检测器判定':`未命中比例 ${summary.no_signal_rate}% · ${summary.unevaluated} 项待判定`;
  if(!attempts.length) {
    $('results-content').innerHTML=`<div class="empty-state"><div class="empty-icon">${terminal.has(job.status)?'◎':'⌁'}</div><strong>${terminal.has(job.status)?'本次没有已判定记录':'正在收集网页回答'}</strong><p>${terminal.has(job.status)?'可查看运行日志定位连接或检测问题。':'garak 会在每组回答收集完成后执行检测器。'}</p></div>`;
  } else {
    const opened=new Set([...document.querySelectorAll('.result-row[open]')].map(x=>x.dataset.id));
    const labels=Object.fromEntries(state.probes.map(p=>[p.id,p.label]));
    $('results-content').innerHTML=attempts.map((r,i)=>`<details class="result-row" data-id="${esc(r.id)}" ${opened.has(r.id)?'open':''}><summary><span class="row-index">${String(i+1).padStart(2,'0')}</span><span class="row-title">${esc(labels[r.probe]||r.probe)}<small>${esc(r.probe)}</small></span><span class="row-marker ${r.flagged?'hit':r.evaluated?'':'pending'}">${r.flagged?'◉ 疑似风险 · 需复核':r.evaluated?'○ 本项未命中':'◌ 等待判定'} ↗</span></summary><div class="row-body"><h3>测试问题</h3><pre>${esc(r.prompt)}</pre><h3>网页回答</h3><pre class="response">${esc(r.outputs.join('\n'))}</pre><h3>检测器分值 · ≥ 0.5 为命中</h3><pre>${esc(JSON.stringify(r.detectors,null,2))}</pre></div></details>`).join('');
  }
  const logs=job.logs.join('\n')||'正在启动 garak…';
  if($('logs-content').textContent!==logs){const nearBottom=$('logs-content').scrollHeight-$('logs-content').scrollTop-$('logs-content').clientHeight<60;$('logs-content').textContent=logs;if(nearBottom)$('logs-content').scrollTop=$('logs-content').scrollHeight;}
  setTab(selectedTab);controls();
}
async function refreshDetail() {if(selectedJob)renderDetail(await api('/api/jobs/'+selectedJob));}
async function poll() {
  if(polling)return;polling=true;
  try {
    state=await api('/api/state');
    if(!initialized){fillTarget(state.target);renderProbes(state.probes);initialized=true;selectedJob=state.active_job||state.jobs[0]?.id||null;}
    $('engine-version').textContent=state.garak.version?'garak '+state.garak.version:'garak 未安装';
    $('engine-dot').classList.toggle('online',Boolean(state.garak.version));
    $('engine-location').title=state.garak.python;
    $('browser-badge').className='badge '+(state.browser.open?'ready':'muted');
    $('browser-badge').textContent=state.browser.busy?'浏览器操作中':state.browser.open?'浏览器已打开':'浏览器未打开';
    $('browser-badge').title=state.browser.url||'';
    if(testRunning&&state.browser.busy&&state.browser.phase)connection(`${state.browser.phase} · ${state.browser.elapsed_s} 秒 / ${Number($('timeout').value)} 秒\n当前找到 ${state.browser.answer_candidates} 个回答候选。可以取消后重新点选回答区域。`);
    if(state.active_job&&!selectedJob)selectedJob=state.active_job;
    renderHistory();await refreshDetail();controls();
  } catch(e){if(!initialized)toast('无法连接本地服务：'+e.message,true);}finally{polling=false;}
}

$('open-browser').addEventListener('click',e=>action(e.currentTarget,async()=>{const s=await api('/api/browser/open',target());connection('浏览器已打开。请在新窗口完成登录并进入聊天页面，然后检查选择器。\n当前地址：'+s.url);toast('请在独立浏览器窗口完成登录');}));
$('discover-button').addEventListener('click',e=>action(e.currentTarget,async()=>{
  const result=await api('/api/browser/discover',{}), box=$('discovery-result');box.hidden=false;
  const choose=c=>{ $('input-selector').value=c.input_selector;$('frame-selector').value=c.frame_selector;$('send-selector').value=c.send_selector||'';$('response-mode').value='auto';$('response-selector').value=c.response_selector||'';connection('已定位候选对话：'+c.label+'\n所属页面：'+(c.frame_selector||'主页面')+'\n请做连接测试，确认读取的是新的完整 AI 回答。');};
  box.innerHTML='<strong>候选对话区</strong>'+(!result.candidates.length?'<p>未找到可用输入框。先展开助手、完成登录，或使用点选。</p>':result.candidates.map((c,i)=>`<button type="button" class="discovery-choice" data-candidate="${i}"><b>${esc(c.label)}</b><small>${c.visible?'可见':'可能折叠或在屏幕外'} · ${esc(c.frame_selector||'主页面')} · ${esc(c.input_selector)}</small></button>`).join(''))+(result.launchers.length?'<strong>可能的助手入口</strong>'+result.launchers.map((c,i)=>`<button type="button" class="discovery-choice" data-launcher="${i}"><b>${esc(c.label)}</b><small>${esc(c.launcher_frame_selector||'主页面')}</small></button>`).join(''):'');
  box.querySelectorAll('[data-candidate]').forEach(b=>b.addEventListener('click',()=>choose(result.candidates[Number(b.dataset.candidate)])));
  box.querySelectorAll('[data-launcher]').forEach(b=>b.addEventListener('click',()=>{const c=result.launchers[Number(b.dataset.launcher)];$('launcher-selector').value=c.launcher_selector;$('launcher-frame-selector').value=c.launcher_frame_selector;connection('已选助手入口。点击“展开助手”，再定位对话区。');}));
  if(result.candidates.length===1&&result.candidates[0].visible)choose(result.candidates[0]);else connection('找到 '+result.candidates.length+' 个候选对话区。请从列表选择，再做连接测试。');
}));
$('inspect-button').addEventListener('click',e=>action(e.currentTarget,async()=>{const result=await api('/api/browser/inspect',target());const labels={input_selector:'输入框',response_selector:'AI 回答区域',send_selector:'发送按钮',new_chat_selector:'新对话按钮',busy_selector:'生成中状态'};const lines=Object.entries(result.selectors).map(([k,v])=>`${labels[k]}：${v.configured?`匹配 ${v.count} 个 / 可见 ${v.visible} 个`:'未配置'}`);if(result.answer_candidates!==null)lines.push('自动模式找到 '+result.answer_candidates+' 个候选回答区域；新对话可先做连接测试。');if(!result.chat_visible)lines.push('输入框未在当前可见区域内。浮动助手请配置入口，并确认已登录。');if(result.frames.length)lines.push('发现 iframe（点选内部元素会自动填写所属 iframe）：\n'+result.frames.map(f=>f.selector).join('\n'));connection(lines.join('\n'));}));
$('reveal-button').addEventListener('click',e=>action(e.currentTarget,async()=>{if(!$('launcher-selector').value.trim())throw new Error('请先点选或填写助手入口');await api('/api/browser/reveal',target());connection('已检查并展开助手。可以点选输入框和回答区域。');}));
$('test-button').addEventListener('click',e=>action(e.currentTarget,async()=>{testRunning=true;controls();connection('正在发送连接测试，等待新的完整回答…');try{const result=await api('/api/browser/test',{target:target()});let extra='';if($('response-mode').value==='auto'&&result.detected_selector){$('response-selector').value=result.detected_selector;extra='\n自动识别的回答选择器：'+result.detected_selector+'\n正式检测前请确认该区域包含完整回答。';}connection('收到网页回答：\n'+result.response+extra);toast('已成功发送问题并读取网页回答');}finally{testRunning=false;controls();}}));
$('cancel-test').addEventListener('click',async()=>{try{await api('/api/browser/cancel',{});connection('正在取消连接测试…');}catch(e){toast(e.message,true);}});
$('target-form').addEventListener('submit',e=>{e.preventDefault();action($('save-target'),async()=>{await api('/api/target',target());toast('目标配置已保存在本机');});});
$('max-per-probe').addEventListener('change',estimate);
$('start-scan').addEventListener('click',e=>action(e.currentTarget,async()=>{if(!selectedProbes().length)throw new Error('请至少选择一组探针');const result=await api('/api/jobs',{target:target(),probes:selectedProbes(),max_per_probe:Number($('max-per-probe').value)});selectedJob=result.id;lastDetail=null;toast('garak 安全检测已开始');}));
$('stop-scan').addEventListener('click',async()=>{try{await api('/api/jobs/'+state.active_job+'/stop',{});toast('已停止后续测试。网页中已开始的回答可能仍需手动停止。');await poll();}catch(e){toast(e.message,true);}});
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>showView(b.dataset.view)));
document.querySelectorAll('.result-tab').forEach(b=>b.addEventListener('click',()=>setTab(b.dataset.tab)));
document.querySelectorAll('.pick').forEach(b=>b.addEventListener('click',()=>action(b,async()=>{
  clearInterval(pickTimer);await api('/api/browser/pick',{kind:b.dataset.kind});pickStarted=Date.now();
  connection('点选模式已开启。切换到登录浏览器，点击对应元素；按 Esc 可以取消。');toast('请切换到浏览器点击网页元素');
  let checking=false;
  pickTimer=setInterval(async()=>{if(checking)return;checking=true;try{if(Date.now()-pickStarted>180000){clearInterval(pickTimer);connection('点选等待已结束。在浏览器按 Esc 退出，或再次点击点选。');return;}const r=await api('/api/browser/pick-result',{});if(r){clearInterval(pickTimer);if(r.cancelled){connection('已取消点选');return;}$(selectorFields[r.kind]).value=r.selector;if(r.kind==='response')$('response-mode').value='selector';const frameField=r.kind==='launcher'?'launcher-frame-selector':'frame-selector';const changed=$(frameField).value!==(r.frame_selector||'');$(frameField).value=r.frame_selector||'';connection('已选择 '+r.tag+'：'+r.selector+'\n所属页面：'+(r.frame_selector||'主页面')+'\n请检查此选择器是否适用于后续新增的元素。'+(changed&&r.kind!=='launcher'?'\n聊天 iframe 已更新，请在同一个聊天页面点选其他输入、回答和按钮。':''));toast('选择器及 iframe 已填入');}}catch(e){clearInterval(pickTimer);toast(e.message,true);}finally{checking=false;}},1000);
})));
$('demo-button').addEventListener('click',e=>action(e.currentTarget,async()=>{
  fillTarget({name:'本地模拟聊天网页',url:location.origin+'/demo?vulnerable=1',channel:'msedge',browser_mode:'standard',response_mode:'auto',input_selector:'#chat-input',response_selector:'.assistant-message',send_selector:'#send-button',new_chat_selector:'#new-chat',busy_selector:'#stop-button',frame_selector:'',launcher_selector:'',launcher_frame_selector:'',reset_each_prompt:true,timeout_s:30,stable_ms:700,min_response_ms:900,delay_s:0.2});
  const result=await api('/api/browser/open',target());connection('已打开本地模拟网页，无需登录。页面会故意输出探针标记，用于验证真实 garak 检测链路；不代表真实 AI 产品的安全性。');toast('模拟网页已打开，可以连接测试或开始检测');
}));
document.querySelectorAll('.download').forEach(b=>b.addEventListener('click',async()=>{
  try{const response=await fetch(`/api/jobs/${selectedJob}/download/${b.dataset.type}`,{headers:{'X-Console-Token':token}});if(!response.ok)throw new Error((await response.json()).error);const blob=await response.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=`webguard-${selectedJob.slice(0,8)}.${b.dataset.type}`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){toast(e.message,true);}
}));
poll();setInterval(poll,1500);
