"""AC2: the event model, the hash-chained store and secret redaction.

Synthetic data only, under tmp_path; fake keys are built at runtime so no
key-shaped literal ever sits in a tracked file.
"""

import ast
import base64
import json
import multiprocessing
import warnings
from datetime import UTC, datetime
from pathlib import Path

import pytest

from companion.agent_control import Event, EventStore, register, scrub, unregister, validate_event
from companion.agent_control import events as event_model
from companion.agent_control import redact as redact_module
from companion.agent_control import store as store_module

TS = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()


def make_event(**overrides):
    fields = dict(
        ts=TS, source="git_diff", source_ref="session/example@0123abc:docs/x.md",
        run_id="demo-N1-20260101T000000-a1b2c3", feature="demo", node="N1",
        channel="muse:0", model="muse-wonder-full-1.0", actor="worker",
        action="file_write", target="docs/x.md", outcome="ok",
        attrs={"sha": "0123abc", "insertions": 10, "deletions": 2},
    )
    fields.update(overrides)
    return Event(**fields)


def fake_key(prefix):
    return prefix + base64.urlsafe_b64encode(bytes(range(24))).decode().rstrip("=")


SECRET = "hunter2hunter-xyz"


def _concurrent_worker(args):
    root, index, count = args
    store = EventStore(root)
    for n in range(count):
        store.append(make_event(source_ref=f"proc/{index}/{n}", target=f"docs/{index}-{n}.md"))


# -- the event model -------------------------------------------------------


def test_event_id_is_stable_and_derived_from_source_ref():
    event = make_event()
    assert event.event_id == event_model.make_event_id("git_diff", event.source_ref)
    assert make_event().event_id == event.event_id


@pytest.mark.parametrize("change", [
    {"action": "explode"}, {"source": "nowhere"}, {"actor": "nobody"}, {"outcome": "shrugged"},
])
def test_unknown_enums_are_refused(change):
    with pytest.raises(ValueError, match=next(iter(change))):
        make_event(**change)


def test_unknown_missing_and_mismatched_fields_are_refused():
    row = make_event().to_dict()
    with pytest.raises(ValueError, match="unknown field"):
        validate_event({**row, "surprise": 1})
    broken = dict(row)
    del broken["target"]
    with pytest.raises(ValueError, match="target"):
        validate_event(broken)
    with pytest.raises(ValueError, match="event_id"):
        validate_event({**row, "event_id": "f" * 32})
    with pytest.raises(ValueError, match="feature"):
        validate_event({**row, "run_id": None})


def test_field_shapes_are_refused():
    with pytest.raises(ValueError, match="target"):
        make_event(action="network", target="https://example.com/path?q=1")
    with pytest.raises(ValueError, match="tokens_in"):
        make_event(tokens_in=-1)
    with pytest.raises(ValueError, match="channel"):
        make_event(channel="smoke-signal:1")
    with pytest.raises(ValueError, match="cost_usd"):
        make_event(cost_usd=1.25)
    measured = make_event(cost_usd=1.25, attrs={"price_source": "usage-row-1"})
    assert measured.cost_usd == 1.25


def test_attrs_are_bounded():
    with pytest.raises(ValueError, match="unknown key"):
        make_event(attrs={"transcript": "words"})
    seventeen = dict.fromkeys(sorted(event_model.ATTRS_KEYS)[:17], 1)
    with pytest.raises(ValueError, match="at most 16"):
        make_event(attrs=seventeen)
    with pytest.raises(ValueError, match="serialized size"):
        make_event(attrs={"branch": "b" * 400, "worktree": "w" * 400, "legacy_kind": "k" * 400})
    with pytest.raises(ValueError, match="printable"):
        make_event(attrs={"branch": {"nested": True}})


def test_loop_level_event_has_null_run_fields():
    event = make_event(run_id=None, feature=None, node=None, channel=None, actor="dispatcher",
                       action="heartbeat", source="dispatcher_event", source_ref="tick#1")
    assert event.run_id is None


# -- redaction ---------------------------------------------------------------


@pytest.mark.parametrize("prefix,kind", [
    ("sk" + "-", "api_key"), ("xai" + "-", "api_key"), ("ghp" + "_", "github_token"),
])
def test_prefixed_keys_are_redacted(prefix, kind):
    key = fake_key(prefix)
    assert scrub(f"value {key} end") == f"value [redacted:{kind}] end"


