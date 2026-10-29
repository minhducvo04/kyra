/* Explicit Guess on /model3d: provider=guess only when the user asks, the same body/health
   refusal as companion.model3d_guess.refuse, and no job is created when that fires. */
export const PLACEHOLDER = 'An electric kettle';
export const DISCLAIMER = ('Guessed by a model, not researched: the inner parts, sizes and layout are a plausible '
  + 'illustration and may not match the real object.');
export const REFUSAL = 'This demo guesses the inside of objects and machines; it does not model bodies or health.';

// Same words as src/companion/model3d_guess.py _BODY, plus the health-device cases that is_health holds there.
const BODY = /\b(?:(?:human|my|your|his|her|their|person'?s|man'?s|woman'?s|child'?s|baby'?s)\s+body|anatom\w*|organs?|heart|lungs?|liver|kidneys?|brain|stomach|intestines?|bowels?|bladder|uterus|womb|skeleton|bones?|muscles?|blood|veins?|arter(?:y|ies)|nerves?|pregnan\w*|fetus|foetus|tumou?rs?|cancer|disease|illness|symptoms?|diagnos\w*|medical|medicine|medication|drugs?|dose|health|injur(?:y|ies)|wound|pain|surgery|surgical|pacemaker|insulin|wheelchairs?)\b/i;

export function refuse(prompt) {
  return BODY.test(prompt || '') ? REFUSAL : null;
}

export function installGuess({ api, loadJobs, openJob, launchBusy, isBusy }) {
  const prompt = document.getElementById('prompt');
  const create = document.getElementById('create');
  const status = document.getElementById('create-status');
  if (prompt) prompt.placeholder = PLACEHOLDER;
  let guess = document.getElementById('guess');
  if (!guess && create) {
    guess = document.createElement('button');
    guess.id = 'guess';
    guess.type = 'submit';
    guess.className = 'quiet';
    guess.textContent = 'Guess';
    create.after(guess);
  }
  if (!guess || !status) return;
  guess.addEventListener('click', async event => {
    event.preventDefault();
    if (isBusy()) return;
    const text = (prompt?.value || '').trim();
    if (document.getElementById('image')?.files?.[0]) {
      status.textContent = 'Guess makes models from a text prompt only.';
      return;
    }
    if (!text) { status.textContent = 'Describe an object to guess.'; return; }
    const reason = refuse(text);
    if (reason) { status.textContent = reason; return; }
    launchBusy(true);
    status.textContent = 'Sending…';
    try {
      const body = await api('/api/model3d/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: text, provider: 'guess' }),
      });
      status.textContent = 'Started.';
      if (prompt) prompt.value = '';
      const image = document.getElementById('image');
      if (image) image.value = '';
      await loadJobs();
      await openJob(body.job_id);
    } catch (error) {
      status.textContent = error.status === 404
        ? 'The 3D jobs service is not on this server yet.'
        : `Could not start: ${error.message}`;
    } finally {
      launchBusy(false);
    }
  });
}
