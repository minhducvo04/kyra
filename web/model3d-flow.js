// Specified incompressible steady flow in a closed loop. Metres and seconds.
// No pressure, viscosity, turbulence or CFD; marker travel follows Q = area * speed.
const points = [[-.45, -.25, 0], [.45, -.25, 0], [0, .45, 0]];
const diameters = [.04, .02, .06];
const segments = points.map((start, i) => {
  const end = points[(i + 1) % points.length], diameter = diameters[i];
  return { id: `pipe-${i + 1}`, start, end, diameter, area: Math.PI * diameter ** 2 / 4,
    length: Math.hypot(...end.map((v, axis) => v - start[axis])) };
});
const volume = segments.reduce((sum, s) => sum + s.length * s.area, 0);
const wrap = value => ((value % volume) + volume) % volume;

function positionAt(amount) {
  let remaining = wrap(amount);
  for (const [index, s] of segments.entries()) {
    const capacity = s.area * s.length;
    if (remaining < capacity || index === segments.length - 1) {
      const distance = Math.min(remaining / s.area, s.length);
      return { segment: index, distance, position: s.start.map((v, axis) => v + (s.end[axis] - v) * distance / s.length) };
    }
    remaining -= capacity;
  }
}

export class PipeFlow {
  #inflow = .2;
  #elapsed = 0;
  #phase = 0;
  setInflow(value) {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > .5) return false;
    this.#inflow = value;
    return true;
  }
  advance(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0 || seconds > .1) return false;
    this.#elapsed += seconds;
    this.#phase = wrap(this.#phase + seconds * this.#inflow * segments[0].area);
    return true;
  }
  reset() { this.#elapsed = 0; this.#phase = 0; }
  particleAt(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return null;
    return positionAt(seconds * this.#inflow * segments[0].area);
  }
  snapshot() {
    const flow = this.#inflow * segments[0].area;
    return structuredClone({ inflow: this.#inflow, elapsed: this.#elapsed, flow,
      period: flow > 0 ? volume / flow : 0,
      segments: segments.map(s => ({ ...s, speed: flow / s.area })),
      particles: Array.from({ length: 36 }, (_, i) => positionAt(this.#phase + i * volume / 36)) });
  }
}
