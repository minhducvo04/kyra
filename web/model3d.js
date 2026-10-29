/* Kyra 3D viewer: make a model job, then orbit, explode and cut it open. Contract: GLB nodes named part:<slug>,
   optional extras.explode_dir (unit vector, parent space), else parts move radially from the model center. */
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GearMechanism } from './model3d-gears.js';
import { GearLesson } from './model3d-lesson.js';
import { buildGearDisplay, updateGearDisplay } from './model3d-gear-mesh.js';
import { PipeFlow } from './model3d-flow.js';
import { buildFlowDisplay, updateFlowDisplay } from './model3d-flow-mesh.js';
import { solve as solveMates, jointValues } from './model3d-mates.js';
import { installGuess } from './model3d-guess.js';
import { KyraCharacter, KYRA_URL, KYRA_STATES, KYRA_STATE_TEXT, KYRA_LINES, missingNodes } from './model3d-kyra.js';

const API = '/api/model3d/jobs';
const JOB_ID = /^[0-9a-f]{32}$/;
const EXPLODE_SCALE = 1.2;  // at full explode a part travels 1.2 model radii
const HIGHLIGHT = new THREE.Color(0x29d8ff).multiplyScalar(0.35);
const $ = id => document.getElementById(id);

const canvas = $('canvas'), viewport = $('viewport'), labelLayer = $('labels');
// Without WebGL (a blocked GPU, an old browser) the page still lists, generates and opens models; only the 3D view
// says why it is off, instead of a startup error that left the shelf and the model list stuck on "Loading".
let renderer = null;
try {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.localClippingEnabled = true;
} catch (error) {
  renderer = null;
}
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 1000);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
scene.add(new THREE.HemisphereLight(0xd9f2ff, 0x0a1119, 1.6));
const key = new THREE.DirectionalLight(0xffffff, 2.2); key.position.set(3, 5, 4); scene.add(key);
const rim = new THREE.DirectionalLight(0x29d8ff, 0.8); rim.position.set(-4, 2, -3); scene.add(rim);

const view = { root: null, parts: [], center: new THREE.Vector3(), radius: 1, box: new THREE.Box3(),
               hovered: null, spread: 1, jobId: null, modelUrl: null, poll: 0, loadToken: 0, partsKind: 'named' };
let lesson = null;
let mechanism = null, gearPlaying = false, lastFrame = null;
let flow = null, flowPlaying = false, flowLastFrame = null;
let kyra = null, kyraCycle = 0;  // the Kyra character demo and its state-cycle timer
// A library model with mates (asset extras.motion): the driven value, the last accepted solve and the play loop.
let motion = null, motionPlaying = false, motionLastFrame = null;
// Automatic parts are cut by geometry, not meaning; the header says so (job.parts_kind from the pipeline).
const PARTS_KINDS = {
  named: ['Named parts', ''],
  components: ['Automatic components', 'Split where the mesh has separate pieces. These are not real anatomy or named parts.'],
  slices: ['Automatic slices', 'Cut into layers by height so you can look inside. These are not real anatomy or named parts.'],
};
const clipPlane = new THREE.Plane(new THREE.Vector3(-1, 0, 0), 0);
const raycaster = new THREE.Raycaster(), pointer = new THREE.Vector2();

function notice(text) { $('notice').textContent = text || ''; $('notice').hidden = !text; }

// The job's stage and percent from the 1.5 s poll; hidden once the job is done or failed.
const stageLabel = stage => stage ? stage.charAt(0).toUpperCase() + stage.slice(1) : '';
function progress(job) {
  const live = job && (job.status === 'queued' || job.status === 'running');
  $('progress').hidden = !live;
  if (!live) return;
  const percent = Math.min(Math.max(Number(job.percent) || 0, 0), 100);
  $('stage').textContent = `${stageLabel(job.stage || job.status)} · ${percent}%`;
  $('progress-bar').value = percent;
}

async function api(path, options) {
  const response = await fetch(path, { credentials: 'same-origin', ...options });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const message = body?.error?.message || `${response.status} ${response.statusText}`;
    throw Object.assign(new Error(message), { status: response.status });
  }
  return body;
}

function sameOrigin(url) {
  try { return new URL(url, location.origin).origin === location.origin; } catch { return false; }
}

// ---------- jobs ----------
const jobKey = job => job.job_id || job.id || '';
function jobTitle(job) { return job.prompt || job.title || (job.kind === 'image' || job.input === 'image' ? 'From a photo' : `Model ${jobKey(job).slice(0, 8)}`); }

async function loadJobs() {
  const list = $('jobs');
  try {
    const body = await api(API);
    const jobs = (Array.isArray(body) ? body : body?.jobs || []).filter(job => JOB_ID.test(jobKey(job)));
    list.replaceChildren(...(jobs.length ? jobs.map(jobItem) : [muted('No models yet.')]));
  } catch (error) {
    list.replaceChildren(muted(error.status === 404 ? 'The 3D jobs service is not on this server yet.' : `Could not load models: ${error.message}`));
  }
}

function muted(text) { const li = document.createElement('li'); li.className = 'muted'; li.textContent = text; return li; }

function jobItem(job) {
  const li = document.createElement('li'), button = document.createElement('button');
  const title = document.createElement('strong'), meta = document.createElement('small');
  button.type = 'button'; button.dataset.job = jobKey(job);
  if (jobKey(job) === view.jobId) button.setAttribute('aria-current', 'true');
  title.textContent = jobTitle(job);
  const status = document.createElement('span'); status.className = `status-${job.status}`;
  const live = job.status === 'queued' || job.status === 'running';
  status.textContent = live && job.stage ? `${job.stage} ${Number(job.percent) || 0}%` : job.status || 'unknown';
  meta.append(status, ` · ${job.provider || 'provider unknown'}${job.demo ? ' · demo' : ''}`);
  button.append(title, meta);
  button.addEventListener('click', () => openJob(jobKey(job)));
  li.append(button);
  return li;
}

