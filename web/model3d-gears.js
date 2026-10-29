// One ideal external spur pair. Dimensions are mm; angles are degrees. No DOM, mesh or physics engine.
// Fixed pressure angle, parallel axes and zero profile shift. This is not a manufacturing fit model.
const clone = value => structuredClone(value);
const fixed = [
  { id: 'frame', position: [0, 0, -7], axleSpacing: 30 },
  { id: 'shaft-in', position: [-15, 0, 0], diameter: 5 },
  { id: 'shaft-out', position: [15, 0, 0], diameter: 5 },
];
const gear = (id, x, teeth) => ({ id, position: [x, 0, 0], teeth, module: 1, bore: 5, face: 6 });

function validate(a, b) {
  for (const p of [a, b]) {
    if (!Number.isInteger(p.teeth) || p.teeth < 10 || p.teeth > 80) return 'Use a whole tooth count from 10 to 80.';
    if (!Number.isFinite(p.module) || p.module <= 0 || p.module > 3) return 'Use a finite module above 0 and at most 3 mm.';
    if (p.bore !== 5) return `bore ${p.bore} mm does not fit 5 mm shaft`;
    if (p.face !== 6) return 'This fixture supports only a 6 mm face width.';
  }
  const moduleText = value => Number.isInteger(value) ? value.toFixed(1) : String(value);
  if (a.module !== b.module) return `module ${moduleText(b.module)} does not mesh with module ${moduleText(a.module)}`;
  const distance = a.module * (a.teeth + b.teeth) / 2;
  if (Math.abs(distance - 30) > .05) {
    const partner = 60 / b.module - b.teeth;
    return `needs ${distance.toFixed(1)} mm axle spacing; frame fixes 30.0 mm.`
      + (Number.isInteger(partner) && partner >= 10 && partner <= 80
        ? ` An ideal ${partner}-tooth input partner would meet this spacing; other fit checks still apply.` : ' Choose a compatible pair.');
  }
  // The fixed plate has an open 80 x 52 mm envelope, no surrounding cover.
  if ([a, b].some(p => p.module * (p.teeth + 2) / 2 > 25)) return 'Gear exceeds this fixture’s 25 mm radial clearance.';
  return '';
}

export class GearMechanism {
  #parts = [...clone(fixed), gear('gear-in', -15, 20), gear('gear-out', 15, 40)];
  #angles = { input: 0, output: 0 };
  #history = [];

  get ratio() { return -this.#parts[3].teeth / this.#parts[4].teeth; }
  get canUndo() { return this.#history.length > 0; }
  snapshot() { return clone({ units: 'mm', evidence: 'specified', parts: this.#parts, angles: this.#angles, ratio: this.ratio }); }

  turn(degrees) {
    if (!Number.isFinite(degrees) || Math.abs(degrees) > 1e9) return false;
    this.#angles = { input: degrees, output: degrees * this.ratio };
    return true;
  }

  swap(edit) {
    if (!edit || typeof edit !== 'object' || Array.isArray(edit) || Object.keys(edit).some(k => !['input', 'output'].includes(k))) {
      return { ok: false, reason: 'Only the two gears can be replaced.' };
    }
    for (const patch of Object.values(edit)) {
      if (!patch || typeof patch !== 'object' || Array.isArray(patch)
        || Object.keys(patch).some(k => !['teeth', 'module', 'bore', 'face'].includes(k))
        || Object.values(patch).some(v => !Number.isFinite(v))) return { ok: false, reason: 'Use finite gear dimensions only.' };
    }
    const a = { ...this.#parts[3], ...edit.input }, b = { ...this.#parts[4], ...edit.output };
    const reason = validate(a, b);
    if (reason) return { ok: false, reason };
    if (JSON.stringify([a, b]) === JSON.stringify(this.#parts.slice(3))) return { ok: true, reason: 'This pair is already fitted.' };
    this.#history.push(this.snapshot());
    if (this.#history.length > 20) this.#history.shift();
    this.#parts = [...this.#parts.slice(0, 3), a, b];
    this.turn(0); // Re-seat the replacement pair at its reference phase.
    return { ok: true, reason: 'Pair fits the ideal fixture. Motion reset to the reference pose.' };
  }

  undo() {
    const previous = this.#history.pop();
    if (!previous) return false;
    this.#parts = previous.parts; this.#angles = previous.angles;
    return true;
  }
}
