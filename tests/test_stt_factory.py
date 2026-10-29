"""STT opt-in, offline cache and growing-buffer contracts without model loads."""
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from companion import voice


@pytest.fixture
def whisper(monkeypatch):
    calls = []

    class Model:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def transcribe(self, audio, **kwargs):
            calls.append((audio.copy(), kwargs))
            return iter([SimpleNamespace(text="  Hello, "), SimpleNamespace(text=" world!  ")]), None

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(voice, "get_settings", lambda: SimpleNamespace(stt_model="small"))
    return calls


def test_unset_is_byte_identical_whisper(monkeypatch, whisper):
    monkeypatch.delenv("KYRA_STT", raising=False)
    monkeypatch.delenv("KYRA_STT_LANGUAGE", raising=False)
    stt = voice.build_stt()
    assert isinstance(stt, voice.FasterWhisperSTT)
    assert whisper == [(("small",), {"device": "cpu", "compute_type": "int8"})]
    audio = np.array([0.25, -0.5], dtype=np.float64)
    assert stt.transcribe(audio).encode() == b"Hello, world!"
    np.testing.assert_array_equal(whisper[-1][0], audio.astype(np.float32))
    assert whisper[-1][1] == {"beam_size": 5}
    assert stt.transcribe(np.array([])) == ""
    assert len(whisper) == 2


@pytest.mark.parametrize("language", ["vi", "vi-VN", "Vietnamese"])
def test_vietnamese_routes_to_whisper(monkeypatch, whisper, language):
    monkeypatch.setenv("KYRA_STT", "parakeet")
    monkeypatch.setenv("KYRA_STT_LANGUAGE", language)
    monkeypatch.setattr(voice.ParakeetSTT, "__init__", lambda _: pytest.fail("must not load Parakeet"))
    stt = voice.build_stt()
    assert isinstance(stt, voice.FasterWhisperSTT)
    assert stt.transcribe_partial(np.ones(16, dtype=np.float32)) == "Hello, world!"


def test_other_engine_uses_whisper(monkeypatch, whisper):
    monkeypatch.setenv("KYRA_STT", "whisper")
    assert isinstance(voice.build_stt(), voice.FasterWhisperSTT)


@pytest.mark.parametrize("cache_number", [0, 1])
def test_offline_cache_and_partial_api(monkeypatch, tmp_path, cache_number):
    import huggingface_hub

    caches = [tmp_path / "hub", tmp_path / "other"]
    snapshot = caches[cache_number] / "snapshot"
    snapshot.mkdir(parents=True)
    for name in ("config.json", "model.safetensors"):
        (snapshot / name).write_text("fake")
    calls, buffers = [], []

    def resolve(repo, *, cache_dir, local_files_only):
        assert repo == voice.ParakeetSTT.MODEL
        assert local_files_only is True
        calls.append(cache_dir)
        if cache_dir == caches[cache_number]:
            return str(snapshot)
        raise huggingface_hub.errors.LocalEntryNotFoundError("not cached")

    def generate(audio):
        buffers.append(audio.copy())
        return SimpleNamespace(text=f"  hypothesis {len(audio)}  ")

    def load(path, **kwargs):
        assert path == snapshot
        assert kwargs == {"model_type": "parakeet"}
        return SimpleNamespace(generate=generate)

    monkeypatch.setattr(voice, "_stt_caches", lambda: caches)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", resolve)
    monkeypatch.setitem(sys.modules, "mlx.core", SimpleNamespace(array=np.asarray))
    monkeypatch.setitem(sys.modules, "mlx_audio.stt.utils", SimpleNamespace(load=load))
    monkeypatch.setenv("KYRA_STT", "parakeet")
    monkeypatch.setenv("KYRA_STT_LANGUAGE", "en")
    stt = voice.build_stt()
    assert isinstance(stt, voice.ParakeetSTT)
    assert stt.transcribe_partial(np.array([])) == ""
    assert not buffers
    audio = np.arange(8, dtype=np.float64)
    assert stt.transcribe_partial(audio[:4]) == "hypothesis 4"
    assert stt.transcribe(audio) == "hypothesis 8"
    assert calls == caches[:cache_number + 1]
    np.testing.assert_array_equal(buffers[0], audio[:4].astype(np.float32))
    np.testing.assert_array_equal(buffers[1], audio.astype(np.float32))
    assert buffers[0].dtype == np.float32


@pytest.mark.parametrize("incomplete", [False, True])
def test_missing_cache_refuses_before_backend_import(monkeypatch, tmp_path, incomplete):
    import huggingface_hub

    def resolve(*args, **kwargs):
        assert kwargs["local_files_only"] is True
        if incomplete:
            (tmp_path / "config.json").write_text("{}")
            return str(tmp_path)
        raise huggingface_hub.errors.LocalEntryNotFoundError("not cached")

    monkeypatch.setattr(voice, "_stt_caches", lambda: [tmp_path])
    monkeypatch.setattr(huggingface_hub, "snapshot_download", resolve)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt.utils", None)
    with pytest.raises(FileNotFoundError, match="cached"):
        voice.ParakeetSTT()