async function openJob(id) {
  stopGearDemo();
  stopFlowDemo();
  stopKyraDemo();
  if (!JOB_ID.test(id)) return;
  clearTimeout(view.poll);
  if (view.jobId !== id) ++view.loadToken;  // Invalidate a previous job's pending GLTF load, even if this job is queued.
  view.jobId = id;
  history.replaceState(null, '', `#${id}`);
  document.querySelectorAll('#jobs button').forEach(b => b.toggleAttribute('aria-current', b.dataset.job === id));
  let job;
  try { job = await api(`${API}/${id}`); } catch (error) { notice(`Could not open this model: ${error.message}`); return; }
  if (view.jobId !== id) return;
  $('model-title').textContent = jobTitle(job);
  $('demo-badge').hidden = !job.demo;
  progress(job);
  if (job.status === 'queued' || job.status === 'running') {
    notice(`${job.status === 'queued' ? 'Waiting to start' : 'Generating'}${job.provider ? ` with ${job.provider}` : ''}…`);
    view.poll = setTimeout(() => openJob(id), 1500);
    return;
  }
  if (job.status === 'failed') { notice(`Generation failed: ${job.reason || 'no reason given'}`); return; }
  if (!job.model_url || !sameOrigin(job.model_url)) { notice('This job has no model file from this server.'); return; }
  notice(job.demo ? 'Demo model from the offline procedural provider; it is not generated from your input.' : '');
  showPartsKind(job.parts_kind);
  if (view.modelUrl !== job.model_url) await loadModel(job.model_url, job.parts || []);
  if (typeof job.disclaimer === 'string' && job.disclaimer) {
    $('model-disclaimer').textContent = job.disclaimer;
    $('model-disclaimer').hidden = false;
  }
  loadJobs();
}

// An imported STEP from Studio (/model3d#import=<id>): node extras carry each solid's STEP name and an honest "does".
async function openImport(id) {
  stopGearDemo();
  stopFlowDemo();
  stopKyraDemo();
  if (!JOB_ID.test(id)) return;
  clearTimeout(view.poll);
  view.jobId = null;
  let body;
  try { body = await api(`/api/cad/imports/${id}.json`); } catch (error) { notice(`Could not open this imported file: ${error.message}`); return; }
  const solids = Array.isArray(body?.solids) ? body.solids.filter(s => typeof s?.id === 'string' && typeof s?.name === 'string') : [];
  $('model-title').textContent = typeof body?.filename === 'string' ? body.filename : 'Imported STEP';
  $('demo-badge').hidden = true;
  notice('');
  showPartsKind('named');
  await loadModel(`/api/cad/imports/${id}.glb`, solids.map(s => ({ name: `part:${s.id}`, label: s.name })));
}

let submitting = false;
function launchBusy(busy) {
  submitting = busy;
  $('create').disabled = busy;
  if ($('guess')) $('guess').disabled = busy;
  document.querySelectorAll('#library-models button').forEach(button => { button.disabled = busy; });
}

async function loadLibrary() {
  const status = $('library-status');
  try {
    const body = await api('/api/model3d/library');
    const models = (body?.models || []).filter(model => /^[a-z0-9-]{1,48}$/.test(model.slug));
    $('library-models').replaceChildren(...models.map(model => {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'quiet'; button.textContent = model.title;
      button.title = `${model.summary} · ${model.part_count} parts. ${model.disclaimer}`;
      button.disabled = submitting;
      button.addEventListener('click', async () => {
        if (submitting) return;
        launchBusy(true); status.textContent = `Building ${model.title} locally…`;
        try {
          const body = await api(API, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt: model.slug, provider: 'library' }) });
          await loadJobs(); await openJob(body.job_id);
          status.textContent = 'Started locally. When the model appears, try Explode and select a part.';
        } catch (error) { status.textContent = `Could not open: ${error.message}`; }
        finally { launchBusy(false); }
      });
      return button;
    }));
    status.textContent = models.length ? '' : 'No library models are available on this server.';
  } catch (error) { status.textContent = `Could not load the library: ${error.message}`; }
}

$('create-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (submitting) return;
  const prompt = $('prompt').value.trim(), image = $('image').files[0], status = $('create-status');
  if (!prompt && !image) { status.textContent = 'Describe a model or choose a photo.'; return; }
  let options;
  if (image) {
    const form = new FormData(); form.append('image', image); if (prompt) form.append('prompt', prompt);
    if ($('quality').value) form.append('provider', $('quality').value);
    options = { method: 'POST', body: form };
  } else {
    options = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt }) };
  }
  launchBusy(true); status.textContent = 'Sending…';
  try {
    const body = await api(API, options);
    status.textContent = 'Started.';
    $('prompt').value = ''; $('image').value = '';
    await loadJobs();
    await openJob(body.job_id);
  } catch (error) {
    status.textContent = error.status === 404 ? 'The 3D jobs service is not on this server yet.' : `Could not start: ${error.message}`;
  } finally { launchBusy(false); }
});
installGuess({ api, loadJobs, openJob, launchBusy, isBusy: () => submitting });
$('refresh-jobs').addEventListener('click', loadJobs);

function showPartsKind(kind) {
  view.partsKind = PARTS_KINDS[kind] ? kind : 'named';
  const [title, note] = PARTS_KINDS[view.partsKind];
  $('parts-kind').textContent = title; $('parts-kind').hidden = false;
  $('parts-kind-note').textContent = note; $('parts-kind-note').hidden = !note;
}

// ---------- model ----------
const slugOf = name => (name || '').replace(/^part[:_]/, '');
const titleCase = slug => slug.replace(/[-_]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase());

