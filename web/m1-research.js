/* Separate read-only M1 SHADOW card. No control/order endpoint or inline code. */
(() => {
  'use strict';
  const finite=v=>typeof v==='number'&&Number.isFinite(v)?v:null;
  const text=v=>typeof v==='string'?v.slice(0,100):'';
  function normalize(value,now=Date.now()) {
    if(!value||value.schema_version!=='m1-research-status/1'||value.mode!=='M1_RESEARCH_SHADOW'||value.phase==='stopped'||
       value.main_engine_hook_enabled!==false||value.exchange_execution_enabled!==false||value.v1_registry_bridge_enabled!==false||
       value.interval_ms!==60000||!Number.isSafeInteger(value.heartbeat_at_ms)||
       now-value.heartbeat_at_ms< -5000||now-value.heartbeat_at_ms>180000)return null;
    const k=value.kelly||{},a=value.analysis||{},j=value.jev||{},r=value.research||{};
    return {mode:value.mode,phase:text(value.phase),heartbeat_at_ms:value.heartbeat_at_ms,
      data_fresh:value.data?.fresh===true,wf:text(r.wf_state),forward:text(r.forward_state),
      candidate_count:Number.isSafeInteger(r.candidate_count)?r.candidate_count:0,family:text(r.winner_family),
      analysis:{source:['RULE_SCORED','STRATA','GROK_FAST'].includes(a.source)?a.source:'UNAVAILABLE',
        score:finite(a.score),configured_model:text(a.configured_model),reported_model:text(a.reported_model),expired:!(finite(a.expires_at_ms)>now)},
      jev:{status:['JEV_ACTION_SELECTED','JEV_WAIT','NO_JEV_DECISION'].includes(j.status)?j.status:'NO_JEV_DECISION',
        model:text(j.reported_model),expired:!(finite(j.expires_at_ms)>now)},
      kelly:{sample_count:Number.isSafeInteger(k.sample_count)?k.sample_count:0,raw:finite(k.raw_kelly),
        conservative:finite(k.conservative_kelly),fraction:[0,.25,.5,1].includes(k.auto_fraction)?k.auto_fraction:0,reason:text(k.auto_reason)},
      lanes:(Array.isArray(value.lanes)?value.lanes:[]).slice(0,5).filter(l=>
        ['ZERO_SHADOW','QUARTER_SHADOW','HALF_SHADOW','FULL_SHADOW','AUTO_SHADOW'].includes(l.name)).map(l=>
        ({name:l.name,fraction:finite(l.fraction),equity:finite(l.equity),trades:finite(l.trade_count),
          notional:finite(l.applied_notional),caps:(Array.isArray(l.cap_reasons)?l.cap_reasons:[]).slice(0,3).map(text)}))};
  }
  if(typeof module!=='undefined'&&module.exports){module.exports={normalize};return;}
  if(window.M1ResearchClient)return;
  let status=null;
  const card=document.createElement('section');card.id='m1ResearchCard';card.className='m1-card';
  card.setAttribute('aria-label','M1 影子研究');
  const heading=document.createElement('h3');heading.textContent='M1 多空研究 · Kelly SHADOW';
  const body=document.createElement('div');card.append(heading,body);
  const host=document.getElementById('strategyLabPanel')||document.body;host.appendChild(card);
  const css=document.createElement('link');css.rel='stylesheet';css.href='/m1-research.css';document.head.appendChild(css);
  const fmt=v=>v===null?'—':Number(v).toFixed(3);
  function render(mode) {
    body.replaceChildren();
    const line=value=>{const p=document.createElement('p');p.textContent=value;body.appendChild(p);};
    if(mode==='DEMO'){line('DEMO：不讀取真實 M1／JEV 狀態。');return;}
    if(!status){line('M1 研究尚未連線，未取得新決策。');return;}
    line(`1m · ${status.data_fresh?'公開資料新鮮':'資料 HOLD'} · ${status.candidate_count} 個候選 · ${status.family}`);
    line(`歷史 WF：${status.wf||'等待'}；固定未來驗證：${status.forward||'DATA_ACCRUAL'}`);
    line(`分析：${status.analysis.source}${status.analysis.expired?'（已過期）':''} · ${status.analysis.reported_model|| (status.analysis.configured_model?'設定 '+status.analysis.configured_model+'（未核實）':'規則')} · 評分 ${fmt(status.analysis.score)}`);
    line(`JEV：${status.jev.status}${status.jev.expired?'（已過期）':''} · ${status.jev.model||'未取得真實回覆'}`);
    line(`Kelly：${status.kelly.sample_count} 筆先前結束樣本 · 原始 ${fmt(status.kelly.raw)} · 保守 ${fmt(status.kelly.conservative)} · AUTO ${status.kelly.fraction}×`);
    for(const lane of status.lanes)line(`${lane.name} · ${lane.fraction}× · ${lane.trades??0} 筆 · 模擬名目 ${fmt(lane.notional)}${lane.caps.length?' · '+lane.caps.join('/') : ''}`);
    line('所有艙位均為獨立 SHADOW；分數與 JEV 集中度不是勝率。未接主引擎或交易所。');
  }
  async function poll(){
    const mode=window.__JEV_DEMO__===true?'DEMO':window.StrategyLabClient?.readback().viewerMode||'UNVERIFIED';
    if(mode!=='REAL_PAPER'){status=null;window.__m1ResearchState=null;render(mode);setTimeout(poll,5000);return;}
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),2000);
    try{const response=await fetch('/m1-research/status.json',{cache:'no-store',signal:controller.signal});
      if(!response.ok||Number(response.headers.get('content-length')||0)>131072)throw Error('HOLD');
      const raw=await response.text();if(raw.length>131072)throw Error('HOLD');
      status=window.__JEV_DEMO__===true?null:normalize(JSON.parse(raw));
    }catch(_){status=null;}finally{clearTimeout(timer);window.__m1ResearchState=status;render(mode);setTimeout(poll,5000);}
  }
  window.M1ResearchClient={normalize,readback:()=>window.__JEV_DEMO__===true||!status||Date.now()-status.heartbeat_at_ms>180000?null:status};render('UNVERIFIED');poll();
})();
