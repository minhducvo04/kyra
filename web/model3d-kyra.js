// Kyra's bust in /model3d: the Blender-made GLB (scripts/blender/kyra_bust.py) animated in four states.
// The GLB node names are the contract: Head (pivot at the neck), EyeL, EyeR, Mouth, Body, Ring.
// Motion is procedural (no rig, no visemes yet): breathing, blinks, head tilt, eye drift and a mouth open level.
import * as THREE from 'three';

export const KYRA_URL = '/static/models/kyra-bust.glb';
export const KYRA_STATES = ['idle', 'listening', 'thinking', 'speaking'];
export const KYRA_STATE_TEXT = {
  idle: 'Idle: slow breathing, a blink every 4 s, the ring dim and still.',
  listening: 'Listening: head tilts toward you, the ring pulses like a voice level.',
  thinking: 'Thinking: eyes drift up, sparks orbit the hair, the ring turns violet.',
  speaking: 'Speaking: the mouth opens with the words, the ring follows as a waveform, a caption shows below.',
};
const RING_COLOR = { idle: 0x29d8ff, listening: 0x29d8ff, thinking: 0x9b8cff, speaking: 0x5ff2ff };
const NEEDED = ['Head', 'EyeL', 'EyeR', 'Mouth', 'Ring'];

export function missingNodes(root) { return NEEDED.filter(name => !root.getObjectByName(name)); }

export class KyraCharacter {
  constructor(root) {
    this.root = root;
    this.nodes = Object.fromEntries(['Head', 'EyeL', 'EyeR', 'Mouth', 'Body', 'Ring'].map(n => [n, root.getObjectByName(n)]));
    this.base = Object.fromEntries(Object.entries(this.nodes).filter(([, n]) => n)
      .map(([k, n]) => [k, { rot: n.rotation.clone(), scale: n.scale.clone() }]));
    this.ringMaterials = [];
    this.nodes.Ring?.traverse(n => {
      if (!n.isMesh) return;
      n.material = n.material.clone(); this.ringMaterials.push(n.material);
    });
    // Thinking sparks: three small lights that orbit the hair; hidden in the other states.
    this.sparks = new THREE.Group(); this.sparks.name = 'Sparks';
    for (let i = 0; i < 3; i++) {
      const s = new THREE.Mesh(new THREE.SphereGeometry(0.012, 12, 8), new THREE.MeshBasicMaterial({ color: 0xc8bfff }));
      s.userData.phase = i * 2.1; this.sparks.add(s);
    }
    this.sparks.visible = false;
    (this.nodes.Head || root).add(this.sparks);
    this.state = 'idle'; this.since = 0; this.t = 0; this.blink = 0; this.mouthOpen = 0; this.ringLevel = 0;
  }

  setState(state) {
    if (!KYRA_STATES.includes(state)) throw new Error(`unknown state ${state}`);
    if (state !== this.state) { this.state = state; this.since = this.t; }
    this.sparks.visible = state === 'thinking';
    for (const m of this.ringMaterials) { m.color?.set(RING_COLOR[state]); m.emissive?.set(RING_COLOR[state]); }
  }

  // t in seconds since start; called every frame.
  update(t) {
    this.t = t;
    const { Head, EyeL, EyeR, Mouth, Body, Ring } = this.nodes, b = this.base, s = this.state;
    const ease = Math.min((t - this.since) / 0.4, 1);  // 0.4 s blend into a new pose
    const lerp = (a, z) => a + (z - a) * ease;
    if (Body) Body.scale.y = b.Body.scale.y * (1 + 0.012 * Math.sin(t * 2 * Math.PI / 4));  // one breath per 4 s
    if (Head) {
      const tiltZ = s === 'listening' ? 0.14 : 0, tiltX = s === 'listening' ? 0.08 : s === 'thinking' ? -0.06 : 0;
      Head.rotation.z = lerp(Head.rotation.z, b.Head.rot.z + tiltZ + 0.015 * Math.sin(t * 0.7));
      Head.rotation.x = lerp(Head.rotation.x, b.Head.rot.x + tiltX);
      Head.rotation.y = b.Head.rot.y + 0.04 * Math.sin(t * 0.45);
    }
    // A blink every 4 s (and a quick one while speaking); 0.15 s closed-open.
    const phase = t % 4;
    this.blink = phase < 0.15 ? Math.sin(phase / 0.15 * Math.PI) : 0;
    const lookUp = s === 'thinking' ? -0.35 : 0;
    for (const eye of [EyeL, EyeR]) {
      if (!eye) continue;
      const key = eye === EyeL ? 'EyeL' : 'EyeR';
      eye.scale.y = b[key].scale.y * (1 - 0.9 * this.blink);
      eye.rotation.x = lerp(eye.rotation.x, b[key].rot.x + lookUp);
    }
    // Speaking: a syllable-like open level (two mixed rates), closed otherwise.
    this.mouthOpen = s === 'speaking' ? Math.max(0, Math.sin(t * 11) * 0.6 + Math.sin(t * 4.3) * 0.4) : 0;
    if (Mouth) { Mouth.scale.y = b.Mouth.scale.y * (1 + 3.2 * this.mouthOpen); Mouth.scale.x = b.Mouth.scale.x * (1 - 0.2 * this.mouthOpen); }
    // The floor ring: dim and still (idle), voice-level pulse (listening), slow turn (thinking), waveform (speaking).
    const level = s === 'idle' ? 0.25 : s === 'listening' ? 0.55 + 0.45 * Math.abs(Math.sin(t * 5)) * Math.abs(Math.sin(t * 1.7))
      : s === 'thinking' ? 0.6 : 0.5 + 0.5 * this.mouthOpen;
    this.ringLevel = level;
    for (const m of this.ringMaterials) m.emissiveIntensity = 0.3 + 1.6 * level;
    if (Ring) {
      const pulse = s === 'listening' || s === 'speaking' ? 1 + 0.05 * level : 1;
      Ring.scale.set(b.Ring.scale.x * pulse, b.Ring.scale.y, b.Ring.scale.z * pulse);
      Ring.rotation.y = s === 'thinking' ? t * 0.8 : b.Ring.rot.y;
    }
    for (const spark of this.sparks.children) {
      const a = t * 1.6 + spark.userData.phase;
      spark.position.set(Math.cos(a) * 0.22, 0.2 + 0.05 * Math.sin(a * 1.3), Math.sin(a) * 0.22);
    }
  }

  snapshot() {
    const { Head, EyeL } = this.nodes;
    return { state: this.state, blink: this.blink, mouthOpen: this.mouthOpen, ringLevel: this.ringLevel,
             headTilt: Head ? Head.rotation.z : null, eyePitch: EyeL ? EyeL.rotation.x : null, sparks: this.sparks.visible };
  }
}

// Short sample lines for the caption; fictional, no personal data.
export const KYRA_LINES = ['Moved. Practice at 7, call at 8.', 'I found two notes about that project.', 'Want me to start the build?'];
