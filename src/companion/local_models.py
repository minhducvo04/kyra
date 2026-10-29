"""Offline specialist roster and a serial, private Unix-socket model manager.

No roster score means unmeasured, never permission to promote a worker. Adapters
and heads must already exist. This module does not train, download or run tools.
"""
from __future__ import annotations

import gc
import getpass
import importlib.util
import json
import math
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

RESIDENT_CAP = 8.0
QWEN7 = 'mlx-community/Qwen2.5-7B-Instruct-4bit'
BIG = 'mlx-community/Qwen3.6-35B-A3B-4bit'

# Exact repo ids only: unknown models retain their tokenizer's defaults.
GENERATION_SETTINGS = {
    **{f'mlx-community/{name}': {'thinking': 'off', 'template_kwargs': {'enable_thinking': False}}
       for name in ('Qwen3.5-9B-4bit', 'Qwen3.6-35B-A3B-4bit',
                    'gemma-4-12b-it-4bit', 'gemma-4-26b-a4b-it-4bit')},
    # GPT-OSS cannot disable reasoning; requesting final is not a leak guarantee.
    'mlx-community/gpt-oss-20b-MXFP4-Q4': {
        'thinking': 'low', 'template_kwargs': {'reasoning_effort': 'low'},
        'answer_prefix': '<|channel|>final<|message|>',
    },
}


def generation_settings(repo):
    """Return opt-in settings without changing any generation defaults otherwise."""
    if os.environ.get('KYRA_LOCAL_MODEL_SETTINGS') != '1':
        return {}
    return GENERATION_SETTINGS.get(repo, {})


class Refused(RuntimeError):
    pass


def available_budget_gb():
    """Benchmark allowance sampled before loading: available RAM less configured headroom (default 2 GiB)."""
    import psutil
    try:
        headroom = float(os.environ.get('KYRA_BENCH_HEADROOM_GB', '2.0'))
        if not math.isfinite(headroom) or headroom < 0:
            raise ValueError('headroom must be finite and non-negative')
        budget = psutil.virtual_memory().available / 1024**3 - headroom
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError('insufficient headroom')
        return budget
    except (OSError, ValueError) as exc:
        raise Refused('cannot determine available memory budget; check KYRA_BENCH_HEADROOM_GB') from exc


def data_root():
    if value := os.environ.get('KYRA_DATA_DIR'):
        return Path(value)
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                            capture_output=True, text=True, check=True)
    return Path(result.stdout.strip()).parent / 'data'


def initial_roster():
    rows = []
    for name, model, adapter, ram, mode in [
        *[(name, 'convaiinnovations/laya', f'decisions/heads/{name}', 1.2, 'resident')
          for name in ('request_triage', 'md_classification', 'file_triage')],
        ('dedupe_check', 'google/embeddinggemma-2', None, 1.2, 'resident'),
        ('log_reading', QWEN7, 'local-adapters/log_reading', 4.5, 'resident'),
        ('draft_writer', QWEN7, 'local-adapters/draft_writer', 4.5, 'resident'),
        ('local_engineer', BIG, None, 20, 'on_demand'),
        ('tiny_extraction_1.5b', 'mlx-community/Qwen2.5-1.5B-Instruct-4bit', None, 1.2, 'on_demand'),
        ('tiny_extraction_0.5b', 'mlx-community/Qwen2.5-0.5B-Instruct-4bit', None, .5, 'on_demand'),
    ]:
        rows.append(dict(id=name, **{'class': name}, base_model=model, adapter=adapter,
                         max_ram_gb=ram, mode=mode,
                         bench=dict(accuracy=None, p50_ms=None, peak_ram_gb=None, set=None, t=None)))
    return rows


