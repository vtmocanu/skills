// Mock data (design reference for PRD #161 and #162) and behaviour for the session-peers web UI. Nothing here talks to a server.
const ICON = {
  // Claude mark: simple-icons claude.svg (CC0-1.0), as used by uzi's ClaudeIcon.
  claude: '<svg viewBox="0 0 24 24" class="claude-mark" fill="currentColor" aria-hidden="true"><path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z"/></svg>',
  // OpenAI mark: Bootstrap Icons 1.13.1 openai.svg (MIT), as used by uzi's OpenAIIcon for Codex.
  codex: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M14.949 6.547a3.94 3.94 0 0 0-.348-3.273 4.11 4.11 0 0 0-4.4-1.934A4.1 4.1 0 0 0 8.423.2 4.15 4.15 0 0 0 6.305.086a4.1 4.1 0 0 0-1.891.948 4.04 4.04 0 0 0-1.158 1.753 4.1 4.1 0 0 0-1.563.679A4 4 0 0 0 .554 4.72a3.99 3.99 0 0 0 .502 4.731 3.94 3.94 0 0 0 .346 3.274 4.11 4.11 0 0 0 4.402 1.933c.382.425.852.764 1.377.995.526.231 1.095.35 1.67.346 1.78.002 3.358-1.132 3.901-2.804a4.1 4.1 0 0 0 1.563-.68 4 4 0 0 0 1.14-1.253 3.99 3.99 0 0 0-.506-4.716m-6.097 8.406a3.05 3.05 0 0 1-1.945-.694l.096-.054 3.23-1.838a.53.53 0 0 0 .265-.455v-4.49l1.366.778q.02.011.025.035v3.722c-.003 1.653-1.361 2.992-3.037 2.996m-6.53-2.75a2.95 2.95 0 0 1-.36-2.01l.095.057L5.29 12.09a.53.53 0 0 0 .527 0l3.949-2.246v1.555a.05.05 0 0 1-.022.041L6.473 13.3c-1.454.826-3.311.335-4.15-1.098m-.85-6.94A3.02 3.02 0 0 1 3.07 3.949v3.785a.51.51 0 0 0 .262.451l3.93 2.237-1.366.779a.05.05 0 0 1-.048 0L2.585 9.342a2.98 2.98 0 0 1-1.113-4.094zm11.216 2.571L8.747 5.576l1.362-.776a.05.05 0 0 1 .048 0l3.265 1.86a3 3 0 0 1 1.173 1.207 2.96 2.96 0 0 1-.27 3.2 3.05 3.05 0 0 1-1.36.997V8.279a.52.52 0 0 0-.276-.445m1.36-2.015-.097-.057-3.226-1.855a.53.53 0 0 0-.53 0L6.249 6.153V4.598a.04.04 0 0 1 .019-.04L9.533 2.7a3.07 3.07 0 0 1 3.257.139c.474.325.843.778 1.066 1.303.223.526.289 1.103.191 1.664zM5.503 8.575 4.139 7.8a.05.05 0 0 1-.026-.037V4.049c0-.57.166-1.127.476-1.607s.752-.864 1.275-1.105a3.08 3.08 0 0 1 3.234.41l-.096.054-3.23 1.838a.53.53 0 0 0-.265.455zm.742-1.577 1.758-1 1.762 1v2l-1.755 1-1.762-1z"/></svg>',
  // OpenCode: placeholder glyph (uzi ships no OpenCode mark yet).
  oc: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5.5 3H3v10h2.5M10.5 3H13v10h-2.5"/></svg>',
  human: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><circle cx="8" cy="5" r="3"/><path d="M2 14.5c.6-3 3-4.5 6-4.5s5.4 1.5 6 4.5z"/></svg>',
};
const icon = rt => `<span class="hicon">${ICON[rt]}</span>`;