def test_aws_bearer_and_assignment_secrets_are_redacted():
    aws = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
    assert scrub(aws) == "[redacted:aws_access_key]"
    assert scrub("Bearer " + fake_key("")) == "[redacted:bearer]"
    assert scrub({"access_token": "short"}) == {"access_token": "[redacted:secret]"}
    assert "hunter2hunter" not in scrub("password=hunter2hunter")


def test_high_entropy_is_redacted_but_hashes_and_paths_pass():
    token = fake_key("")
    assert scrub(token) == "[redacted:high_entropy]"
    digest = "0123456789abcdef" * 4
    assert scrub(digest) == digest
    assert scrub("src/companion/agent_control/store.py") == "src/companion/agent_control/store.py"
    original = {"branch": token}
    scrub(original)
    assert original["branch"] == token  # scrub never mutates its input


def test_no_code_path_opens_a_dotenv_file():
    for module in (event_model, store_module, redact_module):
        tree = ast.parse(Path(module.__file__).read_text())
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "open":
                constants = [node.value for node in ast.walk(call) if isinstance(node, ast.Constant)]
                assert not any(isinstance(value, str) and ".env" in value for value in constants)


# -- the store -----------------------------------------------------------------


def read_lines(store):
    path = store.events_dir / "2026-01-02.jsonl"
    return path, path.read_text().splitlines()


def test_append_writes_one_chained_line_per_event_in_a_utc_date_file(tmp_path):
    store = EventStore(tmp_path)
    assert store.append(make_event()) is True
    assert store.append(make_event(source_ref="session/example@0123abc:docs/y.md",
                                   target="docs/y.md")) is True
    path, lines = read_lines(store)
    assert path.parent == tmp_path / "agent_control" / "events"
    first, second = (json.loads(line) for line in lines)
    assert first["prev_hash"] == event_model.GENESIS
    assert second["prev_hash"] == first["hash"]
    assert store.verify_chain().ok
    assert [event.target for event in store.read_events()] == ["docs/x.md", "docs/y.md"]


def test_append_is_idempotent_on_source_and_ref(tmp_path):
    store = EventStore(tmp_path)
    assert store.append(make_event()) is True
    assert store.append(make_event()) is False
    assert len(store.read_events()) == 1


def test_store_scrubs_secrets_before_any_write(tmp_path):
    store = EventStore(tmp_path)
    key = fake_key("sk" + "-")
    assert store.append(make_event(attrs={"branch": key})) is True
    path, _lines = read_lines(store)
    assert key not in path.read_text()
    assert store.read_events()[0].attrs["branch"] == "[redacted:api_key]"


@pytest.mark.parametrize("tamper", ["modify", "remove", "reorder"])
def test_verify_chain_reports_the_first_bad_line(tmp_path, tamper):
    store = EventStore(tmp_path)
    for index in range(3):
        store.append(make_event(source_ref=f"session/example@0123abc:docs/{index}.md",
                                target=f"docs/{index}.md"))
    path, lines = read_lines(store)
    if tamper == "modify":
        row = json.loads(lines[1])
        row["outcome"] = "failed"
        lines[1] = json.dumps(row, sort_keys=True)
        expected_line = 2
    elif tamper == "remove":
        lines = [lines[0], lines[2]]
        expected_line = 2
    else:
        lines = [lines[0], lines[2], lines[1]]
        expected_line = 2
    path.write_text("\n".join(lines) + "\n")
    report = store.verify_chain()
    assert not report.ok
    assert report.file == "2026-01-02.jsonl"
    assert report.line == expected_line
    assert report.reason


def test_torn_last_line_is_skipped_with_one_warning_and_reads_survive(tmp_path):
    store = EventStore(tmp_path)
    store.append(make_event())
    path, _lines = read_lines(store)
    with open(path, "a") as handle:
        handle.write('{"event_id": "torn')
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        found = store.read_events()
    assert [event.target for event in found] == ["docs/x.md"]
    assert len([warning for warning in caught if "torn" in str(warning.message)]) == 1
    damaged = path.read_bytes()
    with pytest.raises(ValueError, match="integrity"):
        store.append(make_event(source_ref="session/example@0123abc:docs/z.md",
                                target="docs/z.md"))
    assert path.read_bytes() == damaged
    assert not store.verify_chain().ok


def test_adapter_registry_feeds_ingest_without_touching_the_store(tmp_path):
    register("claims", lambda: [make_event(source="claims", source_ref="team/claims.jsonl#1",
                                           actor="courier", action="dispatch")])
    try:
        store = EventStore(tmp_path)
        assert store.ingest() == 1
        assert store.ingest() == 0  # replay appends nothing
        with pytest.raises(ValueError, match="already registered"):
            register("claims", lambda: [])
    finally:
        unregister("claims")
    with pytest.raises(ValueError, match="unknown source"):
        register("nowhere", lambda: [])


