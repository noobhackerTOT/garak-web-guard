"""Read-only discovery of editable chat candidates and floating assistant entry points."""
DISCOVER_SCRIPT = r"""body => {
  const roots=[body], inputs=[], launchers=[];
  for(let i=0;i<roots.length;i++) for(const el of roots[i].querySelectorAll('*')) if(el.shadowRoot) roots.push(el.shadowRoot);
  const hint = el => [el.getAttribute('aria-label'),el.getAttribute('title'),el.getAttribute('placeholder'),el.getAttribute('name'),el.id,typeof el.className==='string'?el.className:''].filter(Boolean).join(' ');
  const chat = /chat|message|question|prompt|assistant|ask|聊天|消息|问题|提问|问我|对话/i;
  const send = /send|submit|发送|提交|提问/i;
  const visible = el => {let r=el.getBoundingClientRect(),s=getComputedStyle(el);return Boolean(r.width&&r.height&&s.visibility!=='hidden'&&s.display!=='none')};
  for(const root of roots) {
    for(const el of root.querySelectorAll('textarea,input[type="text"],input[type="search"],input:not([type]),[contenteditable="true"],[role="textbox"]')) {
      if(el.disabled||el.readOnly||el.closest('nav,header,footer,[role="navigation"]')) continue;
      let score=el.matches('textarea,[contenteditable="true"]')?25:5;
      if(chat.test(hint(el))) score+=60;
      if(el.getAttribute('role')==='textbox') score+=10;
      let button='', best=0, parent=el.parentElement||el.getRootNode();
      for(let level=0;parent&&level<5;level++,parent=parent.parentElement) {
        for(const b of parent.querySelectorAll('button,[role="button"],a')) {
          if(!visible(b)) continue;
          const label=hint(b)+' '+(b.innerText||'').slice(0,80);
          let rank=(send.test(label)?60:0)+(b.getAttribute('type')==='submit'?35:0)-level*5;
          if(rank>best){best=rank;button=window.__wgSelector(b,'send');}
        }
        if(best>=60) break;
      }
      if(button) score+=15;
      inputs.push({input_selector:window.__wgSelector(el,'input'),send_selector:button,score,tag:el.tagName,
        label:el.getAttribute('placeholder')||el.getAttribute('aria-label')||el.getAttribute('name')||el.tagName,
        rendered:visible(el)});
    }
    for(const el of root.querySelectorAll('button,[role="button"],a,[onclick]')) {
      if(!visible(el)) continue;
      const text=(el.innerText||'').trim();
      const label=hint(el)+' '+(text.length<=40?text:'');
      if(!/assistant|chatbot|live.chat|客服|助手|AI\b|AI[\u4e00-\u9fff]|在线咨询/i.test(label)) continue;
      if(/close|关闭|停止|stop|send|发送/i.test(hint(el))) continue;
      launchers.push({launcher_selector:window.__wgSelector(el,'launcher'),label:text||el.getAttribute('aria-label')||el.getAttribute('title')||'助手入口'});
    }
  }
  return {inputs:inputs.slice(0,40),launchers:launchers.slice(0,20)};
}"""
