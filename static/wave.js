/* 行情波浪：每个柱体对应一只自选股。
 * 颜色与高度反映当日涨跌；鼠标划过产生水波，悬停查看行情，点击打开个股详情。
 * 仅使用本地 three.min.js，不发起任何外部请求。
 */
(function () {
  "use strict";

  const wrap = document.getElementById("waveGrid");
  const canvas = document.getElementById("waveCanvas");
  const tip = document.getElementById("waveTip");
  if (!wrap || !canvas || !window.THREE) return;

  let gl = null;
  try {
    gl = canvas.getContext("webgl2")
      || canvas.getContext("webgl")
      || canvas.getContext("experimental-webgl");
  } catch (e) { gl = null; }
  if (!gl) { wrap.style.display = "none"; return; }

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas: canvas, context: gl, antialias: true, alpha: true });
  } catch (e) { wrap.style.display = "none"; return; }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 300);

  const MAX_ROWS = 4, MAX_COLS = 12;
  const CAPACITY = MAX_ROWS * MAX_COLS;
  const SPACING = 2.0;
  const CUBE_W = 1.5, CUBE_H = 3.0;
  const MAX_TRAIL = 128;

  const FILLER = new THREE.Color(0xDED8CB);
  const FLAT = new THREE.Color(0x8A8578);
  const UP = new THREE.Color(0xB91C1C);
  const DOWN = new THREE.Color(0x15803D);

  let quotes = [];
  let layout = { rows: 2, cols: 8 };

  const geometry = new THREE.BoxGeometry(CUBE_W, CUBE_H, CUBE_W);
  const offsetAttr = new THREE.InstancedBufferAttribute(new Float32Array(CAPACITY * 2), 2);
  geometry.setAttribute("aOffset", offsetAttr);

  const material = new THREE.MeshPhongMaterial({ color: 0xffffff });
  const mesh = new THREE.InstancedMesh(geometry, material, CAPACITY);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  scene.add(mesh);

  const ambient = new THREE.AmbientLight(0xffffff, 0.95);
  scene.add(ambient);
  const key = new THREE.DirectionalLight(0xffffff, 2.1);
  key.position.set(-10, 14, 8);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.radius = 6;
  key.shadow.camera.near = 0.5;
  key.shadow.camera.far = 80;
  key.shadow.camera.left = -28;
  key.shadow.camera.right = 28;
  key.shadow.camera.top = 28;
  key.shadow.camera.bottom = -28;
  key.shadow.bias = 0.0001;
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.9);
  fill.position.set(9, 5, -7);
  scene.add(fill);

  const trailData = new Float32Array(MAX_TRAIL * 4);
  const trailTex = new THREE.DataTexture(
    trailData, MAX_TRAIL, 1, THREE.RGBAFormat, THREE.FloatType
  );
  trailTex.needsUpdate = true;

  const uniforms = {
    uTrailTexture: { value: trailTex },
    uTrailCount: { value: 0 },
    uWaveSpeed: { value: 5.8 },
    uWaveFreq: { value: 1.0 },
    uWaveWidth: { value: 2.8 },
    uFadeTime: { value: 2.2 },
    uAmplitude: { value: 0.36 },
    uJitter: { value: 0.18 },
    uMaxHeight: { value: 0.45 },
    uColorHigh: { value: new THREE.Color(0xB45309) },
  };

  const SHADER_COMMON = `
    varying float vHeight;
    attribute vec2 aOffset;
    uniform sampler2D uTrailTexture;
    uniform float uWaveSpeed;
    uniform float uWaveFreq;
    uniform float uWaveWidth;
    uniform float uFadeTime;
    uniform float uAmplitude;
    uniform float uJitter;
    uniform float uMaxHeight;
    const int TRAIL_COUNT = 128;
    vec2 hash2(vec2 p){
      p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)));
      return fract(sin(p) * 43758.5453123) - 0.5;
    }`;
  const SHADER_BEGIN = `
    #include <begin_vertex>
    vHeight = 0.0;
    if (position.y > 0.0) {
      vec2 jitter  = hash2(aOffset) * uJitter;
      vec2 worldXZ = aOffset + jitter;
      float waveHeight  = 0.0;
      float totalWeight = 0.0;
      for (int i = 0; i < TRAIL_COUNT; i++) {
        vec4 td = texture2D(uTrailTexture, vec2((float(i) + 0.5) / 128.0, 0.5));
        float dist      = length(worldXZ - td.rg);
        float wavefront = uWaveSpeed * td.b;
        float relDist   = dist - wavefront;
        float window = exp(-(relDist * relDist) / (uWaveWidth * uWaveWidth));
        float fade   = exp(-td.b / uFadeTime);
        float atten  = 1.0 / (1.0 + dist * 0.1);
        float weight = fade * window * atten * td.a;
        waveHeight  += weight * cos(uWaveFreq * relDist);
        totalWeight += weight;
      }
      waveHeight /= max(totalWeight, 1.0);
      float displacement = clamp(waveHeight * uAmplitude, -uMaxHeight, uMaxHeight);
      transformed.y += displacement;
      vHeight = displacement;
    }`;

  function patchShader(shader, withColor){
    Object.assign(shader.uniforms, uniforms);
    shader.vertexShader = shader.vertexShader
      .replace("#include <common>", `#include <common>
${SHADER_COMMON}`)
      .replace("#include <begin_vertex>", SHADER_BEGIN);
    if (withColor){
      shader.fragmentShader = shader.fragmentShader
        .replace(
          "#include <common>",
          `#include <common>
 varying float vHeight;
 uniform vec3 uColorHigh;
 uniform float uMaxHeight;`
        )
        .replace(
          "#include <color_fragment>",
          `#include <color_fragment>
#ifdef USE_INSTANCING_COLOR
 diffuseColor.rgb *= vColor;
#endif
 float t = clamp(vHeight / uMaxHeight, 0.0, 1.0);
 diffuseColor.rgb = mix(diffuseColor.rgb, uColorHigh, t);`
        );
    }
  }

  material.onBeforeCompile = shader => patchShader(shader, true);
  const depthMat = new THREE.MeshDepthMaterial();
  depthMat.onBeforeCompile = shader => patchShader(shader, false);
  mesh.customDepthMaterial = depthMat;

  function layoutFor(n){
    if (n <= 0) return { rows: 2, cols: 8 };
    let rows = n <= 16 ? 2 : (n <= 30 ? 3 : 4);
    let cols = Math.ceil(n / rows);
    if (cols > MAX_COLS){ cols = MAX_COLS; rows = Math.ceil(n / cols); }
    if (rows > MAX_ROWS){ rows = MAX_ROWS; cols = MAX_COLS; }
    return { rows: rows, cols: cols };
  }

  function activeCount(){ return layout.rows * layout.cols; }

  function renderCubes(){
    const rows = layout.rows, cols = layout.cols;
    const active = rows * cols;
    const offX = (cols - 1) * SPACING / 2;
    const offZ = (rows - 1) * SPACING / 2;
    const dummy = new THREE.Object3D();
    for (let i = 0; i < CAPACITY; i++){
      if (i >= active){
        dummy.position.set(0, 0, 0);
        dummy.scale.set(0, 0, 0);
        dummy.updateMatrix();
        mesh.setMatrixAt(i, dummy.matrix);
        offsetAttr.setXY(i, 0, 0);
        continue;
      }
      const r = Math.floor(i / cols), c = i % cols;
      const x = c * SPACING - offX;
      const z = r * SPACING - offZ;
      let sy = 0.72;
      if (i < quotes.length){
        const pct = quotes[i].pct_chg;
        sy = pct == null ? 1.05 : 1.0 + Math.min(0.7, Math.abs(pct) / 14);
      }
      dummy.position.set(x, 0, z);
      dummy.scale.set(1, sy, 1);
      dummy.updateMatrix();
      mesh.setMatrixAt(i, dummy.matrix);
      offsetAttr.setXY(i, x, z);
    }
    mesh.instanceMatrix.needsUpdate = true;
    offsetAttr.needsUpdate = true;
  }

  function paintColors(){
    const active = activeCount();
    for (let i = 0; i < CAPACITY; i++){
      let color = FILLER;
      if (i < active && i < quotes.length){
        const pct = quotes[i].pct_chg;
        if (pct == null) color = FLAT;
        else if (pct > 0) color = UP;
        else if (pct < 0) color = DOWN;
        else color = FLAT;
      }
      mesh.setColorAt(i, color);
    }
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  }

  function fitCamera(){
    const rows = layout.rows, cols = layout.cols;
    const W = (cols - 1) * SPACING;
    const D = (rows - 1) * SPACING;
    camera.fov = rows <= 2 ? 40 : (rows === 3 ? 46 : 52);
    const aspect = camera.aspect || 4;
    const targetW = W * 2.0;
    const dist = targetW / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * aspect);
    baseCam = { x: 0, y: D * 0.65 + 3.4, z: dist + D * 0.18 };
    camera.position.set(baseCam.x, baseCam.y, baseCam.z);
    camera.lookAt(0, 1.1, 0);
    camera.updateProjectionMatrix();
  }

  function setWaveQuotes(list){
    quotes = (list || []).filter(q => q && q.code);
    const next = layoutFor(Math.max(quotes.length, 1));
    if (next.rows !== layout.rows || next.cols !== layout.cols) layout = next;
    renderCubes();
    paintColors();
    fitCamera();
    if (!running && !reduced) start();
  }

  const trail = [];
  let lastPoint = null;
  let lastMove = 0;
  let randomMode = true;
  let randomTimer = 0;
  let hoveredStock = -1;
  const raycaster = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const rayPlane = new THREE.Mesh(
    new THREE.PlaneGeometry((MAX_COLS * SPACING) * 1.6, (MAX_ROWS * SPACING) * 1.6),
    new THREE.MeshBasicMaterial({ side: THREE.DoubleSide, visible: false })
  );
  rayPlane.rotation.x = -Math.PI / 2;
  rayPlane.updateMatrixWorld(true);

  function addTrailPoint(x, z, strength){
    if (trail.length >= MAX_TRAIL) trail.shift();
    trail.push({ x: x, z: z, age: 0, distDelta: strength });
  }

  function showTip(clientX, clientY, q){
    if (!tip || !q) return;
    const r = wrap.getBoundingClientRect();
    let px = clientX - r.left + 14;
    let py = clientY - r.top + 14;
    if (px + 230 > r.width) px = clientX - r.left - 230;
    if (py + 64 > r.height) py = clientY - r.top - 64;
    const clsName = q.pct_chg == null ? "" : (q.pct_chg > 0 ? "up" : (q.pct_chg < 0 ? "down" : ""));
    tip.innerHTML =
      '<div class="t-name">' + esc(q.name || q.code) + ' <span class="t-code">' + esc(q.code) + '</span></div>' +
      '<div class="t-meta">' + (q.price == null ? "--" : Number(q.price).toFixed(2)) +
      ' · <span class="' + clsName + '">' + fmtPct(q.pct_chg) + '</span></div>';
    tip.style.left = px + "px";
    tip.style.top = py + "px";
    tip.classList.add("show");
  }

  function hideTip(){
    if (tip) tip.classList.remove("show");
    hoveredStock = -1;
  }

  canvas.addEventListener("pointermove", e => {
    const r = canvas.getBoundingClientRect();
    if (!r.width || !r.height) return;
    ndc.set(
      ((e.clientX - r.left) / r.width) * 2 - 1,
      -((e.clientY - r.top) / r.height) * 2 + 1
    );
    raycaster.setFromCamera(ndc, camera);

    const planeHits = raycaster.intersectObject(rayPlane);
    if (planeHits.length){
      const pt = planeHits[0].point;
      let distDelta = 0;
      if (lastPoint){
        distDelta = Math.sqrt(Math.pow(pt.x - lastPoint.x, 2) + Math.pow(pt.z - lastPoint.z, 2));
        if (distDelta >= 0.2){
          addTrailPoint(pt.x, pt.z, Math.min(1.6, 0.35 + distDelta));
          lastPoint = { x: pt.x, z: pt.z };
          lastMove = 0;
          randomMode = false;
          randomTimer = 0;
        }
      } else {
        addTrailPoint(pt.x, pt.z, 0.8);
        lastPoint = { x: pt.x, z: pt.z };
        lastMove = 0;
        randomMode = false;
        randomTimer = 0;
      }
    }

    const stockHits = raycaster.intersectObject(mesh);
    const id = stockHits.length ? stockHits[0].instanceId : -1;
    if (id >= 0 && id < quotes.length && id < activeCount()){
      hoveredStock = id;
      showTip(e.clientX, e.clientY, quotes[id]);
    } else if (hoveredStock !== -1){
      hideTip();
    }
  });

  canvas.addEventListener("click", e => {
    const r = canvas.getBoundingClientRect();
    if (!r.width || !r.height) return;
    ndc.set(
      ((e.clientX - r.left) / r.width) * 2 - 1,
      -((e.clientY - r.top) / r.height) * 2 + 1
    );
    raycaster.setFromCamera(ndc, camera);
    const stockHits = raycaster.intersectObject(mesh);
    if (!stockHits.length) return;
    const id = stockHits[0].instanceId;
    if (id < 0 || id >= quotes.length || id >= activeCount()) return;
    const code = String(quotes[id].code || "").slice(-6);
    const stocks = window.STOCKS || [];
    const si = stocks.findIndex(s => String(s.code || "").slice(-6) === code);
    if (si >= 0 && typeof window.showDetail === "function") window.showDetail(si);
  });

  canvas.addEventListener("pointerleave", () => {
    lastPoint = null;
    hideTip();
  });

  const camMouse = { x: 0, y: 0 };
  const camLerp = { x: 0, y: 0 };
  wrap.addEventListener("pointermove", e => {
    const r = wrap.getBoundingClientRect();
    if (!r.width || !r.height) return;
    camMouse.x = ((e.clientX - r.left) / r.width) * 2 - 1;
    camMouse.y = -((e.clientY - r.top) / r.height) * 2 + 1;
  });
  wrap.addEventListener("pointerleave", () => { camMouse.x = 0; camMouse.y = 0; });

  function resize(){
    const w = wrap.clientWidth, h = wrap.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    fitCamera();
  }

  function uploadTrail(){
    const n = Math.min(trail.length, MAX_TRAIL);
    for (let i = 0; i < MAX_TRAIL; i++){
      const t = i * 4;
      if (i < n){
        trailData[t] = trail[i].x;
        trailData[t + 1] = trail[i].z;
        trailData[t + 2] = trail[i].age;
        trailData[t + 3] = trail[i].distDelta;
      } else {
        trailData[t] = 0;
        trailData[t + 1] = 0;
        trailData[t + 2] = 9999;
        trailData[t + 3] = 0;
      }
    }
    trailTex.needsUpdate = true;
    uniforms.uTrailCount.value = n;
  }

  let running = false;
  let reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let lastTime = performance.now();
  let baseCam = { x: 0, y: 3.4, z: 8 };

  function frame(now){
    const delta = Math.min(0.05, (now - lastTime) / 1000 || 0.016);
    lastTime = now;
    for (let i = trail.length - 1; i >= 0; i--){
      trail[i].age += delta;
      if (trail[i].age > 8) trail.splice(i, 1);
    }
    lastMove += delta;
    if (lastMove >= 3 && !randomMode){
      randomMode = true;
      randomTimer = 0;
    }
    if (randomMode){
      randomTimer += delta;
      if (randomTimer >= 2.5){
        addTrailPoint(
          (Math.random() * 0.6 - 0.3) * (layout.cols - 1) * SPACING,
          (Math.random() * 0.6 - 0.3) * (layout.rows - 1) * SPACING,
          0.55 + Math.random() * 0.2
        );
        randomTimer = 0;
      }
    }
    uploadTrail();
    camLerp.x += (camMouse.x - camLerp.x) * 0.045;
    camLerp.y += (camMouse.y - camLerp.y) * 0.045;
    camera.position.set(
      baseCam.x + camLerp.x * 1.1,
      baseCam.y + camLerp.y * 0.45,
      baseCam.z - camLerp.y * 0.35
    );
    camera.lookAt(0, 1.1, 0);
    renderer.render(scene, camera);
  }

  function loop(now){
    if (!running) return;
    frame(now);
    if (running) requestAnimationFrame(loop);
  }
  function start(){
    if (running) return;
    running = true;
    lastTime = performance.now();
    requestAnimationFrame(loop);
  }
  function stop(){ running = false; }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop(); else start();
  });

  function seedWaves(){
    addTrailPoint(0, 0, 1.0);
    addTrailPoint(SPACING * 0.8, 0, 0.7);
    addTrailPoint(-SPACING * 0.8, 0, 0.7);
  }

  renderCubes();
  paintColors();
  resize();
  seedWaves();

  if (window.ResizeObserver) new ResizeObserver(resize).observe(wrap);
  else window.addEventListener("resize", resize);

  if (reduced){
    frame(performance.now());
  } else {
    start();
  }

  window.setWaveQuotes = setWaveQuotes;
})();
