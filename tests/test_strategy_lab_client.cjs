const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[2]||path.join(__dirname,'../web/strategy-lab.js'),'utf8');
const now=Date.now(),events=[],requests=[];
const raw={schema_version:1,mode:'ADAPTIVE_PAPER',phase:'backtesting',heartbeat_at_ms:now,pid:123,instance_id:'test',tick_count:2,
 engine_hook_enabled:false,exchange_execution_enabled:false,main_engine_unchanged:true,data:{live_ok:true},
 research:{state:'FROZEN',trial_count:5,qualified_count:0,active_candidate_id:null,recent_trials:[]},
 native:{enabled:true,status:'running',alias:'AGY',pid:789,process_alive:true,configured_model:'configured-only'},
 paper:{qualified:null,unqualified_shadow:[]}};
class Element{
 constructor(tag=''){this.tagName=String(tag).toUpperCase();this.children=[];this.attributes={};this.listeners={};this.id='';this.className='';this.textContent='';this.hidden=false;this.disabled=false;}
 appendChild(e){this.children.push(e);return e;}append(...e){this.children.push(...e);}replaceChildren(...e){this.children=e;}
 setAttribute(k,v){this.attributes[k]=String(v);}getAttribute(k){return Object.hasOwn(this.attributes,k)?this.attributes[k]:null;}removeAttribute(k){delete this.attributes[k];}
 addEventListener(type,listener){(this.listeners[type]??=[]).push(listener);}dispatchEvent(event){for(const listener of this.listeners[event.type]||[])listener(event);}focus(){this.focused=true;}
}
function makeDocument(withStub=true){
 const body=new Element('body'),head=new Element('head'),host=new Element('div');host.id='coreLabSlot';body.appendChild(host);
 const coreState=new Element('span');coreState.id='coreLabState';host.appendChild(coreState);
 let stub=null;
 if(withStub){stub=new Element('button');stub.id='strategyLabButton';stub.type='button';stub.disabled=true;stub.className='core-lab-button';stub.textContent='策略實驗室';stub.setAttribute('disabled','');stub.setAttribute('aria-expanded','false');stub.setAttribute('aria-controls','strategyLabPanel');host.appendChild(stub);}
 const visit=(node,fn)=>{if(fn(node))return node;for(const child of node.children||[]){const found=visit(child,fn);if(found)return found;}return null;};
 const all=(node,fn,out=[])=>{if(fn(node))out.push(node);for(const child of node.children||[])all(child,fn,out);return out;};
 return{body,head,host,stub,coreState,createElement:tag=>new Element(tag),
  getElementById(id){return visit(body,node=>node.id===id)||visit(head,node=>node.id===id);},
  countId(id){return all(body,node=>node.id===id).length+all(head,node=>node.id===id).length;}};
}
const doc=makeDocument(true),stageButton=doc.stub;
const win={__JEV_DEMO__:false,dispatchEvent:e=>events.push(e)};win.JevCoreEffects={updateResearch:s=>{win.__strategyLabState={phase:s.phase};}};
const context={window:win,document:doc,CustomEvent:class{constructor(type,opts){this.type=type;this.detail=opts.detail;}},
 Date,Number,Set,AbortController,JSON,setTimeout:()=>1,clearTimeout:()=>{},fetch:async(u,o)=>{requests.push({u,o});return{ok:true,headers:{get:()=>null},text:async()=>JSON.stringify(raw)};}};