# -- redaction: review findings 1 and 2 --------------------------------------


@pytest.mark.parametrize("text", [
    f"OPENAI_API_KEY={SECRET}", f"MY_TOKEN={SECRET}", f"db_password={SECRET}",
    f"DATABASE_PASSWORD={SECRET}", f"client_secret: {SECRET}", f"refreshToken={SECRET}",
    f"service_credential={SECRET}", f"AUTH_TOKEN={SECRET}", f"my_apikey={SECRET}",
    f"db_pwd={SECRET}", f"passwd={SECRET}", f"API_KEY={SECRET}", f"x-api-key: {SECRET}",
])
def test_prefixed_secret_names_in_assignments_are_redacted(text):
    out = scrub(text)
    assert SECRET not in out
    assert "[redacted:secret]" in out


@pytest.mark.parametrize("key", [
    "OPENAI_API_KEY", "MY_TOKEN", "db_password", "apikey", "clientSecret",
    "service_credential", "auth_token", "DB_PWD", "api_key", "Authorization",
])
def test_prefixed_secret_dict_keys_are_redacted(key):
    assert scrub({key: SECRET}) == {key: "[redacted:secret]"}


def test_secret_dict_key_with_non_string_value_is_redacted():
    assert scrub({"password": 12345678}) == {"password": "[redacted:secret]"}
    assert scrub({"token": {"nested": SECRET}}) == {"token": "[redacted:secret]"}


def test_token_count_fields_are_not_secrets():
    row = {"tokens_in": 42, "tokens_out": 7, "tokens_total": 49}
    assert scrub(row) == row


@pytest.mark.parametrize("text", [
    f"authentication={SECRET}", f"accesskey={SECRET}", f"passphrase={SECRET}",
    f"--client-secret={SECRET}",
])
def test_unlisted_secret_name_variants_are_redacted(text):
    out = scrub(text)
    assert SECRET not in out
    assert scrub(out) == out  # redaction is idempotent, never double-bracketed


@pytest.mark.parametrize("text", [
    f"password='{SECRET}'", f'password: "{SECRET}"', f"password=\"{SECRET}\"",
    f'{{"api_key": "{SECRET}"}}', f"OPENAI_API_KEY='{SECRET}'",
])
def test_quoted_secret_values_are_redacted(text):
    assert SECRET not in scrub(text)


@pytest.mark.parametrize("text", [
    f"run --password {SECRET}", f"run --token {SECRET}", f"run --api-key={SECRET}",
    f'run --password "{SECRET}"', f"run --db-password {SECRET} --verbose",
])
def test_cli_flag_secrets_are_redacted(text):
    assert SECRET not in scrub(text)


def test_authorization_basic_and_bearer_are_redacted():
    basic = base64.b64encode(f"bob:{SECRET}".encode()).decode()
    assert basic not in scrub(f"Authorization: Basic {basic}")
    assert SECRET not in scrub(f"Authorization: Bearer {SECRET}")
    assert basic not in scrub(f"authorization: basic {basic}")


def test_url_credentials_are_redacted():
    out = scrub(f"https://bob:{SECRET}@host.example/x")
    assert SECRET not in out
    assert "host.example" in out


@pytest.mark.parametrize("prefix", [
    "sk-", "hf_", "xoxb-", "xoxp-", "AIza", "gsk_", "sk_live_", "rk_live_",
    "ghp_", "github_pat_",
])
def test_known_key_shapes_are_redacted(prefix):
    key = fake_key(prefix)
    assert key not in scrub(f"value {key} end")


def test_slash_does_not_disable_entropy_scan_for_rest_of_string():
    token = fake_key("")
    assert token not in scrub(f"{token} uploaded to models/x")
    out = scrub(f"run scripts/run.py with {token}")
    assert token not in out
    assert "scripts/run.py" in out
    assert scrub("src/companion/agent_control/store.py") == "src/companion/agent_control/store.py"


# -- store: review findings 3, 4, 5 and 6 ------------------------------------


def test_secret_shaped_source_ref_keeps_identity_and_dedup(tmp_path):
    token = fake_key("")
    ref = f"codex-thread-{token}"
    store = EventStore(tmp_path)
    assert store.append(make_event(source_ref=ref)) is True
    assert store.append(make_event(source_ref=ref)) is False
    found = store.read_events()
    assert len(found) == 1
    assert found[0].event_id == event_model.make_event_id("git_diff", ref)
    path, _lines = read_lines(store)
    assert token not in path.read_text()
    assert store.verify_chain().ok


