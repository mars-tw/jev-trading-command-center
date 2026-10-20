/* Chromium pixel acceptance; isolated synthetic HTTP fixture, never calls a trading service. */
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require(process.env.JEV_PLAYWRIGHT_MODULE||'playwright');
const webRoot=path.resolve(process.argv[2]||path.join(__dirname,'../web'));
const resultDir=path.resolve(process.argv[3]||path.join(require('node:os').tmpdir(),'jev-game-art-browser'));
const assets=['codex','claude','grok','agy','jev','muse','qwen'];
for(const a of assets)assert(fs.existsSync(path.join(webRoot,'pets-game-v1',a+'.png')),'Missing completed atlas: '+a);
const vendorRoot=process.env.JEV_THREE_VENDOR||(fs.existsSync(path.join(webRoot,'vendor/three'))?path.join(webRoot,'vendor/three'):path.resolve(webRoot,'../../releases/jev-trading-command-center/web/vendor/three'));
const mime={'.html':'text/html; charset=utf-8','.js':'application/javascript','.png':'image/png','.css':'text/css','.svg':'image/svg+xml','.md':'text/plain; charset=utf-8'};
let releaseImages=false;const pending=[];
function sendFile(res,file){try{res.writeHead(200,{'Content-Type':mime[path.extname(file)]||'application/octet-stream','Cache-Control':'no-store'});res.end(fs.readFileSync(file));}catch{res.writeHead(404);res.end();}}
const server=http.createServer((req,res)=>{
 const route=new URL(req.url,'http://fixture.invalid').pathname;
 if(route==='/api/bootstrap.js'){res.writeHead(200,{'Content-Type':'application/javascript'});return res.end('window.__JEV_DEMO__=false;window.__JEV_TEST_FIXTURE__=true;');}
 if(route.startsWith('/api/')){res.writeHead(200,{'Content-Type':'application/json'});return res.end(JSON.stringify({status:'warming',schema_version:0}));}
 const file=path.resolve(webRoot,'.'+(route==='/'?'/stage.html':route));if(!file.startsWith(webRoot+path.sep)){res.writeHead(403);return res.end();}
 // Delay one atlas only: holding every same-origin HTTP/1 connection would block Three module imports.
 if(route==='/pets-game-v1/codex.png'&&!releaseImages){pending.push([res,file]);return;}sendFile(res,file);
});
async function main(){
 fs.mkdirSync(resultDir,{recursive:true});await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin='http://127.0.0.1:'+server.address().port;
 let browser;const result={fixture:'isolated synthetic state; no live services',webRoot,cases:[]};
 try{
  browser=await chromium.launch({headless:true,args:['--use-angle=swiftshader','--enable-unsafe-swiftshader']});const page=await browser.newPage({viewport:{width:1440,height:1000}});
  await page.route('http://127.0.0.1:8766/**',route=>route.fulfill({status:200,contentType:'application/json',body:'{"schema_version":0}'}));
  await page.route('https://cdn.jsdelivr.net/npm/three@0.186.1/**',route=>{const suffix=new URL(route.request().url()).pathname.split('/three@0.186.1/')[1],file=path.resolve(vendorRoot,suffix);return route.fulfill({status:200,contentType:'application/javascript',body:fs.readFileSync(file)});});
  await page.goto(origin+'/stage.html',{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>window.LineWorkers&&window.__threeOK&&window.__lineWorkerReadback?.length===4,{timeout:30000});
  await page.evaluate(()=>{document.body.classList.add('noanim');window.__artBefore=LineWorkers.makeTextureCanvas('CODEX','front',0,false);const g=document.getElementById('pipeEdges');if(g&&!g.querySelector('path')){const p=document.createElementNS('http://www.w3.org/2000/svg','path');p.setAttribute('d','M 80 74 L 1120 74');g.appendChild(p);}});
  await page.waitForFunction(()=>document.querySelector('.lw-svg-pet'));
  const before=await page.evaluate(()=>{window.__blankArtUrl=document.querySelector('.lw-svg-pet').getAttribute('href');return{status:LineWorkers.artStatus(),ready:__artBefore.dataset.ready,url:__blankArtUrl,workers:__lineWorkerReadback.map(w=>({id:w.workerId,position:w.position,view:w.view,frame:w.frame,textureVersion:w.textureVersion}))};});
  assert.equal(before.status.state,'loading');assert.equal(before.ready,'false');assert(before.workers.every(w=>w.textureVersion>=1));
  releaseImages=true;for(const [res,file]of pending.splice(0))sendFile(res,file);
  await page.waitForFunction(()=>LineWorkers.artStatus().state!=='loading');const status=await page.evaluate(()=>LineWorkers.whenArtReady());assert.equal(status.state,'ready');assert.equal(status.loaded,7);
  await page.waitForFunction(()=>__lineWorkerReadback.every(w=>w.textureReady==='true')&&document.querySelector('.lw-svg-pet').getAttribute('href')!==window.__blankArtUrl);
  const after=await page.evaluate(()=>({sameCanvas:__artBefore===LineWorkers.makeTextureCanvas('CODEX','front',0,false),ready:__artBefore.dataset.ready,url:document.querySelector('.lw-svg-pet').getAttribute('href'),workers:__lineWorkerReadback.map(w=>({id:w.workerId,position:w.position,view:w.view,frame:w.frame,textureVersion:w.textureVersion})),phase:LineWorkers.advancePhase(.3,.08,true),frame1:LineWorkers.frameFor(200),frame2:LineWorkers.frameFor(9000)}));
  assert(after.sameCanvas);assert.equal(after.ready,'true');assert.notEqual(after.url,before.url);assert.equal(after.phase,.3);assert.equal(after.frame1,after.frame2);
  for(const old of before.workers){const w=after.workers.find(w=>w.id===old.id);assert(w);assert(w.textureVersion>old.textureVersion);assert.deepEqual(w.position,old.position);assert.equal(w.view,old.view);assert.equal(w.frame,old.frame);}
  result.cases.push({name:'async PNG load updates same canvases, SVG href and Three texture versions during total pause',pass:true});
  const pixels=await page.evaluate(()=>{
   function stats(c){const a=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let hash=2166136261,visible=0,transparent=0;for(let i=0;i<a.length;i++){hash=Math.imul(hash^a[i],16777619)>>>0;if(i%4===3){if(a[i])visible++;else transparent++;}}return{hash,visible,transparent,cell:c.dataset.cell};}
   return['CODEX','CLAUDE','GROK','AGY','JEV','MUSE','QWEN'].map(alias=>{const a=LineWorkers.makeTextureCanvas(alias,'front',1,false).getContext('2d').getImageData(0,0,192,192).data,b=LineWorkers.makeTextureCanvas(alias,'front',3,false).getContext('2d').getImageData(0,0,192,192).data;let delta=0;for(let i=0;i<a.length;i++)delta+=Math.abs(a[i]-b[i]);return{alias,patrolDuplicateDelta:delta/a.length,views:['front','right','back','left'].map(v=>stats(LineWorkers.makeTextureCanvas(alias,v,1,false))),patrol:[0,1,2,3].map(f=>stats(LineWorkers.makeTextureCanvas(alias,'front',f,false))),work:[0,1].map(f=>stats(LineWorkers.makeTextureCanvas(alias,'front',f,true))),expressions:['pleased','focused','concerned','tired'].map(e=>stats(LineWorkers.makeExpressionCanvas(alias,e)))};});
  });
  result.pixels=pixels;
  for(const p of pixels){assert.equal(new Set(p.views.map(v=>v.hash)).size,4,p.alias+' four facings');assert.equal(new Set(p.expressions.map(v=>v.hash)).size,4,p.alias+' four expressions');assert.equal(new Set(p.patrol.slice(0,3).map(v=>v.hash)).size,3,p.alias+' three original patrol poses');assert.deepEqual(p.patrol.map(p=>p.cell),['0,1','0,0','0,2','0,0']);assert(p.patrolDuplicateDelta<1,p.alias+' repeated idle cell agrees within canvas rounding');assert.equal(new Set(p.work.map(v=>v.hash)).size,2,p.alias+' two work poses');assert(!p.work.some(w=>p.patrol.some(i=>i.hash===w.hash)),p.alias+' genuine work drawings');for(const s of [...p.views,...p.work,...p.expressions]){assert(s.visible>50,p.alias+' visible original pixels');assert(s.transparent>50,p.alias+' transparent cutout');}}
  result.cases.push({name:'seven real PNGs have transparent visible pixels, four facings, two work poses and four expressions',pass:true});
  await page.screenshot({path:path.join(resultDir,'stage-paused-loaded.png'),fullPage:true});
  await page.goto(origin+'/pets-game-v1/preview.html',{waitUntil:'networkidle'});await page.waitForFunction(()=>window.__petGalleryReadback?.().loaded===7);
  await page.selectOption('#mode','work');await page.selectOption('#view','2');assert((await page.evaluate(()=>__petGalleryReadback().cards)).every(c=>c.cell==='2,3'||c.cell==='2,4'));
  await page.selectOption('#mode','expression');await page.selectOption('#emotion','2');assert((await page.evaluate(()=>__petGalleryReadback().cards)).every(c=>c.cell==='2,5'));
  await page.screenshot({path:path.join(resultDir,'gallery-expressions.png'),fullPage:true});await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(resultDir,'gallery-mobile.png'),fullPage:true});
  result.cases.push({name:'interactive gallery all seven roles, work/back direction, expressions and 390px width',pass:true});
  result.pass=true;
 }catch(e){result.pass=false;result.error=e.message;throw e;}finally{releaseImages=true;for(const [res,file]of pending.splice(0))sendFile(res,file);if(browser)await browser.close();await new Promise(r=>server.close(r));fs.writeFileSync(path.join(resultDir,'browser-acceptance.json'),JSON.stringify(result,null,2));}
 console.log(JSON.stringify({pass:result.pass,cases:result.cases,result:path.join(resultDir,'browser-acceptance.json')},null,2));
}
main().catch(e=>{console.error(e.stack);process.exitCode=1;});