vm.runInNewContext(source,context);
(async()=>{await new Promise(resolve=>setImmediate(resolve));const normalize=win.StrategyLabClient.normalizeStatus;
 assert.equal(normalize(raw,now).available,true);
 for(const invalid of [{...raw,mode:'LIVE'},{...raw,exchange_execution_enabled:true},{...raw,engine_hook_enabled:undefined},{...raw,heartbeat_at_ms:now-180001}])assert.equal(normalize(invalid,now).available,false);
 const unproved=normalize({...raw,native:{...raw.native,pid:null}},now);assert.equal(unproved.native.process_alive,false);assert.equal(unproved.native.status,'unavailable');
 assert.equal(doc.countId('strategyLabButton'),1);assert.strictEqual(doc.getElementById('strategyLabButton'),stageButton);
 assert.equal(stageButton.disabled,false);assert.equal(stageButton.getAttribute('disabled'),null);assert.equal(stageButton.getAttribute('aria-expanded'),'false');
 assert.equal(stageButton.getAttribute('aria-controls'),'strategyLabPanel');assert(stageButton.className.split(/\s+/).includes('core-lab-button'));assert(stageButton.className.split(/\s+/).includes('sl-button'));
 assert.strictEqual(doc.getElementById('coreLabState'),doc.coreState);
 assert.equal(win.__strategyLabState.native.pid,789);assert.equal(win.__strategyLabState.mode,'ADAPTIVE_PAPER');
 assert.equal(win.__strategyLabState.engine_hook_enabled,false);assert(events.some(e=>e.type==='jev:strategy-lab-state'&&e.detail.native?.pid===789));
 assert.equal(requests.length,1);assert.equal(requests[0].u,'/strategy-lab/status.json');assert.equal(requests[0].o.method,'GET');assert.equal(requests[0].o.cache,'no-store');
 async function modeCase(flag,responses={},location,withStub=true){
  const calls=[],w={dispatchEvent(){},location};if(flag!==undefined)w.__JEV_DEMO__=flag;
  const d=makeDocument(withStub);
  const env={...context,window:w,document:d,fetch:async(url,options)=>{calls.push({url,options});const response=responses[url];
    if(response instanceof Error)throw response;
    if(response===undefined)return{ok:false,headers:{get:()=>null},text:async()=>''};
    const descriptor=typeof response==='object'&&response!==null&&('body'in response||'ok'in response||'contentLength'in response)?response:{body:response};
    const body=String(descriptor.body??'');
    return{ok:descriptor.ok!==false,headers:{get:name=>name.toLowerCase()==='content-length'&&descriptor.contentLength!=null?String(descriptor.contentLength):null},text:async()=>body};}};
  vm.runInNewContext(source,env);await new Promise(resolve=>setImmediate(resolve));return{calls,state:w.__strategyLabState,client:w.StrategyLabClient,document:d};
 }
 const fallback=await modeCase(false,{},undefined,false);assert.equal(fallback.document.countId('strategyLabButton'),1);assert.equal(fallback.document.getElementById('strategyLabButton').disabled,false);
 const directDemo=await modeCase(true,{});assert.equal(directDemo.calls.length,0);assert.equal(directDemo.state.mode,'DEMO');assert.equal(directDemo.state.native.enabled,false);
 const tainted={...raw,research:{...raw.research,qualified_count:1,active_candidate_id:'a'.repeat(64)},paper:{qualified:{classification:'QUALIFIED',simulated:true,net_pnl:999999,trade_count:99},unqualified_shadow:[]}};
 const rejectedDemo=directDemo.client.normalizeStatus(tainted,now);assert.equal(rejectedDemo.available,false);assert.equal(rejectedDemo.mode,'DEMO');assert.equal(rejectedDemo.native.pid,null);assert.equal(rejectedDemo.native.process_alive,false);assert.equal(rejectedDemo.research.active_candidate_id,null);assert.equal(rejectedDemo.paper.qualified,null);
 const bootstrapDemo=await modeCase(undefined,{'/api/bootstrap.js':'window.__JEV_DEMO__=true;'});
 assert.deepEqual(bootstrapDemo.calls.map(call=>call.url),['/api/bootstrap.js']);assert.equal(bootstrapDemo.state.mode,'DEMO');
 const healthDemo=await modeCase(undefined,{'/api/health':JSON.stringify({mode:'DEMO',synthetic:true})});
 assert(!healthDemo.calls.some(call=>call.url==='/strategy-lab/status.json'));assert.equal(healthDemo.state.mode,'DEMO');
 const original=await modeCase(undefined,{'/api/health':JSON.stringify({ok:true,jev_env_set:false}),'/strategy-lab/status.json':JSON.stringify(raw)},{hostname:'127.0.0.1',port:'8765'});
 assert(original.calls.some(call=>call.url==='/strategy-lab/status.json'&&call.options.method==='GET'));assert.equal(original.state.mode,'ADAPTIVE_PAPER');
 const unknown=await modeCase(undefined,{'/api/health':JSON.stringify({ok:true,jev_env_set:false})},{hostname:'127.0.0.1',port:'8775'});
 assert(!unknown.calls.some(call=>call.url==='/strategy-lab/status.json'));assert.equal(unknown.state.mode,'UNVERIFIED');
 const unavailableCases=[
  await modeCase(false,{}),
  await modeCase(false,{'/strategy-lab/status.json':new Error('synthetic fetch failure')}),
  await modeCase(false,{'/strategy-lab/status.json':'{'}),
  await modeCase(false,{'/strategy-lab/status.json':{body:'{}',contentLength:131073}}),
  await modeCase(false,{'/strategy-lab/status.json':{body:'x'.repeat(131073)}})
 ];
 for(const result of unavailableCases){assert.equal(result.state.available,false);assert.equal(result.state.native.enabled,false);assert.equal(result.state.native.pid,null);assert.equal(result.state.paper.qualified,null);}
 console.log(JSON.stringify({status:'PASS',cases:20,claims:['mode/safety flags','freshness','live PID proof','full state after visual hook','single reused or fallback button','DEMO zero native-status reads and taint rejection','missing/error/malformed/oversize unavailable','same-origin GET-only status','standalone bootstrap','safe original-health fallback']}));
})().catch(e=>{console.error(e);process.exitCode=1;});