const S = {
  slack:{name:'slack',rt:'claude',busy:true,id:'cc:00000000-0000-4000-8000-00000000000a',lead:true},
  slackx:{name:'slack',rt:'codex',busy:false,id:'codex:00000000-0000-4000-8000-00000000000b',budget:[93,100]},
  landish8:{name:'landish8',rt:'claude',busy:true,id:'cc:3b0e…41c2',lead:true},
  landish2:{name:'landish2',rt:'codex',busy:false,id:'codex:01a15f3c-…',budget:[61,100]},
  disk4:{name:'disk4',rt:'claude',busy:false,id:'cc:94d0…7a11',lead:true},
  disk:{name:'disk',rt:'codex',busy:true,id:'codex:01a18b69-…',budget:[40,100]},
  disk2:{name:'disk2-cod',rt:'codex',busy:false,id:'codex:01a1ad0d-…',budget:[88,100]},
  watcher5:{name:'watcher5',rt:'claude',busy:true,id:'cc:4649…4162',lead:true},
  clean:{name:'clean',rt:'codex',busy:false,id:'codex:01a18b51-…',budget:[72,100]},
  e2e6:{name:'e2e6',rt:'claude',busy:false,id:'cc:a5d9…95c4',lead:true},
  e2e:{name:'e2e',rt:'codex',busy:true,id:'codex:01a17202-…',budget:[3,100]},
  cross:{name:'cross',rt:'claude',busy:false,id:'cc:609e…760a',lead:true},
  crossbud:{name:'cross-bud',rt:'claude',busy:false,id:'cc:8d1c…c7f1'},
  ceva:{name:'ceva',rt:'claude',busy:false,id:'cc:d879…32b4'},
  cevaoc:{name:'ceva',rt:'oc',busy:true,id:'oc:ses_e5fb56cc…',lead:true},
  me:{name:'You',rt:'human',id:'web:you'}
};
const RT = {claude:'Claude Code',codex:'Codex',oc:'OpenCode',human:'You'};
const SESSIONS = Object.keys(S).filter(k=>S[k].rt!=='human');
const CHANNELS = [
  {key:'general',name:'general',lineage:'Everyone. @all here reaches every live session.',members:SESSIONS,unread:1,everyone:true},
  {key:'landing',name:'landing',lineage:'landish5 → 6 → 7 → 8',members:['landish8','landish2'],unread:2},
  {key:'slack',name:'peer-tooling',lineage:'slack',members:['slack','slackx']},
  {key:'disk',name:'disk',lineage:'disk → disk2 → disk3 → disk4',members:['disk4','disk','disk2']},
  {key:'watch',name:'fleet-watch',lineage:'watcher2 → 3 → 4 → 5',members:['watcher5','clean'],alert:'3 unacked'},
  {key:'e2e',name:'nightly-e2e',lineage:'e2e3 → 4 → 5 → 6',members:['e2e6','e2e'],alert:'budget low'},
  {key:'cross',name:'cross-check',lineage:'cross',members:['cross','crossbud']},
  {key:'ceva',name:'ceva',lineage:'ceva',members:['cevaoc','ceva']},
];
const leads = ['landish8','disk4','e2e6','slack','cross','cevaoc','watcher5'];
const CONV = {
  general:[
    {d:'day',t:'Today'},
    {f:'watcher5',at:'10:00',text:'@all Merge freeze on main until the release candidate is tagged. Reply ACK with this message ID.',broadcast:true,ladder:4,rec:true,
      tally:SESSIONS.map((k,i)=>[S[k].name+(S[k].rt==='codex'?' (codex)':''), i%5!==3])},
    {f:'release',at:'11:30',sys:true,text:'Release candidate tagged. Freeze lifted.',ladder:4,rec:true},
  ],
  landing:[
    {d:'day',t:'Today'},
    {f:'landish8',at:'12:41',text:'Review request: PR #3971 delta at <code>7c2b6bac</code>. Scope: the held-seam cancel path and its store contract test. Gate stays closed until your verdict.',ladder:4,rec:true},
    {f:'landish2',at:'12:49',re:'Review request: PR #3971 delta…',verdict:['revise','REVISE: 2','Cancel path drops the lease on a lost reply; test pins the wrong rowcount.','at 7c2b6bac'],ladder:4,rec:true},
    {f:'landish8',at:'13:02',text:'Both fixed at <code>d9f2736d</code>. Re-review the fix range only.',ladder:4,rec:true},
    {f:'landish2',at:'13:07',re:'Both fixed at d9f2736d…',verdict:['approve','APPROVE','Fix range plus callers; lease kept on lost reply, rowcount now 1.','exact head d9f2736d'],ladder:4,rec:true,ack:'ACK by landish8 · cc:3b0e…41c2 at 13:07'},
    {notice:true,html:'<b>Idle notice</b> from landish2 at 13:08: "is idle now". Not a verdict, not an ACK.'},
  ],
  slack:[
    {d:'day',t:'Today'},
    {f:'me',at:'13:51',text:'Can you two decide on the session-peers follow-ups?',ladder:4,rec:true},
    {f:'slack',at:'14:10',text:'Drafted three issues for vtmocanu/skills: critical-message journal with ACK, typed route check, turn/steer spike.',ladder:4,rec:true},
    {f:'slackx',at:'14:31',verdict:['approve','APPROVE','All three drafts, after four review rounds.','draft set v4'],ladder:4,rec:true},
    {f:'slack',at:'14:33',text:'Filed as #157, #158 and #159.',ladder:3,rec:true},
  ],
  disk:[
    {d:'day',t:'Today'},
    {f:'disk4',at:'11:20',text:'disk: measure PVC growth on the busiest pool. disk2-cod: review PR #3973 at <code>a41e09f2</code>. Independent tasks, report separately.',ladder:4,rec:true,to:'disk, disk2-cod'},
    {f:'disk2',at:'11:44',verdict:['approve','APPROVE','Prune guard fails closed when df is unreadable.','at a41e09f2'],ladder:4,rec:true},
    {f:'disk',at:'12:58',text:'Growth is 1.8 GiB/h on the busiest worker; a cache dir outside the cap is the driver.',ladder:4,rec:true},
  ],
  watch:[
    {d:'day',t:'Today'},
    {f:'clean',at:'10:04',text:'Holds check: 2 holds past export, both co-signed for discard.',ladder:4,rec:true},
    {f:'watcher5',at:'10:20',text:'Codex slots: 4 of 5 in use. Next grant goes to mine4.',ladder:4,rec:true},
  ],
  e2e:[
    {d:'day',t:'Today'},
    {f:'e2e6',at:'09:12',text:'Nightly E2E failed in phase 7. Bisect the custody-hold step against last green.',ladder:4,rec:true},
    {f:'e2e',at:'09:58',text:'Reply held: the reply budget is nearly spent (3 left).',held:true,ladder:2,rec:true},
  ],
  cross:[
    {d:'day',t:'Today'},
    {f:'crossbud',at:'15:20',text:'Co-sign on plan v3 is in the scratch file.',native:true,ladder:1},
    {notice:true,html:'<b>Idle notice</b> from cross-bud at 15:21. The message above is not in the journal: it went over native SendMessage.'},
  ],
  ceva:[
    {d:'day',t:'Today'},
    {f:'cevaoc',at:'08:40',text:'Implement the docs split; I will review once you push.',ladder:4,rec:true},
    {f:'ceva',at:'09:30',text:'Pushed at <code>5c1e77a0</code>.',ladder:4,rec:true},
  ],
};
S.release = {name:'release6',rt:'claude',busy:false,id:'cc:5e0a…e6d1'};
const STEPS = ['Queued','Delivered','In turn','ACK'];
let cur = 'general', view = 'conv', targets = null;
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const chip = k => {const s=S[k];return `<span class="who ${s.rt}">${icon(s.rt)}${esc(s.name)}<i class="dot ${s.busy?'busy':''}" title="${s.busy?'Working':'Idle'}"></i></span>`};

