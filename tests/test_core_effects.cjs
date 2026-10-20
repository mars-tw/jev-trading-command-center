/* Real Three geometry acceptance; no renderer, GPU, engine, or network is used. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),{pathToFileURL}=require('node:url');
const webRoot=path.resolve(process.argv[2]||path.join(__dirname,'../web'));
const vendor=process.env.JEV_THREE_VENDOR||(fs.existsSync(path.join(webRoot,'vendor/three/build/three.module.js'))?path.join(webRoot,'vendor/three/build/three.module.js'):path.resolve(webRoot,'../../releases/jev-trading-command-center/web/vendor/three/build/three.module.js'));
async function main(){
 const fx=await import(pathToFileURL(path.join(webRoot,'core-effects.js'))),THREE=await import(pathToFileURL(vendor)),cases=[];
 async function test(name,fn){try{await fn();cases.push({name,pass:true});}catch(e){cases.push({name,pass:false,message:e.message});}}
 await test('particle budgets are finite, bounded and adapt to screen/CPU/reduced-motion',()=>{
  for(const width of [NaN,Infinity,-100,0,390,737,1024,1440,90000])for(const reduced of [true,false])for(const cores of [NaN,2,8,64]){
   const b=fx.coreVisualBudget(width,reduced,cores);for(const k of ['tier','mainParticles','accentParticles','motes','pixelRatio'])assert(Number.isFinite(b[k]),k);
   assert(b.mainParticles+b.accentParticles+b.motes<=4096);assert(b.motes<=192);assert(b.tier>=0&&b.tier<=2);assert(b.pixelRatio<=1.25);
  }
  assert.equal(fx.coreVisualBudget(390).tier,0);assert.equal(fx.coreVisualBudget(737).tier,1);assert.equal(fx.coreVisualBudget(1440).tier,2);assert.equal(fx.coreVisualBudget(1440,true).tier,0);
 });
 await test('fresh genuine JEV receipt is required for submitted colour; WAIT/reject/missing remain distinct',()=>{
  const state=(status,final_outcome)=>({jev_core:{last_decision:{status,final_outcome}}});
  assert.equal(fx.corePhase(state('JEV_WAIT'),[]).phase,'wait');assert.equal(fx.corePhase(state('RISK_REJECTED'),[]).phase,'reject');assert.equal(fx.corePhase(state('PLAN_INVALIDATED'),[]).phase,'reject');assert.equal(fx.corePhase(state('NO_JEV_DECISION'),[]).phase,'offline');
  assert.equal(fx.corePhase(state('JEV_ACTION_SELECTED','ORDER_SUBMITTED'),[]).phase,'submitted');assert.equal(fx.corePhase(state('JEV_ACTION_SELECTED','RISK_PASSED'),[]).phase,'idle');assert.equal(fx.corePhase(state('JEV_ACTION_SELECTED','ORDER_SUBMITTED'),[],false).phase,'offline');
  assert.equal(fx.corePhase({orders:[{status:'FILLED'}]},[]).phase,'idle');assert.equal(fx.corePhase({jev_core:{busy:true}},[]).phase,'working');
 });
 await test('only actual working booleans count, with a hard twelve-worker cap',()=>{
  assert.equal(fx.corePhase({},[{working:false},{working:'true'}]).active,0);assert.equal(fx.corePhase({},[{working:true},{working:false}]).active,1);assert.equal(fx.corePhase({},Array.from({length:99},()=>({working:true}))).active,12);
 });
 await test('cinematic geometry is layered, has an open aperture and never allocates per-frame geometry',()=>{
  const world=new THREE.Group(),visual=fx.createCinematicCore(THREE,world,{motes:999999}),group=world.children[0],initialChildren=group.children.length,initial=visual.readback();
  assert(initial.ringCount>=3);assert(initial.ribbonCount>=6);assert.equal(initial.particleLimit,192);assert(initial.allocatedGeometryCount<=20);
  group.traverse(child=>{if(!child.geometry)return;assert.notEqual(child.geometry.type,'SphereGeometry');if(child.userData.coreSeed)return;const attr=child.geometry.getAttribute('position');for(let i=0;i<attr.count;i++)assert(Math.hypot(attr.getX(i),attr.getZ(i))>=1.2-1e-5,'outer energy geometry must leave the aperture open');});
  const working=fx.corePhase({jev_core:{busy:true}},[{working:true}]);for(let i=0;i<120;i++)visual.update(.016,i*.016,working,.25);
  assert.equal(group.children.length,initialChildren);assert.equal(visual.readback().allocatedGeometryCount,initial.allocatedGeometryCount);assert.equal(visual.readback().particleLimit,192);visual.dispose();
 });
 await test('central focus is a tiny open wire lattice, three thin filaments and a texture-only soft corona',()=>{
  const world=new THREE.Group(),visual=fx.createCinematicCore(THREE,world,{motes:32}),state=visual.readback(),seed=world.children[0].children.find(c=>c.userData.coreSeed);assert(seed);assert.equal(state.coreSeedRadius,.37);assert.equal(state.coreSeedFilaments,3);assert.equal(state.coreSeedCoronaWidth,1.05);assert.equal(seed.children.filter(c=>c.type==='LineLoop').length,3);
  const wire=seed.children.find(c=>c.type==='Mesh');assert.equal(wire.material.wireframe,true);assert(wire.material.transparent);wire.geometry.computeBoundingSphere();assert(wire.geometry.boundingSphere.radius<=.38);const corona=seed.children.find(c=>c.type==='Sprite');assert(!corona.visible,'no opaque rectangle without a supplied radial texture');assert(corona.material.transparent);visual.dispose();
 });
 await test('graph mode dims only orbital decoration to thirty percent and preserves the physical seed',()=>{const a=new THREE.Group(),b=new THREE.Group(),normal=fx.createCinematicCore(THREE,a,{motes:32}),graph=fx.createCinematicCore(THREE,b,{motes:32,graphMode:true}),state=fx.corePhase({jev_core:{busy:true}},[]);normal.update(0,0,state);graph.update(0,0,state);const left=[],right=[];a.traverse(o=>{if(o.material)left.push(o);});b.traverse(o=>{if(o.material)right.push(o);});assert.equal(left.length,right.length);for(let i=0;i<left.length;i++){const x=left[i],y=right[i];if(x.userData.coreSeed){assert.equal(y.material.opacity,x.material.opacity);}else if(x.material.uniforms?.uDecor){assert.equal(x.material.uniforms.uDecor.value,1);assert.equal(y.material.uniforms.uDecor.value,.3);}else{assert(Math.abs(y.material.opacity-x.material.opacity*.3)<1e-9);}}assert.equal(graph.readback().decorativeScale,.3);normal.dispose();graph.dispose();});
 await test('zero-delta freezes every geometry, rotation, shader clock and intensity snapshot',()=>{
  const world=new THREE.Group(),visual=fx.createCinematicCore(THREE,world,{motes:64}),state=fx.corePhase({jev_core:{busy:true}},[]);
  visual.update(.05,2,state,.2);const snapshot=JSON.stringify(world.toJSON());visual.update(0,999999,state,.2);assert.equal(visual.readback().time,2);assert.equal(visual.readback().lastDt,0);assert.equal(JSON.stringify(world.toJSON()),snapshot);visual.dispose();
 });
 await test('quality changes clamp draw count without new allocations and dispose only owned geometry',()=>{
  const world=new THREE.Group(),foreign=new THREE.Group();world.add(foreign);const visual=fx.createCinematicCore(THREE,world,{motes:96}),group=world.children.find(c=>c!==foreign),allocated=visual.readback().allocatedGeometryCount;let disposed=0;
  group.traverse(child=>child.geometry?.addEventListener('dispose',()=>disposed++));for(const quality of [-99,0,1,2,NaN,999]){visual.setQuality(quality);const s=visual.readback();assert(Number.isFinite(s.particleDrawCount));assert(s.particleDrawCount>=1&&s.particleDrawCount<=96);assert.equal(s.allocatedGeometryCount,allocated);}visual.dispose();assert.equal(world.children.length,1);assert.equal(world.children[0],foreign);assert.equal(disposed,allocated);
 });
 await test('edge dock is keyboard-operable, keeps original IDs, external CSP assets and existing motion override',()=>{
  const html=fs.readFileSync(path.join(webRoot,'stage.html'),'utf8'),css=fs.readFileSync(path.join(webRoot,'core-effects.css'),'utf8'),module=fs.readFileSync(path.join(webRoot,'core-effects.js'),'utf8'),three=fs.readFileSync(path.join(webRoot,'three-viz.js'),'utf8');
  for(const id of ['jevCore','jcStatus','jcMeta','coreDetailsToggle','coreLabSlot'])assert(html.includes('id="'+id+'"'));
  assert(html.includes('aria-controls="jcMeta"'));assert(html.includes('href="/core-effects.css"'));assert(module.includes("event.key==='Escape'"));assert(module.includes("hostWindow.JevCoreEffects="));assert(!/fetch\s*\(/.test(module));assert(css.includes('grid-template-rows:38px auto'));assert(css.includes('grid-template-rows:35px auto'));assert(!/min-height\s*:/.test(css));
  assert(three.includes('allowCoreMotion=window.LineWorkers?.motionAllowed?.()??!reducedCore'));assert(three.includes('document.body.classList.contains("noanim")||document.hidden'));
  assert(three.includes('ringComposer.render();renderLineWorkers();'),'held refresh repaints the base before alpha sprites');
  assert(three.includes('decisionGraph.tick(0);'),'held refresh cannot advance graph packets');
  assert(three.includes('ringComposer?.setSize(w,h);'),'paused resize updates compositor render targets');
  assert(three.includes('if(ringComposer&&document.body.classList.contains("noanim"))renderFrozen();'),'paused resize repaints without resuming');
 });
 const failed=cases.filter(c=>!c.pass);console.log(JSON.stringify({webRoot,total:cases.length,passed:cases.length-failed.length,failed},null,2));if(failed.length)process.exitCode=1;
}
main().catch(e=>{console.error(e.stack);process.exitCode=1;});