def init_roster(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x') as handle:
            json.dump(initial_roster(), handle, indent=2)
    except FileExistsError:
        pass
    return json.loads(path.read_text())


def browser_record(root):
    try:
        from companion.browser_lock import read_lock
    except ImportError:  # base predates wait-free; same 90-minute/dead-pid rule
        try:
            row = json.loads((root / 'team/browser-phase.lock').read_text())
            pid = int(row['pid'])
            if pid <= 0 or time.time() - float(row['started']) >= 5400:
                return None
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return None
            except PermissionError:
                pass
            return row
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            return None
    return read_lock(root / 'team/browser-phase.lock')


def external_big_models():
    """Conservative local process observation, no commands or prompts returned."""
    import psutil
    found = []
    # Our own ancestors (the gate_lock.py run wrapper, the shells that launched it) carry the model id in argv and
    # hold no model: skip the whole chain, not only this pid (2026-10-15: the profiler refused itself).
    try:
        own = {os.getpid()} | {p.pid for p in psutil.Process().parents()}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        own = {os.getpid()}
    for proc in psutil.process_iter(['pid', 'cmdline', 'memory_info']):
        if proc.pid in own:
            continue
        try:
            cmd = ' '.join(proc.cmdline()).lower()
            model_process = any(word in cmd for word in ('mlx', 'ollama', 'llama', 'decision_worker', 'local_models'))
            named_big = any(size in cmd for size in ('35b', '27b', '26b', '31b', '14b', '12b', '20b', '32b', '70b'))
            rss = proc.memory_info().rss
            # A name alone is a launcher or a wrapper; a loaded 12B+ model is never under 2 GiB resident.
            if model_process and ((named_big and rss >= 2 * 1024**3) or rss >= 8 * 1024**3):
                found.append(proc.pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            try:
                if proc.username() != getpass.getuser():
                    continue
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
            raise Refused('cannot inspect another process for big-model exclusion') from None
    return found


@contextmanager
def ownership(root):
    """One manager or bench process per data root, including across worktrees."""
    import fcntl
    path = root / 'team/local-models.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('local model manager or bench already running') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Manager:
    def __init__(self, rows, loader, *, browser, external_big, peak_budget_gb=None):
        if peak_budget_gb is not None and (not math.isfinite(peak_budget_gb) or peak_budget_gb <= 0):
            raise Refused('invalid benchmark peak budget')
        self.peak_budget_gb = peak_budget_gb
        self.rows = {r['id']: r for r in rows}
        if len(self.rows) != len(rows):
            raise Refused('duplicate roster id')
        for row in rows:
            ram = row['max_ram_gb']
            if (not isinstance(ram, (int, float)) or not math.isfinite(ram) or ram <= 0
                    or ram > 24 or row['mode'] not in ('resident', 'on_demand')
                    or (row['mode'] == 'resident' and ram > RESIDENT_CAP)):
                raise Refused('invalid roster memory or mode')
        self.loader, self.browser, self.external_big = loader, browser, external_big
        self.loaded, self.unavailable = {}, {}
        self.lock = threading.RLock()

    def unload_all(self):
        for model in list(self.loaded):
            handle = self.loaded.pop(model)['handle']
            self.loader.unload(handle)
            del handle
        gc.collect()

    def status(self):
        with self.lock:
            return {'running': True, 'loaded': [dict(base_model=k, adapter=v['adapter'], max_ram_gb=v['ram'])
                                              for k, v in self.loaded.items()],
                    'ram_gb': self.loader.ram_gb(), 'resident_cap_gb': RESIDENT_CAP,
                    'unavailable': dict(self.unavailable)}

    def _use(self, row):
        model = row['base_model']
        ram = max(r['max_ram_gb'] for r in self.rows.values() if r['base_model'] == model)
        big = ram > RESIDENT_CAP
        if big:
            if lock := self.browser():
                raise Refused(f"browser phase running (train {lock['train']}, pid {lock['pid']})")
            if self.external_big() or any(v['ram'] > RESIDENT_CAP for v in self.loaded.values()):
                raise Refused('another big model is loaded')
            self.unload_all()  # 35B runs alone, never alongside residents
        elif any(v['ram'] > RESIDENT_CAP for v in self.loaded.values()):
            raise Refused('another big model is loaded')
        if model not in self.loaded:
            if not big and sum(v['ram'] for v in self.loaded.values()) + ram > RESIDENT_CAP:
                raise Refused('resident set would exceed 8 GB')
            handle = self.loader.load(row)
            self.loaded[model] = dict(handle=handle, adapter=None, ram=ram)
        entry = self.loaded[model]
        try:
            if entry['adapter'] != row['adapter']:
                self.loader.swap(entry['handle'], row)
                entry['adapter'] = row['adapter']
            if self.loader.ram_gb() > (self.peak_budget_gb if self.peak_budget_gb is not None else (24 if big else RESIDENT_CAP)):
                raise Refused('measured RAM exceeds model-set budget')
        except Exception:
            self.unload_all()
            raise
        return entry['handle']

    def warm(self):
        with self.lock:
            for row in self.rows.values():
                if row['mode'] == 'resident' and row.get('selected', True):
                    try:
                        self._use(row)
                    except (Refused, ImportError, OSError):
                        self.unavailable[row['id']] = 'local runtime, model, head or adapter unavailable'

    def request(self, agent, request):
        with self.lock:
            if agent not in self.rows:
                raise Refused('unknown roster agent')
            row = self.rows[agent]
            if row.get('selected') is False:
                raise Refused('candidate not selected by benchmark')
            try:
                handle = self._use(row)
                result = self.loader.answer(handle, row, request)
                if self.loader.ram_gb() > (self.peak_budget_gb if self.peak_budget_gb is not None else (24 if row['max_ram_gb'] > RESIDENT_CAP else RESIDENT_CAP)):
                    self.unload_all()
                    raise Refused('measured RAM exceeds model-set budget')
                return result
            finally:
                if row['mode'] == 'on_demand' and row['base_model'] in self.loaded:
                    self.loader.unload(self.loaded.pop(row['base_model'])['handle'])
                    gc.collect()


class OfflineLoader:
    def __init__(self, root, *, peak_budget_gb=None):
        self.root = root
        self.peak_budget_gb = peak_budget_gb

    def load(self, row):
        # Override rather than setdefault: a caller cannot accidentally enable downloads.
        os.environ['HF_HUB_OFFLINE'] = os.environ['TRANSFORMERS_OFFLINE'] = '1'
        from huggingface_hub import snapshot_download
        path = snapshot_download(row['base_model'], local_files_only=True, revision=row.get('revision'),
                                 ignore_patterns=['README.md', '.gitattributes'])
        import psutil
        if self.peak_budget_gb is None and psutil.virtual_memory().available < (row['max_ram_gb'] + 2) * 1024**3:
            raise Refused('insufficient free RAM for model and 2 GB reserve')
        if row['base_model'] == 'convaiinnovations/laya':
            spec = importlib.util.spec_from_file_location('local_decision_worker',
                        Path(__file__).resolve().parents[2] / 'scripts/decision_worker.py')
            worker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(worker)
            import laya
            agent = laya.load(path)
            return dict(kind='laya', model=agent, worker=worker, heads=worker.Heads(agent, self.root / 'decisions/heads'))
        if row['base_model'] == 'google/embeddinggemma-2':
            from sentence_transformers import SentenceTransformer
            return dict(kind='embed', model=SentenceTransformer(path, local_files_only=True))
        from mlx_lm import load
        model, tokenizer = load(path)
        return dict(kind='mlx', model=model, tokenizer=tokenizer)

    def swap(self, handle, row):
        if handle['kind'] == 'laya':
            handle['heads'].use(row['class'])
            return
        if handle['kind'] != 'mlx':
            return
        from mlx_lm.tuner.utils import load_adapters, remove_lora_layers
        model = remove_lora_layers(handle['model'])
        if row['adapter']:
            folder = (self.root / row['adapter']).resolve()
            if not folder.is_relative_to(self.root.resolve()):
                raise Refused('adapter must be inside local data')
            config = json.loads((folder / 'adapter_config.json').read_text())
            if config.get('fine_tune_type', 'lora') != 'lora':
                raise Refused('only reversible LoRA adapters are supported')
            load_adapters(model, str(folder))
        handle['model'] = model

    def answer(self, handle, row, request):
        text = request.get('text', '')
        if not isinstance(text, str) or len(text) > 32000:
            raise Refused('text must be a string of at most 32000 characters')
        if handle['kind'] == 'laya':
            return handle['worker'].answer(handle['model'], handle['heads'],
                   dict(family=row['class'], state=text, options=request.get('options')))
        if handle['kind'] == 'embed':
            other = request.get('other')
            threshold = request.get('threshold', .9)
            if not isinstance(other, str) or len(other) > 32000 or not 0 <= threshold <= 1:
                raise Refused('dedupe requires other text and a threshold in [0,1]')
            vectors = handle['model'].encode([text, other], normalize_embeddings=True)
            cosine = float(vectors[0] @ vectors[1])
            return dict(cosine=cosine, duplicate=cosine >= threshold)
        from mlx_lm import generate
        from mlx_lm.sample_utils import make_sampler
        messages = [{'role': 'user', 'content': text}]
        if request.get('system'):
            messages.insert(0, {'role': 'system', 'content': request['system']})
        prompt = handle['tokenizer'].apply_chat_template(messages,
                    tokenize=False, add_generation_prompt=True, enable_thinking=bool(row.get('thinking', False)))
        if request.get('benchmark'):
            max_tokens = request.get('max_tokens', 512)
            if not isinstance(max_tokens, int) or not 1 <= max_tokens <= 4096:
                raise Refused('benchmark token budget must be between 1 and 4096')
            from mlx_lm import stream_generate
            stop_when = request.get('stop_when')
            if stop_when is not None and not callable(stop_when):
                raise Refused('stop_when must be a callable on the text so far')
            state = request.get('prompt_cache')
            if state is None:
                # the same prefill grid as the cache path below: the two modes compute the same bits
                text_out, last, finish = _stream_until(stream_generate(
                    handle['model'], handle['tokenizer'], prompt=prompt, max_tokens=max_tokens,
                    sampler=make_sampler(temp=0), prefill_step_size=PREFILL_STEP), stop_when)
                return {'text': text_out, 'tokens': last.generation_tokens if last else 0, 'finish': finish}
            # L1: the caller's per-task prompt cache. The stable part of the prompt (the user content so far) is
            # prefilled onto the cache and snapshotted at each boundary the caller names (plus the content end);
            # generation runs on a working copy with the template tail.
            boundaries = request.get('cache_boundaries') or []
            if not isinstance(boundaries, list) or not all(isinstance(b, str) and text.startswith(b) for b in boundaries):
                raise Refused('cache_boundaries must be prefixes of text')
            # Each mark is the prompt cut at a boundary and continued with a newline and a non-blank character:
            # Qwen's template trims trailing whitespace from the content, so a bare newline would vanish and the
            # content-end mark would equal the prompt itself (the snapshot then swallowed the end-of-turn token and
            # never matched the next turn: 2026-10-15 run 2 reused the task prefix only).
            marks = [handle['tokenizer'].apply_chat_template(
                [dict(m, content=b + '\n' + CACHE_MARK) if m is messages[-1] else m for m in messages],
                tokenize=False, add_generation_prompt=True, enable_thinking=bool(row.get('thinking', False)))
                for b in [*boundaries, text]]
            tokens, reused, stable, cache, prefill_seconds = prompt_cache_prefill(handle, state, prompt, marks)
            text_out, last, finish = _stream_until(stream_generate(
                handle['model'], handle['tokenizer'], prompt=tokens[stable:], max_tokens=max_tokens,
                sampler=make_sampler(temp=0), prompt_cache=cache, prefill_step_size=PREFILL_STEP), stop_when)
            return {'text': text_out, 'tokens': last.generation_tokens if last else 0, 'finish': finish,
                    'prefilled_tokens': len(tokens) - reused, 'cache_reused_tokens': reused,
                    'prefill_seconds': prefill_seconds, 'cache_bytes': cache_bytes(state)}
        return {'text': generate(handle['model'], handle['tokenizer'], prompt=prompt,
                                max_tokens=512, sampler=make_sampler(temp=0), verbose=False,
                                prefill_step_size=PREFILL_STEP),
                'mentor_required': row['class'] == 'draft_writer'}

    def unload(self, handle):
        handle.clear()
        gc.collect()
        if 'mlx.core' in sys.modules:
            sys.modules['mlx.core'].clear_cache()
        if 'torch' in sys.modules and sys.modules['torch'].backends.mps.is_available():
            sys.modules['torch'].mps.empty_cache()

    def ram_gb(self):
        import psutil
        # Device allocations can overlap RSS on unified memory: do not sum them.
        try:
            rss = psutil.Process().memory_info().rss
        except Exception:
            return float('inf')  # Fail closed without killing the sampler thread.
        active = 0
        mx = sys.modules.get('mlx.core')
        for owner in (mx, getattr(mx, 'metal', None)):
            try:
                active = owner.get_active_memory()
                break
            except Exception:
                continue
        try:
            torch = sys.modules.get('torch')
            if torch.backends.mps.is_available():
                active = max(active, torch.mps.current_allocated_memory())
        except Exception:
            pass
        return round(max(rss, active) / 1024**3, 3)


def client(path, request):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(120)
        sock.connect(str(path))
        sock.sendall(json.dumps(request).encode() + b'\n')
        with sock.makefile('rb') as handle:
            return json.loads(handle.readline(1 << 20))


def serve(path, manager):
    class Handler(socketserver.StreamRequestHandler):
        timeout = 2

        def handle(self):
            try:
                line = self.rfile.readline(1 << 20)
                if not line.endswith(b'\n'):
                    return
                req = json.loads(line)
                if req.get('op') == 'status':
                    result = manager.status()
                elif req.get('op') == 'run':
                    result = manager.request(req.get('agent'), req)
                else:
                    raise Refused('unknown operation')
            except Refused as exc:
                result = {'error': str(exc)}
            except Exception:
                result = {'error': 'local runtime unavailable or invalid request'}
            self.wfile.write(json.dumps(result).encode() + b'\n')

    # Caller holds ownership; stale socket may be replaced, never an active manager.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = socketserver.UnixStreamServer(str(path), Handler)
    os.chmod(path, 0o600)
    return server


# ---- L1: KV prompt cache across the turns of one task -----------------------------------------------------------------
# The caller (the agent loop) owns a per-task ``state`` dict and passes it as request['prompt_cache']; it never lives
# on the loader or the model handle, so a task cannot see another task's context. mlx_lm's cache for a hybrid model
# (Qwen3.8: ArraysCache on the linear layers) cannot be trimmed, so the generated tokens can never be cut off a cache
# after a turn. Instead each turn prefills the prompt's stable part (the user content so far, before the template
# tail) onto the cache by hand, snapshots it at every boundary the caller names (the agent: the task prefix end
# and the pinned block end) and at the content end, and generates on a working copy. The next turn reuses the
# longest snapshot whose tokens are a prefix of the new prompt: the whole content when only the history grew, the
# task prefix when the pinned block changed. The KV rows are never copied: the attention layers are trimmable,
# so the live cache is trimmed back to the reused snapshot (the generated tokens and the rows after the point
# are dropped); only the untrimmable layers' state is copied per snapshot. The chain holds one snapshot per
# current boundary, so its size is bounded by the number of boundaries, not by the number of turns.
# Output parity (2026-10-15, scripts/probe_cache_parity.py, PARITY-RESULT.md): MLX's attention and small-chunk
# quantized matmul round differently for a different chunk geometry, so a chunk edge the plain prefill does not
# have moves the logits by a few bf16 ulps and a near-tied greedy token flips (L4 run 3: t4 diverged at turn 6).
# Both benchmark modes therefore prefill on one absolute grid of PREFILL_STEP tokens and a snapshot is taken only
# at the grid point at or before each boundary: every row is computed in the same chunk as in a fresh prompt and
# the cached turn computes the same bits; the price is re-prefilling the partial chunk before each boundary.
# The step is system-wide (the owner, 2026-10-15: "512 everywhere, no conflict"): every mlx_lm generate and
# stream_generate call in src/ passes prefill_step_size=PREFILL_STEP (benchmark, intern_bench, intern_shadow and the
# product paths in llm, local_tools, home_router, worker_loop and cad.propose), pinned by
# tests/test_prefill_step_everywhere.py. Prompts over 513 tokens round slightly differently than at mlx_lm's 2048
# default (about 5% of prefill rate; near-tie greedy tokens may flip against their pre-f065704a history).

PREFILL_STEP = 512  # the probe's knee (qwen3_5 9B): about 5% below the 2048 default's prefill rate; 256 cost 11%
PREFILL_CHUNK = PREFILL_STEP  # the earlier name, for outside readers; code and tests use PREFILL_STEP
CACHE_MARK = 'x'  # the non-blank character after a boundary's newline that survives the template's trim


def _common_prefix(a, b):
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def _snapshot(cache):
    """What restores the live cache to this point later: nothing for a trimmable layer (its KV rows up to the
    point stay valid and the layer is trimmed back), a copy of the state for the others (Qwen3.8's ArraysCache:
    the recurrent state at the point, a few MB per layer, never the KV rows)."""
    saved = []
    for layer in cache:
        if layer.is_trimmable():
            saved.append(None)
        else:
            value = layer.state
            saved.append(list(value) if isinstance(value, list) else value)
    return saved


def _restore(cache, saved, point):
    """Bring the live cache back to ``point`` tokens: trim the trimmable layers, reset the others' state."""
    for layer, value in zip(cache, saved, strict=True):
        if value is None:
            if layer.offset < point:
                raise Refused('prompt cache lost its prefix')
            layer.trim(layer.offset - point)
        else:
            layer.state = list(value) if isinstance(value, list) else value


def _array_bytes(value):
    if isinstance(value, (list, tuple)):
        return sum(_array_bytes(v) for v in value)
    return int(getattr(value, 'nbytes', 0) or 0)


def cache_bytes(state):
    """Bytes held by the task's live cache and its snapshots (the measured footprint the profiler records)."""
    live = sum(_array_bytes(layer.state) for layer in state.get('cache') or [])
    return live + sum(_array_bytes(entry['snapshot']) for entry in state.get('chain') or [])


STOP_RULE = 'stop_rule'  # the finish reason when the caller's stop_when ended decoding before EOS or the cap


def _stream_until(stream, stop_when=None):
    """(text, last response, finish) of a benchmark stream; ``stop_when(text so far)`` ends decoding early (the
    full_bench agent stops once its reply verdict is fixed, agent.reply_decided). The stream is closed either way."""
    pieces, last = [], None
    try:
        for last in stream:
            pieces.append(last.text)
            if stop_when is not None and getattr(last, 'finish_reason', None) is None and stop_when(''.join(pieces)):
                return ''.join(pieces), last, STOP_RULE
    finally:
        if hasattr(stream, 'close'):
            stream.close()
    return ''.join(pieces), last, getattr(last, 'finish_reason', None)


def _prefill(model, cache, tokens, offset=0):
    """Run the model over ``tokens`` into ``cache`` in chunks that end on absolute multiples of PREFILL_STEP
    (``offset`` is the position of tokens[0] in the prompt), the chunk edges mlx_lm's generate_step uses when it
    prefills a whole prompt from position 0 with prefill_step_size=PREFILL_STEP. With ``offset`` and the end on
    the grid (snapshot_points), every chunk is a chunk of the fresh prefill and the cache state is bit-identical."""
    import mlx.core as mx
    start = 0
    while start < len(tokens):
        end = min(len(tokens), start + PREFILL_STEP - (offset + start) % PREFILL_STEP)
        model(mx.array(tokens[start:end])[None], cache=cache)
        mx.eval([c.state for c in cache])
        start = end


PROMPT_CACHE_TOKENS = 14000  # the 32,000-character prompt cap at about 2.7 characters a token, plus the 2,048 answer


def prompt_cache_budget_gb(model_id, tokens=PROMPT_CACHE_TOKENS, snapshots=3):
    """The prompt cache's declared footprint for a model, from its config alone (no weights load): the KV rows of
    the attention layers at ``tokens`` (held once, counted twice for the transient slices mx.eval materialises),
    plus the untrimmable layers' recurrent state once per snapshot and once live. None when the config does not
    say enough; a dense model (no layer_types) counts every layer as attention."""
    os.environ['HF_HUB_OFFLINE'] = os.environ['TRANSFORMERS_OFFLINE'] = '1'
    from huggingface_hub import snapshot_download
    try:
        path = Path(snapshot_download(model_id, local_files_only=True, allow_patterns=['config.json']))
        config = json.loads((path / 'config.json').read_text())
    except Exception:
        return None
    text = config.get('text_config', config)
    try:
        layers = int(text['num_hidden_layers'])
        kv_heads, head_dim = int(text['num_key_value_heads']), int(text['head_dim'])
    except (KeyError, TypeError, ValueError):
        return None
    kinds = text.get('layer_types') or ['full_attention'] * layers
    attention = sum(1 for k in kinds if 'full' in str(k))
    linear = layers - attention
    kv_bytes = attention * 2 * kv_heads * head_dim * 2 * tokens
    state_bytes = 0
    if linear:
        try:
            heads, dk, dv = (int(text['linear_num_value_heads']), int(text['linear_key_head_dim']),
                             int(text['linear_value_head_dim']))
            keys, kernel = int(text['linear_num_key_heads']), int(text['linear_conv_kernel_dim'])
        except (KeyError, TypeError, ValueError):
            return None
        state_bytes = linear * (heads * dk * dv * 4 + (2 * keys * dk + heads * dv) * kernel * 2)
    return round((2 * kv_bytes + (snapshots + 1) * state_bytes) / 1024**3, 3)


def snapshot_points(tokens, marks):
    """Where the cache path snapshots: each mark's common token prefix with the prompt (never the whole prompt),
    rounded down to the PREFILL_STEP grid, so the hand prefill never ends a chunk where a fresh prefill would not."""
    points = {min(_common_prefix(tokens, mark), len(tokens) - 1) // PREFILL_STEP * PREFILL_STEP for mark in marks}
    return sorted(p for p in points if p > 0)


def prompt_cache_prefill(handle, state, prompt, marks):
    """(tokens, reused, stable, cache, seconds): the full prompt tokens, how many came from a snapshot, the
    prefilled length, the live cache and the hand prefill's wall seconds. Each mark is the prompt cut at a
    boundary and continued with a newline and CACHE_MARK inside the user content: its common token prefix with the
    prompt is where a snapshot is taken, always a token prefix of any prompt that continues the boundary with a
    newline (the last mark is the content end, where the template tail begins); snapshot_points puts it on the
    prefill grid. ``state['chain']`` holds the snapshots, shortest first; the KV rows live once, in
    ``state['cache']``, which generation extends and the next turn trims back to the snapshot it reuses."""
    from mlx_lm.models import cache as kv
    started = time.perf_counter()
    tokenizer, model = handle['tokenizer'], handle['model']
    tokens = list(tokenizer.encode(prompt))
    points = snapshot_points(tokens, [list(tokenizer.encode(mark)) for mark in marks])
    chain = (state.get('chain') or []) if state.get('model') == id(model) and state.get('cache') is not None else []
    kept = [entry for entry in chain if _common_prefix(entry['tokens'], tokens) == len(entry['tokens'])]
    if kept:
        cache, reused = state['cache'], len(kept[-1]['tokens'])
        _restore(cache, kept[-1]['snapshot'], reused)
    else:
        cache, reused = kv.make_prompt_cache(model), 0
    chain, stable = [entry for entry in kept if len(entry['tokens']) in points], reused
    for point in points:
        if point <= reused:
            continue
        _prefill(model, cache, tokens[stable:point], offset=stable)
        chain.append(dict(tokens=tokens[:point], snapshot=_snapshot(cache)))
        stable = point
    state.update(cache=cache, chain=chain, model=id(model))
    return tokens, reused, max(stable, points[-1] if points else 0), cache, time.perf_counter() - started
