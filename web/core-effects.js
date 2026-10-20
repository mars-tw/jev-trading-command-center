/** @license MIT — cinematic core visual (browser ES module, THREE injected) */
const CY = [0.22, 0.72, 1.0], ROSE = [1.0, 0.28, 0.42], AMB = [1.0, 0.72, 0.22], MINT = [0.28, 0.92, 0.62];
const clamp = (v, a, b) => Math.min(b, Math.max(a, Number.isFinite(v) ? v : a));
const fin = (v, d = 0) => (Number.isFinite(v) ? v : d);

export function coreVisualBudget(width, reduced = false, cpu = 8) {
  const w = fin(width, NaN), c = fin(cpu, 8);
  if (reduced === true) return { tier: 0, mainParticles: 600, accentParticles: 32, motes: 32, pixelRatio: 1 };
  if (!Number.isFinite(w) || w <= 640) return { tier: 0, mainParticles: 1000, accentParticles: 64, motes: 64, pixelRatio: 1 };
  if (w <= 1100 || c <= 4) return { tier: 1, mainParticles: 2000, accentParticles: 96, motes: 96, pixelRatio: 1 };
  return { tier: 2, mainParticles: 3400, accentParticles: 160, motes: 192, pixelRatio: 1.25 };
}

export function corePhase(data, workers, fresh = true) {
  const jc = (data && data.jev_core) || {};
  const act = Array.isArray(workers)?workers.slice(0,12).reduce((n,w)=>n+(w?.working===true?1:0),0):0;
  const pack = (phase, rgb, power) => ({ phase, color: rgb.slice(), power: clamp(power, 0, 1), active: act });
  if (fresh !== true || jc.status === 'NO_JEV_DECISION') return pack('offline', CY, 0.12);
  const busy = jc.busy === true || (Array.isArray(workers) && workers.slice(0, 12).some((w) => w && w.working === true));
  if (busy) return pack('working', CY, 0.68);
  const ld = jc.last_decision || (Array.isArray(data && data.decisions) ? data.decisions[0] : null) || {};
  const t = ld.status || ld.type || ld.decision || '';
  if(t==='NO_JEV_DECISION')return pack('offline',CY,0.12);
  if (t === 'RISK_REJECTED' || t === 'PLAN_INVALIDATED') return pack('reject', ROSE, 0.38);
  if (t === 'JEV_WAIT') return pack('wait', AMB, 0.28);
  if (t === 'JEV_ACTION_SELECTED' && ld.final_outcome === 'ORDER_SUBMITTED') return pack('submitted', MINT, 0.82);
  return pack('idle', CY, 0.22);
}

function seed(i) { let x = (i * 2654435761) >>> 0; x ^= x << 13; x ^= x >>> 17; x ^= x << 5; return (x >>> 0) / 4294967296; }