async function loadModel(url, jobParts) {
  const token = ++view.loadToken;
  let gltf;
  try { gltf = await new GLTFLoader().loadAsync(url); } catch (error) { notice(`Could not load the model file: ${error.message || error}`); return; }
  if (token !== view.loadToken) return;
  disposeModel();
  view.root = gltf.scene; view.modelUrl = url;
  // Library models carry a disclaimer (asset extras) and one sentence per part on what it does (node extras).
  const disclaimer = gltf.asset?.extras?.disclaimer;
  $('model-disclaimer').textContent = typeof disclaimer === 'string' ? disclaimer : '';
  $('model-disclaimer').hidden = typeof disclaimer !== 'string' || !disclaimer;
  scene.add(view.root);
  view.root.updateMatrixWorld(true);
  view.box.setFromObject(view.root);
  view.box.getCenter(view.center);
  view.radius = Math.max(view.box.getBoundingSphere(new THREE.Sphere()).radius, 1e-3);
  view.parts = collectParts(view.root, jobParts);
  $('show-labels').checked = view.parts.length <= 25;  // many labels overlap; the user can still turn them on
  $('empty').hidden = true;
  renderParts();
  setupMotion(gltf.asset?.extras?.motion);
  resetView();
}

function disposeModel() {
  stopMotion();
  if (!view.root) return;
  scene.remove(view.root);
  view.root.traverse(node => {
    node.geometry?.dispose();
    if (node.material) [].concat(node.material).forEach(material => material.dispose());
  });
  view.hovered = null;
}

// ---------- ideal mechanism demo (no generation request or persisted model job) ----------
function pauseGears() {
  gearPlaying = false; lastFrame = null;
  $('gear-play').textContent = 'Play'; $('gear-play').setAttribute('aria-pressed', 'false');
}

function stopGearDemo() {
  pauseGears(); mechanism = null; lesson = null; $('gear-controls').hidden = true; $('lesson').hidden = true;
}

function gearReadout() {
  if (!mechanism) return;
  const s = mechanism.snapshot();
  updateGearDisplay(view.root, s);
  $('gear-readout').textContent = `${s.parts[3].teeth} → ${s.parts[4].teeth} teeth · Ratio ${s.ratio.toFixed(3)} · `
    + `Input ${s.angles.input.toFixed(1)}° / output ${s.angles.output.toFixed(1)}°`;
  $('gear-turn').value = s.angles.input <= 360 ? s.angles.input : s.angles.input % 360;
  $('gear-undo').disabled = !mechanism.canUndo;
  for (const p of view.parts.filter(p => p.slug.startsWith('gear-'))) {
    const spec = s.parts.find(spec => spec.id === p.slug);
    p.el.querySelector('.part-does').textContent = `${spec.teeth} teeth · module ${spec.module} mm · specified demo part`;
  }
}

function openGears() {
  stopFlowDemo(); stopKyraDemo();
  clearTimeout(view.poll); ++view.loadToken; view.jobId = null; view.modelUrl = null;
  pauseGears(); disposeModel(); lesson = null; $('lesson').hidden = true; mechanism = new GearMechanism();
  view.root = buildGearDisplay(mechanism.snapshot()); scene.add(view.root); view.root.updateMatrixWorld(true);
  view.box.setFromObject(view.root); view.box.getCenter(view.center);
  view.radius = view.box.getBoundingSphere(new THREE.Sphere()).radius;
  view.parts = collectParts(view.root, []); showPartsKind('named');
  $('model-title').textContent = 'Gear mechanism'; $('demo-badge').hidden = true; progress(null); notice('');
  $('model-disclaimer').textContent = 'Ideal kinematic demo · specified parts, fixed axles, schematic teeth. Motion follows a ratio constraint, not tooth-contact simulation.';
  $('model-disclaimer').hidden = false; $('empty').hidden = !!renderer;
  $('show-labels').checked = false; renderParts(); resetView();
  $('gear-controls').hidden = false; $('gear-play').disabled = !renderer;
  $('gear-status').textContent = 'Fixed axles: 30 mm apart. Hide and explode change only the view.';
  $('gear-status').removeAttribute('data-rejected');
  document.querySelectorAll('#jobs button').forEach(b => b.removeAttribute('aria-current'));
  history.replaceState(null, '', '#gears'); gearReadout();
}
$('open-gears').addEventListener('click', openGears);

// ---------- guided gear lesson: state in model3d-lesson.js, graded by the mechanism ----------
function openLesson() {
  openGears();
  lesson = new GearLesson(() => new GearMechanism()); mechanism = lesson.mechanism;
  $('gear-controls').hidden = true; $('lesson').hidden = false;
  history.replaceState(null, '', '#lesson=gears'); renderLesson();
}
function renderLesson() {
  if (!lesson) return;
  mechanism = lesson.mechanism; gearReadout();
  const v = lesson.view();
  $('lesson-step').textContent = `${v.step} of ${v.total}`;
  $('lesson-prompt').textContent = v.prompt;
  $('lesson-choices').replaceChildren(...v.choices.map(c => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'quiet'; b.dataset.choice = c.id; b.textContent = c.label;
    b.setAttribute('aria-pressed', String(v.picked === c.id)); b.disabled = !!v.picked;
    b.addEventListener('click', () => { lesson.answer(c.id); renderLesson(); });
    return b;
  }));
  $('lesson-feedback').textContent = v.feedback;
  $('lesson-feedback').toggleAttribute('data-wrong', v.correct === false);
  $('lesson-action').hidden = !v.action; $('lesson-action').textContent = v.action || '';
  $('lesson-next').hidden = v.done; $('lesson-next').disabled = !v.canNext;
  $('lesson-undo').disabled = !v.canUndo;
}
$('lesson-action').addEventListener('click', () => { lesson?.act(); renderLesson(); });
$('lesson-next').addEventListener('click', () => { lesson?.next(); renderLesson(); });
$('lesson-undo').addEventListener('click', () => { lesson?.undo(); renderLesson(); });
$('lesson-restart').addEventListener('click', () => { lesson?.restart(); renderLesson(); });
$('gear-play').addEventListener('click', () => {
  if (!mechanism) return;
  if (gearPlaying) pauseGears();
  else { gearPlaying = true; lastFrame = null; $('gear-play').textContent = 'Pause'; $('gear-play').setAttribute('aria-pressed', 'true'); }
});
$('gear-turn').addEventListener('input', () => {
  if (!mechanism) return;
  pauseGears(); mechanism.turn(Number($('gear-turn').value)); gearReadout();
});
$('gear-speed').addEventListener('input', () => { $('gear-rpm').textContent = `${$('gear-speed').value} rpm`; });
const gearSwaps = {
  '25-35': { input: { teeth: 25 }, output: { teeth: 35 } },
  '30-30': { input: { teeth: 30 }, output: { teeth: 30 } },
  'too-large': { output: { teeth: 50 } },
  'wrong-module': { output: { module: 1.5 } },
  'wrong-bore': { output: { bore: 6 } },
};
$('gear-apply').addEventListener('click', () => {
  if (!mechanism) return;
  pauseGears();
  const result = mechanism.swap(gearSwaps[$('gear-swap').value]);
  $('gear-status').textContent = `${result.ok ? 'Accepted' : 'Refused'}: ${result.reason}`;
  $('gear-status').toggleAttribute('data-rejected', !result.ok); gearReadout();
});
$('gear-undo').addEventListener('click', () => {
  if (!mechanism) return;
  pauseGears(); mechanism.undo(); gearReadout();
  $('gear-status').textContent = 'Previous pair and motion restored.'; $('gear-status').removeAttribute('data-rejected');
});

