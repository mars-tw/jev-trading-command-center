import { createDecisionGraph } from "./decision-graph.js";
import { createCinematicCore, coreVisualBudget, corePhase, initCoreDock } from "./core-effects.js";
// Three.js 沉浸視覺：電漿決策球 + 藍紫粒子環面軌道核心。資料由 classic script 寫入 window.LIVE。
import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";

const PLASMA_VERT = `
varying vec3 vPos; varying vec3 vNor;
void main(){ vPos=position; vNor=normalize(normalMatrix*normal);
  gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0); }`;
const PLASMA_FRAG = `
uniform float uTime; uniform vec3 uColor; uniform float uIntensity;
varying vec3 vPos; varying vec3 vNor;
void main(){
  float n = sin(vPos.x*6.0+uTime)*0.34 + sin(vPos.y*8.0-uTime*1.3)*0.28
          + sin((vPos.z+vPos.x)*10.0+uTime*0.7)*0.22 + sin(length(vPos)*14.0-uTime*1.8)*0.16;
  n = n*0.5+0.5;
  float filaments = smoothstep(0.52, 0.95, n);
  vec3 viewDir = vec3(0.0,0.0,1.0);
  float fres = pow(1.0 - abs(dot(viewDir, normalize(vNor))), 2.0);
  vec3 col = uColor * (0.25 + filaments*1.6*uIntensity) + vec3(1.0)*filaments*0.35;
  col += uColor * fres * 1.2;
  gl_FragColor = vec4(col, 0.92);
}`;

function makePlasma(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(240, 240, false);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
  camera.position.z = 4.2;
  const group = new THREE.Group(); scene.add(group);
  const mat = new THREE.ShaderMaterial({
    uniforms: { uTime: { value: 0 }, uColor: { value: new THREE.Color(0x37e0ff) }, uIntensity: { value: 1 } },
    vertexShader: PLASMA_VERT, fragmentShader: PLASMA_FRAG, transparent: true,
  });
  group.add(new THREE.Mesh(new THREE.SphereGeometry(1.05, 48, 48), mat));
  const core = new THREE.Mesh(new THREE.SphereGeometry(0.22, 24, 24),
    new THREE.MeshBasicMaterial({ color: 0xffffff }));
  group.add(core);
  const shell = new THREE.Mesh(new THREE.SphereGeometry(1.18, 48, 48),
    new THREE.MeshPhysicalMaterial({ color: 0x88ccff, transparent: true, opacity: 0.10, roughness: 0.1, metalness: 0 }));
  group.add(shell);
  const arcs = [];
  const arcMat = new THREE.LineBasicMaterial({ color: 0xbfefff, transparent: true, opacity: 0.9 });
  for (let i = 0; i < 11; i++) {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(8 * 3), 3));
    const line = new THREE.Line(g, arcMat); group.add(line); arcs.push(line);
  }
  const rings = [];
  for (let i = 0; i < 2; i++) {
    const ring = new THREE.Mesh(new THREE.TorusGeometry(1.45 + i * 0.22, 0.012, 8, 90),
      new THREE.MeshBasicMaterial({ color: i ? 0xff2ea6 : 0x37e0ff, transparent: true, opacity: 0.75 }));
    ring.rotation.x = Math.PI / 2.4 + i * 0.5; group.add(ring); rings.push(ring);
  }
  function regenArcs() {
    for (const line of arcs) {
      const a = Math.random() * Math.PI * 2, b = Math.acos(2 * Math.random() - 1);
      const end = new THREE.Vector3(Math.sin(b) * Math.cos(a), Math.cos(b), Math.sin(b) * Math.sin(a)).multiplyScalar(1.12);
      const pos = line.geometry.attributes.position;
      for (let s = 0; s < 8; s++) {
        const t = s / 7, p = end.clone().multiplyScalar(t);
        if (s > 0 && s < 7) { p.x += (Math.random() - .5) * .16; p.y += (Math.random() - .5) * .16; p.z += (Math.random() - .5) * .16; }
        pos.setXYZ(s, p.x, p.y, p.z);
      }
      pos.needsUpdate = true;
    }
  }
  regenArcs();
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  composer.addPass(new UnrealBloomPass(new THREE.Vector2(240, 240), 1.15, 0.55, 0.12));
  let drag = null, vx = 0.004, vy = 0.002;
  canvas.addEventListener("pointerdown", e => { drag = { x: e.clientX, y: e.clientY }; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener("pointermove", e => { if (!drag) return; vx = (e.clientX - drag.x) * 0.0006; vy = (e.clientY - drag.y) * 0.0006; drag = { x: e.clientX, y: e.clientY }; });
  canvas.addEventListener("pointerup", () => drag = null);
  const clock = new THREE.Clock(); let frames = 0;
  renderer.setAnimationLoop(() => {
    if (document.body.classList.contains("noanim")) return;
    const t = clock.getElapsedTime();
    mat.uniforms.uTime.value = t;
    const st = window.__plasmaState || { color: "#37e0ff", intensity: 1 };
    mat.uniforms.uColor.value.set(st.color);
    mat.uniforms.uIntensity.value = st.intensity || 1;
    core.material.color.set(st.color);
    group.rotation.y += vx + 0.0035; group.rotation.x += vy;
    vx *= 0.96; vy *= 0.96;
    rings[0].rotation.z = t * 0.5; rings[1].rotation.z = -t * 0.35;
    if (++frames % 7 === 0) regenArcs();
    composer.render();
  });
  return { renderer };
}

function textSprite(text, color) {
  const c = document.createElement("canvas"); c.width = 512; c.height = 128;
  const x = c.getContext("2d");
  x.font = "bold 46px Consolas, monospace"; x.textAlign = "center";
  x.shadowColor = color; x.shadowBlur = 14; x.fillStyle = "#fff";
  x.fillText(text.slice(0, 14), 256, 84);
  const tex = new THREE.CanvasTexture(c);
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false }));
  sp.scale.set(2.0, 0.5, 1);
  return sp;
}

