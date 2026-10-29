"""Opt-in local voice prefetch. Speculation never executes tools or publishes text."""
import copy
import os
import re
from concurrent.futures import ThreadPoolExecutor


def enabled():
    return os.environ.get("KYRA_VOICE_PREFETCH", "").lower() in {"1", "true", "yes", "on"}


def speculative_ok(partial: str, final: str, pause: float) -> bool:
    """A punctuation-terminated clause, 400 ms silence, and fewer than 3 added words.

    Lexical stability is experimental, not proof of semantic equivalence.
    """
    def words(text):
        return re.findall(r"\w+", text.casefold())
    before, after = words(partial), words(final)
    return bool(before and partial.rstrip().endswith((".", "?", "!")) and pause >= .4
                and after[:len(before)] == before and 0 <= len(after) - len(before) < 3)


class PrefixCache:
    """Immutable warm snapshot; each generation gets its own exact-prefix KV copy."""

    def __init__(self, backend):
        self.backend = backend
        self.tokens, self.cache = [], None
        self.reused = 0

    def warm(self, system):
        from mlx_lm.models.cache import can_trim_prompt_cache, make_prompt_cache

        from companion.local_models import _prefill

        b = self.backend
        tokens = b._tokenizer.apply_chat_template([{"role": "system", "content": system}],
                                                 add_generation_prompt=False)
        cache = make_prompt_cache(b._model)
        if not can_trim_prompt_cache(cache):
            raise ValueError("Voice prefetch requires a trimmable KV model")
        _prefill(b._model, cache, tokens)
        self.tokens, self.cache = tokens, cache

    def suffix(self, prompt):
        from mlx_lm.models.cache import trim_prompt_cache

        from companion.local_models import _common_prefix

        self.reused = min(_common_prefix(self.tokens, prompt), len(prompt) - 1)
        if not self.reused:
            return prompt, {}
        cache = copy.deepcopy(self.cache)
        trim_prompt_cache(cache, len(self.tokens) - self.reused)
        return prompt[self.reused:], {"prompt_cache": cache}


class VoicePrefetch:
    def __init__(self, conversation, backend):
        self.conversation, self.backend = conversation, backend
        self.prefix = PrefixCache(backend)
        backend._voice_prefix = self.prefix
        self.partial_pack = None

    def warm(self):
        system, _, _ = self.conversation.assemble("", "voice", backend=self.backend).to_legacy()
        self.prefix.warm(system)

    def partial(self, text):
        if text.strip():
            self.partial_pack = self.conversation.assemble(text, "voice", backend=self.backend)
        return self.partial_pack

    def listen(self, listener, stt):
        """At most one growing-buffer STT job; never stall the microphone callback.

        Final assembly in handle_turn refreshes the pack. CLI speculation stays
        in the replay experiment until the lexical rule has quality evidence.
        """
        pending, last = None, 0
        with ThreadPoolExecutor(max_workers=1) as pool:
            def observe(audio):
                nonlocal pending, last
                if len(audio) - last >= 9600 and (pending is None or pending.done()):
                    if pending:
                        pending.result()
                    last = len(audio)
                    pending = pool.submit(lambda: self.partial(stt.transcribe(audio)))

            audio = listener.listen(on_audio=observe)
            if pending:
                pending.result()
        return audio
