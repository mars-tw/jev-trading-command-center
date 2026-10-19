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
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 1.25));
  const scene = new THREE.Scene();
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
    const cBlue = new THREE.Color(0x2456ff), cPurp = new THREE.Color(0x8b5cff),
      cCyan = new THREE.Color(0x37e0ff), cWhite = new THREE.Color(0xd8fbff), tmp = new THREE.Color();
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
    const mat = new THREE.PointsMaterial({ size, map: spriteTex, vertexColors: true, transparent: true, opacity: 0.95,
      blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true });
    const pts = new THREE.Points(geo, mat); world.add(pts);
    return { geo, seed, count, mat };
  }
  // 柔光粒子貼圖（徑向漸層），避免方形點
  const spriteTex = (() => { const c = document.createElement("canvas"); c.width = c.height = 64; const x = c.getContext("2d");
    const g = x.createRadialGradient(32, 32, 0, 32, 32, 32);
    g.addColorStop(0, "rgba(255,255,255,1)"); g.addColorStop(0.35, "rgba(255,255,255,.6)"); g.addColorStop(1, "rgba(255,255,255,0)");
    x.fillStyle = g; x.fillRect(0, 0, 64, 64); return new THREE.CanvasTexture(c); })();
  const cloudA = makeCloud(5200, 0.05), cloudB = makeCloud(300, 0.09);
  // 交錯絲流軌跡（繞環螺旋線）
  const flowLines = [];
  for (let k = 0; k < 20; k++) {
    const turns = 3 + (k % 5), phase = (k / 20) * Math.PI * 2, pts = [];
    for (let s = 0; s <= 200; s++) {
      const u = (s / 200) * Math.PI * 2;
      pts.push(torusPos(u, u * turns + phase, TUBE * 1.03));
    }
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: k % 3 ? 0x37e0ff : 0x8b5cff, transparent: true, opacity: 0.30, blending: THREE.AdditiveBlending, depthWrite: false }));
    world.add(line); flowLines.push({ line, phase });
  }
  // Additive worker pets travel on the EXACT existing helical flow-line curves.
  const workerSprites = new Map(), workerTextures = new Map();
  const workerScene = new THREE.Scene(), workerRoot = new THREE.Group();
  workerRoot.matrixAutoUpdate = false; workerScene.add(workerRoot);
  function petTexture(alias,view,frame,working) {
    const key = [alias,view,frame,working].join('|'); let texture = workerTextures.get(key);
    if (!texture) { texture = new THREE.CanvasTexture(window.LineWorkers.makeTextureCanvas(alias,view,frame,working)); texture.colorSpace = THREE.SRGBColorSpace; workerTextures.set(key,texture); }
    return texture;
  }
  function updateLineWorkers(dt) {
    const api = window.LineWorkers, state = window.__lineWorkersState;
    if (!api) return;
    const items = state && Date.now() < state.valid_until ? state.workers : [], keep = new Set();
    items.slice(0, 12).forEach((w, i) => {
      keep.add(w.id); let sp = workerSprites.get(w.id);
      if (!sp) {
        sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: petTexture(w.alias,'front',0,false), transparent: true, depthWrite: false, depthTest: false, sizeAttenuation: false, toneMapped: false }));
        sp.scale.set(0.085, 0.085, 1); sp.renderOrder = 100; workerRoot.add(sp); workerSprites.set(w.id, sp);
      }
      const flowIndex = Number.isInteger(sp.userData.flowIndex) ? sp.userData.flowIndex : i % flowLines.length;
      const f = flowLines[flowIndex], turns = 3 + flowIndex % 5;
      const u = api.advancePhase(sp.userData.u ?? i * 0.57, dt, w.working, Math.PI * 2);
      sp.position.copy(torusPos(u, u * turns + f.phase, TUBE * 1.03));
      sp.material.opacity = w.working ? 1 : 0.68;
      sp.userData = { ...sp.userData, workerId: w.id, source: w.source, alias: w.alias, name:w.name, action:w.action, label: w.label, working: w.working, flowIndex, u, v: u * turns + f.phase };
    });
    for (const [id, sp] of workerSprites) if (!keep.has(id)) { workerRoot.remove(sp); sp.material.dispose(); workerSprites.delete(id); }
  }
  function renderLineWorkers() {
    const api = window.LineWorkers; if (!api) return;
    workerRoot.matrix.copy(world.matrixWorld); workerRoot.updateMatrixWorld(true);
    const points = [], now = performance.now(), bucket = Math.floor(now/140);
    for (const sp of workerSprites.values()) {
      const d=sp.userData,point=sp.getWorldPosition(new THREE.Vector3()).project(camera),f=flowLines[d.flowIndex],u=d.u+0.025;
      const ahead=torusPos(u,u*(3+d.flowIndex%5)+f.phase,TUBE*1.03).applyMatrix4(world.matrixWorld).project(camera);
      const view=api.viewFor((ahead.x-point.x)*canvas.clientWidth,-(ahead.y-point.y)*canvas.clientHeight),busy=d.source==='CLI'&&d.working;
      const moving=api.motionAllowed();
      if (!d.appearanceKey || (moving && d.artBucket!==bucket) || d.artWorking!==busy || d.artAlias!==d.alias) {
        d.view=moving||!d.appearanceKey?view:d.view;d.frame=moving||!d.appearanceKey?api.frameFor(now):d.frame;d.artBucket=bucket;d.artWorking=busy;d.artAlias=d.alias;d.appearanceKey=[d.alias,d.view,d.frame,busy].join('|');sp.material.map=petTexture(d.alias,d.view,d.frame,busy);
      }
      d.screenX=(point.x+1)*canvas.clientWidth/2;d.screenY=(1-point.y)*canvas.clientHeight/2;
      points.push({id:d.workerId,x:d.screenX,y:d.screenY});
    }
    const autoClear = renderer.autoClear;
    renderer.autoClear = false; renderer.clearDepth();
    renderer.render(workerScene, camera); renderer.autoClear = autoClear;
    api.placeLabels(points);
    window.__lineWorkerReadback=[...workerSprites.values()].map(sp=>({...sp.userData,position:sp.position.toArray()}));
  }

  // 絲線環（沿管面緯線）+ 中心輝光，強化中空立體感
  const filaments = [];
  for (let k = 0; k < 3; k++) {
    const pts = [], vb = 0.7 + k * 1.85;
    for (let i = 0; i <= 240; i++) { const u = (i / 240) * Math.PI * 2; pts.push(torusPos(u, vb + Math.sin(u * 3 + k * 2.1) * 0.16, TUBE * 1.02)); }
    const ln = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: k === 1 ? 0x8b5cff : 0x37e0ff, transparent: true, opacity: 0.17, blending: THREE.AdditiveBlending, depthWrite: false }));
    world.add(ln); filaments.push({ ln, dir: k % 2 ? -1 : 1 });
  }
  const centerGlow = new THREE.Sprite(new THREE.SpriteMaterial({ map: spriteTex, color: 0x6a5cff, transparent: true, opacity: 0.30, blending: THREE.AdditiveBlending, depthWrite: false }));
  centerGlow.scale.set(4.6, 4.6, 1); world.add(centerGlow);
  // 訊號錨點（策略/風控/執行等狀態標籤的 3D 附著點）
  const anchors = [];
  const anchorDefs = ["chipChoice", "chipGate", "chipRisk"];
  for (let i = 0; i < 6; i++) {
    const m = new THREE.Mesh(new THREE.SphereGeometry(0.058, 14, 14),
      new THREE.MeshBasicMaterial({ color: i % 2 ? 0xd8fbff : 0x37e0ff }));
    m.userData = { u: (i / 6) * Math.PI * 2 + 0.5, v: 0.9 + (i % 3) * 0.7, chip: anchorDefs[i % 3], idx: i };
    world.add(m); anchors.push(m);
    const lg = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]);
    const ln = new THREE.Line(lg, new THREE.LineBasicMaterial({ color: 0x37e0ff, transparent: true, opacity: 0.55 }));
    world.add(ln); m.userData.line = ln;
  }
  // 角色節點（內圈標籤附著於環面內緣，供點選）
  const dyn = new THREE.Group(); world.add(dyn);
  const pulseGroup = new THREE.Group(); world.add(pulseGroup);
  const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
  let roleMeshes = [];
  canvas.addEventListener("pointerdown", e => {
    const r = canvas.getBoundingClientRect();
    ptr.x = ((e.clientX - r.left) / r.width) * 2 - 1;
    ptr.y = -((e.clientY - r.top) / r.height) * 2 + 1;
    ray.setFromCamera(ptr, camera);
    const hit = ray.intersectObjects(roleMeshes, false)[0];
    if (hit && window.__selectRole) window.__selectRole(hit.object.userData.roleId);
  });
  let drag = null;
  canvas.addEventListener("pointerdown", e => { drag = { x: e.clientX, y: e.clientY }; });
  let parX = 0, parY = 0, parTX = 0, parTY = 0;
  canvas.addEventListener("pointermove", e => { const r = canvas.getBoundingClientRect(); parTX = ((e.clientX - r.left) / r.width - 0.5); parTY = ((e.clientY - r.top) / r.height - 0.5); });
  window.addEventListener("pointermove", e => { if (drag && window.__camMode === "fixed") { world.rotation.y += (e.clientX - drag.x) * 0.005; world.rotation.x += (e.clientY - drag.y) * 0.003; drag = { x: e.clientX, y: e.clientY }; } });
  window.addEventListener("pointerup", () => drag = null);
  function resize() {
    const w = canvas.clientWidth || 900, h = canvas.clientHeight || 430;
    renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
  }
  resize(); window.addEventListener("resize", resize);
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  composer.addPass(new UnrealBloomPass(new THREE.Vector2(900, 430), 0.65, 0.5, 0.22));
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
  // 共作電路：角色不是裝飾，依工作流程順序連成電路，脈衝只在真實事件發生時傳遞
  const WORKFLOW = ["feed", "strategy", "sizer", "risk", "exec", "position", "record", "chief"];
  const rolePos = {}, arcCurves = {}, pulses = [], flashUntil = {};
  let prevRoleStatus = {}, prevPoll = -1, prevScan = -1, prevOrderSig = "", surgeUntil = 0, prevReviewSig = "", prevDecisionSig = "";
  function spawnPulse(fromId, color) {
    const curve = arcCurves[fromId];
    if (!curve) return;
    const mesh = new THREE.Sprite(new THREE.SpriteMaterial({ map: spriteTex, color: color || 0xd8fbff, transparent: true, opacity: 0.95, blending: THREE.AdditiveBlending, depthWrite: false }));
    mesh.scale.set(0.42, 0.42, 1);
    pulseGroup.add(mesh);
    pulses.push({ mesh, curve, t: 0 });
    const now = performance.now();
    flashUntil[fromId] = now + 900;
    const idx = WORKFLOW.indexOf(fromId);
    if (idx >= 0) flashUntil[WORKFLOW[(idx + 1) % WORKFLOW.length]] = now + 1300;
  }
  function rebuild(data) {
    while (dyn.children.length) dyn.remove(dyn.children[0]);
    roleMeshes = [];
    (data.roles || []).forEach((r, i) => {
      const a = (i / 8) * Math.PI * 2;
      const pos = new THREE.Vector3(Math.cos(a) * 1.15, -0.9, Math.sin(a) * 1.15);
      rolePos[r.id] = pos;
      const m = new THREE.Mesh(new THREE.SphereGeometry(0.17, 16, 16),
        new THREE.MeshBasicMaterial({ color: new THREE.Color(r.color || "#37e0ff") }));
      m.position.copy(pos); m.userData.roleId = r.id; dyn.add(m); roleMeshes.push(m);
      // 中心不再放小字貼圖（放大也讀不到）；「誰決策/誰在跑」由中央 HTML 大字與組織圖承擔
    });
    // 依工作流程把角色連成共作電路（弧線往中心收，像電路走線）
    for (let i = 0; i < WORKFLOW.length; i++) {
      const a = rolePos[WORKFLOW[i]], b = rolePos[WORKFLOW[(i + 1) % WORKFLOW.length]];
      if (!a || !b) continue;
      const mid = a.clone().add(b).multiplyScalar(0.32).setY(-0.55);
      const curve = new THREE.QuadraticBezierCurve3(a, mid, b);
      arcCurves[WORKFLOW[i]] = curve;
      // 用細管代替 1px 線（WebGL 線寬恆為 1），通電線路才看得清楚
      dyn.add(new THREE.Mesh(new THREE.TubeGeometry(curve, 24, 0.016, 6, false),
        new THREE.MeshBasicMaterial({ color: 0x37e0ff, transparent: true, opacity: 0.55, blending: THREE.AdditiveBlending, depthWrite: false })));
    }
  }
  const chipEls = {};
  let _fpsFrames = 0, _fpsT0 = performance.now();
  window.__fps = 0;
  renderer.setAnimationLoop(() => {
    if (document.body.classList.contains("noanim")) return;
    _fpsFrames++;
    const _n = performance.now();
    if (_n - _fpsT0 >= 1000) { window.__fps = Math.round(_fpsFrames * 1000 / (_n - _fpsT0)); _fpsFrames = 0; _fpsT0 = _n; }
    const dt = Math.min(clock.getDelta(), 0.05), t = clock.elapsedTime;
    updateLineWorkers(dt);
    const fx = window.__coreFX || { kind: "", at: 0 };
    const fxAge = (performance.now() - (fx.at || 0)) / 1000;
    const fxBoost = fx.at && fxAge < 2.5 ? (1 - fxAge / 2.5) : 0;
    // 粒子沿管面流動；環色/流速由市場偏向與訊號活躍度驅動（偏多綠、偏空紅、中性藍紫）
    const bias = window.__marketBias || { score: 0, signals: 0 };
    const biasCol = new THREE.Color(bias.score > 0.06 ? 0x8fffbc : bias.score < -0.06 ? 0xff9db4 : 0xffffff);
    const actBoost = Math.min(2.2, (bias.signals || 0) / 140);
    for (const cloud of [cloudA, cloudB]) {
      const pos = cloud.geo.attributes.position;
      cloud.mat.color.lerp(biasCol, 0.02);
      for (let i = 0; i < cloud.count; i++) {
        const s = cloud.seed[i]; s.v += dt * s.sp * (0.55 + actBoost) * (1 + fxBoost * 2.2);
        const p = torusPos(s.u, s.v, s.rr);
        pos.setXYZ(i, p.x, p.y, p.z);
      }
      pos.needsUpdate = true;
      if (fx.kind === "reject" && fxBoost > 0) cloud.mat.color.set(0xff9db4);
    }
    flowLines.forEach((f, i) => { f.line.material.opacity = 0.20 + 0.16 * (0.5 + 0.5 * Math.sin(t * 1.4 + f.phase * 3)) + fxBoost * 0.3; });
    filaments.forEach((f, i) => { f.ln.rotation.z += dt * 0.05 * f.dir; f.ln.material.opacity = 0.13 + 0.07 * (0.5 + 0.5 * Math.sin(t * 0.9 + i * 2.1)) + fxBoost * 0.25; });
    centerGlow.material.opacity = 0.26 + fxBoost * 0.35 + 0.04 * Math.sin(t * 1.3);
    // 待機只保留極慢環流；真正的流動由工作事件驅動（見下方脈衝系統）
    const surging = performance.now() < surgeUntil;
    world.rotation.y += dt * (surging ? 0.22 : 0.03) * (1 + fxBoost * 1.5);
    // 工作偵測：行情輪詢/掃描輪次/角色狀態/訂單狀態一變，就沿共作電路送脈衝
    const data = window.LIVE;
    if (data) {
      if (prevPoll >= 0 && data.feed && data.feed.poll_count !== prevPoll) spawnPulse("feed", 0x37e0ff);
      if (data.feed) prevPoll = data.feed.poll_count;
      if (prevScan >= 0 && data.scan_count !== prevScan) { surgeUntil = performance.now() + 1800; spawnPulse("strategy", 0xffb020); }
      if (data.scan_count != null) prevScan = data.scan_count;
      for (const r of (data.roles || [])) {
        if (prevRoleStatus[r.id] && prevRoleStatus[r.id] !== "working" && r.status === "working") spawnPulse(r.id, new THREE.Color(r.color || "#37e0ff").getHex());
        prevRoleStatus[r.id] = r.status;
      }
      const o0 = (data.orders || [])[0];
      if (o0) {
        const osig = o0.id + o0.status;
        if (prevOrderSig && prevOrderSig !== osig) {
          if (o0.status === "WAITING_FILL" || o0.status === "PARTIALLY_FILLED") spawnPulse("exec", 0xdbe4f5);
          else if (o0.status === "FILLED") spawnPulse("position", 0x2dd4bf);
          else if (o0.status === "CLOSED") spawnPulse("record", 0x9fb2d8);
          else if (o0.status === "RISK_REJECTED") spawnPulse("risk", 0xff4d6d);
        }
        prevOrderSig = osig;
      }
      // 複審事件：新複審紀錄出現 -> 紫色脈衝沿策略/風控電路傳遞 + 短暫加速環流
      const rv = (data.reviews || [])[0];
      if (rv) {
        const rsig = rv.symbol + rv.at;
        if (prevReviewSig && prevReviewSig !== rsig) { spawnPulse("strategy", 0x8b5cff); spawnPulse("risk", 0x8b5cff); surgeUntil = performance.now() + 1500; }
        prevReviewSig = rsig;
      }
      // JEV 決策事件：新決策回執出現 -> 策略->風控->執行脈衝 + 環流加速（狀態由中央大字呈現）
      const dc = (data.decisions || [])[0];
      if (dc) {
        const dsig = (dc.decision_id || "") + dc.status;
        if (prevDecisionSig && prevDecisionSig !== dsig) {
          spawnPulse("strategy", 0xff2ea6); spawnPulse("risk", dc.status === "RISK_REJECTED" ? 0xff4d6d : 0x2dff8f);
          if (dc.final_outcome === "ORDER_SUBMITTED") spawnPulse("exec", 0xdbe4f5);
          surgeUntil = performance.now() + 1800;
        }
        prevDecisionSig = dsig;
      }
      // 脈衝沿電路前進，走完移除；角色被點亮時放大
      for (let i = pulses.length - 1; i >= 0; i--) {
        const p = pulses[i]; p.t += dt / 1.15;
        if (p.t >= 1) { pulseGroup.remove(p.mesh); pulses.splice(i, 1); continue; }
        p.mesh.position.copy(p.curve.getPoint(p.t));
        p.mesh.material.opacity = 0.95 * (1 - p.t * 0.4);
      }
      const nowMs = performance.now();
      for (const m of roleMeshes) {
        const hot = (flashUntil[m.userData.roleId] || 0) > nowMs;
        m.scale.setScalar(hot ? 1.55 : 1.0);
      }
    }
    anchors.forEach((m, i) => {
      const p = torusPos(m.userData.u + t * 0.05, m.userData.v, TUBE * 1.06);
      m.position.copy(p);
      const s = 1 + fxBoost * 1.6 * (i % 3 === 0 ? 1 : 0.3) + 0.18 * Math.sin(t * 2 + i);
      m.scale.setScalar(Math.max(0.4, s));
      const lp = m.userData.line.geometry.attributes.position;
      lp.setXYZ(0, p.x, p.y, p.z); lp.setXYZ(1, p.x * 0.4, p.y + 0.7, p.z * 0.4); lp.needsUpdate = true;
    });
    if (data) {
      const s2 = JSON.stringify((data.roles || []).map(r => r.id + r.status));
      if (s2 !== sig) { sig = s2; rebuild({ roles: data.roles || [] }); }
    }
    if (window.__camMode === "cruise") {
      cruiseT += dt;
      const phase = cruiseCam(dt);
      if (phase !== lastPhase) { lastPhase = phase; if (window.__onCamPhase) window.__onCamPhase(phase); }
    } else if (window.__camMode === "fixed") {
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