// 藍紫粒子環面軌道核心：立體中空環面 + 絲流軌跡 + 訊號錨點 + 事件特效 + 鏡頭巡航
function makeRing(canvas) {
  const reducedMedia=window.matchMedia?.("(prefers-reduced-motion: reduce)");
  let reducedCore=!!reducedMedia?.matches;
  reducedMedia?.addEventListener?.("change",event=>{reducedCore=!!event.matches;});
  const budget=coreVisualBudget(canvas.clientWidth||900,reducedCore&&!window.LineWorkers?.motionAllowed?.(),navigator.hardwareConcurrency||8);
  const dock=initCoreDock(document,window);
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, budget.pixelRatio));
  const scene = new THREE.Scene();
  let ringComposer=null;
  const camera = new THREE.PerspectiveCamera(48, 2, 0.1, 100);
  camera.position.set(0, 2.6, 7.4); camera.lookAt(0, 0, 0);
  const world = new THREE.Group(); world.rotation.x = 0.38; world.scale.setScalar(1.12); scene.add(world);
  const R = 2.35, TUBE = 0.78;
  const torusPos = (u, v, rr) => new THREE.Vector3(
    (R + rr * Math.cos(v)) * Math.cos(u), rr * Math.sin(v), (R + rr * Math.cos(v)) * Math.sin(u));
  // 密集粒子（藍->紫->青，少數青白亮點）
  function makeCloud(count, size) {
    const geo = new THREE.BufferGeometry();
    const pos = new Float32Array(count * 3), col = new Float32Array(count * 3), seed = [];
    const cBlue = new THREE.Color(0x1657ad), cPurp = new THREE.Color(0x6965bb),
      cCyan = new THREE.Color(0x75deef), cWhite = new THREE.Color(0xe8f6ff), tmp = new THREE.Color();
    for (let i = 0; i < count; i++) {
      const u = Math.random() * Math.PI * 2, v = Math.random() * Math.PI * 2;
      const rr = TUBE * (0.72 + Math.random() * 0.38);
      const p = torusPos(u, v, rr);
      pos.set([p.x, p.y, p.z], i * 3);
      const f = 0.5 + 0.5 * Math.sin(u * 2.0 + v * 1.3);
      tmp.copy(cBlue).lerp(cPurp, f).lerp(cCyan, Math.max(0, Math.sin(v + u)) * 0.32);
      if (Math.random() < 0.03) tmp.copy(cWhite);
      col.set([tmp.r, tmp.g, tmp.b], i * 3);
      seed.push({ u, v, rr, sp: 0.5 + Math.random() * 1.1 });
    }
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
    const mat = new THREE.PointsMaterial({ size, map: spriteTex, vertexColors: true, transparent: true, opacity: 0.20,
      blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true });
    const pts = new THREE.Points(geo, mat); world.add(pts);
    return { geo, seed, count, activeCount:count, mat };
  }
  // 柔光粒子貼圖（徑向漸層），避免方形點
  const spriteTex = (() => { const c = document.createElement("canvas"); c.width = c.height = 64; const x = c.getContext("2d");
    const g = x.createRadialGradient(32, 32, 0, 32, 32, 32);
    g.addColorStop(0, "rgba(255,255,255,1)"); g.addColorStop(0.35, "rgba(255,255,255,.6)"); g.addColorStop(1, "rgba(255,255,255,0)");
    x.fillStyle = g; x.fillRect(0, 0, 64, 64); return new THREE.CanvasTexture(c); })();
  const cloudA = makeCloud(budget.mainParticles, 0.043), cloudB = makeCloud(budget.accentParticles, 0.076);
  // Whole original atlas pets travel on stable declared graph channels.
  const workerSprites=new Map(),workerTextures=new Map(),petPhaseMemory=new Map(),workerScene=new THREE.Scene(),workerRoot=new THREE.Group();workerScene.add(workerRoot);
  const graphCamera=new THREE.OrthographicCamera(0,900,500,0,.1,10);graphCamera.position.z=5;
  const decisionGraph=createDecisionGraph(THREE,canvas.parentElement,workerScene,{readState:()=>({live:window.LIVE,workerState:window.__lineWorkersState,lab:window.__strategyLabState,demoPage:window.__JEV_DEMO__===true}),onInvalidate:()=>{if(ringComposer&&!document.hidden){updateLineWorkers(0);renderFrozen();}}});
  function petTexture(alias,view,frame,working){const key=[alias,view,frame,working].join('|');let texture=workerTextures.get(key);if(!texture){texture=new THREE.CanvasTexture(window.LineWorkers.makeTextureCanvas(alias,view,frame,working));texture.colorSpace=THREE.SRGBColorSpace;workerTextures.set(key,texture);}const revision=Number(window.LineWorkers.artRevision?.()||0);if(texture.userData.artRevision!==revision){texture.needsUpdate=true;texture.userData.artRevision=revision;}return texture;}
  window.LineWorkers?.onArtChange?.(()=>{for(const texture of workerTextures.values())texture.needsUpdate=true;if(ringComposer&&!document.hidden)renderFrozen();});
  const petTarget=new THREE.Vector3(),petAhead=new THREE.Vector3();
  function updateLineWorkers(dt,reproject=false){
    const api=window.LineWorkers,state=window.__lineWorkersState;if(!api)return;const items=state&&Date.now()<state.valid_until?state.workers:[],keep=new Set(),moving=dt>0&&api.motionAllowed(),w=canvas.clientWidth||900,h=canvas.clientHeight||500;
    items.slice(0,12).forEach((worker,i)=>{keep.add(worker.id);let sp=workerSprites.get(worker.id),created=false,restored=false;
      if(!sp){sp=new THREE.Sprite(new THREE.SpriteMaterial({map:petTexture(worker.alias,'front',0,false),transparent:true,depthWrite:false,depthTest:false,sizeAttenuation:true,toneMapped:false}));sp.renderOrder=100;workerRoot.add(sp);workerSprites.set(worker.id,sp);const held=petPhaseMemory.get(worker.id);restored=!!held&&Date.now()-held.at<600000;sp.userData=restored?{u:held.u,view:held.view,frame:held.frame,routeId:held.routeId,appearanceKey:'held',artRevision:-1}:{u:(i*.173)%1};if(restored)sp.position.copy(held.position);created=true;}
      const d=sp.userData,nextRoute=decisionGraph.routeForWorker(worker),nextU=api.advancePhase(d.u,dt,worker.working);
      if(!d.routeId)d.routeId=nextRoute;
      else if(d.routeId!==nextRoute&&moving){d.transition={from:sp.position.clone(),age:0};d.routeId=nextRoute;}
      d.u=nextU;decisionGraph.sample(d.routeId,d.u,petTarget);
      if(d.transition&&moving){d.transition.age+=Math.min(.1,dt);const a=Math.min(1,d.transition.age/.55),k=a*a*(3-2*a);sp.position.copy(d.transition.from).lerp(petTarget,k);if(a===1)d.transition=null;}
      else if(moving||created&&!restored||reproject)sp.position.copy(petTarget);
      const size=Math.max(32,Math.min(46,w*.06));sp.scale.set(size,size,1);sp.material.opacity=worker.working?1:.64;
      Object.assign(d,{workerId:worker.id,source:worker.source,alias:worker.alias,name:worker.name,action:worker.action,label:worker.label,working:worker.working,job:worker.job,lane:worker.lane,pid:worker.pid,native_process:worker.native_process,demo_working:worker.demo_working,routePending:nextRoute!==d.routeId,renderer:'declared-graph'});
    });
    for(const [id,sp]of workerSprites)if(!keep.has(id)){const d=sp.userData;petPhaseMemory.set(id,{u:d.u,view:d.view,frame:d.frame,routeId:d.routeId,position:sp.position.clone(),at:Date.now()});while(petPhaseMemory.size>64)petPhaseMemory.delete(petPhaseMemory.keys().next().value);workerRoot.remove(sp);sp.material.dispose();workerSprites.delete(id);}
  }
  function renderLineWorkers(){
    const api=window.LineWorkers;if(!api)return;const points=[],now=performance.now(),bucket=Math.floor(now/140),width=canvas.clientWidth||900,height=canvas.clientHeight||500;
    for(const sp of workerSprites.values()){
      const d=sp.userData;decisionGraph.sample(d.routeId,d.u+.012,petAhead);const view=api.viewFor(petAhead.x-sp.position.x,-(petAhead.y-sp.position.y)),busy=d.working&&(d.source==='CLI'&&d.native_process===true||d.source==='DEMO'&&d.demo_working===true),moving=api.motionAllowed(),revision=Number(api.artRevision?.()||0);
      if(!d.appearanceKey||(moving&&d.artBucket!==bucket)||d.artWorking!==busy||d.artAlias!==d.alias||d.artRevision!==revision){d.view=moving||!d.appearanceKey?view:d.view;d.frame=moving||!d.appearanceKey?api.frameFor(now):d.frame;d.artBucket=bucket;d.artWorking=busy;d.artAlias=d.alias;d.artRevision=revision;d.appearanceKey=[d.alias,d.view,d.frame,busy].join('|');sp.material.map=petTexture(d.alias,d.view,d.frame,busy);}
      d.screenX=sp.position.x;d.screenY=height-sp.position.y;points.push({id:d.workerId,x:d.screenX,y:d.screenY});
    }
    const autoClear=renderer.autoClear;renderer.autoClear=false;renderer.clearDepth();renderer.render(workerScene,graphCamera);renderer.autoClear=autoClear;api.placeLabels(points);
    window.__lineWorkerReadback=[...workerSprites.values()].map(sp=>({...sp.userData,transition:sp.userData.transition?{age:sp.userData.transition.age}:null,position:sp.position.toArray(),textureVersion:sp.material.map?.version,textureReady:sp.material.map?.image?.dataset?.ready,textureSource:sp.material.map?.image?.dataset?.source}));window.__decisionGraphReadback=decisionGraph.readback();
  }
  function renderFrozen(){if(!ringComposer)return;const g=decisionGraph.readback(),data=window.LIVE;const cs=corePhase(data,[],g.mainFresh);dock?.setPhase(cs);coreAura.update(0,coreTime,cs,0);decisionGraph.tick(0);window.__coreVisualReadback={...(window.__coreVisualReadback||{}),...coreAura.readback(),coreTime,mainFresh:g.mainFresh};ringComposer.render();renderLineWorkers();}

  const graphDecor=decisionGraph.enabled?.3:1;
  // 絲線環（沿管面緯線）+ 中心輝光，強化中空立體感
  const filaments = [];
  for (let k = 0; k < 3; k++) {
    const pts = [], vb = 0.7 + k * 1.85;
    for (let i = 0; i <= 240; i++) { const u = (i / 240) * Math.PI * 2; pts.push(torusPos(u, vb + Math.sin(u * 3 + k * 2.1) * 0.16, TUBE * 1.02)); }
    const ln = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: k === 1 ? 0x8b5cff : 0x37e0ff, transparent: true, opacity: 0.17*graphDecor, blending: THREE.AdditiveBlending, depthWrite: false }));
    world.add(ln); filaments.push({ ln, dir: k % 2 ? -1 : 1 });
  }
  const centerGlow = new THREE.Sprite(new THREE.SpriteMaterial({ map: spriteTex, color: 0x6a5cff, transparent: true, opacity: 0.30, blending: THREE.AdditiveBlending, depthWrite: false }));
  centerGlow.scale.set(4.6, 4.6, 1); world.add(centerGlow);
  const coreAura=createCinematicCore(THREE,world,{mainRadius:R,tubeRadius:TUBE,motes:budget.motes,spriteTex,graphMode:decisionGraph.enabled});
  let qualityTier=budget.tier,slowFrames=0,coreTime=0,lastCoreReadback=0;
  coreAura.setQuality(qualityTier);
  let drag = null;
  canvas.addEventListener("pointerdown", e => { drag = { x: e.clientX, y: e.clientY }; });
  let parX = 0, parY = 0, parTX = 0, parTY = 0;
  canvas.addEventListener("pointermove", e => { const r = canvas.getBoundingClientRect(); parTX = ((e.clientX - r.left) / r.width - 0.5); parTY = ((e.clientY - r.top) / r.height - 0.5); });
  window.addEventListener("pointermove", e => { if (drag && window.__camMode === "fixed") { world.rotation.y += (e.clientX - drag.x) * 0.005; world.rotation.x += (e.clientY - drag.y) * 0.003; drag = { x: e.clientX, y: e.clientY }; } });
  window.addEventListener("pointerup", () => drag = null);
  function resize() {
    const w = canvas.clientWidth || 900, h = canvas.clientHeight || 430;
    renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
    graphCamera.right=w;graphCamera.top=h;graphCamera.updateProjectionMatrix();decisionGraph.resize(w,h);updateLineWorkers(0,true);ringComposer?.setSize(w,h);
    if(ringComposer&&document.body.classList.contains("noanim"))renderFrozen();
  }
  resize(); window.addEventListener("resize", resize);
  const composer = new EffectComposer(renderer);
  ringComposer=composer;
  composer.addPass(new RenderPass(scene, camera));
  composer.addPass(new UnrealBloomPass(new THREE.Vector2(900, 430), 0.62, 0.32, 0.48));
  // 鏡頭巡航：全景 -> 推近 -> 孔洞近景 -> 斜側 -> 拉回（平滑加減速 + 停留）
  const KEYS = [
    { p: [0, 2.6, 7.4], dwell: 3.0 }, { p: [0, 1.5, 4.8], dwell: 2.0 },
    { p: [0, 0.6, 2.9], dwell: 3.0 }, { p: [5.0, 2.0, 4.2], dwell: 3.0 },
    { p: [0, 2.6, 7.4], dwell: 2.5 },
  ];
  let cruiseT = 0;
  const ease = t => t * t * (3 - 2 * t);
  function cruiseCam(dt) {
    const segDur = 3.2; let t = cruiseT % ((segDur + 2.6) * KEYS.length);
    for (let i = 0; i < KEYS.length; i++) {
      const a = KEYS[i], b = KEYS[(i + 1) % KEYS.length];
      if (t < a.dwell) { camera.position.set(...a.p); camera.lookAt(0, 0, 0); return i; }
      t -= a.dwell;
      if (t < segDur) {
        const k = ease(t / segDur);
        camera.position.set(a.p[0] + (b.p[0] - a.p[0]) * k, a.p[1] + (b.p[1] - a.p[1]) * k, a.p[2] + (b.p[2] - a.p[2]) * k);
        camera.lookAt(0, 0, 0); return i;
      }
      t -= segDur;
    }
    return 0;
  }
  window.__camMode = window.__camMode || "fixed";
  let sig = "", clock = new THREE.Clock(), lastPhase = -1;
  let surgeUntil=0;
  const chipEls = {};
  let _fpsFrames = 0, _fpsT0 = performance.now();
  window.__fps = 0;
  renderer.setAnimationLoop(() => {
    if (document.body.classList.contains("noanim")||document.hidden) return;
    _fpsFrames++;
    const _n = performance.now();
    if (_n - _fpsT0 >= 1000) { window.__fps = Math.round(_fpsFrames * 1000 / (_n - _fpsT0)); _fpsFrames = 0; _fpsT0 = _n;slowFrames=window.__fps<28?slowFrames+1:0;if(slowFrames>=2&&qualityTier>0){qualityTier--;slowFrames=0;const factor=qualityTier===0?.45:.72;for(const cloud of [cloudA,cloudB]){cloud.activeCount=Math.max(24,Math.floor(cloud.count*factor));cloud.geo.setDrawRange(0,cloud.activeCount);}coreAura.setQuality(qualityTier);} }
    const rawDt=Math.min(clock.getDelta(),0.05),allowCoreMotion=window.LineWorkers?.motionAllowed?.()??!reducedCore,dt=allowCoreMotion?rawDt:0;coreTime+=dt;const t=coreTime;
    updateLineWorkers(rawDt);
    const fx = window.__coreFX || { kind: "", at: 0 };
    const fxAge = (performance.now() - (fx.at || 0)) / 1000;
    const fxBoost = allowCoreMotion&&fx.at&&fxAge<2.5?(1-fxAge/2.5):0;
    // 粒子沿管面流動；環色/流速由市場偏向與訊號活躍度驅動（偏多綠、偏空紅、中性藍紫）
    const bias = window.__marketBias || { score: 0, signals: 0 };
    const biasCol = new THREE.Color(bias.score > 0.06 ? 0x8fffbc : bias.score < -0.06 ? 0xff9db4 : 0xffffff);
    const actBoost = Math.min(2.2, (bias.signals || 0) / 140);
    for (const cloud of [cloudA, cloudB]) {
      const pos = cloud.geo.attributes.position;
      cloud.mat.color.lerp(biasCol, 0.02);
      for (let i = 0; dt>0&&i < cloud.activeCount; i++) {
        const s = cloud.seed[i]; s.v += dt * s.sp * (0.55 + actBoost) * (1 + fxBoost * 2.2);
        const p = torusPos(s.u, s.v, s.rr);
        pos.setXYZ(i, p.x, p.y, p.z);
      }
      pos.needsUpdate = true;
      if (fx.kind === "reject" && fxBoost > 0) cloud.mat.color.set(0xff9db4);
    }
    filaments.forEach((f, i) => { f.ln.rotation.z += dt * 0.05 * f.dir; f.ln.material.opacity = (0.13 + 0.07 * (0.5 + 0.5 * Math.sin(t * 0.9 + i * 2.1)) + fxBoost * 0.25)*graphDecor; });
    centerGlow.material.opacity = 0.045 + fxBoost * 0.08 + 0.015 * Math.sin(t * 1.3);
    // 待機只保留極慢環流；真正的流動由工作事件驅動（見下方脈衝系統）
    const surging = performance.now() < surgeUntil;
    world.rotation.y += dt * (surging ? 0.22 : 0.03) * (1 + fxBoost * 1.5);
    // 工作偵測：行情輪詢/掃描輪次/角色狀態/訂單狀態一變，就沿共作電路送脈衝
    const data = window.LIVE;
    const workerState=window.__lineWorkersState,workers=workerState&&Date.now()<workerState.valid_until?workerState.workers:[];
    const coreState=corePhase(data,[],decisionGraph.mainFresh());
    dock?.setPhase(coreState);coreAura.update(dt,t,coreState,fxBoost);decisionGraph.tick(dt);
    if(_n-lastCoreReadback>200){lastCoreReadback=_n;window.__coreVisualReadback={...coreAura.readback(),coreTime,reducedMotion:reducedCore,motionAllowed:allowCoreMotion,mainParticles:cloudA.activeCount,accentParticles:cloudB.activeCount,totalParticles:cloudA.activeCount+cloudB.activeCount+coreAura.readback().particleDrawCount,particleCap:4096,qualityTier,fps:window.__fps,centerObstruction:false};}
    if (allowCoreMotion&&window.__camMode === "cruise") {
      cruiseT += dt;
      const phase = cruiseCam(dt);
      if (phase !== lastPhase) { lastPhase = phase; if (window.__onCamPhase) window.__onCamPhase(phase); }
    } else if (allowCoreMotion&&window.__camMode === "fixed") {
      parX += (parTX - parX) * 0.04; parY += (parTY - parY) * 0.04;
      camera.position.lerp(new THREE.Vector3(parX * 0.9, 2.6 - parY * 0.6, 7.4), 0.05); camera.lookAt(0, 0, 0);
    }
    composer.render();
    renderLineWorkers();
    // 晶片固定在四角（不再跟隨 3D 投影移動，避免跑版/遮擋）；錨點光點仍在環上流動
  });
  return { renderer };
}

try {
  makePlasma(document.getElementById("plasmaCv"));
  makeRing(document.getElementById("ringCv"));
  window.__threeOK = true;
} catch (e) {
  window.__threeOK = false;
  console.warn("Three.js 視覺載入失敗，改用 2D 備援：", e);
}

