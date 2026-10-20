/* Compact additive companion HUD. Reads existing state only; it never polls. */
(function () {
'use strict';
const W=window,D=document,FAMILIES=['CODEX','CLAUDE','GROK','AGY'],ART=['JEV','MUSE','QWEN'];
const NAMES={CODEX:'Codex',CLAUDE:'Claude',GROK:'Grok',AGY:'Antigravity',JEV:'JEV',MUSE:'MUSE',QWEN:'QWEN'};
const STATES={busy:'工作中',idle:'待命',cooldown:'冷卻',offline:'離線'};
const FAILURES=new Set(['quota','auth','cooldown','offline']);
const panels=new Map(),panelButtons=new Map();let nav=null,started=false,selected='CODEX',timer=0;
const clean=(v,n=120)=>String(v??'').replace(/[\r\n\t]+/g,' ').slice(0,n);
const node=(tag,cls,text)=>{const n=D.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const setText=(n,v)=>{const s=String(v);if(n.textContent!==s)n.textContent=s;};
const isFresh=(s,now)=>!!(s&&typeof s==='object'&&Number.isFinite(s.valid_until)&&now<s.valid_until);
function failureLabel(kind){return kind==='quota'?'額度冷卻':kind==='auth'?'驗證冷卻':kind==='cooldown'?'冷卻中':'離線';}
function deriveCrew(state,now=Date.now()){
  const fresh=isFresh(state,now),demo=!!(fresh&&state.demo_mode===true&&state.source_mode==='DEMO'&&state.native_active===0);
  const workers=fresh&&Array.isArray(state.workers)?state.workers:[],byAlias=new Map();
  for(const w of workers)if(w&&FAMILIES.includes(w.alias)&&!byAlias.has(w.alias))byAlias.set(w.alias,w);
  const demoTaint=!!(fresh&&(state.demo===true||state.demo_mode===true||state.source_mode==='DEMO'||workers.some(w=>w&&(w.source==='DEMO'||w.demo_working===true))));
  const crew=FAMILIES.map(alias=>{
    const w=byAlias.get(alias),job=w&&typeof w.job==='string'?clean(w.job,100).trim():'',offlineSignal=!!(w&&(w.failure_kind==='offline'||['offline','disabled','unavailable'].includes(w.status)));
    const kind=offlineSignal?'offline':w&&FAILURES.has(w.failure_kind)?w.failure_kind:(w&&w.status==='cooldown'?'cooldown':'');
    const blocked=!!kind,heartbeatOk=!!(w&&Number.isFinite(w.heartbeat_at_ms)&&w.heartbeat_at_ms>=now-5000&&w.heartbeat_at_ms<=now+5000);
    const native=!!(fresh&&!demoTaint&&!blocked&&state.connected===true&&state.source_mode==='CLI'&&w&&w.source==='CLI'&&w.working===true&&w.native_process===true&&w.process_alive===true&&Number.isSafeInteger(w.pid)&&w.pid>0&&w.status==='running'&&job&&heartbeatOk);
    const demoWorker=!!(demo&&w&&w.source==='DEMO'&&w.native_process===false&&w.pid===null&&w.process_alive===false),demoBusy=!!(demoWorker&&!blocked&&w.demo_working===true&&w.working===true);
    const queued=!!(w&&w.status==='queued'),normalIdle=!!(fresh&&!demoTaint&&w&&(w.status==='idle'||queued||(w.working===false&&[undefined,null,'','ready','waiting','stopped'].includes(w.status))));
    const demoIdle=!!(demoWorker&&w.demo_working===false&&w.working===false&&([undefined,null,'','idle','ready','waiting','stopped'].includes(w.status)||queued));
    const stateName=offlineSignal?'offline':kind?'cooldown':native||demoBusy?'busy':normalIdle||demoIdle?'idle':'offline';
    const task=stateName==='busy'?clean(w.task||w.label||job||'目前任務'):stateName==='cooldown'?failureLabel(kind):stateName==='idle'&&queued?clean(w.task||w.label||'queued'):STATES[stateName];
    const item={alias,name:NAMES[alias],state:stateName,task,source:demoWorker?'DEMO':native?'CLI':'WAIT',failureKind:kind||null,modelSetting:w&&w.model?clean(w.model,100):'',provenance:'UNVERIFIED',nativeProof:native,demo:demoWorker};
    if(native){item.pid=w.pid;item.heartbeatMs=w.heartbeat_at_ms;item.job=job;}
    return item;
  });
  for(const alias of ART)crew.push({alias,name:NAMES[alias],state:'idle',task:'角色夥伴待命',source:demo?'DEMO':'ART',failureKind:null,modelSetting:'',provenance:'UNVERIFIED',nativeProof:false,demo,artOnly:true});
  return crew;
}
function receiptList(live){
  const j=live&&live.jev_core||{},all=[],lists=[live&&live.decision_history,live&&live.history,j.decision_history,j.history];
  for(const list of lists)if(Array.isArray(list))for(const r of list)if(r&&r.decision_id)all.push(r);
  if(j.last_decision&&j.last_decision.decision_id)all.push(j.last_decision);
  return all;
}
function receiptKey(r){return [clean(r.decision_id,80),clean(r.status,40),clean(r.final_outcome,40)].join('|');}
function receiptSummary(r){
  const labels={RISK_REJECTED:'風控未通過',PLAN_INVALIDATED:'計畫失效',NO_JEV_DECISION:'未取得決策',JEV_WAIT:'等待',JEV_ACTION_SELECTED:r.final_outcome==='ORDER_SUBMITTED'?'已送出模擬委託':'已選計畫'};
  return (labels[r.status]||'決策更新')+' · '+clean(r.decision_id,36);
}
function createSession(clock=()=>performance.now()){
  const seen=new Set(),timeline=[];let initialized=false,scanBase=0,scans=0,decisions=0,instance='',began=clock();
  const remember=k=>{if(!k||seen.has(k))return false;seen.add(k);if(seen.size>160)seen.delete(seen.values().next().value);return true;};
  function observe(live,line){
    if(!live||typeof live!=='object')return read();
    const nextInstance=clean(live.instance_id||line&&line.instance_id,100),scan=Number.isFinite(live.scan_count)?Math.max(0,Math.floor(live.scan_count)):0,receipts=receiptList(live);
    if(!initialized){initialized=true;instance=nextInstance;scanBase=scan;for(const r of receipts)remember(receiptKey(r));return read();}
    if(instance&&nextInstance&&instance!==nextInstance){instance=nextInstance;scanBase=scan;for(const r of receipts)remember(receiptKey(r));return read();}
    if(nextInstance)instance=nextInstance;
    if(scan>=scanBase)scans+=scan-scanBase;scanBase=scan;
    for(const r of receipts)if(remember(receiptKey(r))){decisions++;timeline.unshift(receiptSummary(r));if(timeline.length>12)timeline.length=12;}
    return read();
  }
  function read(){return{scans,decisions,elapsedMs:Math.max(0,clock()-began),timeline:timeline.slice(),seenSize:seen.size,instance};}
  return{observe,read};
}
const session=createSession();
function addPanelButton(id,label,target){
  if(!nav)return;
  let b=panelButtons.get(id);if(!b){b=node('button','',label);b.type='button';b.dataset.panel=id;b.addEventListener('click',()=>{const p=panels.get(id);if(p&&typeof p.node.scrollIntoView==='function')p.node.scrollIntoView({behavior:'smooth',block:'start'});if(id==='companionHud'){p.node.classList.remove('is-collapsed');p.node.querySelector('.companion-hud__toggle')?.setAttribute('aria-expanded','true');}});panelButtons.set(id,b);nav.appendChild(b);}else setText(b,label);
}
function registerPanel(id,label,target){
  if(typeof id!=='string'||!/^[A-Za-z][\w:-]{0,63}$/.test(id)||!target||typeof target.scrollIntoView!=='function')return false;
  const safeLabel=clean(label,18)||id;panels.set(id,{label:safeLabel,node:target});addPanelButton(id,safeLabel,target);return true;
}
W.JEVCompanion={registerPanel,_test:Object.freeze({deriveCrew,createSession,receiptSummary,failureLabel,isFresh})};
function setupDrawer(){
  const mq=W.matchMedia?.('(max-width: 1100px)'),header=D.querySelector('header');if(!mq||!header)return;
  const moving=[D.getElementById('demoBanner'),...header.querySelectorAll('.hstat'),header.querySelector('.headerActions')].filter(Boolean),marks=[];
  let details=null,content=null;
  const apply=()=>{
    if(mq.matches){if(!details){details=node('details','companion-mobile-drawer');const summary=node('summary','companion-mobile-drawer__summary',W.__JEV_DEMO__===true?'DEMO · 合成資料 · 真實 CLI 0 · 狀態與控制 ▾':'狀態與控制 ▾');content=node('div','companion-mobile-drawer__content');details.append(summary,content);}for(const n of moving){if(!marks.some(x=>x.node===n)){const mark=D.createComment('companion-restore');n.parentNode?.insertBefore(mark,n);marks.push({node:n,mark});}content.appendChild(n);}header.appendChild(details);details.open=false;}
    else if(details&&details.parentNode){for(const x of marks)x.mark.parentNode?.insertBefore(x.node,x.mark.nextSibling);details.remove();}
  };
  mq.addEventListener?.('change',apply);apply();
}
function paintPet(ref,alias){
  if(ref.painted)return;const api=W.LineWorkers;if(!api||typeof api.makeTextureCanvas!=='function')return;
  try{const status=typeof api.artStatus==='function'?api.artStatus():null;if(status&&status.ready===false)return;const source=api.makeTextureCanvas(alias,'front',0,false),ctx=ref.canvas.getContext?.('2d');if(source&&ctx){ctx.clearRect(0,0,44,44);ctx.drawImage(source,0,0,44,44);ref.painted=true;}}catch(e){}
}
function locked(){try{return !!W.JEVRemote?.isLocked?.();}catch(e){return true;}}
function start(){
  if(started||locked())return;started=true;setupDrawer();
  let hud=D.getElementById('companionHud');if(!hud){hud=node('aside','companion-hud is-collapsed');hud.id='companionHud';D.body.appendChild(hud);}
  hud.setAttribute('aria-label','模型夥伴 HUD');const head=node('div','companion-hud__head'),title=node('b','companion-hud__title','模型夥伴'),mode=node('span','companion-hud__mode','狀態未連線'),toggle=node('button','companion-hud__toggle','展開');
  toggle.type='button';toggle.setAttribute('aria-controls','companionHudBody');toggle.setAttribute('aria-expanded','false');head.append(title,mode,toggle);
  const body=node('div','companion-hud__body');body.id='companionHudBody';const crewBox=node('div','companion-hud__crew'),detail=node('section','companion-task'),detailName=node('h2','companion-task__name'),detailState=node('span','companion-task__state'),detailLine=node('div','companion-task__line'),detailModel=node('div','companion-task__line'),detailProof=node('div','companion-task__provenance');
  detailName.appendChild(detailState);detail.append(detailName,detailLine,detailModel,detailProof);const metrics=node('div','companion-session'),metricNodes={};
  for(const [key,label] of [['elapsed','觀察時間'],['scans','新增掃描'],['decisions','新決策']]){const box=node('div'),value=node('b','','0'),caption=node('span','',label);box.append(value,caption);metrics.appendChild(box);metricNodes[key]=value;}
  const timeline=node('ol','companion-timeline');timeline.setAttribute('aria-label','本次視窗觀察時間軸');body.append(crewBox,detail,metrics,timeline);hud.append(head,body);
  const refs=new Map();for(const alias of [...FAMILIES,...ART]){const b=node('button','companion-pet');b.type='button';b.dataset.alias=alias;b.setAttribute('aria-selected',String(alias===selected));const canvas=node('canvas','companion-pet__art');canvas.width=44;canvas.height=44;canvas.setAttribute('aria-hidden','true');const name=node('b','companion-pet__name',NAMES[alias]),status=node('span','companion-pet__status','離線');b.append(canvas,name,status);b.addEventListener('click',()=>{selected=alias;render();});crewBox.appendChild(b);refs.set(alias,{button:b,canvas,status,painted:false});}
  nav=node('nav','companion-quick-tabs');nav.setAttribute('aria-label','快速前往');D.body.appendChild(nav);const sceneOverview=D.querySelector('.torusWrap')||D.getElementById('jevPanel');if(sceneOverview)registerPanel('sceneOverview','場景',sceneOverview);for(const [id,label] of [['marketPanel','市場'],['jevPanel','JEV'],['riskPanel','風控'],['positionsPanel','持倉'],['eventsPanel','事件']]){const target=D.getElementById(id);if(target)registerPanel(id,label,target);}registerPanel('companionHud','夥伴',hud);for(const [id,p] of panels)if(!panelButtons.has(id))addPanelButton(id,p.label,p.node);
  toggle.addEventListener('click',()=>{const collapsed=hud.classList.toggle('is-collapsed');toggle.textContent=collapsed?'展開':'收合';toggle.setAttribute('aria-expanded',String(!collapsed));});
  D.addEventListener('keydown',e=>{if(e.key==='Escape'&&!hud.classList.contains('is-collapsed')){hud.classList.add('is-collapsed');toggle.textContent='展開';toggle.setAttribute('aria-expanded','false');toggle.focus();}});
  function render(){
    if(locked()){if(timer)clearInterval(timer);timer=0;return;}const now=Date.now(),line=W.__lineWorkersState,s=session.observe(W.LIVE,line),crew=deriveCrew(line,now),chosen=crew.find(x=>x.alias===selected)||crew[0],fresh=isFresh(line,now),demo=fresh&&line.demo_mode===true;
    setText(mode,demo?'DEMO · 合成 · 真實 CLI 0':fresh&&line.source_mode==='CLI'?'本機工作狀態':fresh&&line.source_mode==='SYSTEM'?'系統狀態（非模型）':'狀態未連線');
    for(const item of crew){const ref=refs.get(item.alias);ref.button.dataset.state=item.state;ref.button.setAttribute('aria-selected',String(item.alias===selected));ref.button.setAttribute('aria-label',item.name+'，'+STATES[item.state]+'，'+item.task);setText(ref.status,STATES[item.state]+' · '+item.task);paintPet(ref,item.alias);}
    setText(detailName,chosen.name+' ');detailName.appendChild(detailState);setText(detailState,STATES[chosen.state]);setText(detailLine,'目前狀態：'+chosen.task);setText(detailModel,chosen.modelSetting?'模型設定：'+chosen.modelSetting+'（未驗證）':'模型設定：未提供');
    const proof=chosen.nativeProof?'實際模型來源：UNVERIFIED · PID '+chosen.pid+' · heartbeat '+Math.max(0,Math.round((now-chosen.heartbeatMs)/100)/10)+' 秒 · job '+chosen.job:chosen.demo?'DEMO 合成資料 · 真實 CLI 0':chosen.artOnly?'角色圖像 · 無原生工作宣告':'實際模型來源：UNVERIFIED';setText(detailProof,proof);
    setText(metricNodes.elapsed,Math.floor(s.elapsedMs/60000)+':'+String(Math.floor(s.elapsedMs/1000)%60).padStart(2,'0'));setText(metricNodes.scans,s.scans);setText(metricNodes.decisions,s.decisions);timeline.replaceChildren();if(!s.timeline.length)timeline.appendChild(node('li','companion-empty','尚無新決策'));else for(const entry of s.timeline)timeline.appendChild(node('li','',entry));
    W.__companionReadback={ready:true,readOnly:!!W.JEVRemote?.isReadOnly?.(),selected:chosen.alias,mode:demo?'DEMO':fresh?clean(line.source_mode,12):'WAIT',nativeCli:crew.filter(x=>x.nativeProof).length,crew:crew.map(x=>({alias:x.alias,name:x.name,state:x.state,source:x.source,provenance:x.provenance,modelSetting:x.modelSetting,pid:x.nativeProof?x.pid:null,job:x.nativeProof?x.job:''})),session:{scans:s.scans,decisions:s.decisions,timeline:s.timeline.slice(),seenSize:s.seenSize}};
  }
  W.addEventListener?.('visibilitychange',()=>{if(!D.hidden)render();});render();timer=setInterval(()=>{if(!D.hidden)render();},500);
}
function boot(){const run=()=>D.readyState==='loading'?D.addEventListener('DOMContentLoaded',start,{once:true}):start();if(W.JEVRemote)Promise.resolve(W.JEVRemote.ready).then(ok=>{if(ok!==false&&!locked())run();});else run();}
boot();
})();
