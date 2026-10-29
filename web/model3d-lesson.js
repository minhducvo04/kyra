// Five-step guided lesson on the ideal gear pair. Pure state: no DOM, no three.js.
// Every grade is read from GearMechanism results (angles after a turn, the reason a swap is refused).
const TURNS = [
  { id: 'half', label: 'Half a turn', value: 0.5 },
  { id: 'one', label: 'One full turn', value: 1 },
  { id: 'two', label: 'Two turns', value: 2 },
];
const SWAPS = [
  { id: 'pair-25-35', label: 'Pair: 25 / 35 teeth', edit: { input: { teeth: 25 }, output: { teeth: 35 } } },
  { id: 'single-50', label: 'Only the output: 50 teeth', edit: { output: { teeth: 50 } } },
  { id: 'pair-20-40', label: 'Pair: 20 / 40 teeth', edit: { input: { teeth: 20 }, output: { teeth: 40 } } },
];
const STEPS = [
  { prompt: 'Turn the input gear one full turn. How far will the output turn?', choices: TURNS },
  { prompt: 'Turn the input one full turn and see.', action: 'Turn one full turn' },
  { prompt: 'Swap to 30 / 30 teeth. Predict how far the output turns for one input turn.', choices: TURNS, action: 'Apply 30 / 30 and turn' },
  { prompt: 'Which swap will the frame refuse?', choices: SWAPS },
  { prompt: 'Lesson done.' },
];
const RULE = 'Output turns = input turns × input teeth ÷ output teeth, in the opposite direction; '
  + 'a swap fits only if the two gears still reach across the fixed 30 mm axle spacing.';
const turnLabel = turns => TURNS.find(t => Math.abs(t.value - turns) < 1e-9)?.label || `${turns.toFixed(3)} turns`;
const blank = () => ({ step: 0, picks: {}, results: {} });

export class GearLesson {
  #make; #m; #s = blank(); #history = [];

  constructor(makeMechanism) { this.#make = makeMechanism; this.#m = makeMechanism(); }
  get mechanism() { return this.#m; }

  #record(change) {
    const saved = { s: structuredClone(this.#s), input: this.#m.snapshot().angles.input, swapped: false };
    const done = change(saved);
    if (done) this.#history.push(saved);
    return done;
  }

  answer(id) {
    const step = this.#s.step, choices = STEPS[step].choices;
    if (!choices?.some(c => c.id === id) || this.#s.results[step]) return false;
    return this.#record(() => {
      this.#s.picks[step] = id;
      if (step === 3) this.#s.results[3] = this.#gradeRefusal(id);
      return true;
    });
  }

  act() {
    const step = this.#s.step;
    if (!STEPS[step].action || this.#s.results[step] || (step === 2 && !this.#s.picks[2])) return false;
    return this.#record(saved => {
      if (step === 2) {
        const swap = this.#m.swap({ input: { teeth: 30 }, output: { teeth: 30 } });
        if (!swap.ok) return false;
        saved.swapped = this.#m.canUndo && swap.reason !== 'This pair is already fitted.';
      }
      this.#m.turn(360);
      this.#s.results[step] = this.#gradeTurn(step === 1 ? 0 : 2);
      return true;
    });
  }

  next() {
    if (!this.view().canNext) return false;
    return this.#record(() => { this.#s.step += 1; return true; });
  }

  undo() {
    const saved = this.#history.pop();
    if (!saved) return false;
    if (saved.swapped) this.#m.undo();
    this.#m.turn(saved.input);
    this.#s = saved.s;
    return true;
  }

  restart() { this.#m = this.#make(); this.#s = blank(); this.#history = []; }

  #gradeTurn(pickStep) {
    const snap = this.#m.snapshot(), [a, b] = [snap.parts[3].teeth, snap.parts[4].teeth];
    const turns = Math.abs(snap.angles.output) / 360, picked = TURNS.find(t => t.id === this.#s.picks[pickStep]);
    const correct = Math.abs(picked.value - turns) < 1e-9;
    const why = `${a} teeth drive ${b} teeth, so the output turns ${a} ÷ ${b} = ${+turns.toFixed(3)} of the input, the other way.`;
    return { pickStep, correct, feedback: `${correct ? 'Right' : `You picked ${picked.label}`}. `
      + `Real result: ${turnLabel(turns)} (${snap.angles.output.toFixed(0)}°). ${why}` };
  }

  #gradeRefusal(id) {
    // Try each choice on the real mechanism; put it back after any accepted swap.
    const tried = SWAPS.map(choice => {
      const before = JSON.stringify(this.#m.snapshot()), input = this.#m.snapshot().angles.input;
      const result = this.#m.swap(choice.edit);
      if (JSON.stringify(this.#m.snapshot()) !== before) { this.#m.undo(); this.#m.turn(input); }
      return { ...choice, result };
    });
    const refused = tried.find(t => !t.result.ok), picked = tried.find(t => t.id === id);
    const correct = picked === refused;
    return { pickStep: 3, correct, feedback: `${correct ? 'Right' : `${picked.label} fits`}. `
      + `The frame refuses "${refused.label}": ${refused.result.reason}` };
  }

  view() {
    const s = this.#s, step = STEPS[s.step], result = s.results[s.step];
    const graded = Object.values(s.results), score = graded.filter(r => r.correct).length;
    const needsAnswer = step.choices && !s.picks[s.step], needsAction = step.action && !result;
    const done = s.step === STEPS.length - 1;
    return {
      step: s.step + 1, total: STEPS.length, prompt: step.prompt,
      choices: (step.choices || []).map(({ id, label }) => ({ id, label })),
      picked: s.picks[s.step] || null, action: needsAction && !needsAnswer ? step.action : null,
      correct: result ? result.correct : null,
      feedback: done ? `Score: ${score} of 3. ${RULE}` : result?.feedback || '',
      score, outOf: 3, done, canNext: !done && !needsAnswer && !needsAction, canUndo: this.#history.length > 0,
    };
  }
}
