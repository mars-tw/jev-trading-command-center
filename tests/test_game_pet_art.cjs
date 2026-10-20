/* Focused atlas lifecycle contract. Real PNG pixels are verified separately in Chromium. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const target=path.resolve(process.argv[2]||path.join(__dirname,'../web/line-workers.js'));
const source=fs.readFileSync(target,'utf8');
const match=source.match(/\/\* GAME PET ATLAS START[^\n]*\n([\s\S]*?)\/\* GAME PET ATLAS END \*\//);
assert(match,'production atlas factory marker is present');
const factory=vm.runInNewContext(match[1]+';createGamePetArt;',{});
function env(){
 const images=[],canvases=[];
 class ImageMock{
  constructor(){images.push(this);}
  set src(v){assert.equal(typeof this.onload,'function');assert.equal(typeof this.onerror,'function');this.url=v;}
  load(width=1536,height=2304){this.naturalWidth=width;this.naturalHeight=height;this.onload();}
  fail(){this.onerror();}
 }
 const doc={createElement(tag){assert.equal(tag,'canvas');const c={dataset:{},calls:[],width:0,height:0};
  const context={clearRect(...a){c.calls.push(['clear',...a]);c.pixels='transparent';},drawImage(img,...a){c.calls.push(['draw',img.url,...a]);c.pixels=img.url+'|'+a.slice(0,4).join(',');}};
  c.getContext=()=>context;c.toDataURL=()=>c.pixels;canvases.push(c);return c;}};
 return{images,canvases,api:factory(doc,ImageMock)};
}
async function main(){const cases=[];async function test(name,fn){try{await fn();cases.push({name,pass:true});}catch(e){cases.push({name,pass:false,message:e.message});}}
 await test('all seven original atlases preload without fabricated pixels or DOM insertion',()=>{
  const {images,api}=env();assert.equal(images.length,7);assert.equal(api.artStatus().state,'loading');assert.equal(api.artStatus().total,7);
  assert.equal(new Set(images.map(i=>i.url)).size,7);const c=api.makeTextureCanvas('CODEX');assert.equal(c.width,192);assert.equal(c.height,192);assert.equal(c.dataset.ready,'false');assert.equal(c.pixels,'transparent');assert(!c.calls.some(c=>c[0]==='draw'));
 });
 await test('same cache canvases receive actual atlas crop after asynchronous load and URL invalidation',()=>{
  const {images,api}=env(),a=api.makeTextureCanvas('GROK','front',0,false),busy=api.makeTextureCanvas('GROK','front',0,true),before=api.imageUrl('GROK','front',0,false);
  assert.equal(a,api.makeTextureCanvas('GROK','front',0,false));images.find(i=>i.url.endsWith('/grok.png')).load();
  assert.equal(api.artRevision(),1);assert.equal(a.dataset.ready,'true');assert.equal(a,api.makeTextureCanvas('GROK','front',0,false));
  assert.deepEqual(a.calls.at(-1),['draw','/pets-game-v1/grok.png',0,384,384,384,0,0,192,192]);
  assert.deepEqual(busy.calls.at(-1),['draw','/pets-game-v1/grok.png',0,1152,384,384,0,0,192,192]);
  assert.notEqual(api.imageUrl('GROK','front',0,false),before);assert.equal(api.imageUrl('GROK','front',0,false),a.pixels);
 });
 await test('four original directions and patrol/work frames crop complete cells only',()=>{
  const {images,api}=env();images.find(i=>i.url.endsWith('/codex.png')).load();
  for(const [col,view]of ['front','right','back','left'].entries())for(let frame=0;frame<4;frame++)for(const working of [false,true]){
   const c=api.makeTextureCanvas('CODEX',view,frame,working),row=(working?[3,4,3,4]:[1,0,2,0])[frame];
   assert.equal(c.dataset.cell,`${col},${row}`);assert.deepEqual(c.calls.at(-1),['draw','/pets-game-v1/codex.png',col*384,row*384,384,384,0,0,192,192]);
  }
 });
 await test('expressions are four complete frontal drawings and never imply live work',()=>{
  const {images,api}=env();images.find(i=>i.url.endsWith('/muse.png')).load();
  for(const [col,emotion]of ['pleased','focused','concerned','tired'].entries()){
   const c=api.makeExpressionCanvas('MUSE',emotion);assert.equal(c,api.makeExpressionCanvas('MUSE',emotion));assert.equal(c.dataset.cell,`${col},5`);assert.equal(c.dataset.view,'front');assert.equal(c.dataset.working,'false');assert.equal(c.dataset.ready,'true');
  }
 });
 await test('normalization bounds caches, supports future MUSE/QWEN art, and uses JEV for SYSTEM',()=>{
  const {api}=env();assert.equal(api.makeTextureCanvas('SYSTEM'),api.makeTextureCanvas('JEV'));assert.equal(api.makeTextureCanvas('untrusted'),api.makeTextureCanvas('SYSTEM'));
  assert.equal(api.makeTextureCanvas('CODEX','invalid',-1,false),api.makeTextureCanvas('codex','front',3,false));assert.equal(api.makeTextureCanvas('QWEN').dataset.asset,'qwen');assert.equal(api.makeTextureCanvas('MUSE').dataset.asset,'muse');
  assert.equal(api.makeTextureCanvas('CODEX','front',0,'true').dataset.working,'false');assert.equal(api.makeExpressionCanvas('JEV','unknown'),api.makeExpressionCanvas('JEV','focused'));
 });
 await test('load observers notify after actual pixels and support safe unsubscription',()=>{
  const {images,api}=env(),c=api.makeTextureCanvas('CODEX'),events=[];
  api.onArtChange(()=>{throw Error('independent subscriber');});const stop=api.onArtChange(e=>{assert.equal(c.dataset.ready,'true');events.push({asset:e.asset,revision:e.revision});});
  images.find(i=>i.url.endsWith('/codex.png')).load();assert.deepEqual(events,[{asset:'codex',revision:1}]);stop();images.find(i=>i.url.endsWith('/claude.png')).load();assert.equal(events.length,1);assert.equal(api.artRevision(),2);
 });
 await test('unavailable canvas export falls back to a verified fully transparent pixel',()=>{
  const doc={createElement(){return{width:0,height:0,dataset:{},getContext:()=>null,toDataURL(){throw Error('export unavailable');}};}};
  const api=factory(doc,class{set src(v){}}),url=api.imageUrl('CODEX'),png=Buffer.from(url.split(',')[1],'base64');
  assert.equal(png.subarray(1,4).toString(),'PNG');let at=8,packed=[];while(at<png.length){const len=png.readUInt32BE(at),type=png.subarray(at+4,at+8).toString();if(type==='IDAT')packed.push(png.subarray(at+8,at+8+len));at+=12+len;}
  const rgba=require('node:zlib').inflateSync(Buffer.concat(packed));assert.deepEqual([...rgba],[0,0,0,0,0]);
 });
 await test('invalid dimensions and image errors remain transparent; settled promise reports failure',async()=>{
  const {images,api}=env(),bad=api.makeTextureCanvas('AGY');let done=false;const ready=api.whenArtReady().then(s=>{done=true;return s;});
  images.find(i=>i.url.endsWith('/agy.png')).load(1536,2303);assert.equal(bad.dataset.ready,'false');assert.equal(bad.pixels,'transparent');assert.equal(api.artStatus().assets.agy.state,'error');assert.equal(api.artStatus().assets.agy.error,'invalid atlas size');
  images.find(i=>i.url.endsWith('/claude.png')).fail();for(const img of images)if(!/\/(agy|claude)\.png$/.test(img.url))img.load();
  const status=await ready;assert(done);assert.equal(status.state,'error');assert.equal(status.loaded,5);assert.equal(status.total,7);assert.equal((await api.whenArtReady()).state,'error');
 });
 const failed=cases.filter(c=>!c.pass);console.log(JSON.stringify({source:target,total:cases.length,passed:cases.length-failed.length,failed},null,2));if(failed.length)process.exitCode=1;
}
main().catch(e=>{console.error(e.stack);process.exitCode=1;});