// ---------- specified continuity demonstration ----------
function pauseFlow() {
  flowPlaying = false; flowLastFrame = null;
  $('flow-play').textContent = 'Play'; $('flow-play').setAttribute('aria-pressed', 'false');
}
function stopFlowDemo() { pauseFlow(); flow = null; $('flow-controls').hidden = true; }
function flowReadout() {
  if (!flow) return;
  const s = flow.snapshot(); updateFlowDisplay(view.root, s);
  $('flow-readout').textContent = s.segments.map(p => `${p.diameter * 1000} mm: ${p.speed.toFixed(3)} m/s`).join(' · ')
    + ` · Flow ${(s.flow * 1000).toFixed(3)} L/s`;
}
function openFlow() {
  clearTimeout(view.poll); ++view.loadToken; view.jobId = null; view.modelUrl = null;
  stopGearDemo(); stopKyraDemo(); pauseFlow(); disposeModel(); flow = new PipeFlow();
  view.root = buildFlowDisplay(flow.snapshot()); scene.add(view.root); view.root.updateMatrixWorld(true);
  view.box.setFromObject(view.root); view.box.getCenter(view.center);
  view.radius = view.box.getBoundingSphere(new THREE.Sphere()).radius;
  view.parts = collectParts(view.root, []); showPartsKind('named');
  $('model-title').textContent = 'Liquid flow'; $('demo-badge').hidden = true; progress(null); notice('');
  $('model-disclaimer').textContent = 'continuity demo, not fluid simulation'; $('model-disclaimer').hidden = false;
  $('empty').hidden = !!renderer; $('show-labels').checked = false;
  renderParts(); resetView(); $('flow-controls').hidden = false; $('flow-play').disabled = !renderer;
  $('flow-inflow').value = .2;
  document.querySelectorAll('#jobs button').forEach(b => b.removeAttribute('aria-current'));
  history.replaceState(null, '', '#flow'); flowReadout();
}
$('open-flow').addEventListener('click', openFlow);

