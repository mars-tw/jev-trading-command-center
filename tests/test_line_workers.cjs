// Independent PetOffice acceptance harness. Deliberately reads only frontend source.
// Acceptance matrix: cold history silent; first new receipt audible after opt-in;
// receipt/outcome dedupe; pending-to-submitted; WAIT neutral; errors/fails distinct;
// inactive/missing seats never busy; offline clears stale result; previews read-only.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const sourcePath = process.argv[2] || path.resolve(__dirname,'../web/line-workers.js');

class ClassList {
  constructor(el) { this.el = el; }
  values() { return this.el.className.split(/\s+/).filter(Boolean); }
  contains(v) { return this.values().includes(v); }
  add(...v) { this.el.className = [...new Set([...this.values(), ...v])].join(' '); }
  remove(...v) { this.el.className = this.values().filter(x => !v.includes(x)).join(' '); }
  toggle(v, force) { const add = force === undefined ? !this.contains(v) : !!force; this[add ? 'add' : 'remove'](v); return add; }
}
class Element {
  constructor(tag = 'div', doc) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = doc; this.id = ''; this.className = '';
    this.classList = new ClassList(this); this.children = []; this.dataset = {}; this.attributes = {};
    this.style = {setProperty(k,v) { this[k] = String(v); }, removeProperty(k) { delete this[k]; }};
    this.listeners = {}; this._text = ''; this._html = ''; this.hidden = false; this.disabled = false;
  }
  set textContent(v) { this._text = String(v ?? ''); this._html = ''; this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(html) {
    this._html = String(html); this._text = ''; this.children = [];
    const stack = [this];
    for (const match of this._html.matchAll(/<\/?([a-z][\w:-]*)([^>]*)>|([^<]+)/gi)) {
      if (match[3]) { stack[stack.length - 1]._text += match[3]; continue; }
      if (match[0].startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
      const el = new Element(match[1], this.ownerDocument);
      for (const attr of match[2].matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s'">=]+)))?/g)) el.setAttribute(attr[1], attr[2] ?? attr[3] ?? attr[4] ?? '');
      stack[stack.length - 1].appendChild(el);
      if (!/\/$/.test(match[2]) && !/^(img|input|br|hr|meta|link|source|wbr)$/i.test(match[1])) stack.push(el);
    }
  }
  get innerHTML() { return this._html; }
  setAttribute(k,v) {
    v = String(v); this.attributes[k] = v;
    if(k === 'id') this.id = v; if(k === 'class') this.className = v;
    if(k.startsWith('data-')) this.dataset[k.slice(5).replace(/-([a-z])/g, (_,c) => c.toUpperCase())] = v;
    if(k === 'hidden') this.hidden = true; if(k === 'disabled') this.disabled = true;
  }
  getAttribute(k) { return k === 'id' ? this.id : k === 'class' ? this.className : this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; if(k === 'hidden') this.hidden = false; if(k === 'disabled') this.disabled = false; }
  appendChild(el) { el.parentNode = this; this.children.push(el); return el; }
  append(...els) { els.forEach(e => this.appendChild(typeof e === 'string' ? Object.assign(new Element('span', this.ownerDocument), {_text:e}) : e)); }
  replaceChildren(...els) { this.children = []; this._text = ''; this.append(...els); }
  remove() { if(this.parentNode) this.parentNode.children = this.parentNode.children.filter(c => c !== this); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatchEvent(e) { e.target ||= this; for(const fn of this.listeners[e.type] || []) fn(e); const f = this['on' + e.type]; if(typeof f === 'function') f(e); return true; }
  click() { if(!this.disabled) this.dispatchEvent({type:'click',target:this,isTrusted:true,preventDefault(){},stopPropagation(){}}); }
  matches(selector) {
    const parts = selector.trim().split(/\s+/);
    if(parts.length > 1) {
      if(!this.matches(parts.pop())) return false;
      let ancestor = this.parentNode;
      while(parts.length) { const part=parts.pop(); while(ancestor && !ancestor.matches(part)) ancestor=ancestor.parentNode; if(!ancestor)return false; ancestor=ancestor.parentNode; }
      return true;
    }
    if(selector === '*') return true;
    const attr = [...selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)];
    if(attr.some(a => this.getAttribute(a[1]) === null || (a[2] !== undefined && this.getAttribute(a[1]) !== a[2]))) return false;
    const base = selector.replace(/\[[^\]]+\]/g, '');
    const id = base.match(/#([\w-]+)/); if(id && this.id !== id[1]) return false;
    for(const c of base.matchAll(/\.([\w-]+)/g)) if(!this.classList.contains(c[1])) return false;
    const tag = base.match(/^[a-z][\w-]*/i); return !tag || this.tagName === tag[0].toUpperCase();
  }
  querySelectorAll(selector) {
    const list = []; const alternatives = selector.split(',').map(s => s.trim());
    const visit = el => { for(const child of el.children) { if(alternatives.some(s => child.matches(s))) list.push(child); visit(child); } };
    visit(this); return list;
  }
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }
  closest(s) { for(let el=this; el; el=el.parentNode) if(el.matches(s)) return el; return null; }
  insertAdjacentElement(where,el) { return this.parentNode.appendChild(el); }
  get firstChild() { return this.children[0] || null; }
  getContext() { return new Proxy({}, {get(t,k){return t[k] || (()=>{});},set(t,k,v){t[k]=v;return true;}}); }
  toDataURL() { return 'data:image/png;base64,trusted-art'; }
  contains(el) { return this.querySelectorAll('*').includes(el); }
  animate() { return {cancel(){},finished:Promise.resolve()}; }
  getBoundingClientRect() { return {x:0,y:0,left:0,top:0,width:1000,height:600,right:1000,bottom:600}; }
}

function environment(options={}) {
  const body = new Element('body');
  const doc = {body, head:new Element('head'), hidden:false, visibilityState:'visible', listeners:{}, createElement(tag){return new Element(tag,this);}, createElementNS(ns,tag){return new Element(tag,this);}, getElementById(id){return body.querySelector('#'+id);}, querySelector(s){return body.matches(s)?body:body.querySelector(s);}, querySelectorAll(s){return body.querySelectorAll(s);}, addEventListener(k,f){(this.listeners[k] ||= []).push(f);}};
  body.ownerDocument = doc;
  const host = doc.createElement('div');host.className='torusWrap';body.appendChild(host);
  const tones = [], requests = [], timers = new Map(); let timerId = 0;
  const storage = options.storage || new Map();
  const media = {matches:!!options.reducedMotion,listeners:[],addEventListener(type,fn){if(type==='change')this.listeners.push(fn);}};
  class AudioContext {
    constructor(){this.state='running';this.currentTime=0;this.destination={};}
    resume(){this.state='running';return Promise.resolve();}
    createOscillator(){const tone={stopCalls:0};tones.push(tone);return {frequency:{setValueAtTime(v){tone.frequency=v;},linearRampToValueAtTime(){},exponentialRampToValueAtTime(){}},type:'sine',connect(){},disconnect(){},start(){tone.started=true;},stop(){tone.stopCalls++;}};}
    createGain(){return {gain:{value:0,setValueAtTime(){},linearRampToValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}};}
  }
  const win = {document:doc, AudioContext, webkitAudioContext:AudioContext, navigator:{userAgent:'PetOffice independent validator'}, location:{search:''}, performance:{now:()=>100}, localStorage:{getItem:k=>{if(options.storageUnavailable)throw Error('storage unavailable');return storage.get(k)??null;},setItem:(k,v)=>{if(options.storageUnavailable)throw Error('storage unavailable');storage.set(k,String(v));},removeItem:k=>storage.delete(k)}, matchMedia:()=>media, addEventListener(){}, requestAnimationFrame:fn=>{timers.set(++timerId,{fn,ms:'raf'});return timerId;},cancelAnimationFrame(){}, setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;},clearTimeout:id=>timers.delete(id), setInterval:()=>++timerId,clearInterval(){}, fetch:(...args)=>{requests.push(args);throw new Error('Unexpected network request');}, console};
  win.__JEV_DEMO__=!!options.demo;win.__JEV_NATIVE_ORIGIN__=options.nativeOrigin;win.AbortController = AbortController;
  win.window = win;win.self = win;win.globalThis=win;
  vm.createContext(win);
  return {win,doc,body,tones,requests,timers,storage,media,load:()=>vm.runInContext(fs.readFileSync(sourcePath,'utf8'),win,{filename:sourcePath})};
}

