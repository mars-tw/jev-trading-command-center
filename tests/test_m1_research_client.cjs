const assert=require('node:assert/strict');
const {normalize}=require('../web/m1-research.js');
const now=Date.now();
const good={schema_version:'m1-research-status/1',mode:'M1_RESEARCH_SHADOW',interval_ms:60000,
  heartbeat_at_ms:now,main_engine_hook_enabled:false,exchange_execution_enabled:false,v1_registry_bridge_enabled:false,
  kelly:{auto_fraction:1,raw_kelly:.4,conservative_kelly:.2},analysis:{source:'RULE_SCORED'},jev:{status:'NO_JEV_DECISION'},lanes:[]};
assert(normalize(good,now));
for(const key of ['main_engine_hook_enabled','exchange_execution_enabled','v1_registry_bridge_enabled'])
  assert.equal(normalize({...good,[key]:true},now),null);
assert.equal(normalize({...good,heartbeat_at_ms:now-180001},now),null);
assert.equal(normalize({...good,mode:'DEMO'},now),null);
assert.equal(normalize({...good,interval_ms:900000},now),null);
assert.equal(normalize({...good,kelly:{auto_fraction:999,raw_kelly:NaN}},now).kelly.fraction,0);
assert.equal(normalize({...good,kelly:{auto_fraction:999,raw_kelly:NaN}},now).kelly.raw,null);
console.log('M1 client 9/9 PASS');
