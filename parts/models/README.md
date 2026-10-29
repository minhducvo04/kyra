# Models and routing

**What it does.** Decides which model answers each turn, wraps hosted calls in a labelled and audited request gate, caps spend on calls no human asked for, and runs local models offline under a memory budget.

**Key idea.** Routing is layered from cheapest and most certain to least: an explicit override, then the session mode, then simple greetings, then a small local classifier. The router logs every decision with its reason, and a hosted request needs a release label and a gate before it can be sent.

In this package the default gate refuses every hosted send, because the private release policy is not included. Tests inject their own gate and check that the default makes no transport call.

## How it works

- **Turn router.** [`router.py`](../../src/companion/router.py) `TurnRouter` honours phrases such as "ask claude" or "use local", then a sticky session mode, then sends whole-message greetings and thanks to the local model. Otherwise a small local classifier (`Llama-3.2-3B-Instruct-4bit`) picks the text or tool path and the backend. Tool calls go to the hosted model. [`router_log.py`](../../src/companion/router_log.py) records each decision.
- **Hosted gate.** [`provider.py`](../../src/companion/provider.py) wraps hosted requests: each send needs a release label (a privacy tier and data classes) and passes the request to a gate before it goes out. [`outbound.py`](../../src/companion/outbound.py) is the public seam, and its default gate raises `ReleaseRefused`.
- **Spend cap.** [`usage.py`](../../src/companion/usage.py) writes one ledger line per paid call and refuses background calls once the day's estimated spend reaches `KYRA_API_DAILY_USD` (default 3.00). Calls a person started still go out, with a warning.
- **Local model manager.** [`local_models.py`](../../src/companion/local_models.py) keeps a roster of local specialists and serves them over a Unix socket. The loader forces offline mode, so it never downloads, and a resident model is capped at 8 GB.
- **Decision gates.** [`decide.py`](../../src/companion/decide.py) answers closed questions (route, rank, hold) from a backend's probabilities. Missing calibration always abstains, and it never treats confidence as authorization.
- **Model trials.** [`model_sandbox.py`](../../src/companion/model_sandbox.py) runs offline macOS model trials under a read deny list. Its docstring states that it is not a complete jail for hostile code.
- **Local task lane.** [`local_lane.py`](../../src/companion/local_lane.py) is a placeholder here: it raises an error saying the private local task catalogue is not included.

## Main files

| File | Purpose |
|---|---|
| [`router.py`](../../src/companion/router.py) | Layered turn router |
| [`router_log.py`](../../src/companion/router_log.py) | One log line per routing decision |
| [`provider.py`](../../src/companion/provider.py) | Labelled, audited hosted requests |
| [`outbound.py`](../../src/companion/outbound.py) | Public gate seam that refuses by default |
| [`usage.py`](../../src/companion/usage.py) | Paid call ledger and daily background cap |
| [`local_models.py`](../../src/companion/local_models.py) | Offline roster and model manager |
| [`decide.py`](../../src/companion/decide.py) | Model-free closed-question decisions |

## Tests

```sh
python -m pytest -q tests/test_router*.py tests/test_local_models.py tests/test_models_dir.py tests/test_decide.py tests/test_model_sandbox.py tests/test_keep_local_on_refusal.py tests/test_run_usage.py
```

Some router and local model tests need Apple MLX and downloaded weights; without them they skip.