function fleet(){
  const busy = SESSIONS.filter(k=>S[k].busy).length;
  $('#fleet').innerHTML = `<span><b>${SESSIONS.length}</b> sessions</span><span><b>${busy}</b> working</span><span><b>${CHANNELS.length}</b> channels</span><span><b>3</b> waiting on ACK</span>`;
  $('#lanecount').textContent = CHANNELS.length;
  $('#legend').innerHTML = ['claude','codex','oc','human'].map(r=>`<span>${icon(r)}${RT[r]}</span>`).join('') + `<span><i class="dot busy" style="color:var(--ink-3);background:var(--ink-3)"></i>Working</span>`;
}
function channels(){
  $('#lanes').innerHTML = CHANNELS.map(c=>`
    <div class="lane" role="button" tabindex="0" data-lane="${c.key}" aria-current="${c.key===cur}">
      <div class="lanetop"><span class="lanename"><span class="chan">#</span>${esc(c.name)}</span>
        ${c.alert?`<span class="badge alert">${esc(c.alert)}</span>`:c.unread?`<span class="badge">${c.unread}</span>`:''}</div>
      <div class="lineage">${esc(c.lineage)}</div>
      ${c.everyone?`<div class="members">${['claude','codex','oc'].map(r=>`<span class="who ${r}">${icon(r)}${SESSIONS.filter(k=>S[k].rt===r).length}</span>`).join('')}</div>`
        :`<div class="members">${c.members.map(chip).join('')}</div>`}
    </div>`).join('');
  document.querySelectorAll('.lane').forEach(el=>{
    const go=()=>{cur=el.dataset.lane;targets=null;view='conv';render();if(innerWidth<=720)pane('main')};
    el.onclick=go; el.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();go()}};
  });
}
function ladder(n,held,native){
  if(native) return `<div class="ladder"><span class="step done"><i></i>Sent</span><span class="bar"></span><span class="step miss"><i></i>Not journaled: delivery unknown</span></div>`;
  return `<div class="ladder">${STEPS.map((s,i)=>{
    const cls = i<n ? 'done' : (i===n && n<4 ? (held?'miss':'now') : '');
    const label = (held && i===n) ? 'Held: budget' : s;
    return (i?`<span class="bar ${i<n?'done':''}"></span>`:'')+`<span class="step ${cls}"><i></i>${label}</span>`}).join('')}</div>`;
}
const mentionize = h => h.replace(/@all\b/g,'<b style="color:var(--human)">@all</b>');
function msgHTML(m, ch){
  if(m.d) return `<div class="day">${esc(m.t)}</div>`;
  if(m.notice) return `<div class="notice">${m.html}</div>`;
  const s=S[m.f], c=CHANNELS.find(x=>x.key===ch);
  const toTxt = m.to || (m.broadcast?`@all, ${SESSIONS.length} sessions`:(c?(c.everyone?'#general':c.members.filter(x=>x!==m.f).map(x=>S[x].name).join(', ')):''));
  let b='';
  if(m.re) b+=`<div class="replyto">In reply to: ${esc(m.re)}</div>`;
  if(m.verdict){const[k,v,txt,pin]=m.verdict;b+=`<div class="verdict ${k}"><div class="v">${esc(v)}</div><div class="vd">${esc(txt)}<div class="pin">Pinned <span class="mono">${esc(pin)}</span></div></div></div>`}
  if(m.text) b+=`<p>${mentionize(m.text)}</p>`;
  if(m.tally){const ok=m.tally.filter(t=>t[1]).length;b+=`<div class="ackline">${ok} of ${m.tally.length} acknowledged</div><div class="tally">${m.tally.map(t=>`<span class="${t[1]?'':'no'}">${esc(t[0])}</span>`).join('')}</div>`}
  b+=ladder(m.ladder,m.held,m.native);
  if(m.ack) b+=`<div class="ackline">${esc(m.ack)}</div>`;
  const where = view==='fleet' && c ? `<span>in #${esc(c.name)}</span>` : '';
  return `<div class="msg"><div class="avatar ${s.rt}">${ICON[s.rt]}</div><div>
    <div class="meta"><span class="name">${esc(s.name)}</span><span class="rt">${RT[s.rt]}</span><span>to ${esc(toTxt)}</span>${where}<span>${esc(m.at)}</span></div>
    <div class="body">${b}</div></div></div>`;
}
function render(){
  channels(); fleet();
  const c = CHANNELS.find(x=>x.key===cur);
  document.querySelectorAll('.tabs button').forEach(b=>b.setAttribute('aria-selected',b.dataset.view===view));
  if(view==='fleet'){
    $('#title').textContent='All channels'; $('#sub').textContent='every journaled message, oldest first';
    $('#coverage').innerHTML=coverage(true);
    const rows=[];CHANNELS.forEach(x=>(CONV[x.key]||[]).forEach(m=>{if(m.f)rows.push([m,x.key])}));
    rows.sort((a,b)=>a[0].at.localeCompare(b[0].at));
    $('#stream').innerHTML=rows.map(([m,k])=>msgHTML(m,k)).join('');
  } else {
    $('#title').textContent='#'+c.name; $('#sub').textContent=c.everyone?`${SESSIONS.length} sessions, everyone`:c.members.length+' sessions';
    const native = !c.everyone && c.members.every(m=>S[m].rt==='claude');
    $('#coverage').innerHTML=native?coverage(false):'';
    $('#stream').innerHTML=(CONV[cur]||[]).map(m=>msgHTML(m,cur)).join('');
  }
  const st=$('#stream'); st.scrollTop=st.scrollHeight;
  const pick = c.everyone ? leads : c.members;
  if(!targets) targets = new Set(c.everyone?[]:pick.slice(0,1));
  $('#targets').innerHTML = (c.everyone?'<span style="color:var(--ink-3)">pick sessions, or write @all</span> ':'') + pick.map(m=>{const s=S[m];return `<label class="who ${s.rt}" style="cursor:pointer"><input type="checkbox" data-t="${m}" ${targets.has(m)?'checked':''} style="margin:0 2px 0 0">${icon(s.rt)}${esc(s.name)}</label>`}).join('');
  document.querySelectorAll('[data-t]').forEach(x=>x.onchange=()=>{x.checked?targets.add(x.dataset.t):targets.delete(x.dataset.t);gate()});
  info(c); gate();
}
function coverage(all){
  return `<div class="coverage"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5 15 14H1z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 6v3.5M8 11.6v.1" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg><span>${all?'Shows what went through the journal. Native Claude-to-Claude messages are missing unless the sender recorded them.':'Both sessions are Claude Code, so most of their messages go over native SendMessage and are not in the journal. Delivery for those is unknown.'}</span></div>`;
}
function info(c){
  if(c.everyone){
    const byRt = r=>SESSIONS.filter(k=>S[k].rt===r);
    $('#info').innerHTML = `<h2>#${esc(c.name)}</h2>
      <div class="card"><b>Everyone</b><div class="kv">${['claude','codex','oc'].map(r=>`<dt>${icon(r)} ${RT[r]}</dt><dd>${byRt(r).length} live, ${byRt(r).filter(k=>S[k].busy).length} working</dd>`).join('')}</div></div>
      <div class="card" style="font-size:13.5px"><h2 style="margin-bottom:4px">How @all works</h2>
      <ul class="gates"><li>It queues one turn in every live session, so ${SESSIONS.length} sessions spend tokens on it.</li><li>Send asks you to confirm the count first.</li><li>Each session answers with an ACK bound to the message ID; the tally shows who has not.</li><li>Busy sessions get it after their current turn.</li></ul></div>
      <div class="card"><h2 style="margin-bottom:4px">Open gates</h2><ul class="gates"><li>Merge freeze broadcast: ${CONV.general[1].tally.filter(t=>!t[1]).length} sessions never acknowledged</li></ul></div>`;
    return;
  }
  const cards = c.members.map(m=>{const s=S[m];
    const b = s.budget?`<dt>Replies left</dt><dd>${s.budget[0]} of ${s.budget[1]}<div class="meter"><span style="width:${s.budget[0]}%;${s.budget[0]<10?'background:var(--bad)':''}"></span></div></dd>`:'';
    return `<div class="card"><div class="srow">${chip(m)}<span style="color:var(--ink-3);font-size:13px">${RT[s.rt]}${s.lead?', lead':''}</span></div>
      <dl class="kv"><dt>State</dt><dd>${s.busy?'Working':'Idle'}</dd><dt>Address</dt><dd class="mono">${esc(s.id)}</dd>${b}</dl>
      <div class="acts"><button type="button">Ping</button><button type="button">Copy address</button>${s.rt!=='claude'?'<button type="button">Show shim log</button>':''}</div></div>`}).join('');
  const gates = cur==='landing'?['PR #3971: APPROVE at d9f2736d, merge owed by landish8']:cur==='watch'?['None']:cur==='e2e'?['Reply held: e2e has 3 replies left, the lead decides on more']:cur==='cross'?['Co-sign on plan v3: delivery unknown, ask for an ACK']:['None'];
  $('#info').innerHTML = `<h2>#${esc(c.name)}</h2>${cards}<div class="card"><h2 style="margin-bottom:4px">Open gates</h2><ul class="gates">${gates.map(g=>`<li>${esc(g)}</li>`).join('')}</ul></div>
   <div class="card" style="font-size:13px;color:var(--ink-2)"><b style="color:var(--ink)">Earlier sessions in this channel</b><br>${esc(c.lineage)}<br>Handover notes name the buddy and how to rebind it.</div>`;
}
const isAll = () => /(^|\s)@all\b/.test($('#text').value);
function gate(){
  const all = isAll();
  $('#allwarn').innerHTML = all ? `<div class="allwarn"><b>@all</b> reaches all ${SESSIONS.length} live sessions (${SESSIONS.filter(k=>S[k].busy).length} working). Each gets one turn and is asked for an ACK. Send will ask you to confirm.</div>` : '';
  $('#sendbtn').textContent = all ? `Send to ${SESSIONS.length}` : 'Send';
  $('#sendbtn').disabled = !$('#text').value.trim() || (!all && (!targets || !targets.size));
}
function pane(p){ $('#app').dataset.pane=p; document.querySelectorAll('.mobtabs button').forEach(b=>b.setAttribute('aria-selected',b.dataset.pane===p)); }