def test_refs_differing_only_in_secret_are_distinct_events(tmp_path):
    store = EventStore(tmp_path)
    assert store.append(make_event(source_ref=f"run token={SECRET}a")) is True
    assert store.append(make_event(source_ref=f"run token={SECRET}b")) is True
    found = store.read_events()
    assert len(found) == 2
    assert found[0].event_id != found[1].event_id
    path, _lines = read_lines(store)
    assert SECRET not in path.read_text()
    assert store.verify_chain().ok


def test_append_many_skips_invalid_and_reports_it(tmp_path):
    store = EventStore(tmp_path)
    good1 = make_event(source_ref="batch/1", target="docs/1.md").to_dict()
    good2 = make_event(source_ref="batch/2", target="docs/2.md").to_dict()
    bad = make_event(source_ref="batch/bad").to_dict()
    bad["action"] = "explode"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        appended = store.append_many([good1, bad, good2])
    assert appended == 2
    assert len(store.read_events()) == 2
    assert len(store.append_errors) == 1
    assert any("invalid event" in str(warning.message) for warning in caught)
    assert store.verify_chain().ok


def test_head_sidecar_records_count_and_hash(tmp_path):
    store = EventStore(tmp_path)
    store.append(make_event())
    head = json.loads((tmp_path / "agent_control" / "events.head").read_text())
    entry = head["files"]["2026-01-02.jsonl"]
    assert entry["count"] == 1
    assert entry["hash"] == store.read_events()[0].hash


def test_verify_chain_detects_deleted_last_line(tmp_path):
    store = EventStore(tmp_path)
    for index in range(3):
        store.append(make_event(source_ref=f"del/{index}", target=f"docs/{index}.md"))
    path, lines = read_lines(store)
    path.write_text("\n".join(lines[:-1]) + "\n")
    report = store.verify_chain()
    assert not report.ok
    assert report.reason


def test_verify_chain_detects_torn_last_line(tmp_path):
    store = EventStore(tmp_path)
    store.append(make_event(source_ref="torn/1", target="docs/1.md"))
    store.append(make_event(source_ref="torn/2", target="docs/2.md"))
    path, lines = read_lines(store)
    lines[-1] = lines[-1][:12]
    path.write_text("\n".join(lines))
    assert not store.verify_chain().ok


def test_verify_chain_detects_deleted_date_file(tmp_path):
    store = EventStore(tmp_path)
    store.append(make_event(source_ref="day/one", target="docs/1.md"))
    ts2 = datetime(2026, 1, 3, 3, 4, 5, tzinfo=UTC).timestamp()
    store.append(make_event(ts=ts2, source_ref="day/two", target="docs/2.md"))
    (store.events_dir / "2026-01-02.jsonl").unlink()
    report = store.verify_chain()
    assert not report.ok
    assert report.file == "2026-01-02.jsonl"


def test_tampered_middle_row_does_not_break_reads(tmp_path):
    store = EventStore(tmp_path)
    for index in range(3):
        store.append(make_event(source_ref=f"mid/{index}", target=f"docs/{index}.md"))
    path, lines = read_lines(store)
    row = json.loads(lines[1])
    row["outcome"] = "failed"
    lines[1] = json.dumps(row, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        found = store.read_events()
    assert [event.target for event in found] == ["docs/0.md", "docs/2.md"]
    assert not store.verify_chain().ok


def test_non_object_last_line_refuses_append_without_crashing_reads(tmp_path):
    store = EventStore(tmp_path)
    store.append(make_event(source_ref="obj/1", target="docs/1.md"))
    path, _lines = read_lines(store)
    with open(path, "a") as handle:
        handle.write("[1]\n")
    with pytest.raises(ValueError, match="integrity"):
        store.append(make_event(source_ref="obj/2", target="docs/2.md"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        found = store.read_events()
    assert [event.target for event in found] == ["docs/1.md"]
    assert not store.verify_chain().ok


def test_concurrent_appends_from_several_processes(tmp_path):
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_concurrent_worker, args=((str(tmp_path), i, 10),))
             for i in range(4)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
    assert [proc.exitcode for proc in procs] == [0, 0, 0, 0]
    store = EventStore(tmp_path)
    assert len(store.read_events()) == 40
    assert store.verify_chain().ok
