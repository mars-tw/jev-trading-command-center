/* Additive workers on the existing circuit. No trading actions or layout replacement. */
(function(){
'use strict';
/* 預設同源（ORIGIN=''）：絕不自動連任何私人伴跑程式。
   只有「真實模式 + 伺服器明示 opt-in」的 /api/bootstrap.js 才會給 __JEV_NATIVE_ORIGIN__；
   DEMO 模式永遠同源，並且只接受標了 source=DEMO / simulated 的合成工作狀態。 */
const DEMO=window.__JEV_DEMO__===true;
const NATIVE_ORIGIN=(!DEMO&&typeof window.__JEV_NATIVE_ORIGIN__==='string'
  &&/^http:\/\/(127\.0\.0\.1|localhost):\d{4,5}$/.test(window.__JEV_NATIVE_ORIGIN__))
  ?window.__JEV_NATIVE_ORIGIN__:'';
const ORIGIN=NATIVE_ORIGIN, ALIASES=['CODEX','CLAUDE','GROK','AGY'], LIMIT=5000;
const q=id=>document.getElementById(id), clean=(s,n=140)=>String(s??'').slice(0,n), num=(v,max=999999)=>Number.isFinite(v)?Math.max(0,Math.min(max,Math.floor(v))):0;
const toolbar=document.createElement('span');toolbar.id='lineWorkerTools';toolbar.style.cssText='display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:11px';
const make=(tag,text,id)=>{const e=document.createElement(tag);if(id)e.id=id;e.textContent=text;return e;};
const motionButton=make('button','工作動作：開','lwMotion'),soundButton=make('button','音效：關','lwSound'),toggle=make('button','代理：連線中','lwToggle'),note=make('span','等待代理狀態','lwStatus');
toolbar.append(motionButton,soundButton,toggle,note);const gallery=make('a','角色圖鑑');gallery.href='/pets-game-v1/preview.html';gallery.target='_blank';gallery.rel='noopener';gallery.style.cssText='font-size:11px;color:#bfe9f3;padding:4px 6px;white-space:nowrap';toolbar.appendChild(gallery);const anim=q('btnAnim');if(anim)anim.insertAdjacentElement('afterend',toolbar);
const badge=make('span','○','lwVerdict');badge.style.cssText='display:inline-block;margin:4px 8px;font-size:38px;line-height:1;color:#8bcfdc;vertical-align:middle';badge.setAttribute('role','status');badge.setAttribute('aria-live','polite');q('jevCore')?.appendChild(badge);
const style=document.createElement('style');style.textContent='#lineWorkerTools button{font-size:11px;padding:5px 7px}#lwStatus{max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#a9c7dc}#lwVerdict[data-state=pass]{color:#76efb0}#lwVerdict[data-state=reject],#lwVerdict[data-state=error]{color:#ff9eaa}@media(max-width:600px){header{flex-wrap:wrap}#lineWorkerTools{width:100%}#lwStatus{max-width:150px}#lwVerdict{font-size:30px!important}}';document.head.appendChild(style);
/* Small labels are attached to each traveller; original panels remain untouched. */
const labelHost=document.querySelector('.torusWrap'),labelLayer=make('div','','lwLabels'),labelNodes=new Map(),svgLabels=new Map(),nameIndex=new Map(),nameUsed=new Map(),mobileNodes=new Map(),mobileRail=make('div','','lwMobileRail');mobileRail.setAttribute('role','list');mobileRail.setAttribute('aria-label','機器人即時工作');mobileRail.style.display='none';if(labelHost)labelHost.insertAdjacentElement('afterend',mobileRail);
const labelStyle=document.createElement('style');labelStyle.textContent='#lwLabels{position:absolute;inset:0;z-index:4;pointer-events:none;overflow:hidden}#lwLabels .lw-card{position:absolute;left:0;top:0;width:132px;height:36px;padding:3px 5px;background:rgba(3,11,22,.9);border:1px solid #456777;border-radius:4px;color:#e0edf5;font:11px/14px Consolas,"Microsoft JhengHei",monospace;box-shadow:0 1px 4px #0008;overflow:hidden}#lwLabels .lw-card b,#lwLabels .lw-card span{display:block;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}#lwLabels .lw-card b{color:#bfe9f3;font-size:11px}#lwLabels .lw-card[data-working=true]{border-color:#70c6a5}#lwLabels svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible}#lwLabels line{stroke:#96bace;stroke-width:1;opacity:.55}@media(max-width:600px){#lwLabels .lw-card{font-size:10px;line-height:13px;padding:3px 4px}#lwLabels .lw-card b{font-size:10px}}#lwMobileRail{min-width:0;flex:none;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px;padding:6px;border:1px solid #304e60;background:#07101b}#lwMobileRail .lw-mobile-card{min-width:0;padding:4px 5px;border:1px solid #315165;border-radius:4px;color:#dceaf3;font:11px/15px Consolas,Microsoft JhengHei,monospace}#lwMobileRail b,#lwMobileRail span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}#lwMobileRail b{color:#bfe9f3}#lwLabels .lw-card.lw-tag{padding:1px 2px;background:rgba(3,11,22,.6);font:8px/10px Consolas,Microsoft JhengHei,monospace;border-color:#608296}#lwLabels .lw-card.lw-tag b{font-size:8px;line-height:10px}#lwLabels .lw-tag span{display:none}';document.head.appendChild(labelStyle);
const leaderSvg=document.createElementNS('http://www.w3.org/2000/svg','svg');leaderSvg.setAttribute('aria-hidden','true');labelLayer.appendChild(leaderSvg);if(labelHost)labelHost.appendChild(labelLayer);
const laneNames={market_patrol:'行情巡邏',signal_review:'訊號複核',risk_hunt:'風險找碴',counter_case:'反證挑戰',cross_check:'交叉對照',research_notes:'研究整理',feed:'行情接收',strategy:'策略檢查',sizer:'艙位計算',risk:'風控檢查',exec:'執行追蹤',position:'持倉管理',record:'研究記錄',records:'研究記錄',chief:'總控',control:'總控'};
function shorten(value,n){const a=[...clean(value,400)];return a.length>n?a.slice(0,n-1).join('')+'…':a.join('');}
function describeWorker(w){const family=w.source==='SYSTEM'?'系統':w.source==='DEMO'?'DEMO-'+w.alias:w.alias,key=w.source+':'+family+':'+w.id,group=w.source+':'+family;let number=nameIndex.get(key);if(!number){const used=nameUsed.get(group)||new Set(),tail=String(w.id).match(/[-_#](\d+)$/);number=tail?Number(tail[1]):1;if(!number||number>99||used.has(number)){number=1;while(used.has(number))number++;}used.add(number);nameUsed.set(group,used);nameIndex.set(key,number);}const name=family+' #'+number,model=(w.source==='CLI'||w.source==='DEMO')?clean(w.model,180):'',lane=clean(w.lane_name||laneNames[w.lane]||w.lane,100),task=clean(w.task,280),symbol=clean(w.symbol,40);let action='待命巡線';
 if(w.source==='WAIT'||['unavailable','disabled','offline'].includes(w.status))action='未連線巡線';
 else if(w.status==='cooldown'){const timestamp=Number.isFinite(w.cooldown_until_ms),match=!timestamp&&task.match(/(?:^|[^\d])(\d{1,6})\s*秒/),remaining=timestamp?Math.max(0,Math.ceil((w.cooldown_until_ms-Date.now())/1000)):match?Number(match[1]):0;action=remaining?'冷卻 '+remaining+' 秒':'冷卻巡線';}
 else if(w.working){const detail=task||lane||'研究任務';action=(w.source==='SYSTEM'?'系統工作 · ':w.source==='DEMO'?'DEMO 工作 · ':'工作中 · ')+detail+(symbol&&!detail.includes(symbol)?' · '+symbol:'');}
 const modelLabel=model?(w.source==='DEMO'?'DEMO '+model:'設定 '+model):'',heading=name+(modelLabel?' · '+shorten(modelLabel,20):''),fullTitle=[name,model?(w.source==='DEMO'?'DEMO 合成標籤（沒有真實模型或行程在跑）：':'模型（設定，未驗證實際版本）：')+model:'',action,w.working&&lane?'工作線：'+lane:'',!w.working&&task?'附帶任務資料（非目前執行）：'+task:''].filter(Boolean).join('\n');return{name,heading,action:shorten(action,25),fullTitle};}
function overlap(a,b){return Math.max(0,Math.min(a.x+a.width,b.x+b.width)-Math.max(a.x,b.x))*Math.max(0,Math.min(a.y+a.height,b.y+b.height)-Math.max(a.y,b.y));}
function layoutLabels(points,width,height,reserved=[],cardWidth=132,cardHeight=36){const placed=[],W=Math.max(24,Math.min(cardWidth,width-8)),H=Math.min(cardHeight,height-8),clamp=(v,a,b)=>Math.max(a,Math.min(b,v));for(const point of points.slice(0,12)){if(!Number.isFinite(point.x)||!Number.isFinite(point.y))continue;const candidates=[],ax=clamp(point.x,4,width-4),ay=clamp(point.y,4,height-4);for(const dy of [-H/2,-H-20,20,-H-58,58,-H-96,96])for(const dx of [22,-W-22,10,-W-10])candidates.push({x:clamp(ax+dx,4,width-W-4),y:clamp(ay+dy,4,height-H-4)});for(let y=4;y<=height-H-4;y+=H+4)for(let x=4;x<=width-W-4;x+=W+4)candidates.push({x,y});let best=null,score=Infinity;for(const c of candidates){const box={...c,width:W,height:H},hit=[...reserved,...placed].reduce((n,r)=>n+overlap(box,r)*(r.priority||1),0),cost=hit*1000+Math.hypot(c.x+W/2-ax,c.y+H/2-ay);if(cost<score){score=cost;best=box;}}if(best)placed.push({...best,id:point.id,anchorX:ax,anchorY:ay});}return placed;}
function removeLabel(id){const n=labelNodes.get(id);if(n){n.card.remove();n.line.remove();labelNodes.delete(id);}}
function reconcileLabels(workers){for(const[id,n]of mobileNodes)if(!workers.some(w=>w.id===id)){n.card.remove();mobileNodes.delete(id);}const keep=new Set(workers.map(w=>w.id));for(const id of labelNodes.keys())if(!keep.has(id))removeLabel(id);for(const[id,g]of svgLabels)if(!keep.has(id)){g.remove();svgLabels.delete(id);}}
function syncMobileRail(workers,narrow){mobileRail.style.display=narrow?'grid':'none';mobileRail.dataset.mode=narrow?'rail':'off';const keep=new Set();if(narrow)for(const w of workers){keep.add(w.id);let n=mobileNodes.get(w.id);if(!n){const card=make('div','',''),name=make('b',''),action=make('span','');card.className='lw-mobile-card';card.setAttribute('role','listitem');card.append(name,action);mobileRail.appendChild(card);n={card,name,action};mobileNodes.set(w.id,n);}n.card.dataset.workerId=w.id;n.card.dataset.working=String(w.working);n.card.title=w.fullTitle;n.card.setAttribute('aria-label',w.fullTitle);n.name.textContent=w.heading;n.action.textContent=w.action;}for(const[id,n]of mobileNodes)if(!keep.has(id)){n.card.remove();mobileNodes.delete(id);}window.__lineMobileReadback=narrow?workers.map(w=>({id:w.id,name:w.name,action:w.action})):[];}
function placeLabels(points){if(!labelHost)return;const state=window.__lineWorkersState,workers=state&&Date.now()<state.valid_until?state.workers:[],byId=new Map(workers.map(w=>[w.id,w])),r=labelHost.getBoundingClientRect(),width=labelHost.clientWidth||r.width,height=labelHost.clientHeight||r.height;if(width<20||height<20)return;const narrow=width<500||window.DecisionGraph?.enabled===true;syncMobileRail(workers,narrow);labelLayer.dataset.mode=narrow?'tags':'cards';const reserved=[...labelHost.querySelectorAll('#jevCore,.chip,#nowDoing')].map(el=>{const b=el.getBoundingClientRect();return{x:b.left-r.left-5,y:b.top-r.top-5,width:b.width+10,height:b.height+10,priority:narrow&&el.id==='jevCore'?100000:1};}).concat(window.DecisionGraph?.labelBoxes?.()||[]),list=points.filter(pt=>byId.has(pt.id)),boxes=layoutLabels(list,width,height,reserved,narrow?48:132,narrow?14:36),keep=new Set();for(const box of boxes){const w=byId.get(box.id);if(!w)continue;keep.add(w.id);let n=labelNodes.get(w.id);if(!n){const card=make('div','',''),name=make('b',''),action=make('span',''),line=document.createElementNS('http://www.w3.org/2000/svg','line');card.className='lw-card';card.append(name,action);labelLayer.appendChild(card);leaderSvg.appendChild(line);n={card,name,action,line};labelNodes.set(w.id,n);}n.card.classList.toggle('lw-tag',narrow);n.name.textContent=narrow?w.name.replace(' #','#'):w.heading;n.action.textContent=w.action;n.card.title=w.fullTitle;n.card.setAttribute('aria-label',w.fullTitle);n.card.dataset.workerId=w.id;n.card.dataset.working=String(w.working);n.card.style.width=box.width+'px';n.card.style.height=box.height+'px';n.card.style.transform='translate('+box.x+'px,'+box.y+'px)';n.line.setAttribute('x1',String(box.anchorX));n.line.setAttribute('y1',String(box.anchorY));n.line.setAttribute('x2',String(Math.max(box.x,Math.min(box.x+box.width,box.anchorX))));n.line.setAttribute('y2',String(Math.max(box.y,Math.min(box.y+box.height,box.anchorY))));}for(const id of labelNodes.keys())if(!keep.has(id))removeLabel(id);window.__lineLabelReadback=boxes.map(b=>({id:b.id,x:b.x,y:b.y,width:b.width,height:b.height,anchorX:b.anchorX,anchorY:b.anchorY,name:byId.get(b.id)?.name,action:byId.get(b.id)?.action,mode:narrow?'tag':'card'}));}

let artFrame=0;function frameFor(time=performance.now()){if(motionAllowed())artFrame=Math.floor(time/140)%4;return artFrame;}
function viewFor(dx,dy){return Math.abs(dx)>Math.abs(dy)*.8?(dx>=0?'right':'left'):(dy>=0?'front':'back');}

const media=window.matchMedia?.('(prefers-reduced-motion: reduce)');let override=true, guild=null,guildAt=0,liveAt=0,online=false,baseline=false,ctx=null,soundOn=false,main=null,instance='',polling=false,controlBusy=false;
const seen=new Set(),tones=new Set();let lastFrame=0;const svgWorkers=new Map();
function motionAllowed(){return (override===null?!media?.matches:override)&&!document.hidden&&!document.body.classList.contains('noanim');}
function advancePhase(previous,dt,working,period=1){const phase=Number.isFinite(previous)?previous:0;if(!motionAllowed())return phase;return (phase+Math.max(0,Math.min(.1,Number(dt)||0))*(working?.12:.035)*period)%period;}
function motion(){const on=override===null?!media?.matches:override;motionButton.textContent=on?'工作動作：開':'啟用工作動作';motionButton.setAttribute('aria-pressed',String(on));}
motionButton.onclick=()=>{override=!(override===null?!media?.matches:override);motion();};media?.addEventListener?.('change',motion);motion();
function stopSound(){for(const x of tones){try{x.stop();}catch(e){}}tones.clear();}
function cue(state){if(!soundOn||!ctx||ctx.state!=='running'||document.hidden||!['pass','reject','error'].includes(state))return;const t=ctx.currentTime,frequencies=state==='pass'?[523,659,784]:[294,220];frequencies.forEach((f,i)=>{const x=ctx.createOscillator(),g=ctx.createGain();x.type=state==='pass'?'sine':'triangle';x.frequency.value=f;g.gain.setValueAtTime(0,t+i*.09);g.gain.linearRampToValueAtTime(.035,t+i*.09+.012);g.gain.exponentialRampToValueAtTime(.001,t+i*.09+.16);x.connect(g);g.connect(ctx.destination);tones.add(x);x.onended=()=>{tones.delete(x);x.disconnect();g.disconnect();};x.start(t+i*.09);x.stop(t+i*.09+.18);});}
soundButton.onclick=async()=>{if(soundOn){soundOn=false;stopSound();}else try{const A=window.AudioContext||window.webkitAudioContext;if(!A)throw Error();ctx ||= new A();await ctx.resume();soundOn=ctx.state==='running';}catch(e){soundOn=false;}soundButton.textContent=soundOn?'音效：開':'音效：關';soundButton.setAttribute('aria-pressed',String(soundOn));};
function classify(d,busy){if(busy)return{state:'working',symbol:'◌',label:'JEV 決策工作中'};if(!d)return{state:'idle',symbol:'○',label:'等待決策'};if(['RISK_REJECTED','PLAN_INVALIDATED'].includes(d.status))return{state:'reject',symbol:'×',label:'判定未通過'};if(d.status==='NO_JEV_DECISION')return{state:'error',symbol:'×',label:'未取得決策'};if(d.status==='JEV_WAIT')return{state:'wait',symbol:'○',label:'JEV 等待，本次不交易'};if(d.status==='JEV_ACTION_SELECTED')return d.final_outcome==='ORDER_SUBMITTED'?{state:'pass',symbol:'✓',label:'模擬委託已送出'}:{state:'pending',symbol:'◌',label:'已選計畫，等待風控與送單'};return{state:'unknown',symbol:'○',label:'結果待確認'};}
function remember(k){if(k)seen.add(k);if(seen.size>160)seen.delete(seen.values().next().value);}
function update(s){if(!s||typeof s!=='object')return offlineMain();const ready=baseline&&Date.now()-liveAt<LIMIT;main=s;liveAt=Date.now();const j=s.jev_core||{},d=j.last_decision,k=d?.decision_id?[d.decision_id,d.status||'',d.final_outcome||''].join('|'):null,v=classify(d,!!j.busy),fresh=k&&!seen.has(k);if(!ready||!j.busy)remember(k);baseline=true;badge.textContent=v.symbol;badge.dataset.state=v.state;badge.title=v.label;badge.setAttribute('aria-label',v.label);if(ready&&fresh&&!j.busy)cue(v.state);publish();}
function offlineMain(){main=null;liveAt=0;baseline=false;stopSound();badge.textContent='○';badge.dataset.state='offline';badge.title='即時決策未連線';publish();}
function fresh(t){return Number.isFinite(t)&&Date.now()-t<=LIMIT&&t-Date.now()<=LIMIT;}
function validWorker(w,jobs){return w&&ALIASES.includes(w.alias)&&typeof w.worker_id==='string'&&w.worker_id.length<160&&Number.isSafeInteger(w.pid)&&w.pid>0&&w.status==='running'&&w.process_alive===true&&fresh(w.heartbeat_at_ms)&&jobs.some(j=>j.id===w.job_id&&j.worker_id===w.worker_id&&j.alias===w.alias&&j.status==='running'&&j.lane_id===w.lane_id);}
function safeFailureKind(w){const summary=typeof w?.result_summary==='string'?w.result_summary:'';if(summary==='供應商回報額度／限流，已進入冷卻')return'quota';if(w?.failure_kind==='auth'||summary==='供應商驗證失敗，請重新登入'||summary==='供應商驗證失敗，請檢查登入狀態')return'auth';if(w?.status==='cooldown')return'cooldown';if(['offline','disabled','unavailable'].includes(w?.status))return'offline';return null;}
/* DEMO 專用：只有「本頁是 DEMO 伺服器提供的」且酬載自稱 source=DEMO/simulated，
   而且工作者沒有宣稱任何 PID／存活行程，才允許合成寵物動起來。真實檢查完全不放寬。 */
function demoWorker(w,jobs,snap){return DEMO&&snap&&snap.source==='DEMO'&&snap.simulated===true&&w&&ALIASES.includes(w.alias)&&typeof w.worker_id==='string'&&w.worker_id.length<160&&w.demo_working===true&&w.status==='running'&&w.pid==null&&w.process_alive!==true&&fresh(w.heartbeat_at_ms)&&jobs.some(j=>j.id===w.job_id&&j.worker_id===w.worker_id&&j.alias===w.alias&&j.status==='running'&&j.lane_id===w.lane_id);}
function ingest(s){if(DEMO&&(!s||s.source!=='DEMO'||s.simulated!==true||s.native_cli_count!==0||s.real_cli_agents!==0||!Array.isArray(s.workers)||s.workers.some(w=>w&&(w.pid!=null||w.process_alive===true)))){offline();return false;}if(!s||s.schema_version!==1||!fresh(s.heartbeat_at_ms)||!fresh(s.server_time_ms)||!Array.isArray(s.workers)||!Array.isArray(s.jobs)||!s.scheduler){offline();return false;}guild=s;guildAt=Date.now();online=true;instance=clean(s.instance_id);publish();return true;}
function offline(){online=false;guild=null;guildAt=0;publish();}
function publish(){const out=[];const good=online&&guild&&fresh(guild.heartbeat_at_ms)&&Date.now()-guildAt<=LIMIT;
 if(good){const ids=new Set();for(const w of guild.workers){if(out.length>=12)break;if(!w||!ALIASES.includes(w.alias)||typeof w.worker_id!=='string'||w.worker_id.length>159||ids.has(w.worker_id))continue;ids.add(w.worker_id);const native=!DEMO&&validWorker(w,guild.jobs),demo=!native&&demoWorker(w,guild.jobs,guild),working=native||demo;out.push({demo_working:!!demo,native_process:!!native,pid:native?w.pid:null,process_alive:!!native,failure_kind:safeFailureKind(w),model:clean(w.model,180),status:clean(w.status,24),task:clean(w.task_label,280),symbol:clean(w.symbol,40),cooldown_until_ms:w.cooldown_until_ms,lane_name:clean((Array.isArray(guild.lanes)?guild.lanes:[]).find(l=>l&&l.id===w.lane_id)?.label,100),id:w.worker_id,alias:w.alias,working,label:working?clean(w.task_label||w.status):(['unavailable','disabled'].includes(w.status)?'離線巡線':'待命巡線')+' · '+clean(w.task_label||w.status),job:clean(w.job_id),lane:clean(w.lane_id),source:DEMO?'DEMO':'CLI',heartbeat_at_ms:w.heartbeat_at_ms});}}
 else if(main&&Date.now()-liveAt<LIMIT){for(const r of (Array.isArray(main.roles)?main.roles:[])){if(out.length>=8)break;if(r&&typeof r.id==='string')out.push({id:'SYSTEM:'+clean(r.id,80),alias:'SYSTEM',status:r.status,task:clean(r.task_label||r.name||r.id,280),working:r.status==='working',label:(r.status==='working'?'系統工作 · ':'系統待命巡線 · ')+clean(r.name||r.id),lane:clean(r.id),source:DEMO?'DEMO':'SYSTEM',heartbeat_at_ms:liveAt});}}
 if(!out.length)ALIASES.forEach((alias,i)=>out.push({id:'WAIT:'+alias,alias,status:'offline',working:false,label:'離線巡線 · 等待代理狀態',lane:String(i),source:DEMO?'DEMO':'WAIT'}));
 for(const w of out)Object.assign(w,describeWorker(w));reconcileLabels(out);
 const active=out.filter(w=>w.working&&w.source==='CLI').length,demoActive=out.filter(w=>w.working&&w.source==='DEMO').length,demoMode=!!good&&DEMO&&guild.source==='DEMO'&&guild.simulated===true;
 window.__lineWorkersState={workers:out,at:Date.now(),valid_until:Date.now()+1100,instance_id:instance,connected:!!good,demo:DEMO,demo_mode:demoMode,native_active:active,demo_active:demoActive,source_mode:demoMode?'DEMO':good?'CLI':out.some(w=>w.source==='SYSTEM')?'SYSTEM':'WAIT'};
 toggle.textContent=good?(demoMode?'DEMO 代理 '+demoActive+'/'+num(guild.scheduler.capacity,12):'代理 '+active+'/'+num(guild.scheduler.capacity,12))+(guild.scheduler.running?' · 暫停':' · 繼續'):'代理未連線';toggle.disabled=!good||controlBusy;
 note.textContent=good?(demoMode?'DEMO 模擬工作 · '+demoActive+' 工作 / '+num(guild.scheduler.queued)+' 排隊 · 真實 CLI 0':(guild.scheduler.running?'真實 CLI · ':'派工已暫停 · ')+active+' 工作 / '+num(guild.scheduler.queued)+' 排隊'):out.some(w=>w.source==='SYSTEM')?'系統工作狀態 · CLI 狀態未連線':'離線巡線 · 等待真實工作狀態';
 }
async function control(action){if(!['pause','resume'].includes(action)||!online||!guild||!fresh(guild.heartbeat_at_ms)||controlBusy)return false;controlBusy=true;publish();const a=new AbortController(),timer=setTimeout(()=>a.abort(),2500);try{const r=await fetch(ORIGIN+'/api/guild/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action}),signal:a.signal});if(!r.ok)throw Error();const s=await r.json();if(s.schema_version===1)ingest(s);else await poll();return true;}catch(e){note.textContent='代理控制未完成，等待更新';return false;}finally{clearTimeout(timer);controlBusy=false;publish();}}
toggle.onclick=()=>control(guild?.scheduler.running?'pause':'resume');
async function poll(){if(document.hidden||polling)return;polling=true;const a=new AbortController(),timer=setTimeout(()=>a.abort(),2500);try{const r=await fetch(ORIGIN+'/api/guild/status',{cache:'no-store',signal:a.signal});if(!r.ok)throw Error();ingest(await r.json());}catch(e){offline();}finally{clearTimeout(timer);polling=false;}}
/* GAME PET ATLAS START: complete original PNG cells only. */
function createGamePetArt(doc, ImageCtor) {
  var ATLAS_W = 1536;
  var ATLAS_H = 2304;
  var CELL = 384;
  var OUT = 192;
  var ASSET_NAMES = ['codex', 'claude', 'grok', 'agy', 'jev', 'muse', 'qwen'];
  var VIEW_NAMES = ['front', 'right', 'back', 'left'];
  var EMOTION_NAMES = ['pleased', 'focused', 'concerned', 'tired'];
  var PATROL_ROWS = [1, 0, 2, 0];
  var WORK_ROWS = [3, 4, 3, 4];
  var EXPRESSION_ROW = 5;
  var TRANSPARENT_PNG =
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAarVyFEAAAAASUVORK5CYII=';

  var revision = 0;
  var settledCount = 0;
  var allSettled = false;
  var readyResolvers = [];
  var changeListeners = [];
  var textureCache = Object.create(null);
  var expressionCache = Object.create(null);
  var dataUrlCache = Object.create(null);
  var canvasesByAsset = Object.create(null);
  var records = Object.create(null);

  function onArtChange(callback) {
    if (typeof callback !== 'function') return function () {};
    changeListeners.push(callback);
    return function () { changeListeners = changeListeners.filter(function (fn) { return fn !== callback; }); };
  }

  function artRevision() {
    return revision;
  }

  function normalizeAsset(alias) {
    var raw = alias == null ? '' : String(alias).replace(/^\s+|\s+$/g, '');
    var lower = raw.toLowerCase();
    if (lower === 'codex') return 'codex';
    if (lower === 'claude') return 'claude';
    if (lower === 'grok') return 'grok';
    if (lower === 'agy') return 'agy';
    if (lower === 'muse') return 'muse';
    if (lower === 'qwen') return 'qwen';
    return 'jev';
  }

  function normalizeView(view) {
    var lower = view == null ? '' : String(view).toLowerCase();
    if (lower === 'right' || lower === 'back' || lower === 'left' || lower === 'front') {
      return lower;
    }
    return 'front';
  }

  function normalizeFrame(frame) {
    var n = Number(frame);
    if (!isFinite(n)) return 0;
    n = Math.floor(n) % 4;
    if (n < 0) n += 4;
    return n;
  }

  function normalizeWorking(working) {
    return working === true;
  }

  function normalizeEmotion(emotion) {
    var lower = emotion == null ? '' : String(emotion).toLowerCase();
    if (lower === 'pleased' || lower === 'focused' || lower === 'concerned' || lower === 'tired') {
      return lower;
    }
    return 'focused';
  }

  function viewCol(view) {
    var i = 0;
    for (i = 0; i < VIEW_NAMES.length; i++) {
      if (VIEW_NAMES[i] === view) return i;
    }
    return 0;
  }

  function emotionCol(emotion) {
    var i = 0;
    for (i = 0; i < EMOTION_NAMES.length; i++) {
      if (EMOTION_NAMES[i] === emotion) return i;
    }
    return 1;
  }

  function textureKey(asset, view, frame, working) {
    return asset + '|' + view + '|' + String(frame) + '|' + (working ? '1' : '0');
  }

  function expressionKey(asset, emotion) {
    return asset + '|expr|' + emotion;
  }

  function atlasUrl(asset) {
    return '/pets-game-v1/' + asset + '.png';
  }

  function textureRow(frame, working) {
    return working ? WORK_ROWS[frame] : PATROL_ROWS[frame];
  }

  function makeImage() {
    if (typeof ImageCtor === 'function') {
      try {
        return new ImageCtor();
      } catch (errNew) {
        try {
          return ImageCtor();
        } catch (errCall) {
          return doc.createElement('img');
        }
      }
    }
    return doc.createElement('img');
  }

  function makeBlankCanvas() {
    var canvas = doc.createElement('canvas');
    try {
      canvas.width = OUT;
      canvas.height = OUT;
    } catch (errSize) {}
    try {
      var ctx = canvas.getContext('2d');
      if (ctx) ctx.clearRect(0, 0, OUT, OUT);
    } catch (errClear) {}
    return canvas;
  }

  function setDataset(canvas, fields) {
    try {
      var d = canvas.dataset;
      if (!d) return;
      if (fields.asset != null) d.asset = String(fields.asset);
      if (fields.view != null) d.view = String(fields.view);
      if (fields.frame != null) d.frame = String(fields.frame);
      if (fields.working != null) d.working = fields.working ? 'true' : 'false';
      if (fields.ready != null) d.ready = fields.ready ? 'true' : 'false';
      if (fields.cell != null) d.cell = String(fields.cell);
      if (fields.source != null) d.source = String(fields.source);
      if (fields.revision != null) d.revision = String(fields.revision);
    } catch (errData) {}
  }

  function cellToken(col, row) {
    return String(col) + ',' + String(row);
  }

  function parseCell(canvas) {
    var raw = '';
    try {
      raw = canvas.dataset && canvas.dataset.cell ? canvas.dataset.cell : '0,0';
    } catch (errCell) {
      raw = '0,0';
    }
    var parts = String(raw).split(',');
    var col = Number(parts[0]);
    var row = Number(parts[1]);
    if (!isFinite(col)) col = 0;
    if (!isFinite(row)) row = 0;
    return { col: col, row: row };
  }

  function rememberCanvas(asset, canvas) {
    if (!canvasesByAsset[asset]) canvasesByAsset[asset] = [];
    canvasesByAsset[asset].push(canvas);
  }

  function paintCanvas(canvas, asset) {
    var rec = records[asset];
    var cell = parseCell(canvas);
    try {
      var ctx = canvas.getContext('2d');
      if (!ctx) {
        setDataset(canvas, { ready: false, revision: revision });
        return;
      }
      ctx.clearRect(0, 0, canvas.width || OUT, canvas.height || OUT);
      if (!rec || rec.state !== 'ready' || !rec.img) {
        setDataset(canvas, { ready: false, revision: revision });
        return;
      }
      ctx.drawImage(
        rec.img,
        cell.col * CELL,
        cell.row * CELL,
        CELL,
        CELL,
        0,
        0,
        canvas.width || OUT,
        canvas.height || OUT
      );
      setDataset(canvas, { ready: true, revision: revision });
    } catch (errDraw) {
      setDataset(canvas, { ready: false, revision: revision });
    }
  }

  function redrawAsset(asset) {
    var list = canvasesByAsset[asset] || [];
    var i = 0;
    for (i = 0; i < list.length; i++) {
      paintCanvas(list[i], asset);
    }
  }

  function clearDataUrls() {
    var key;
    for (key in dataUrlCache) {
      if (Object.prototype.hasOwnProperty.call(dataUrlCache, key)) {
        delete dataUrlCache[key];
      }
    }
  }

  function fallbackDataUrl() {
    try {
      var c = makeBlankCanvas();
      return c.toDataURL('image/png');
    } catch (errUrl) {
      return TRANSPARENT_PNG;
    }
  }

  function settleAsset() {
    if (allSettled) return;
    settledCount += 1;
    if (settledCount >= ASSET_NAMES.length) {
      allSettled = true;
      var status = artStatus();
      var resolvers = readyResolvers.slice();
      readyResolvers.length = 0;
      var i = 0;
      for (i = 0; i < resolvers.length; i++) {
        try {
          resolvers[i](status);
        } catch (errResolve) {}
      }
    }
  }

  function markError(asset, reason, dimensions) {
    var rec = records[asset];
    if (!rec || rec.settled) return;
    rec.settled = true;
    rec.state = 'error';
    rec.error = reason;
    if (dimensions) rec.dimensions = dimensions;
    settleAsset();
  }

  function markReady(asset, img, dimensions) {
    var rec = records[asset];
    if (!rec || rec.settled) return;
    rec.settled = true;
    rec.state = 'ready';
    rec.img = img;
    rec.dimensions = dimensions;
    rec.error = undefined;
    revision += 1;
    clearDataUrls();
    redrawAsset(asset);
    changeListeners.slice().forEach(function (fn) { try { fn({asset: asset, revision: revision}); } catch (errNotify) {} });
    settleAsset();
  }

  function artStatus() {
    var assets = {};
    var loaded = 0;
    var loading = 0;
    var errors = 0;
    var i = 0;
    for (i = 0; i < ASSET_NAMES.length; i++) {
      var name = ASSET_NAMES[i];
      var rec = records[name];
      var entry = { state: rec ? rec.state : 'loading' };
      if (rec && rec.dimensions) {
        entry.dimensions = {
          width: rec.dimensions.width,
          height: rec.dimensions.height
        };
      }
      if (rec && rec.error) entry.error = rec.error;
      assets[name] = entry;
      if (entry.state === 'ready') loaded += 1;
      else if (entry.state === 'error') errors += 1;
      else loading += 1;
    }
    var state = 'loading';
    if (loading === 0) state = errors > 0 ? 'error' : 'ready';
    return {
      state: state,
      revision: revision,
      loaded: loaded,
      total: ASSET_NAMES.length,
      assets: assets
    };
  }

  function whenArtReady() {
    if (allSettled) {
      return Promise.resolve(artStatus());
    }
    return new Promise(function (resolve) {
      readyResolvers.push(resolve);
    });
  }

  function makeTextureCanvas(alias, view, frame, working) {
    var asset = normalizeAsset(alias);
    var normView = normalizeView(view);
    var normFrame = normalizeFrame(frame);
    var isWorking = normalizeWorking(working);
    var key = textureKey(asset, normView, normFrame, isWorking);
    if (textureCache[key]) return textureCache[key];
    var col = viewCol(normView);
    var row = textureRow(normFrame, isWorking);
    var canvas = makeBlankCanvas();
    setDataset(canvas, {
      asset: asset,
      view: normView,
      frame: normFrame,
      working: isWorking,
      ready: false,
      cell: cellToken(col, row),
      source: atlasUrl(asset),
      revision: revision
    });
    textureCache[key] = canvas;
    rememberCanvas(asset, canvas);
    paintCanvas(canvas, asset);
    return canvas;
  }

  function makeExpressionCanvas(alias, emotion) {
    var asset = normalizeAsset(alias);
    var normEmotion = normalizeEmotion(emotion);
    var key = expressionKey(asset, normEmotion);
    if (expressionCache[key]) return expressionCache[key];
    var col = emotionCol(normEmotion);
    var canvas = makeBlankCanvas();
    setDataset(canvas, {
      asset: asset,
      view: 'front',
      frame: col,
      working: false,
      ready: false,
      cell: cellToken(col, EXPRESSION_ROW),
      source: atlasUrl(asset),
      revision: revision
    });
    expressionCache[key] = canvas;
    rememberCanvas(asset, canvas);
    paintCanvas(canvas, asset);
    return canvas;
  }

  function imageUrl(alias, view, frame, working) {
    var asset = normalizeAsset(alias);
    var normView = normalizeView(view);
    var normFrame = normalizeFrame(frame);
    var isWorking = normalizeWorking(working);
    var key = textureKey(asset, normView, normFrame, isWorking);
    if (Object.prototype.hasOwnProperty.call(dataUrlCache, key) && dataUrlCache[key]) {
      return dataUrlCache[key];
    }
    var url = TRANSPARENT_PNG;
    try {
      var canvas = makeTextureCanvas(asset, normView, normFrame, isWorking);
      url = canvas.toDataURL('image/png');
    } catch (errDataUrl) {
      url = fallbackDataUrl();
    }
    dataUrlCache[key] = url;
    return url;
  }

  function preloadAsset(asset) {
    var rec = {
      state: 'loading',
      img: null,
      dimensions: undefined,
      error: undefined,
      settled: false
    };
    records[asset] = rec;
    canvasesByAsset[asset] = canvasesByAsset[asset] || [];
    var img;
    try {
      img = makeImage();
    } catch (errImg) {
      markError(asset, 'image create failed');
      return;
    }
    rec.img = img;
    img.onload = function () {
      var w = Number(img.naturalWidth);
      var h = Number(img.naturalHeight);
      if (!isFinite(w) || w === 0) w = Number(img.width);
      if (!isFinite(h) || h === 0) h = Number(img.height);
      var dimensions = { width: w, height: h };
      if (w !== ATLAS_W || h !== ATLAS_H) {
        rec.img = null;
        markError(asset, 'invalid atlas size', dimensions);
        return;
      }
      markReady(asset, img, dimensions);
    };
    img.onerror = function () {
      rec.img = null;
      markError(asset, 'image load failed');
    };
    try {
      img.src = atlasUrl(asset);
    } catch (errSrc) {
      rec.img = null;
      markError(asset, 'image load failed');
    }
  }

  var n = 0;
  for (n = 0; n < ASSET_NAMES.length; n++) {
    preloadAsset(ASSET_NAMES[n]);
  }

  return {
    makeTextureCanvas: makeTextureCanvas,
    makeExpressionCanvas: makeExpressionCanvas,
    onArtChange: onArtChange,
    artRevision: artRevision,
    artStatus: artStatus,
    whenArtReady: whenArtReady,
    imageUrl: imageUrl
  };
}
/* GAME PET ATLAS END */
const gamePetArt=createGamePetArt(document,window.Image);
const {makeTextureCanvas,makeExpressionCanvas,onArtChange,artRevision,artStatus,whenArtReady,imageUrl}=gamePetArt;

function pipeTick(t){const dt=lastFrame?Math.min((t-lastFrame)/1000,.08):0;lastFrame=t;const state=window.__lineWorkersState,items=state&&Date.now()<state.valid_until?(window.DecisionGraph?.enabled?state.workers.filter(w=>w.source==='SYSTEM'):state.workers):[],paths=[...document.querySelectorAll('#pipeEdges path')],host=q('pipeSvg'),points=[];if(host&&paths.length){const keep=new Set();items.slice(0,12).forEach((w,i)=>{keep.add(w.id);let img=svgWorkers.get(w.id);if(!img){img=document.createElementNS('http://www.w3.org/2000/svg','image');img.classList.add('lw-svg-pet');img.setAttribute('width','34');img.setAttribute('height','34');const title=document.createElementNS('http://www.w3.org/2000/svg','title');img.appendChild(title);svgWorkers.set(w.id,img);img._lwPhase=(i*.19)%1;img._lwPath=i%paths.length;}if(img.parentNode!==host||!host.contains(img))host.appendChild(img);const p=paths[img._lwPath%paths.length],u=advancePhase(img._lwPhase,dt,w.working),length=p.getTotalLength(),pt=p.getPointAtLength(length*u),next=p.getPointAtLength(length*Math.min(1,u+.01));img._lwPhase=u;const frame=frameFor(t),view=viewFor(next.x-pt.x,next.y-pt.y),busy=w.working&&(w.source==='CLI'||(DEMO&&w.source==='DEMO')),key=w.alias+'|'+view+'|'+frame+'|'+busy+'|'+artRevision();if(img._lwArt!==key){img.setAttribute('href',imageUrl(w.alias,view,frame,busy));img._lwArt=key;}img.setAttribute('x',String(pt.x-17));img.setAttribute('y',String(pt.y-35));img.setAttribute('opacity',w.working?'1':'.68');img.firstChild.textContent=w.fullTitle;img.dataset.workerId=w.id;img.dataset.view=view;img.dataset.frame=String(frame);img.dataset.state=w.working?'working':w.source==='WAIT'?'offline-patrol':'standby-patrol';points.push({id:w.id,x:pt.x,y:pt.y-18});});for(const [id,img]of svgWorkers)if(!keep.has(id)){img.remove();svgWorkers.delete(id);}const boxes=layoutLabels(points,1200,148,[{x:0,y:40,width:1200,height:68}],140,32),byId=new Map(items.map(w=>[w.id,w]));for(const box of boxes){const w=byId.get(box.id);let g=svgLabels.get(w.id);if(!g){g=document.createElementNS('http://www.w3.org/2000/svg','g');g.classList.add('lw-svg-label');const leader=document.createElementNS('http://www.w3.org/2000/svg','line'),bg=document.createElementNS('http://www.w3.org/2000/svg','rect'),name=document.createElementNS('http://www.w3.org/2000/svg','text'),action=document.createElementNS('http://www.w3.org/2000/svg','text'),title=document.createElementNS('http://www.w3.org/2000/svg','title');g.append(leader,bg,name,action,title);g._lw={leader,bg,name,action,title};svgLabels.set(w.id,g);}if(g.parentNode!==host||!host.contains(g))host.appendChild(g);const n=g._lw;g.dataset.workerId=w.id;n.leader.setAttribute('x1',String(box.anchorX));n.leader.setAttribute('y1',String(box.anchorY));n.leader.setAttribute('x2',String(box.x+box.width/2));n.leader.setAttribute('y2',String(box.y+box.height/2));n.leader.setAttribute('stroke','#8ab8c5');n.leader.setAttribute('opacity','.6');n.bg.setAttribute('x',String(box.x));n.bg.setAttribute('y',String(box.y));n.bg.setAttribute('width',String(box.width));n.bg.setAttribute('height',String(box.height));n.bg.setAttribute('rx','3');n.bg.setAttribute('fill','#06111be8');n.bg.setAttribute('stroke','#436777');for(const[e,y]of[[n.name,box.y+13],[n.action,box.y+27]]){e.setAttribute('x',String(box.x+4));e.setAttribute('y',String(y));e.setAttribute('font-size','12');e.setAttribute('font-family','Consolas,Microsoft JhengHei,monospace');e.setAttribute('fill','#d8edf3');}n.name.textContent=w.name;n.action.textContent=shorten(w.action,13);n.title.textContent=w.fullTitle;}for(const[id,g]of svgLabels)if(!keep.has(id)){g.remove();svgLabels.delete(id);}window.__lineSvgLabelReadback=boxes.map(b=>({id:b.id,x:b.x,y:b.y,anchorX:b.anchorX,anchorY:b.anchorY}));}requestAnimationFrame(pipeTick);}

document.addEventListener('visibilitychange',()=>{if(document.hidden)stopSound();else poll();});
window.LineWorkers={update,offline:offlineMain,ingest,offlineGuild:offline,classify,control,motionAllowed,advancePhase,makeTextureCanvas,makeExpressionCanvas,onArtChange,artRevision,artStatus,whenArtReady,imageUrl,describeWorker,layoutLabels,placeLabels,viewFor,frameFor,validWorker,demoWorker,origin:()=>ORIGIN,isDemo:()=>DEMO};publish();poll();setInterval(poll,1000);setInterval(()=>{if(online&&(!fresh(guild?.heartbeat_at_ms)||Date.now()-guildAt>LIMIT))offline();else publish();if(liveAt&&Date.now()-liveAt>LIMIT)offlineMain();},500);requestAnimationFrame(pipeTick);
})();