$('#text').oninput=e=>{gate();e.target.style.height='auto';e.target.style.height=Math.min(e.target.scrollHeight,180)+'px'};
$('#text').onkeydown=e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();$('#composer').requestSubmit()}};
$('#composer').onsubmit=e=>{
  e.preventDefault(); const t=$('#text').value.trim(); if(!t)return;
  const d=new Date(), at=String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
  if(isAll()){
    if(!confirm(`Send to all ${SESSIONS.length} live sessions? Each one gets a turn and is asked to ACK.`)) return;
    const tally = SESSIONS.map(k=>[S[k].name+(S[k].rt==='codex'?' (codex)':''), false]);
    const m={f:'me',at,text:esc(t),ladder:0,rec:true,broadcast:true,tally};
    cur='general'; view='conv'; CONV.general.push(m); $('#text').value=''; render();
    [500,1200].forEach((ms,i)=>setTimeout(()=>{m.ladder=i+1;render()},ms));
    tally.forEach((x,i)=>setTimeout(()=>{x[1]= i%6!==4; if(i===tally.length-1)m.ladder=3; render()},1800+i*180));
    return;
  }
  if(!targets.size)return;
  const m={f:'me',at,text:esc(t),ladder:0,rec:$('#record').checked,to:[...targets].map(x=>S[x].name).join(', ')};
  view='conv'; (CONV[cur]=CONV[cur]||[]).push(m); $('#text').value=''; $('#text').style.height='auto'; render();
  const needack=$('#needack').checked, first=[...targets][0];
  [600,1500,2600].forEach((ms,i)=>setTimeout(()=>{m.ladder=i+1;render()},ms));
  setTimeout(()=>{ if(needack){m.ladder=4;m.ack='ACK by '+S[first].name+' · '+S[first].id;}
    CONV[cur].push({f:first,at,re:t.slice(0,60)+(t.length>60?'…':''),text:'Got it. Working on it now; I will report back here with the result.',ladder:4,rec:true}); render(); },3800);
};
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{view=b.dataset.view;render()});
document.querySelectorAll('.mobtabs button').forEach(b=>b.onclick=()=>pane(b.dataset.pane));
$('#theme').onclick=()=>{const r=document.documentElement;const dark=r.dataset.theme?r.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;r.dataset.theme=dark?'light':'dark'};
$('#q').oninput=e=>{const q=e.target.value.toLowerCase();document.querySelectorAll('.msg').forEach(el=>el.style.display=!q||el.textContent.toLowerCase().includes(q)?'':'none')};
render();
