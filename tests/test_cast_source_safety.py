"""Approvals keep their original classification across retries, reopens and upgrades."""
import hashlib
import json
import sqlite3

import pytest

from companion.cast_artifacts import ArtifactStore, declaration_label
from companion.outbound import ReleaseRefused
from companion.privacy import PrivacyClass, Tier

PNG = b'\x89PNG\r\n\x1a\n' + b'fixture'


def test_malformed_declarations_never_coerce_truthy_values():
    for source, declaration in [(None, None), ('other', {}), ('window', None),
                                ('window', dict(no_health=1, no_family=True, no_other_people=True)),
                                ('window', dict(no_health='true', no_family=True, no_other_people=True))]:
        assert declaration_label(source, declaration) == (Tier.T2, frozenset({PrivacyClass.unknown}))


def test_existing_unlabelled_approval_migrates_to_unknown(tmp_path):
    db = sqlite3.connect(tmp_path / 'state.sqlite3')
    db.execute('CREATE TABLE approvals (verifier TEXT PRIMARY KEY, freeze TEXT, digest TEXT, session TEXT, expires REAL)')
    db.execute('INSERT INTO approvals VALUES (?, ?, ?, ?, ?)',
               (hashlib.sha256(b'approval').hexdigest(), 'f', hashlib.sha256(PNG).hexdigest(), 'owner', 100))
    db.commit()
    db.close()
    store = ArtifactStore(tmp_path)
    with pytest.raises(ReleaseRefused):
        store.release('approval', PNG, 'f', model=lambda *args: pytest.fail('Must not call the model'), now=2)
    assert store.audit_rows() == [{'accepted': False, 'bytes': len(PNG), 'tier': 2, 'classes': ['unknown']}]


def test_label_survives_reopen_and_cannot_be_overridden_at_release(tmp_path):
    store = ArtifactStore(tmp_path)
    label = (Tier.T2, frozenset({PrivacyClass.unknown}))
    approval = store.approve('f', hashlib.sha256(PNG).hexdigest(), 'owner', now=1, label=label)
    fresh = ArtifactStore(tmp_path)
    with pytest.raises(TypeError):
        fresh.release(approval, PNG, 'f', label=(Tier.T0, frozenset()), model=lambda *a: '{}', now=2)
    with pytest.raises(ReleaseRefused):
        fresh.release(approval, PNG, 'f', model=lambda *args: pytest.fail('Must not call the model'), now=2)
    assert not list(tmp_path.glob('*.json'))
    assert 'unknown' in json.dumps(fresh.audit_rows())