export function createCinematicCore(THREE, world, options = {}) {
  const group = new THREE.Group();
  world.add(group);
  const geos = [], mats = [];
  const track = (g, m) => { if (g) geos.push(g); if (m) mats.push(m); return m; };
  const addMat = (m) => { m.depthWrite = false; m.blending = THREE.AdditiveBlending; m.transparent = true; return track(null, m); };
  const col = new THREE.Color(0x4aa8ff);
  let decorativeScale=options.graphMode===true?.3:1;
  let phase = 'idle', active = 0, tier = 2, storedTime = 0, lastDt = 0, qLevel = 2, power = 0.22;
  const moteN = Math.floor(clamp(fin(options.motes, 96), 16, 192));

  // 3 thin full LineLoop orbits (XZ, slight inclination)
  const rings = [1.35, 2.35, 3.35].map((r, i) => {
    const n = 96, pos = new Float32Array(n * 3);
    const inc = (i - 1) * 0.08;
    for (let k = 0; k < n; k++) {
      const a = (k / n) * Math.PI * 2;
      pos[k * 3] = Math.cos(a) * r; pos[k * 3 + 1] = Math.sin(a) * r * Math.sin(inc); pos[k * 3 + 2] = Math.sin(a) * r * Math.cos(inc);
    }
    const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.BufferAttribute(pos, 3)); geos.push(g);
    const m = addMat(new THREE.LineBasicMaterial({ color: col, opacity: 0.28 }));
    const line = new THREE.LineLoop(g, m); line.userData.spin = (i%2?-1:1)*(0.12 + i * 0.05); group.add(line); return line;
  });

  // 6 thin TubeGeometry partial arcs
  const tubes = [];
  for (let i = 0; i < 6; i++) {
    const R = 2.1 + (i / 5) * 1.2, start = (i / 6) * Math.PI * 2, sweep = Math.PI * 0.55, inc = ((i % 3) - 1) * 0.12;
    const curve = new THREE.Curve();
    curve.getPoint = (t) => {
      const a = start + t * sweep;
      return new THREE.Vector3(Math.cos(a) * R, Math.sin(a) * R * Math.sin(inc), Math.sin(a) * R * Math.cos(inc));
    };
    const g = new THREE.TubeGeometry(curve, 64, 0.012, 5, false); geos.push(g);
    const m = addMat(new THREE.MeshBasicMaterial({ color: 0x7a6cff, opacity: 0.22 }));
    const mesh = new THREE.Mesh(g, m); mesh.userData.spin = 0.08 + i * 0.03; group.add(mesh); tubes.push(mesh);
  }

  // 6 soft luminous ribbon strips (torus R=2.35, tube=0.78, outside aperture 1.2)
  const ribbonVS = `varying vec2 vUv; uniform float uTime; void main(){ vUv=uv; vec3 p=position; p += normal*0.002*sin(uTime*1.7+uv.x*6.28); gl_Position=projectionMatrix*modelViewMatrix*vec4(p,1.0); }`;
  const ribbonFS = `varying vec2 vUv; uniform float uTime; uniform float uPower; uniform float uDecor; uniform vec3 uColor;
    void main(){ float edge=sin(3.14159265*vUv.y); float beam=smoothstep(0.0,0.15,abs(fract(vUv.x*2.0-uTime*0.15)-0.5));
      float a=edge*edge*(0.18+0.55*uPower)*(0.55+0.45*(1.0-beam)); gl_FragColor=vec4(uColor,a*uDecor); }`;
  const ribbons = [];
  for (let i = 0; i < 6; i++) {
    const steps = 96, verts = steps * 2, pos = new Float32Array(verts * 3), nrm = new Float32Array(verts * 3), uvs = new Float32Array(verts * 2);
    const idx = new Uint16Array((steps-1) * 6), R = 2.35, rt = 0.78, yaw = (i / 6) * Math.PI * 2, pitch = ((i % 3) - 1) * 0.18;
    for (let s = 0; s < steps; s++) {
      const t = s / (steps - 1), a = t * Math.PI * 2;
      const v=a*(2+i%3)+yaw;
      for(let e=0;e<2;e++){
        const vi=(s*2+e)*3,ui=(s*2+e)*2,edge=v+(e?1:-1)*0.027,cv=Math.cos(edge),sv=Math.sin(edge),ca=Math.cos(a),sa=Math.sin(a),rr=R+rt*cv;
        pos[vi]=rr*ca;pos[vi+1]=rt*sv;pos[vi+2]=rr*sa;
        nrm[vi]=cv*ca;nrm[vi+1]=sv;nrm[vi+2]=cv*sa;uvs[ui]=t;uvs[ui+1]=e;
      }
      if (s < steps - 1) {
        const b = s * 6, v = s * 2; idx[b] = v; idx[b + 1] = v + 1; idx[b + 2] = v + 2; idx[b + 3] = v + 1; idx[b + 4] = v + 3; idx[b + 5] = v + 2;
      }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('normal', new THREE.BufferAttribute(nrm, 3));
    g.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
    g.setIndex(new THREE.BufferAttribute(idx, 1)); geos.push(g);
    const m = addMat(new THREE.ShaderMaterial({
      side:THREE.DoubleSide, vertexShader: ribbonVS, fragmentShader: ribbonFS, uniforms: {
        uTime: { value: 0 }, uPower: { value: 0.22 }, uDecor: { value: decorativeScale }, uColor: { value: new THREE.Color(0x88aaff) }
      }
    }));
    const mesh = new THREE.Mesh(g, m); group.add(mesh); ribbons.push(mesh);
  }

  // seeded motes on orbital ellipses
  const mPos = new Float32Array(moteN * 3), mAttr = new Float32Array(moteN * 4);
  for (let i = 0; i < moteN; i++) {
    mAttr[i * 4] = seed(i); mAttr[i * 4 + 1] = 1.4 + seed(i + 7) * 2.2;
    mAttr[i * 4 + 2] = seed(i + 13) * Math.PI * 2; mAttr[i * 4 + 3] = 0.4 + seed(i + 19) * 0.6;
  }
  for(let i=0;i<moteN;i++){const a=mAttr[i*4+2],rr=mAttr[i*4+1],e=mAttr[i*4+3];let x=Math.cos(a)*rr,z=Math.sin(a)*rr*e;const scale=Math.max(1,1.3/Math.max(.001,Math.hypot(x,z)));mPos[i*3]=x*scale;mPos[i*3+1]=Math.sin(a*e)*.35;mPos[i*3+2]=z*scale;}
  const mGeo = new THREE.BufferGeometry();
  mGeo.setAttribute('position', new THREE.BufferAttribute(mPos, 3)); geos.push(mGeo);
  const mMat = addMat(new THREE.PointsMaterial({ map:options.spriteTex||null,color: 0xcfe8ff, size: 0.035, opacity: 0.55, sizeAttenuation: true }));
  const points = new THREE.Points(mGeo, mMat); group.add(points);
  let particleDraw = moteN; mGeo.setDrawRange(0, moteN);

  const tmpC = col.clone();
  // A small physical reactor seed; open wire lattice and light only, never a HUD plane.
  const seedRoot=new THREE.Group();seedRoot.userData.coreSeed=true;group.add(seedRoot);
  const seedGeo=new THREE.IcosahedronGeometry(.37,1);geos.push(seedGeo);
  const seedWhite=new THREE.Color(0xd5f6ff),seedWire=new THREE.Mesh(seedGeo,addMat(new THREE.MeshBasicMaterial({color:seedWhite,wireframe:true,opacity:.46})));seedWire.userData.coreSeed=true;seedRoot.add(seedWire);
  const seedCorona=new THREE.Sprite(addMat(new THREE.SpriteMaterial({map:options.spriteTex||null,color:0x65cfff,opacity:.28,toneMapped:false})));seedCorona.userData.coreSeed=true;seedCorona.visible=!!options.spriteTex;seedCorona.scale.set(1.05,1.05,1);seedRoot.add(seedCorona);
  for(let k=0;k<3;k++){const points=[];for(let j=0;j<96;j++){const a=j/96*Math.PI*2,r=.43+.035*Math.cos(a*3+k);points.push(new THREE.Vector3(r*Math.cos(a),.24*Math.sin(a*2+k),r*Math.sin(a)));}const g=new THREE.BufferGeometry().setFromPoints(points);geos.push(g);const coil=new THREE.LineLoop(g,addMat(new THREE.LineBasicMaterial({color:k===1?0xc8eeff:0x65cfff,opacity:.25})));coil.userData.coreSeed=true;coil.rotation.z=(k-1)*Math.PI/3;seedRoot.add(coil);}

  function applyState(state) {
    const st = state || {};
    phase = st.phase || phase; active = clamp(fin(st.active, active), 0, 12);
    power = clamp(fin(st.power, power), 0, 1);
    const rgb = st.color; if (Array.isArray(rgb) && rgb.length >= 3) tmpC.setRGB(fin(rgb[0]), fin(rgb[1]), fin(rgb[2]));
    rings.forEach((r) => { r.material.color.copy(tmpC); r.material.opacity = (0.18 + power * 0.35)*decorativeScale; });
    tubes.forEach((t) => { t.material.color.copy(tmpC); t.material.opacity = (0.12 + power * 0.28)*decorativeScale; });
    ribbons.forEach((r) => { const u = r.material.uniforms; u.uPower.value = power; u.uDecor.value=decorativeScale; u.uColor.value.copy(tmpC); });
    mMat.color.copy(tmpC); mMat.opacity = (0.3 + power * 0.45)*decorativeScale;
    seedWire.material.color.copy(tmpC).lerp(seedWhite,.68);seedWire.material.opacity=.38+power*.28;seedCorona.material.color.copy(tmpC);seedCorona.material.opacity=.22+power*.16;
  }

  function update(dt, time, state, impulse = 0) {
    lastDt = clamp(fin(dt, 0), 0, 0.05);
    if (lastDt > 0) storedTime = fin(time, storedTime + lastDt); else storedTime = fin(storedTime, 0);
    applyState(state);
    const boost = clamp(fin(impulse, 0), 0, 1);
    if (lastDt > 0) {
      const t = storedTime;
      seedRoot.rotation.y+=lastDt*(.12+power*.1);seedRoot.rotation.x=.3+Math.sin(t*.18)*.14;const breath=1.05*(.98+.025*Math.sin(t*.7));seedCorona.scale.set(breath,breath,1);
      rings.forEach((r) => { r.rotation.y += lastDt*r.userData.spin*(.35+power); r.rotation.z = Math.sin(t * 0.2) * 0.04; });
      tubes.forEach((u, i) => { u.rotation.y += lastDt*u.userData.spin*(.35+power); u.rotation.x = Math.sin(t * 0.15 + i) * 0.05; });
      for (let i = 0; i < moteN; i++) {
        const a = mAttr[i * 4 + 2] + t * (0.15 + mAttr[i * 4] * 0.35), rr = mAttr[i * 4 + 1], e = mAttr[i * 4 + 3];
        const x=Math.cos(a)*rr,z=Math.sin(a)*rr*e,scale=Math.max(1,1.3/Math.max(.001,Math.hypot(x,z)));mPos[i*3]=x*scale;mPos[i*3+1]=Math.sin(a*e)*.35;mPos[i*3+2]=z*scale;
      }
      mGeo.attributes.position.needsUpdate = true;
    }
    ribbons.forEach((r) => { r.material.uniforms.uTime.value = storedTime; });
  }

  function setQuality(level) {
    qLevel = clamp(Math.round(fin(level, 2)), 0, 2);
    tier = qLevel;
    const frac = qLevel === 0 ? 0.4 : qLevel === 1 ? 0.7 : 1;
    particleDraw = Math.max(1, Math.floor(moteN * frac));
    mGeo.setDrawRange(0, particleDraw);
    const showT = qLevel === 0 ? 2 : qLevel === 1 ? 4 : 6;
    tubes.forEach((t, i) => { t.visible = i < showT; });
    ribbons.forEach((r, i) => { r.visible = i < showT; });
  }

  function readback() {
    return {
      phase, active, tier, decorativeScale, ringCount: rings.length, ribbonCount: ribbons.length,
      particleLimit: moteN, particleDrawCount: particleDraw, time: storedTime, lastDt,
      allocatedGeometryCount: geos.length,coreSeedRadius:.37,coreSeedFilaments:3,coreSeedCoronaWidth:1.05
    };
  }

  function dispose() {
    group.removeFromParent();
    geos.forEach((g) => g.dispose());
    mats.forEach((m) => m.dispose());
  }

  setQuality(2);
  return { setGraphMode(value){decorativeScale=value===true?.3:1;applyState({});}, update, setQuality, readback, dispose };
}