// ---------- Kyra character demo: the Blender-made bust, four states (model3d-kyra.js) ----------
function setKyraState(state) {
  if (!kyra) return;
  kyra.setState(state);
  document.querySelectorAll('#kyra-states button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.kyraState === state)));
  $('kyra-readout').textContent = KYRA_STATE_TEXT[state];
  const caption = $('kyra-caption');
  caption.hidden = state !== 'speaking';
  if (state === 'speaking') caption.textContent = `“${KYRA_LINES[Math.floor(performance.now() / 3000) % KYRA_LINES.length]}”`;
}
function stopKyraCycle() {
  clearInterval(kyraCycle); kyraCycle = 0;
  $('kyra-cycle').textContent = 'Cycle'; $('kyra-cycle').setAttribute('aria-pressed', 'false');
}
function stopKyraDemo() {
  stopKyraCycle(); kyra = null; $('kyra-controls').hidden = true; $('kyra-caption').hidden = true;
}
async function openKyra() {
  clearTimeout(view.poll); view.jobId = null;
  stopGearDemo(); stopFlowDemo(); stopKyraDemo();
  document.querySelectorAll('#jobs button').forEach(b => b.removeAttribute('aria-current'));
  history.replaceState(null, '', '#kyra');
  $('model-title').textContent = 'Kyra'; $('demo-badge').hidden = true; progress(null); notice('');
  await loadModel(KYRA_URL, []);
  if (view.modelUrl !== KYRA_URL || location.hash !== '#kyra') return;  // the user opened something else meanwhile
  const missing = missingNodes(view.root);
  if (missing.length) { notice(`This Kyra model is missing ${missing.join(', ')}; states are off.`); return; }
  kyra = new KyraCharacter(view.root);
  $('model-disclaimer').textContent = 'Stylized bust built in Blender from a script. Procedural motion, not a rig or lip-sync.';
  $('model-disclaimer').hidden = false;
  $('show-labels').checked = false;
  // Face her: the bust looks along +Z; frame head and shoulders from the front.
  const distance = view.radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 0.9;
  const target = view.center.clone().add(new THREE.Vector3(0, view.radius * 0.05, 0));
  camera.position.copy(target).add(new THREE.Vector3(0, 0.1, 1).normalize().multiplyScalar(distance));
  controls.target.copy(target); controls.update();
  $('kyra-controls').hidden = false; $('kyra-cycle').disabled = !renderer;
  setKyraState('idle');
}
$('open-kyra').addEventListener('click', openKyra);
$('kyra-states').addEventListener('click', event => {
  const state = event.target.closest('button')?.dataset.kyraState;
  if (state) { stopKyraCycle(); setKyraState(state); }
});
$('kyra-cycle').addEventListener('click', () => {
  if (!kyra) return;
  if (kyraCycle) { stopKyraCycle(); return; }
  $('kyra-cycle').textContent = 'Stop'; $('kyra-cycle').setAttribute('aria-pressed', 'true');
  kyraCycle = setInterval(() => setKyraState(KYRA_STATES[(KYRA_STATES.indexOf(kyra.state) + 1) % KYRA_STATES.length]), 3500);
});
$('flow-play').addEventListener('click', () => {
  if (!flow || !renderer) return;
  if (flowPlaying) pauseFlow();
  else { flowPlaying = true; flowLastFrame = null; $('flow-play').textContent = 'Pause'; $('flow-play').setAttribute('aria-pressed', 'true'); }
});
$('flow-inflow').addEventListener('input', () => {
  if (flow) { flow.setInflow(Number($('flow-inflow').value)); flowReadout(); }
});
$('flow-reset').addEventListener('click', () => {
  if (flow) { pauseFlow(); flow.reset(); flowReadout(); }
});

// ---------- mates: a library model moves by its joint rules (asset extras.motion, solver in model3d-mates.js) ----------
// Imported STEP carries geometry, not joint intent. Author one joint explicitly, in assembled coordinates.
const importMateEditor = document.createElement('section');
importMateEditor.id = 'import-mate-editor';
importMateEditor.className = 'gear-controls card';
importMateEditor.hidden = true;
const importMateTitle = document.createElement('h2');
importMateTitle.textContent = 'Mate imported solids';
const importMateNote = document.createElement('p');
importMateNote.className = 'muted';
importMateNote.textContent = 'Choose the parent and moving solid. Axis and anchor use the original STEP coordinates. Saving replaces the existing joint; geometry stays unchanged.';
importMateEditor.append(importMateTitle, importMateNote);
const importMateFields = {};
function mateField(key, label, choices, value = '0') {
  const row = document.createElement('label'); row.className = 'mate-value'; row.style.flexWrap = 'wrap';
  const title = document.createElement('span'); title.textContent = label;
  const input = document.createElement(choices ? 'select' : 'input');
  input.id = `import-mate-${key}`; row.htmlFor = input.id;
  if (choices) for (const [v, text] of choices) { const option = document.createElement('option'); option.value = v; option.textContent = text; input.append(option); }
  else { input.type = 'number'; input.step = 'any'; input.value = value; }
  input.style.maxWidth = '100%'; input.style.minWidth = '0';
  row.append(title, input); importMateEditor.append(row); importMateFields[key] = input;
}
mateField('parent', 'Parent solid', []);
mateField('child', 'Moving solid', []);
mateField('kind', 'Mate type', [['revolute', 'Hinge'], ['slider', 'Slider']]);
for (const [i, axis] of ['X', 'Y', 'Z'].entries()) mateField(`axis${i}`, `Axis ${axis}`, null, i === 1 ? '1' : '0');
for (const [i, axis] of ['X', 'Y', 'Z'].entries()) mateField(`anchor${i}`, `Anchor ${axis} (mm)`, null);
mateField('low', 'Lower limit', null, '0');
mateField('high', 'Upper limit', null, '120');
const importMateUnits = document.createElement('p'); importMateUnits.className = 'muted';
const importMateSave = document.createElement('button'); importMateSave.type = 'button'; importMateSave.textContent = 'Save mate';
const importMateStatus = document.createElement('p'); importMateStatus.id = 'import-mate-status'; importMateStatus.setAttribute('role', 'status');
importMateEditor.append(importMateUnits, importMateSave, importMateStatus);
$('mate-controls').before(importMateEditor);
function mateUnits() {
  const slider = importMateFields.kind.value === 'slider';
  importMateUnits.textContent = slider ? 'Limits in mm. The driven control below uses metres.' : 'Limits in degrees. Anchor in mm.';
  for (let i = 0; i < 3; i++) importMateFields[`anchor${i}`].parentElement.hidden = slider;
}
importMateFields.kind.addEventListener('change', mateUnits);
function setupImportMate(record) {
  if (!/^\/api\/cad\/imports\/[0-9a-f]{32}\.glb$/.test(view.modelUrl || '') || view.parts.length < 2) return;
  const saved = record?.mates?.[0];
  for (const [key, index] of [['parent', 0], ['child', 1]]) {
    const select = importMateFields[key]; select.replaceChildren();
    for (const part of view.parts) { const option = document.createElement('option'); option.value = part.spec; option.textContent = part.label; select.append(option); }
    select.value = saved?.parts?.[index] || view.parts[index].spec;
  }
  importMateFields.kind.value = saved?.kind || 'revolute';
  for (let i = 0; i < 3; i++) {
    importMateFields[`axis${i}`].value = saved?.axis?.[i] ?? (i === 1 ? 1 : 0);
    importMateFields[`anchor${i}`].value = (saved?.anchor?.[i] || 0) * 1000;
  }
  const scale = saved?.kind === 'slider' ? 1000 : 1;
  importMateFields.low.value = (saved?.range?.[0] ?? 0) * scale;
  importMateFields.high.value = (saved?.range?.[1] ?? 120) * scale;
  importMateStatus.textContent = ''; importMateSave.disabled = false;
  importMateEditor.hidden = false; mateUnits();
}
importMateSave.addEventListener('click', async () => {
  const url = view.modelUrl, token = view.loadToken;
  if (!/^\/api\/cad\/imports\/[0-9a-f]{32}\.glb$/.test(url || '')) return;
  const number = key => importMateFields[key].value.trim() === '' ? NaN : Number(importMateFields[key].value);
  const kind = importMateFields.kind.value, scale = kind === 'slider' ? .001 : 1;
  const body = {kind, parts: [importMateFields.parent.value, importMateFields.child.value],
    axis: [0, 1, 2].map(i => number(`axis${i}`)), range: [number('low') * scale, number('high') * scale]};
  if (kind === 'revolute') body.anchor = [0, 1, 2].map(i => number(`anchor${i}`) * .001);
  importMateSave.disabled = true; pauseMotion();
  try {
    const response = await fetch(url.replace(/\.glb$/, '/mates'), {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const result = await response.json();
    if (token !== view.loadToken || url !== view.modelUrl || importMateEditor.hidden) return;
    if (!response.ok || !result.ok) throw new Error(result.reasons?.[0]?.reason || result.error?.message || 'Could not save mate.');
    setupMotion(result.motion); importMateStatus.textContent = 'Mate saved. Use the driven control below.';
  } catch (error) {
    if (token === view.loadToken && url === view.modelUrl && !importMateEditor.hidden) importMateStatus.textContent = `Refused: ${error.message}`;
  } finally {
    if (token === view.loadToken && url === view.modelUrl) importMateSave.disabled = false;
  }
});

const MATE_SPEED = 60;  // driven degrees per second (10 rpm) while playing; a slider mate moves its range in 4 s

function pauseMotion() {
  motionPlaying = false; motionLastFrame = null;
  $('mate-play').textContent = 'Play'; $('mate-play').setAttribute('aria-pressed', 'false');
}

function stopMotion() { pauseMotion(); motion = null; $('mate-controls').hidden = true; importMateEditor.hidden = true; }

function setupMotion(record) {
  stopMotion();
  setupImportMate(record);
  if (!record || !record.driven || !Array.isArray(record.mates)) return;
  const mate = record.mates.find(m => m && m.id === record.driven.mate);
  if (!mate) return;
  const first = solveMates(record, record.driven.value);
  if (!first.ok) { $('mate-status').textContent = `This model's mates do not solve: ${first.reason}`; $('mate-status').toggleAttribute('data-rejected', true); $('mate-controls').hidden = false; return; }
  const range = Array.isArray(mate.range) ? mate.range : (mate.kind === 'slider' ? [-0.1, 0.1] : [0, 360]);
  motion = { record, mate, range, value: record.driven.value, result: first, applied: {} };
  const slider = $('mate-turn');
  slider.min = range[0]; slider.max = range[1]; slider.step = mate.kind === 'slider' ? 0.001 : 1;
  $('mate-turn-label').textContent = mate.kind === 'slider' ? `${mate.id} (m)` : `${mate.id} (°)`;
  $('mate-controls').hidden = false; $('mate-play').disabled = !renderer;
  $('mate-status').textContent = 'Hide and explode change only the view.'; $('mate-status').removeAttribute('data-rejected');
  applyMotion();
}

// The driven value goes through the solver; a refusal keeps the last accepted poses and shows the reason.
function setMotion(value) {
  if (!motion) return false;
  const result = solveMates(motion.record, value);
  if (!result.ok) {
    $('mate-status').textContent = `Refused: ${result.reason}`; $('mate-status').toggleAttribute('data-rejected', true);
    return false;
  }
  motion.value = value; motion.result = result;
  $('mate-status').textContent = 'Hide and explode change only the view.'; $('mate-status').removeAttribute('data-rejected');
  applyMotion();
  return true;
}

function applyMotion() {
  if (!motion) return;
  const poses = motion.result.poses;
  for (const part of view.parts) {
    const pose = poses[part.spec];
    if (!pose) continue;
    const r = pose.rotation;
    part.mateQuaternion.setFromRotationMatrix(new THREE.Matrix4().set(
      r[0][0], r[0][1], r[0][2], 0, r[1][0], r[1][1], r[1][2], 0, r[2][0], r[2][1], r[2][2], 0, 0, 0, 0, 1));
    part.mateTranslation.set(...pose.translation);
  }
  applyExplode();
  const values = jointValues(motion.record, motion.value);
  const unit = motion.mate.kind === 'slider' ? ' m' : '°';
  const digits = motion.mate.kind === 'slider' ? 3 : 1;
  $('mate-readout').textContent = Object.entries(values).map(([id, v]) => `${id} ${v.toFixed(digits)}${unit}`).join(' · ');
  const span = motion.range[1] - motion.range[0];
  const shown = motion.value >= motion.range[0] && motion.value <= motion.range[1] ? motion.value
    : motion.range[0] + (((motion.value - motion.range[0]) % span) + span) % span;
  $('mate-turn').value = shown;
  if (document.activeElement !== $('mate-value')) $('mate-value').value = Number(motion.value.toFixed(digits + 2));
}

$('mate-turn').addEventListener('input', () => { if (motion) { pauseMotion(); setMotion(Number($('mate-turn').value)); } });
$('mate-value').addEventListener('input', () => {
  if (!motion) return;
  pauseMotion();
  const raw = $('mate-value').value.trim();
  setMotion(raw === '' ? NaN : Number(raw));
});
$('mate-play').addEventListener('click', () => {
  if (!motion || !renderer) return;
  if (motionPlaying) pauseMotion();
  else { motionPlaying = true; motionLastFrame = null; $('mate-play').textContent = 'Pause'; $('mate-play').setAttribute('aria-pressed', 'true'); }
});

function collectParts(root, jobParts) {
  const nodes = [];
  root.traverse(node => { if (/^part[:_]/.test(node.userData.name || node.name) && !nodes.some(p => isAncestor(p, node))) nodes.push(node); });
  if (!nodes.length) nodes.push(...(root.children.length === 1 && root.children[0].children.length ? root.children[0].children : root.children));
  const labels = new Map(jobParts.map(part => [slugOf(part.name), part.label]));
  return nodes.map((node, index) => {
    const slug = slugOf(node.userData.name || node.name) || `part-${index + 1}`;
    const materials = [];
    node.traverse(child => {
      if (!child.isMesh) return;
      child.material = (Array.isArray(child.material) ? child.material : [child.material]).map(m => m.clone());
      if (child.material.length === 1) child.material = child.material[0];
      materials.push(...[].concat(child.material));
    });
    materials.forEach(m => { m.userData.emissive = m.emissive ? m.emissive.clone() : null; });
    return { node, slug, label: labels.get(slug) || node.userData.label || titleCase(slug), base: node.position.clone(),
             dir: explodeDir(node), distance: explodeDistance(node), materials, el: null, labelEl: null,
             does: typeof node.userData.does === 'string' ? node.userData.does : '',
             // Mates: a repeated copy is posed by its spec slug (extras.part); the pose composes with the node's own
             // rotation and the explode offset is added after it, so explode and hide stay display only.
             spec: typeof node.userData.part === 'string' ? node.userData.part : slug,
             baseQuaternion: node.quaternion.clone(), mateQuaternion: new THREE.Quaternion(), mateTranslation: new THREE.Vector3() };
  });
}

// Optional extras.explode_distance (0..1) scales a part's travel, so stacked automatic slices spread apart.
function explodeDistance(node) {
  const d = node.userData.explode_distance;
  return Number.isFinite(d) ? Math.min(Math.max(d, 0), 1) : 1;
}

function isAncestor(a, b) { for (let n = b.parent; n; n = n.parent) if (n === a) return true; return false; }

function explodeDir(node) {
  const extra = node.userData.explode_dir;
  if (Array.isArray(extra) && extra.length === 3 && extra.every(Number.isFinite)) {
    const dir = new THREE.Vector3(...extra);
    return dir.lengthSq() > 0 ? dir.normalize() : dir;
  }
  const center = new THREE.Box3().setFromObject(node).getCenter(new THREE.Vector3());
  const world = center.sub(view.center);
  if (world.lengthSq() < 1e-10) return new THREE.Vector3();
  const parentInverse = new THREE.Matrix4().copy(node.parent.matrixWorld).invert();
  return world.transformDirection(parentInverse);  // into the parent's frame, normalized
}

function renderParts() {
  const list = $('parts');
  labelLayer.replaceChildren();
  if (!view.parts.length) { list.replaceChildren(muted('This model has no separate parts.')); return; }
  list.replaceChildren(...view.parts.map(part => {
    const li = document.createElement('li'), label = document.createElement('label');
    const box = document.createElement('input'), isolate = document.createElement('button');
    li.dataset.part = part.slug;
    box.type = 'checkbox'; box.checked = true;
    box.addEventListener('change', () => { part.node.visible = box.checked; });
    label.append(box, part.label);
    isolate.type = 'button'; isolate.className = 'quiet'; isolate.textContent = 'Isolate';
    isolate.setAttribute('aria-label', `Isolate ${part.label}`);
    isolate.addEventListener('click', () => view.parts.forEach(p => setVisible(p, p === part)));
    li.append(label, isolate);
    if (part.does) li.append(Object.assign(document.createElement('small'), { className: 'muted part-does', textContent: part.does }));
    li.addEventListener('pointerenter', () => setHover(part));
    li.addEventListener('pointerleave', () => setHover(null));
    part.el = li;
    part.labelEl = Object.assign(document.createElement('span'), { className: 'part-label', textContent: part.label });
    if (part.does) part.labelEl.title = part.does;
    labelLayer.append(part.labelEl);
    return li;
  }));
}

function setVisible(part, visible) { part.node.visible = visible; part.el.querySelector('input').checked = visible; }
$('show-all').addEventListener('click', () => view.parts.forEach(p => setVisible(p, true)));

function setHover(part) {
  if (view.hovered === part) return;
  for (const p of [view.hovered, part]) {
    if (!p) continue;
    const on = p === part;
    p.materials.forEach(m => { if (m.emissive) m.emissive.copy(on ? HIGHLIGHT : m.userData.emissive); });
    p.el?.classList.toggle('hovered', on);
    p.labelEl?.classList.toggle('hovered', on);
  }
  view.hovered = part;
}

canvas.addEventListener('pointermove', event => {
  if (!view.root) return;
  const rect = canvas.getBoundingClientRect();
  pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(view.parts.filter(p => p.node.visible).map(p => p.node), true)
    .find(h => !clipped(h.point));
  setHover(hit ? view.parts.find(p => p.node === hit.object || isAncestor(p.node, hit.object)) : null);
});
canvas.addEventListener('pointerleave', () => setHover(null));

// ---------- explode and cut-away ----------
function applyExplode() {
  const t = Number($('explode').value) / 100;
  for (const part of view.parts) {
    part.node.quaternion.copy(part.mateQuaternion).multiply(part.baseQuaternion);
    part.node.position.copy(part.base).applyQuaternion(part.mateQuaternion).add(part.mateTranslation)
      .addScaledVector(part.dir, t * part.distance * view.radius * EXPLODE_SCALE);
  }
  // Dolly out as the parts spread so they stay in frame; the user's own orbit and zoom are kept relative.
  const spread = 1 + t * EXPLODE_SCALE * 0.6;
  camera.position.sub(controls.target).multiplyScalar(spread / view.spread).add(controls.target);
  view.spread = spread;
}
$('explode').addEventListener('input', applyExplode);

function clipping() { return Number($('cut').value) > 0; }
function clipped(point) { return clipping() && clipPlane.distanceToPoint(point) < 0; }

function applyCut() {
  const amount = Number($('cut').value) / 100, axis = $('cut-axis').value;
  const index = { x: 0, y: 1, z: 2 }[axis];
  const min = view.box.min.getComponent(index), max = view.box.max.getComponent(index);
  // Remove everything beyond the plane on the positive side of the axis; 0 means no cut.
  clipPlane.normal.set(0, 0, 0).setComponent(index, -1);
  clipPlane.constant = max - amount * (max - min) * 1.0001;
  const planes = amount > 0 ? [clipPlane] : [];
  for (const part of view.parts) for (const m of part.materials) {
    m.clippingPlanes = planes;
    m.side = amount > 0 ? THREE.DoubleSide : (m.userData.side ?? (m.userData.side = m.side));
    m.needsUpdate = true;
  }
}
$('cut').addEventListener('input', applyCut);
$('cut-axis').addEventListener('change', applyCut);

function resetView() {
  $('explode').value = 0; $('cut').value = 0;
  view.parts.forEach(p => setVisible(p, true));
  applyExplode(); applyCut();
  view.spread = 1;
  const distance = view.radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.25;
  camera.position.copy(view.center).add(new THREE.Vector3(0.6, 0.45, 1).normalize().multiplyScalar(distance));
  camera.near = distance / 100; camera.far = distance * 100; camera.updateProjectionMatrix();
  controls.target.copy(view.center);
  controls.update();
}
$('reset').addEventListener('click', resetView);

// ---------- render loop ----------
const projected = new THREE.Vector3(), partBox = new THREE.Box3();
function updateLabels(width, height) {
  const show = $('show-labels').checked;
  for (const part of view.parts) {
    if (!part.labelEl) continue;
    partBox.setFromObject(part.node).getCenter(projected).project(camera);
    const visible = show && part.node.visible && projected.z < 1 && !clipped(partBox.getCenter(new THREE.Vector3()));
    part.labelEl.hidden = !visible;
    if (visible) part.labelEl.style.transform = `translate(${(projected.x + 1) / 2 * width}px, ${(1 - projected.y) / 2 * height}px) translate(-50%, -50%)`;
  }
}

function frame(timestamp) {
  if (!renderer) return;
  if (kyra) kyra.update(timestamp / 1000);
  if (flow && flowPlaying) {
    const delta = flowLastFrame === null ? 0 : Math.max(0, Math.min((timestamp - flowLastFrame) / 1000, .05));
    flow.advance(delta); updateFlowDisplay(view.root, flow.snapshot()); flowLastFrame = timestamp;
  }
  if (motion && motionPlaying) {
    const delta = motionLastFrame === null ? 0 : Math.max(0, Math.min((timestamp - motionLastFrame) / 1000, .05));
    const rate = motion.mate.kind === 'slider' ? (motion.range[1] - motion.range[0]) / 4 : MATE_SPEED;
    if (!setMotion(motion.value + delta * rate)) pauseMotion();  // a range limit stops the play, reason shown
    motionLastFrame = timestamp;
  }
  if (mechanism && gearPlaying) {
    const delta = lastFrame === null ? 0 : Math.min((timestamp - lastFrame) / 1000, .05);
    mechanism.turn(mechanism.snapshot().angles.input + delta * Number($('gear-speed').value) * 6);
    gearReadout(); lastFrame = timestamp;
  }
  const width = viewport.clientWidth, height = viewport.clientHeight;
  if (canvas.width !== Math.floor(width * renderer.getPixelRatio()) || canvas.height !== Math.floor(height * renderer.getPixelRatio())) {
    renderer.setSize(width, height, false);
    camera.aspect = width / Math.max(height, 1); camera.updateProjectionMatrix();
  }
  controls.update();
  renderer.render(scene, camera);
  updateLabels(width, height);
  requestAnimationFrame(frame);
}
if (renderer) requestAnimationFrame(frame);
else {
  viewport.setAttribute('data-no-webgl', 'true');
  $('empty').textContent = 'This browser cannot show 3D here (WebGL is off). Models still build and open on the headset; '
    + 'on a Mac, turn on hardware acceleration in the browser settings and reload.';
}

// A read-only snapshot for browser tests and debugging; it changes nothing.
window.kyraModel3d = {
  state: () => ({
    modelUrl: view.modelUrl,
    mechanism: mechanism?.snapshot() || null,
    lesson: lesson?.view() || null,
    flow: flow?.snapshot() || null,
    kyra: kyra?.snapshot() || null,
    motion: motion ? { mate: motion.mate.id, value: motion.value, values: jointValues(motion.record, motion.value),
                       poses: motion.result.poses, playing: motionPlaying, status: $('mate-status').textContent,
                       rejected: $('mate-status').hasAttribute('data-rejected') } : null,
    partsKind: view.partsKind,
    clipping: { enabled: !!renderer && renderer.localClippingEnabled && view.parts.some(p => p.materials.some(m => m.clippingPlanes?.length)),
                normal: clipPlane.normal.toArray(), constant: clipPlane.constant },
    parts: view.parts.map(p => ({ slug: p.slug, label: p.label, visible: p.node.visible,
                                  offset: p.node.position.clone().sub(p.base).toArray(), hovered: view.hovered === p,
                                  quaternion: p.node.quaternion.toArray() })),
  }),
};

async function loadQuality() {
  // The slow, better photo model is offered only when the server says it can run here.
  const body = await api('/api/model3d/providers').catch(() => null);
  const trellis = (body?.providers || []).find(p => p.name === 'local_trellis' && p.available);
  if (!trellis) return;
  const option = document.createElement('option');
  option.value = 'local_trellis'; option.textContent = 'High quality photo model (3 to 5 min)';
  $('quality').append(option);
  $('quality').hidden = false; $('quality-label').hidden = false;
}

loadJobs();
loadQuality();
const initial = location.hash.slice(1);
if (JOB_ID.test(initial)) openJob(initial);
else if (initial === 'gears') openGears();
else if (initial === 'lesson=gears') openLesson();
else if (initial === 'flow') openFlow();
else if (initial === 'kyra') openKyra();
else if (initial.startsWith('import=')) openImport(initial.slice(7));

loadLibrary();
