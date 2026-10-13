"""Identify answer candidates by DOM semantics; never treat arbitrary page changes as answers."""
AUTO_SCRIPT = r"""(body, options) => {
  if (!window.__wgIds) {window.__wgIds=new WeakMap();window.__wgId=0;}
  if (!window.__wgDocument) window.__wgDocument=Math.random().toString(36).slice(2);
  const norm = s => s.replace(/\s+/g,'').trim();
  const prompt=norm(options.prompt), expected=options.expected;
  const roots=[body];
  for(let i=0;i<roots.length;i++) for(let el of roots[i].querySelectorAll('*')) if(el.shadowRoot) roots.push(el.shadowRoot);
  const result=[];
  for(const root of roots) for(const el of root.querySelectorAll('*')) {
    if(el.matches('script,style,link,meta,svg,button,input,textarea,select,option,[contenteditable="true"]')) continue;
    if(el.closest('nav,header,footer,button,[role="button"],[role="navigation"],[data-message-author-role="user"],[data-role="user"]')) continue;
    const role=[el.getAttribute('data-message-author-role'),el.getAttribute('data-role'),el.getAttribute('data-author')].filter(Boolean).join(' ');
    if(/\b(user|human)\b/i.test(role)) continue;
    const hint=el.className && typeof el.className==='string' ? el.className : '';
    const attrs=(hint+' '+(el.getAttribute('data-testid')||'')).replace(/([a-z])([A-Z])/g,'$1 $2');
    if(/(?:^|[\s_-])(question|prompt|reasoning|thinking)(?:$|[\s_-])/i.test(attrs)) continue;
    let score=/\b(assistant|model|bot)\b/i.test(role)?100:0;
    if(/(?:^|[\s_-])(markdown|answer|response|assistant|bot)(?:$|[\s_-])/i.test(attrs)) score=Math.max(score,80);
    try {if(options.configured && el.matches(options.configured)) score=120;} catch {}
    if(!score && !expected) continue;
    const r=el.getBoundingClientRect(), style=getComputedStyle(el);
    if(!r.width||!r.height||style.visibility==='hidden'||style.display==='none') continue;
    const text=(el.innerText||'').trim();
    if(!text || norm(text)===prompt) continue;
    if(score<100 && prompt && norm(text).includes(prompt)) continue;
    let candidate=el;
    if(!score && expected && text===expected && ![...el.children].some(c=>(c.innerText||'').trim()===text)) {
      score=35;
      // A harmless connectivity reply lets us learn a container even without role attributes.
      while(candidate.parentElement && candidate.parentElement!==body && (candidate.parentElement.innerText||'').trim()===expected)
        candidate=candidate.parentElement;
    }
    if(!score) continue;
    if(!window.__wgIds.has(candidate)) window.__wgIds.set(candidate,++window.__wgId);
    result.push({id:window.__wgIds.get(candidate),document:window.__wgDocument,text,score,
      selector:window.__wgSelector(candidate,'response'),order:result.length});
  }
  return result.slice(-500);
}"""


def fresh_candidate(before, current):
    old = {(r["document"], r["id"]): r["text"] for r in before}
    old_texts = {r["text"] for r in before}
    old_documents = {r["document"] for r in before}
    candidates = []
    for row in current:
        key = (row["document"], row["id"])
        fresh = key not in old or old[key] != row["text"]
        # A navigation can recreate history nodes. Identical historical text alone is insufficient.
        if before and row["document"] not in old_documents and row["text"] in old_texts:
            fresh = False
        if fresh and row["text"].strip():
            candidates.append(row)
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r["score"], r["order"]))