export function initCoreDock(doc, hostWindow) {
  const dock=doc.getElementById('jevCore'),button=doc.getElementById('coreDetailsToggle'),meta=doc.getElementById('jcMeta'),status=doc.getElementById('jcStatus');
  if(!dock||!button||!meta)return null;
  const surface=dock.closest('.torusWrap');
  function expand(value){dock.dataset.expanded=String(value);button.setAttribute('aria-expanded',String(value));meta.setAttribute('aria-hidden',String(!value));button.textContent=value?'收合詳情':'決策詳情';}
  expand(false);
  button.addEventListener('click',()=>expand(button.getAttribute('aria-expanded')!=='true'));
  dock.addEventListener('keydown',event=>{if(event.key==='Escape'&&button.getAttribute('aria-expanded')==='true'){expand(false);button.focus();event.preventDefault();}});
  let feedAt=hostWindow.LIVE?Date.now():0;
  if(typeof hostWindow.MutationObserver==='function'){
    const observer=new hostWindow.MutationObserver(()=>{feedAt=Date.now();if(status)status.title=status.textContent;});
    if(status)observer.observe(status,{childList:true,characterData:true,subtree:true});observer.observe(meta,{childList:true,characterData:true,subtree:true});
  }
  const researchLabels={idle:'研究待命',scanning:'樣本掃描',backtesting:'紙上回測',evaluating:'比較評估',archiving:'研究歸檔',cooldown:'研究冷卻',unavailable:'未連線'};
  const lab=doc.getElementById('coreLabState');
  let lastPhase='';
  const api={
    feedFresh:()=>feedAt>0&&Date.now()-feedAt<5500,
    setPhase(state){if(lastPhase===state.phase)return;lastPhase=state.phase;dock.dataset.phase=state.phase;const color=Array.isArray(state.color)?'#'+state.color.slice(0,3).map(c=>Math.round(Math.max(0,Math.min(1,Number(c)||0))*255).toString(16).padStart(2,'0')).join(''):'#75dfff';if(surface)surface.style.setProperty('--core-accent',color);},
    updateResearch(value){const phase=typeof value?.phase==='string'&&Object.hasOwn(researchLabels,value.phase)?value.phase:'unavailable';hostWindow.__strategyLabVisualPhase=phase;if(lab)lab.textContent=researchLabels[phase];return phase;},
    readback(){return{expanded:button.getAttribute('aria-expanded')==='true',phase:lastPhase,feedFresh:api.feedFresh(),researchPhase:hostWindow.__strategyLabState?.phase||'unavailable'};}
  };
  hostWindow.JevCoreEffects={...(hostWindow.JevCoreEffects||{}),updateResearch:api.updateResearch,dock:api.readback};
  return api;
}
