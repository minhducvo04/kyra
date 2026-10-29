"""KYRA_MODELS_DIR (plan step 4.5): local model files come from <user-home>/kyra-models once it exists, else data/ as today.

Scratch HOME and data folders only; no model is loaded and nothing under the real data/ is touched.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from companion.settings import PROJECT_ROOT, Settings, get_settings


def build(tmp_path, monkeypatch, **env):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('KYRA_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.delenv('KYRA_MODELS_DIR', raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_before_the_move_models_stay_in_the_data_dir(tmp_path, monkeypatch):
    (tmp_path / 'home').mkdir()
    assert build(tmp_path, monkeypatch).models_dir == (tmp_path / 'data').resolve()


def test_after_the_move_models_come_from_home_kyra_models(tmp_path, monkeypatch):
    (tmp_path / 'home' / 'kyra-models').mkdir(parents=True)
    assert build(tmp_path, monkeypatch).models_dir == tmp_path / 'home' / 'kyra-models'


def test_env_wins_and_expands_home(tmp_path, monkeypatch):
    (tmp_path / 'home' / 'kyra-models').mkdir(parents=True)
    settings = build(tmp_path, monkeypatch, KYRA_MODELS_DIR='~/elsewhere')
    assert settings.models_dir == tmp_path / 'home' / 'elsewhere'


def test_local_llm_weights_load_from_the_models_dir(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != 'HF_HOME'}
    env.update(KYRA_DATA_DIR=str(tmp_path / 'data'), KYRA_MODELS_DIR=str(tmp_path / 'models'),
               PYTHONPATH=str(PROJECT_ROOT / 'src'))
    out = subprocess.run([sys.executable, '-c', 'import os, companion.llm; print(os.environ["HF_HOME"])'],
                         env=env, capture_output=True, text=True, check=True, timeout=120)
    assert out.stdout.strip() == str(tmp_path / 'models' / 'local_llm_models')


def test_kokoro_reads_its_files_from_the_models_dir(tmp_path, monkeypatch):
    pytest.importorskip('kokoro_onnx')
    from companion.voice import KokoroTTS
    monkeypatch.setenv('KYRA_MODELS_DIR', str(tmp_path / 'models'))
    get_settings.cache_clear()
    try:
        with pytest.raises(FileNotFoundError) as caught:  # scratch folder is empty, so the absolute paths are named
            KokoroTTS()
    finally:
        get_settings.cache_clear()
    message = str(caught.value)
    for name in ('kokoro-v1.0.onnx', 'voices-v1.0.bin'):
        assert str(Path(tmp_path / 'models' / 'voice_models' / name)) in message