async function fresh(options={}) {
  const e=environment(options);for(const id of ['btnAnim','jevCore','pipeSvg']){const el=e.doc.createElement(id==='btnAnim'?'button':'div');el.id=id;e.body.appendChild(el);}e.load();await new Promise(r=>setImmediate(r));assert(e.win.LineWorkers);return e;
}
const snap=(workers=[],jobs=[])=>({schema_version:1,instance_id:'T',server_time_ms:Date.now(),heartbeat_at_ms:Date.now(),scheduler:{running:true,capacity:8,queued:2},workers,jobs});
const worker=(id='A',alias='CODEX')=>({worker_id:id,alias,pid:1234,status:'running',process_alive:true,heartbeat_at_ms:Date.now(),job_id:'JOB-'+id,lane_id:'market',task_label:'research'});
const job=w=>({id:w.job_id,alias:w.alias,worker_id:w.worker_id,lane_id:w.lane_id,status:'running'});
const state=e=>e.win.__lineWorkersState.workers;
async function main(){const cases=[];async function test(name,fn){try{await fn();cases.push({name,pass:true});}catch(e){cases.push({name,pass:false,message:e.message});}}
 await test('same model instances are independent and only matched fresh live processes work',async()=>{const e=await fresh(),a=worker('A'),b=worker('B');assert(e.win.LineWorkers.ingest(snap([a,b],[job(a),job(b)])));assert.equal(state(e).filter(x=>x.working).length,2);assert.equal(new Set(state(e).map(x=>x.id)).size,2);assert.equal(e.doc.getElementById('lwToggle').textContent,'代理 2/8 · 暫停');});
 await test('queued false-alive mismatched and stale workers never move; paused dispatch drains live work',async()=>{const e=await fresh();for(const change of [{status:'idle'},{process_alive:false},{pid:0},{pid:1.2},{heartbeat_at_ms:Date.now()-7000},{job_id:'missing'}]){const a=worker();Object.assign(a,change);e.win.LineWorkers.ingest(snap([a],[job(worker())]));assert(state(e).every(x=>!x.working));}const a=worker(),s=snap([a],[{...job(a),status:'queued'}]);e.win.LineWorkers.ingest(s);assert(state(e).every(x=>!x.working));s.jobs=[{...job(a),alias:'CLAUDE'}];e.win.LineWorkers.ingest(s);assert(state(e).every(x=>!x.working));s.jobs=[job(a)];s.scheduler.running=false;e.win.LineWorkers.ingest(s);assert.equal(state(e)[0].working,true);assert.match(e.doc.getElementById('lwStatus').textContent,/派工已暫停/);});
 await test('bad schema stale server and future heartbeat fail closed',async()=>{const e=await fresh();for(const bad of [{schema_version:2},{heartbeat_at_ms:Date.now()-7000},{server_time_ms:Date.now()+7000}]){assert.equal(e.win.LineWorkers.ingest(Object.assign(snap(),bad)),false);assert.equal(e.win.__lineWorkersState.connected,false);assert(state(e).every(x=>!x.working));assert(e.doc.getElementById('lwToggle').disabled);}});
 await test('system task fallback is explicitly distinct from native model CLI',async()=>{const e=await fresh();e.win.LineWorkers.update({roles:[{id:'feed',status:'working',name:'行情'}],jev_core:{busy:false}});assert.equal(state(e)[0].source,'SYSTEM');assert.equal(state(e)[0].alias,'SYSTEM');assert.equal(state(e)[0].working,true);assert.match(e.doc.getElementById('lwStatus').textContent,/系統/);assert.doesNotMatch(e.doc.getElementById('lwToggle').textContent,/1\/8/);});
 await test('worker list is bounded aliases allowlisted and payload text stays literal',async()=>{const e=await fresh(),w=Array.from({length:20},(_,i)=>worker('W'+i));w[0].task_label='<img src=x onerror=alert(1)>';w.push({...worker('evil'),alias:'<script>'});e.win.LineWorkers.ingest(snap(w,w.map(job)));assert.equal(state(e).length,12);assert.equal(state(e)[0].label,'<img src=x onerror=alert(1)>');assert.equal(e.doc.querySelectorAll('img').length,0);assert(state(e).every(x=>x.alias==='CODEX'));});
 await test('JEV submitted only passes; WAIT neutral; negative precedence and pending stay distinct',async()=>{const e=await fresh(),c=e.win.LineWorkers.classify;assert.equal(c({status:'JEV_WAIT'},false).symbol,'○');assert.equal(c({status:'JEV_ACTION_SELECTED'},false).state,'pending');assert.equal(c({status:'JEV_ACTION_SELECTED',final_outcome:'ORDER_SUBMITTED'},false).state,'pass');for(const status of ['RISK_REJECTED','PLAN_INVALIDATED','NO_JEV_DECISION'])assert.equal(c({status,final_outcome:'ORDER_SUBMITTED'},false).symbol,'×');});
 await test('JEV history silent then first new final outcome sounds once with opt-in',async()=>{const e=await fresh();e.doc.getElementById('lwSound').click();await new Promise(r=>setImmediate(r));const a={jev_core:{status:'READY',last_decision:{decision_id:'A',status:'RISK_REJECTED'}}};e.win.LineWorkers.update(a);assert.equal(e.tones.length,0);const b={jev_core:{last_decision:{decision_id:'B',status:'JEV_ACTION_SELECTED'}}};e.win.LineWorkers.update(b);assert.equal(e.tones.length,0);b.jev_core.last_decision.final_outcome='ORDER_SUBMITTED';e.win.LineWorkers.update(b);assert.equal(e.tones.length,3);e.win.LineWorkers.update(b);assert.equal(e.tones.length,3);e.win.LineWorkers.offline();e.win.LineWorkers.update(a);assert.equal(e.tones.length,3);});
 await test('busy observations do not consume a new completed receipt',async()=>{const e=await fresh();e.doc.getElementById('lwSound').click();await new Promise(r=>setImmediate(r));e.win.LineWorkers.update({jev_core:{}});const s={jev_core:{busy:true,last_decision:{decision_id:'B',status:'RISK_REJECTED'}}};e.win.LineWorkers.update(s);assert.equal(e.tones.length,0);s.jev_core.busy=false;e.win.LineWorkers.update(s);assert.equal(e.tones.length,2);e.win.LineWorkers.update(s);assert.equal(e.tones.length,2);});
 await test('explicit user request defaults robot motion on even under reduced-motion while pause remains available',async()=>{const e=await fresh({reducedMotion:true}),a=e.win.LineWorkers;assert.equal(a.motionAllowed(),true);assert.equal(e.media.matches,true);assert.equal(e.doc.getElementById('lwMotion').getAttribute('aria-pressed'),'true');e.doc.getElementById('lwMotion').click();assert.equal(a.motionAllowed(),false);e.doc.getElementById('lwMotion').click();assert.equal(a.motionAllowed(),true);for(const fn of e.media.listeners)fn({matches:true});assert.equal(a.motionAllowed(),true);e.body.classList.add('noanim');assert.equal(a.motionAllowed(),false);e.body.classList.remove('noanim');e.doc.hidden=true;assert.equal(a.motionAllowed(),false);});
 await test('busy standby and offline patrol phases all advance continuously without status-transition teleport',async()=>{const e=await fresh({reducedMotion:true}),a=e.win.LineWorkers;const start=.45,busy=a.advancePhase(start,.1,true),idle=a.advancePhase(busy,.1,false),offline=a.advancePhase(idle,.1,false);assert(busy>start&&idle>busy&&offline>idle);assert(Math.abs((busy-start)-.012)<1e-9);assert(Math.abs((idle-busy)-.0035)<1e-9);assert(idle-busy<busy-start);assert(offline-idle<.004);const rad=a.advancePhase(1,.1,false,Math.PI*2);assert(rad>1&&rad-1<.03);e.doc.getElementById('lwMotion').click();assert.equal(a.advancePhase(rad,.1,true,Math.PI*2),rad);assert.equal(e.media.matches,true);});
 await test('standby and offline patrol retain truthful zero native-active count',async()=>{const e=await fresh(),a=worker();a.status='idle';a.process_alive=false;e.win.LineWorkers.ingest(snap([a],[{...job(a),status:'queued'}]));assert.equal(state(e)[0].working,false);assert.match(state(e)[0].label,/待命巡線/);assert.equal(e.doc.getElementById('lwToggle').textContent,'代理 0/8 · 暫停');e.win.LineWorkers.offlineGuild();assert(state(e).every(w=>!w.working));assert(state(e).every(w=>/離線巡線/.test(w.label)));assert.equal(e.doc.getElementById('lwToggle').disabled,true);});
 await test('poll uses loopback guild only, abort cutoff; control never calls trading',async()=>{const e=await fresh();assert.equal(e.requests[0][0],'/api/guild/status');assert(e.requests[0][1].signal instanceof AbortSignal);const a=worker();e.win.LineWorkers.ingest(snap([a],[job(a)]));e.win.fetch=async(url,opts)=>{e.requests.push([url,opts]);return{ok:true,json:async()=>snap()};};assert.equal(await e.win.LineWorkers.control('pause'),true);const request=e.requests.find(x=>x[1]?.method==='POST');assert.equal(request[0],'/api/guild/control');assert.deepEqual(JSON.parse(request[1].body),{action:'pause'});assert(e.requests.every(x=>x[0].startsWith('/api/guild/')));});
 await test('every native instance gets unique stable model identity and truthful current action',async()=>{const e=await fresh(),a=worker('codex-1'),b=worker('codex-2');a.model='gpt-example（設定值未驗證）';a.task_label='行情巡邏｜EXAMPLE';a.symbol='EXAMPLE';b.model='gpt-example';b.status='idle';b.process_alive=false;b.task_label='前次任務不應顯示';b.result_summary='前次已完成';e.win.LineWorkers.ingest(snap([a,b],[job(a),job(b)]));const first=state(e);assert.equal(first[0].name,'CODEX #1');assert.equal(first[1].name,'CODEX #2');assert.match(first[0].heading,/設定/);assert.match(first[0].action,/工作中.*行情巡邏/);assert.equal(first[1].action,'待命巡線');assert.doesNotMatch(first[1].action,/前次|完成/);e.win.LineWorkers.ingest(snap([b,a],[job(a),job(b)]));assert.equal(state(e).find(w=>w.id===a.worker_id).name,'CODEX #1');});
 await test('cooldown countdown uses live task_label schema when deadline absent and never invents seconds',async()=>{const e=await fresh(),w=worker('grok-1','GROK');w.status='cooldown';w.process_alive=false;w.cooldown_until_ms=Date.now()+15000;e.win.LineWorkers.ingest(snap([w],[]));assert.match(state(e)[0].action,/冷卻 1[45] 秒/);delete w.cooldown_until_ms;e.win.LineWorkers.ingest(snap([w],[]));assert.equal(state(e)[0].action,'冷卻巡線');w.task_label='冷卻中（691 秒後重試）';e.win.LineWorkers.ingest(snap([w],[]));assert.equal(state(e)[0].action,'冷卻 691 秒');w.task_label='冷卻中（1234567 秒後重試）';e.win.LineWorkers.ingest(snap([w],[]));assert.equal(state(e)[0].action,'冷卻巡線');w.status='unavailable';e.win.LineWorkers.ingest(snap([w],[]));assert.equal(state(e)[0].action,'未連線巡線');});
 await test('always-on DOM label text escapes payloads and full model/task stays in accessible title',async()=>{const e=await fresh(),w=worker('claude-1','CLAUDE');w.model='<img src=x onerror=alert(1)>';w.task_label='測試工作<script>alert(1)</script>';e.win.LineWorkers.ingest(snap([w],[job(w)]));e.win.LineWorkers.placeLabels([{id:w.worker_id,x:300,y:120}]);const cards=e.doc.querySelectorAll('.lw-card');assert.equal(cards.length,1);assert.match(cards[0].textContent,/CLAUDE #1/);assert.match(cards[0].getAttribute('aria-label'),/script/);assert.equal(e.doc.querySelectorAll('img').length,0);assert.equal(e.doc.querySelectorAll('script').length,0);assert.equal(e.win.__lineLabelReadback.length,1);});
 await test('projection labels clamp avoid core and retain all twelve owners while following anchors',async()=>{const e=await fresh(),ws=Array.from({length:12},(_,i)=>worker('codex-'+(i+1)));e.win.LineWorkers.ingest(snap(ws,ws.map(job)));const points=ws.map((w,i)=>({id:w.worker_id,x:100+i*55,y:100+(i%3)*130})),reserved=[{x:350,y:190,width:300,height:170}],boxes=e.win.LineWorkers.layoutLabels(points,1000,600,reserved);assert.equal(boxes.length,12);for(const b of boxes){assert(b.x>=4&&b.y>=4&&b.x+b.width<=996&&b.y+b.height<=596);assert(!(b.x<650&&b.x+b.width>350&&b.y<360&&b.y+b.height>190));}for(let i=0;i<boxes.length;i++)for(let j=i+1;j<boxes.length;j++)assert(!(boxes[i].x<boxes[j].x+boxes[j].width&&boxes[i].x+boxes[i].width>boxes[j].x&&boxes[i].y<boxes[j].y+boxes[j].height&&boxes[i].y+boxes[i].height>boxes[j].y));e.win.LineWorkers.placeLabels(points);const node=e.doc.querySelector('.lw-card');const before=e.win.__lineLabelReadback[0].anchorX;e.win.LineWorkers.placeLabels(points.map(p=>({...p,x:p.x+20})));assert.equal(e.doc.querySelector('.lw-card'),node);assert.equal(e.win.__lineLabelReadback[0].anchorX,before+20);e.win.LineWorkers.offlineGuild();assert.equal(e.doc.querySelectorAll('.lw-card').length,0);});
 await test('four views and four walk frames cache distinct art with genuine busy props only',async()=>{const e=await fresh(),a=e.win.LineWorkers;const front=a.makeTextureCanvas('GROK'),back=a.makeTextureCanvas('GROK','back',0,false),side=a.makeTextureCanvas('GROK','left',1,false),busy=a.makeTextureCanvas('GROK','front',0,true);assert.equal(front,a.makeTextureCanvas('GROK'));assert.notEqual(front,back);assert.notEqual(front,busy);assert.equal(back.dataset.view,'back');assert.equal(side.dataset.view,'left');assert.equal(side.dataset.frame,'1');assert.equal(busy.dataset.working,'true');assert.equal(a.viewFor(2,0),'right');assert.equal(a.viewFor(-2,0),'left');assert.equal(a.viewFor(0,-2),'back');assert.equal(a.viewFor(0,2),'front');assert.notEqual(a.frameFor(0),a.frameFor(150));e.doc.getElementById('lwMotion').click();const frozen=a.frameFor(150);assert.equal(a.frameFor(900),frozen);});
 await test('SVG traveller images and visible two-line labels reattach after original pipeline redraw without duplicates',async()=>{const e=await fresh(),host=e.doc.getElementById('pipeSvg'),addPath=()=>{const g=e.doc.createElement('g');g.id='pipeEdges';const p=e.doc.createElement('path');p.getTotalLength=()=>100;p.getPointAtLength=d=>({x:200+d,y:74});g.appendChild(p);host.appendChild(g);};addPath();const w=worker('codex-1');w.model='model（設定值未驗證）';w.task_label='訊號複核';e.win.LineWorkers.ingest(snap([w],[job(w)]));const run=t=>{const entry=[...e.timers].find(([,v])=>v.ms==='raf');assert(entry);e.timers.delete(entry[0]);entry[1].fn(t);};run(1000);assert.equal(host.querySelectorAll('.lw-svg-pet').length,1);assert.equal(host.querySelectorAll('.lw-svg-label').length,1);assert.match(host.querySelector('.lw-svg-label').textContent,/CODEX #1.*訊號複核/s);host.innerHTML='';addPath();run(1150);assert.equal(host.querySelectorAll('.lw-svg-pet').length,1);assert.equal(host.querySelectorAll('.lw-svg-label').length,1);run(1300);assert.equal(host.querySelectorAll('.lw-svg-pet').length,1);assert.equal(host.querySelectorAll('.lw-svg-label').length,1);assert.equal(host.querySelectorAll('#pipeEdges path').length,1);});

 await test('narrow layout keeps original torus unchanged and gives all owners readable rail cards with tiny tags',async()=>{const e=await fresh(),host=e.doc.querySelector('.torusWrap'),ws=Array.from({length:12},(_,i)=>worker('codex-'+(i+1)));host.clientWidth=359;host.clientHeight=300;host.getBoundingClientRect=()=>({left:0,top:0,width:359,height:300});const core=e.doc.getElementById('jevCore');core.getBoundingClientRect=()=>({left:34.5,top:61.35,width:290,height:177.3});host.appendChild(core);e.win.LineWorkers.ingest(snap(ws,ws.map(job)));e.win.LineWorkers.placeLabels(ws.map((w,i)=>({id:w.worker_id,x:45+i*23,y:95+(i%4)*28})));const rail=e.doc.getElementById('lwMobileRail');assert.equal(rail.parentNode,host.parentNode);assert.equal(rail.style.display,'grid');assert.equal(rail.querySelectorAll('.lw-mobile-card').length,12);assert.equal(e.win.__lineMobileReadback.length,12);assert(e.win.__lineLabelReadback.every(b=>b.mode==='tag'&&b.width===48&&b.height===14));for(const b of e.win.__lineLabelReadback)assert(!(b.x<324.5&&b.x+b.width>34.5&&b.y<238.65&&b.y+b.height>61.35));assert.equal(host.clientHeight,300);assert.equal(host.style.height,undefined);host.clientWidth=1000;host.clientHeight=600;host.getBoundingClientRect=()=>({left:0,top:0,width:1000,height:600});e.win.LineWorkers.placeLabels(ws.map((w,i)=>({id:w.worker_id,x:50+i*60,y:100+(i%4)*100})));assert.equal(rail.style.display,'none');assert.equal(rail.querySelectorAll('.lw-mobile-card').length,0);assert(e.win.__lineLabelReadback.every(b=>b.mode==='card'&&b.width===132&&b.height===36));});

 await test('DEMO rejects native processes and labels idle as well as busy actors DEMO',async()=>{const e=await fresh({demo:true,nativeOrigin:'http://127.0.0.1:8766'}),w=worker();assert.equal(e.requests[0][0],'/api/guild/status');assert.equal(e.win.LineWorkers.ingest(snap([w],[job(w)])),false);const d={...w,pid:null,process_alive:false,simulated:true},g={...snap([d],[job(d)]),source:'DEMO',simulated:true,native_cli_count:0,real_cli_agents:0};assert(e.win.LineWorkers.ingest(g));assert(state(e).every(x=>x.source==='DEMO'&&!x.native_process));g.workers[0].status='idle';g.workers[0].job_id=null;assert(e.win.LineWorkers.ingest(g));assert(state(e).every(x=>x.source==='DEMO'&&!x.working));});
 await test('native endpoint is used only after explicit non-DEMO bootstrap opt-in',async()=>{const e=await fresh({nativeOrigin:'http://127.0.0.1:8766'});assert.equal(e.requests[0][0],'http://127.0.0.1:8766/api/guild/status');});
 const failed=cases.filter(x=>!x.pass);console.log(JSON.stringify({source:sourcePath,total:cases.length,passed:cases.length-failed.length,failed},null,2));if(failed.length)process.exitCode=1;
}
main().catch(e=>{console.error(e.stack);process.exitCode=1;});
