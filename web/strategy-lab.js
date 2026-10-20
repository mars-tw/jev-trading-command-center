/* Read-only, same-origin adaptive PAPER laboratory view. */
(() => {
  'use strict';
  if (window.StrategyLabClient) return;
  const phases=new Set(['idle','scanning','backtesting','evaluating','archiving','cooldown','unavailable']);
  const aliases=new Set(['AGY','GROK','CODEX','CLAUDE']);
  const families=new Set(['macd','ema_trend','donchian','rsi_mean_reversion']);
  const text=(v,n=100)=>typeof v==='string'?v.slice(0,n):'';
  const count=v=>Number.isSafeInteger(v)&&v>=0?v:0;
  const number=v=>typeof v==='number'&&Number.isFinite(v)?v:null;
  const id=v=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v)?v:null;
  let viewerMode=window.__JEV_DEMO__===true?'DEMO':window.__JEV_DEMO__===false?'REAL_PAPER':'UNVERIFIED';
  const unavailable=(mode=viewerMode)=>({schema_version:1,mode:mode==='DEMO'?'DEMO':mode==='REAL_PAPER'?'ADAPTIVE_PAPER':'UNVERIFIED',phase:'unavailable',available:false,
    heartbeat_at_ms:0,engine_hook_enabled:false,exchange_execution_enabled:false,main_engine_unchanged:true,
    research:{state:mode==='DEMO'?'DEMO_DISABLED':'UNAVAILABLE',qualified_count:0,active_candidate_id:null,recent_trials:[]},
    native:{enabled:false,status:'unavailable',pid:null,process_alive:false},paper:{qualified:null,unqualified_shadow:[]}});
  async function getModeResource(url){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),2000);
    try{const response=await fetch(url,{cache:'no-store',signal:controller.signal});
      if(!response.ok||Number(response.headers.get('content-length')||0)>8192)return null;
      const value=await response.text();return value.length<=8192?value:null;
    }catch(_){return null;}finally{clearTimeout(timer);}
  }
  async function resolveViewerMode(){
    if(window.__JEV_DEMO__===true)return(viewerMode='DEMO');
    if(window.__JEV_DEMO__===false)return(viewerMode='REAL_PAPER');
    if(viewerMode!=='UNVERIFIED')return viewerMode;
    // Read the existing bootstrap declaration; never eval provider/server text.
    const bootstrap=await getModeResource('/api/bootstrap.js');
    const declarations=bootstrap?[...bootstrap.matchAll(/\bwindow\.__JEV_DEMO__\s*=\s*(true|false)\s*;/g)]:[];
    if(declarations.length===1)return(viewerMode=declarations[0][1]==='true'?'DEMO':'REAL_PAPER');
    const healthText=await getModeResource('/api/health');
    try{const health=JSON.parse(healthText||'null');
      if(health?.mode==='DEMO'||health?.synthetic===true)return(viewerMode='DEMO');
      if(health?.mode==='REAL_PAPER'&&health.paper_only===true&&health.synthetic===false)return(viewerMode='REAL_PAPER');
      const location=window.location;
      if(location&&['127.0.0.1','localhost'].includes(location.hostname)&&location.port==='8765'&&health?.ok===true&&
         typeof health.jev_env_set==='boolean'&&health.mode===undefined)return(viewerMode='REAL_PAPER');
    }catch(_){}
    return 'UNVERIFIED';
  }
  function arena(value,classification){
    if(!value||value.classification!==classification||value.simulated!==true)return null;
    return {arena_id:text(value.arena_id,90),classification,simulated:true,
      equity:number(value.equity),initial_cash:number(value.initial_cash),net_pnl:number(value.net_pnl),
      trade_count:count(value.trade_count),open_positions:count(value.open_positions),processed_bars:count(value.processed_bars),
      active_candidate_id:id(value.active_candidate_id),max_drawdown_pct:number(value.max_drawdown_pct),
      fill_basis:'causal_bar_simulation_finalized_after_close',braked:value.braked===true};
  }
  function normalizeStatus(raw,now=Date.now()){
    if(window.__JEV_DEMO__===true||viewerMode==='DEMO')return unavailable('DEMO');
    if(!raw||raw.schema_version!==1||raw.mode!=='ADAPTIVE_PAPER'||raw.engine_hook_enabled!==false||raw.exchange_execution_enabled!==false||raw.main_engine_unchanged!==true)return unavailable();
    const age=now-raw.heartbeat_at_ms;
    if(!Number.isFinite(age)||age< -5000||age>180000)return unavailable();
    const r=raw.research||{},n=raw.native||{},p=raw.paper||{};
    const alive=n.status==='running'&&n.process_alive===true&&Number.isSafeInteger(n.pid)&&n.pid>0&&aliases.has(n.alias);
    const status={schema_version:1,mode:'ADAPTIVE_PAPER',available:true,
      phase:phases.has(raw.phase)?raw.phase:'unavailable',heartbeat_at_ms:raw.heartbeat_at_ms,
      pid:Number.isSafeInteger(raw.pid)&&raw.pid>0?raw.pid:null,instance_id:text(raw.instance_id,80),tick_count:count(raw.tick_count),
      engine_hook_enabled:false,exchange_execution_enabled:false,main_engine_unchanged:true,
      data:{dataset_id:text(raw.data?.dataset_id,90),cutoff_ms:number(raw.data?.cutoff_ms),
        live_ok:raw.data?.live_ok===true,public_fetch_enabled:raw.data?.public_fetch_enabled===true,error:text(raw.data?.error)},
      research:{state:text(r.state,40),batch_id:id(r.batch_id),trial_count:count(r.trial_count),qualified_count:count(r.qualified_count),
        active_candidate_id:id(r.active_candidate_id),holdout_consumed_until_ms:number(r.holdout_consumed_until_ms),
        recent_trials:(Array.isArray(r.recent_trials)?r.recent_trials:[]).slice(0,5).map(t=>({candidate_id:id(t.candidate_id),
          family:families.has(t.family)?t.family:'unknown',source:['native_cli','deterministic_seed'].includes(t.source)?t.source:'unknown',
          state:text(t.state,40),rejection_reasons:(Array.isArray(t.rejection_reasons)?t.rejection_reasons:[]).slice(0,6).map(s=>text(s,120))}))},
      native:{enabled:n.enabled===true,status:alive?'running':n.status==='running'?'unavailable':text(n.status,30),alias:aliases.has(n.alias)?n.alias:null,
        pid:alive?n.pid:null,process_alive:alive,started_at_ms:number(n.started_at_ms),next_due_at_ms:number(n.next_due_at_ms),
        configured_model:text(n.configured_model,120),reported_model:text(n.reported_model,120)||null,actual_model_verified:false},
      paper:{qualified:arena(p.qualified,'QUALIFIED'),unqualified_shadow:(Array.isArray(p.unqualified_shadow)?p.unqualified_shadow:[]).slice(0,4).map(v=>arena(v,'UNQUALIFIED_SHADOW')).filter(Boolean)}};
    return status;
  }
  let current=unavailable(),open=false;
  const create=(tag,value,cls)=>{const element=document.createElement(tag);if(value)element.textContent=value;if(cls)element.className=cls;return element;};
  const host=document.getElementById('coreLabSlot')||document.getElementById('jevCore')||document.body;
  const existingButton=document.getElementById('strategyLabButton');
  const button=existingButton||create('button','策略實驗室','sl-button');
  if(!existingButton)button.id='strategyLabButton';
  if(!String(button.className||'').split(/\s+/).includes('sl-button'))button.className=(String(button.className||'')+' sl-button').trim();
  button.type='button';button.disabled=false;button.removeAttribute?.('disabled');
  button.setAttribute('aria-expanded','false');button.setAttribute('aria-controls','strategyLabPanel');
  const panel=create('section','','sl-panel');panel.id='strategyLabPanel';panel.hidden=true;
  panel.setAttribute('role','region');panel.setAttribute('aria-labelledby','strategyLabTitle');
  const heading=create('h2','策略實驗室');heading.id='strategyLabTitle';
  const close=create('button','關閉','sl-close');close.type='button';
  const body=create('div','','sl-body');panel.append(heading,close,body);
  if(!existingButton)host.appendChild(button);document.body.appendChild(panel);
  const css=create('link');css.rel='stylesheet';css.href='/strategy-lab.css';document.head.appendChild(css);
  function toggle(value){open=value;panel.hidden=!open;button.setAttribute('aria-expanded',String(open));if(!open)button.focus();}
  button.addEventListener('click',()=>toggle(!open));close.addEventListener('click',()=>toggle(false));
  panel.addEventListener('keydown',e=>{if(e.key==='Escape'){toggle(false);e.preventDefault();}});
  function render(){
    if(current.mode==='DEMO'){
      button.textContent='策略實驗室 · DEMO';heading.textContent='策略實驗室 · DEMO';
      body.replaceChildren(create('p','DEMO 示範模式不讀取真實研究服務；不派工、不交易。','sl-muted'));return;
    }
    if(current.mode==='UNVERIFIED'){
      button.textContent='策略實驗室 · 模式未確認';heading.textContent='研究連線未確認';
      body.replaceChildren(create('p','尚未確認頁面模式，未讀取研究或原生 CLI 狀態。','sl-muted'));return;
    }
    heading.textContent='獨立自適應 PAPER';
    button.textContent=current.available?'策略實驗室 · '+current.research.trial_count+' 試驗':'策略實驗室 · 未連線';
    const nodes=[create('p','獨立模擬帳本。主 MACD 引擎維持原策略；沒有串接交易所下單。','sl-boundary')];
    if(!current.available){nodes.push(create('p','等待研究服務的新鮮狀態。','sl-muted'));body.replaceChildren(...nodes);return;}
    const r=current.research,n=current.native,q=current.paper.qualified;
    nodes.push(create('p','研究：'+r.state+' · '+r.trial_count+' 個候選 · '+r.qualified_count+' 次通過紙上門檻'));
    nodes.push(create('p',r.active_candidate_id?'合格紙上策略：'+r.active_candidate_id.slice(0,12):'目前沒有通過門檻的策略；持續蒐集新資料。'));
    nodes.push(create('p','CLI：'+(n.process_alive?n.alias+' · 自有行程 '+n.pid+' 執行中':n.enabled?n.status:'未啟用')+'；模型設定 '+(n.configured_model||'—')+'（實際版本未驗證）','sl-muted'));
    if(q)nodes.push(create('p','合格 PAPER：'+q.trade_count+' 筆平倉 · '+q.open_positions+' 筆持倉 · 淨損益 '+(q.net_pnl??0).toFixed(2)));
    const shadows=current.paper.unqualified_shadow;
    if(shadows.length)nodes.push(create('p','未合格影子試驗：'+shadows.length+' 組，與合格成績分開記錄。','sl-shadow'));
    const list=create('ul','','sl-trials');
    r.recent_trials.forEach(t=>list.appendChild(create('li',t.family+' · '+(t.source==='native_cli'?'原生 CLI 提案':'明示種子／參數後繼')+' · '+t.state)));
    nodes.push(list,create('p','依已收盤 K 棒模擬下一根開盤價，於該根收盤後記錄；不是實際成交。研究閒置時每 60 秒檢查，提案／驗證期間可能延後，再依時間順序補算。','sl-muted'));
    body.replaceChildren(...nodes);
  }
  function publish(status){
    current=status;
    window.JevCoreEffects?.updateResearch?.(status);
    // The visual helper may retain only phase; the graph needs the full proof.
    window.__strategyLabState=status;
    window.dispatchEvent(new CustomEvent('jev:strategy-lab-state',{detail:status}));
    render();
  }
  async function poll(){
    const mode=await resolveViewerMode();
    if(mode!=='REAL_PAPER'){
      publish(unavailable(mode));setTimeout(poll,5000);return;
    }
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),2000);
    try{
      const response=await fetch('/strategy-lab/status.json',{method:'GET',cache:'no-store',signal:controller.signal});
      if(!response.ok||Number(response.headers.get('content-length')||0)>131072)throw Error('unavailable');
      const value=await response.text();if(value.length>131072)throw Error('oversize');
      publish(normalizeStatus(JSON.parse(value)));
    }catch(_){publish(unavailable());}
    finally{clearTimeout(timer);setTimeout(poll,5000);}
  }
  window.StrategyLabClient={normalizeStatus,readback:()=>({open,status:current,viewerMode})};
  publish(current);poll();
})();
