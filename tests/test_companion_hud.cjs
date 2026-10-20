const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
const sourcePath=process.argv[2]||path.join(__dirname,'..','web','companion-hud.js');
const cssPath=path.join(path.dirname(sourcePath),'companion-hud.css');
const raw=fs.readFileSync(sourcePath),css=fs.readFileSync(cssPath,'utf8'),sha=x=>crypto.createHash('sha256').update(x).digest('hex');
let posts=0;
const W={JEVRemote:{ready:Promise.resolve(false),isLocked:()=>true,isReadOnly:()=>true},postMessage(){posts++;}},D={readyState:'loading',addEventListener(){}};
vm.runInNewContext(raw.toString('utf8'),{window:W,document:D,performance:{now:()=>0},setInterval:()=>0,clearInterval(){},Promise,Date},{timeout:1000});
const api=W.JEVCompanion._test,checks=[],now=2000000000000;
function check(name,fn){try{if(!fn())throw new Error('assertion');checks.push({name,status:'PASS'});}catch(e){checks.push({name,status:'FAIL',error_class:e.constructor.name});}}
function state(){return {valid_until:now+2500,connected:true,source_mode:'CLI',demo_mode:false,native_active:1,workers:[{alias:'CODEX',source:'CLI',working:true,native_process:true,process_alive:true,pid:12345,heartbeat_at_ms:now-1000,model:'synthetic-setting',job:'synthetic-job',status:'running'}]};}
function first(s){return api.deriveCrew(s,now).find(x=>x.alias==='CODEX');}
check('null_state_no_native_claim',()=>api.deriveCrew(null,now).every(x=>!x.nativeProof));
check('fresh_cli_proof_working_but_model_unverified',()=>{const x=first(state());return x.nativeProof&&x.state==='busy'&&x.source==='CLI'&&x.provenance==='UNVERIFIED'&&x.modelSetting==='synthetic-setting';});
for(const skew of [1,200,500,5000])check('bounded_future_heartbeat_'+skew+'_accepted',()=>{const s=state();s.workers[0].heartbeat_at_ms=now+skew;const x=first(s);return x.nativeProof&&x.state==='busy';});
check('future_heartbeat_5001_cannot_claim_native',()=>{const s=state();s.workers[0].heartbeat_at_ms=now+5001;const x=first(s);return !x.nativeProof&&x.state!=='busy';});
for(const [label,mutate] of [
 ['missing_pid',s=>delete s.workers[0].pid],['negative_pid',s=>s.workers[0].pid=-1],['string_pid',s=>s.workers[0].pid='12345'],
 ['dead_process',s=>s.workers[0].process_alive=false],['missing_native_flag',s=>s.workers[0].native_process=false],
 ['not_working',s=>s.workers[0].working=false],['expired_packet',s=>s.valid_until=now],['disconnected',s=>s.connected=false],
 ['missing_status',s=>delete s.workers[0].status],['missing_job',s=>delete s.workers[0].job],
 ['stale_heartbeat',s=>s.workers[0].heartbeat_at_ms=now-3600000],['future_heartbeat',s=>s.workers[0].heartbeat_at_ms=now+60000],
 ['system_source',s=>s.source_mode='SYSTEM'],['contradictory_demo_native_active',s=>{s.source_mode='DEMO';s.demo_mode=true;s.native_active=1;}]
])check(label+'_cannot_claim_native',()=>{const s=state();mutate(s);const x=first(s);return !x.nativeProof&&x.state!=='busy';});
check('demo_taint_anywhere_cannot_claim_native',()=>{const a=state(),b=state(),c=state();a.demo=true;b.workers[0].demo_working=true;c.workers.push({alias:'CLAUDE',source:'DEMO',demo_working:false});return [a,b,c].every(s=>{const x=first(s);return !x.nativeProof&&x.state!=='busy';});});
for(const status of ['offline','disabled','unavailable'])check('explicit_'+status+'_not_idle',()=>{const s=state();Object.assign(s.workers[0],{status,working:false,native_process:false,process_alive:false,pid:null,task:'raw-task',error:'raw-error'});const x=first(s);return x.state==='offline'&&x.task==='離線'&&!x.task.includes('raw');});
check('failure_kind_offline_not_idle',()=>{const s=state();Object.assign(s.workers[0],{status:'idle',failure_kind:'offline',working:false,native_process:false,process_alive:false,pid:null,task:'raw-task',error:'raw-error'});const x=first(s);return x.state==='offline'&&x.failureKind==='offline'&&x.task==='離線';});
for(const failure_kind of ['quota','auth','cooldown'])check(failure_kind+'_shown_cooldown',()=>{const s=state();Object.assign(s.workers[0],{status:'cooldown',failure_kind,working:false,native_process:false,process_alive:false,pid:null,task:'raw-task',error:'raw-error'});const x=first(s);return x.state==='cooldown'&&!x.nativeProof&&x.failureKind===failure_kind&&x.task===api.failureLabel(failure_kind)&&!x.task.includes('raw');});
check('demo_busy_is_synthetic_zero_native',()=>{const s=state();Object.assign(s,{source_mode:'DEMO',demo_mode:true,native_active:0});Object.assign(s.workers[0],{source:'DEMO',pid:null,process_alive:false,native_process:false,demo_working:true});const x=first(s);return x.state==='busy'&&x.source==='DEMO'&&!x.nativeProof;});
check('invalid_demo_shape_fails_closed_offline',()=>{const s=state();Object.assign(s,{source_mode:'DEMO',demo_mode:true,native_active:0});Object.assign(s.workers[0],{source:'DEMO',native_process:false,demo_working:true,pid:12345,process_alive:false});const x=first(s);return x.state==='offline'&&x.source==='WAIT'&&!x.nativeProof&&x.provenance==='UNVERIFIED';});
check('art_companions_never_native',()=>api.deriveCrew(state(),now).filter(x=>x.artOnly).every(x=>!x.nativeProof&&x.source==='ART'));
check('qwen_art_label_has_no_fabricated_version',()=>{const x=api.deriveCrew(state(),now).find(x=>x.alias==='QWEN');return x&&x.name==='QWEN'&&!api.deriveCrew(state(),now).some(x=>x.alias==='QWEN7'||x.name==='QWEN7');});
check('session_initial_history_not_counted',()=>{const s=api.createSession(()=>10);const a=s.observe({instance_id:'one',scan_count:100,jev_core:{last_decision:{decision_id:'old',status:'JEV_WAIT'}}});return a.scans===0&&a.decisions===0;});
check('session_real_delta_and_duplicate_receipt_once',()=>{const s=api.createSession(()=>10);s.observe({instance_id:'one',scan_count:100});s.observe({instance_id:'one',scan_count:103,jev_core:{last_decision:{decision_id:'new',status:'JEV_WAIT'}}});const a=s.observe({instance_id:'one',scan_count:103,jev_core:{last_decision:{decision_id:'new',status:'JEV_WAIT'}}});return a.scans===3&&a.decisions===1;});
check('session_restart_does_not_count_old_baseline',()=>{const s=api.createSession(()=>10);s.observe({instance_id:'one',scan_count:100});const a=s.observe({instance_id:'two',scan_count:1000,jev_core:{last_decision:{decision_id:'old',status:'JEV_WAIT'}}});return a.scans===0&&a.decisions===0;});
check('session_buffers_bounded',()=>{const s=api.createSession(()=>10);s.observe({scan_count:0});for(let i=0;i<200;i++)s.observe({scan_count:i+1,jev_core:{last_decision:{decision_id:'fixture-'+i,status:'JEV_WAIT'}}});const a=s.read();return a.seenSize<=160&&a.timeline.length<=12;});
check('register_panel_safe_id_readonly_no_post',()=>{const target={scrollIntoView(){}};return W.JEVCompanion.registerPanel('safe:panel-1','Safe',target)===true&&W.JEVCompanion.registerPanel('../unsafe','Unsafe',target)===false&&W.JEVRemote.isReadOnly()===true&&posts===0;});
const tabletStart=css.indexOf('@media (min-width: 601px) and (max-width: 1100px)'),phoneStart=css.indexOf('@media (max-width: 600px)'),tabletCss=css.slice(tabletStart,phoneStart),navRule=(css.match(/\.companion-quick-tabs\s*\{([\s\S]*?)\}/)||[])[1]||'',navButtonRule=(css.match(/\.companion-quick-tabs button\s*\{([\s\S]*?)\}/)||[])[1]||'';
check('drawer_extends_through_tablet_without_moving_phone_grid_breakpoint',()=>raw.includes("matchMedia?.('(max-width: 1100px)')")&&tabletStart>=0&&phoneStart>tabletStart&&tabletCss.includes('grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)')&&css.slice(phoneStart).includes('grid-template-columns: minmax(0, 1fr) !important'));
check('tablet_scene_height_is_explicitly_bounded',()=>tabletCss.includes('height: clamp(300px, 55dvh, 460px)')&&tabletCss.includes('max-height: 460px')&&tabletCss.includes('flex: none')&&tabletCss.includes('aspect-ratio: auto'));
check('quick_tabs_scroll_without_shrinking_or_document_mask',()=>navRule.includes('flex-wrap: nowrap')&&navRule.includes('overflow-x: auto')&&navButtonRule.includes('flex: 0 0 auto')&&navButtonRule.includes('min-width: 44px')&&navButtonRule.includes('min-height: 44px')&&!/body\s*\{[^}]*overflow-x:\s*hidden/.test(css));
check('scene_tab_targets_original_scene_node',()=>raw.includes("D.querySelector('.torusWrap')||D.getElementById('jevPanel')")&&raw.includes("registerPanel('sceneOverview','場景',sceneOverview)"));
const receipt={source_sha256:sha(raw),source_sha256_after:sha(fs.readFileSync(sourcePath)),css_sha256:sha(fs.readFileSync(cssPath)),scope:'Independent HUD pure truthfulness and bounded responsive fixtures; no live API or native processes',passed:checks.filter(x=>x.status==='PASS').length,failed:checks.filter(x=>x.status==='FAIL').length,checks};
console.log(JSON.stringify({...receipt,checks:checks.filter(x=>x.status!=='PASS')}));
if(receipt.failed)process.exitCode=1;
