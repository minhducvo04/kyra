"""Review 3 regressions: synthetic credentials, real temporary event files."""
import base64
import json
import random

import pytest

from companion.agent_control import Event, EventStore, scrub

PASSWORD = "fixturepw7"
# Deterministic random bytes, not a key; deliberately include base64 '/' and '+'.
PEM_BODY = base64.b64encode(random.Random(310).randbytes(96)).decode()
assert "/" in PEM_BODY and "+" in PEM_BODY


def assert_stored_scrubbed(tmp_path, text, secrets):
    store = EventStore(tmp_path)
    assert store.append(Event(ts=1767323045, source="git_diff", source_ref="fix3/1",
                              actor="worker", action="command", target=text))
    assert store.verify_chain().ok
    stored = store.read_events()[0].target
    disk = next(store.events_dir.glob("*.jsonl")).read_text()
    for secret in secrets:
        assert secret not in disk
        assert secret not in stored
    assert scrub(stored) == stored
    return stored


@pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED "])
@pytest.mark.parametrize("separator", [" ", "\n"])
def test_whole_private_key_block(kind, separator, tmp_path):
    block = (f"-----BEGIN {kind}PRIVATE KEY-----{separator}{PEM_BODY[:64]}"
             f"{separator}{PEM_BODY[64:]}{separator}-----END {kind}PRIVATE KEY-----")
    text = f"echo '{block}' > fixture.key"
    assert scrub(text) == "echo '[redacted:private_key]' > fixture.key"
    if separator == " ":
        assert_stored_scrubbed(tmp_path, text, [PEM_BODY[:64], PEM_BODY[64:]])


def test_unterminated_and_multiple_private_key_blocks():
    first = f"-----BEGIN PRIVATE KEY----- {PEM_BODY} -----END PRIVATE KEY-----"
    second = f"-----BEGIN OPENSSH PRIVATE KEY----- {PEM_BODY}"
    assert scrub(first + " between " + second) == (
        "[redacted:private_key] between [redacted:private_key]")


@pytest.mark.parametrize("name", [
    "PASS", "DB_PASS", "SMTP_PASS", "REDIS_PASS", "dbPass", "db-pass",
    "X-Amz-Signature", "sig", "Signature", "token",
])
def test_secret_names_across_assignments_dicts_and_urls(name, tmp_path):
    assert scrub({name: PASSWORD}) == {name: "[redacted:secret]"}
    assert_stored_scrubbed(tmp_path, f"{name}={PASSWORD}", [PASSWORD])
    result = scrub(f"https://host.example/o?{name}={PASSWORD}&region=west")
    assert PASSWORD not in result
    assert result.endswith("&region=west")


@pytest.mark.parametrize("query", [
    f"X-Amz-Signature={PASSWORD}&X-Amz-Credential=fixture",
    f"sig={PASSWORD}&token=otherfixture8",
    f"%73ig={PASSWORD}",
    f"X-Amz-%53ignature={PASSWORD}",
])
def test_signed_url_values_never_reach_disk(query, tmp_path):
    text = f"curl 'https://host.example/object?{query}&region=west' -o out.bin"
    stored = assert_stored_scrubbed(tmp_path, text, [PASSWORD, "otherfixture8"])
    assert stored.endswith("&region=west' -o out.bin")


@pytest.mark.parametrize("command", [
    f"mysql -uroot -p{PASSWORD} fixture",
    f"mysql --host localhost -p'{PASSWORD}' fixture",
    f"/usr/bin/mysql -p\"{PASSWORD}\" fixture",
    f"sshpass -p {PASSWORD} ssh host.example",
    f"sshpass -p'{PASSWORD}' ssh host.example",
    f"sshpass -p 'public {PASSWORD}' ssh host.example",
    f"curl -u fixture:{PASSWORD} https://host.example",
    f"curl -u 'fixture:{PASSWORD}' https://host.example",
    f"/usr/bin/curl -sS -ufixture:{PASSWORD} https://host.example",
    f"mysql -p'public'{PASSWORD} fixture",
    f"sshpass -p public\\ {PASSWORD} ssh host.example",
])
def test_short_cli_passwords_never_reach_disk(command, tmp_path):
    assert_stored_scrubbed(tmp_path, command, [PASSWORD])


@pytest.mark.parametrize("text", [
    'python -c "print({\\"token\\":\\"' + PASSWORD + '\\"})"',
    'python -c "print({\\"pass\\\\u0077ord\\":\\"' + PASSWORD + '\\"})"',
    json.dumps(json.dumps({"token": PASSWORD})),
    'python -c "print({\\"payload\\":{\\"PASS\\":\\"' + PASSWORD + '\\"}})"',
])
def test_escaped_json_without_named_wrapper(text, tmp_path):
    assert_stored_scrubbed(tmp_path, text, [PASSWORD])


@pytest.mark.parametrize("value", ["X", "Basic short", "Bearer short", "token short"])
def test_header_preserves_closing_quote_and_following_arguments(value, tmp_path):
    text = f"curl -H 'Authorization: {value}' h.example"
    stored = assert_stored_scrubbed(tmp_path, text, [value])
    assert stored == "curl -H 'Authorization: [redacted:auth]' h.example"


@pytest.mark.parametrize("text", [
    "bypass=true passthrough=enabled compass=north assignment=demo design=plain",
    "ssh -p 2222 host.example",
    "python -u script.py",
    "mysql -p fixture",
    "curl -u fixture https://host.example",
    "curl https://host.example/?region=west -o out.bin",
    '{"command":"python -u script.py","tokens_in":42}',
    "-----BEGIN PUBLIC KEY----- fixture -----END PUBLIC KEY-----",
])
def test_ordinary_commands_and_metadata_survive(text):
    assert scrub(text) == text


@pytest.mark.parametrize("text", [
    "curl -su fixture:fixturePass9 https://host.example",
    'python -c "print({\\"token\\":\\"fixturePass9\\"}); print(\'\\x1b[0m\')"',
    'Authorization: Digest username="fixture", realm="demo", nonce="fixturePass9", '
    'uri="/", response="0123456789abcdef0123456789abcdef", qop=auth',
])
def test_review_credential_variants_never_reach_disk(text, tmp_path):
    assert_stored_scrubbed(tmp_path, text, [
        "fixturePass9", "0123456789abcdef0123456789abcdef",
    ])


@pytest.mark.parametrize(("text", "expected"), [
    ("sshpass -p fixturePass9 ssh -p 2222 host.example",
     "sshpass -p [redacted:secret] ssh -p 2222 host.example"),
    ("sshpass -p fixturePass9 ssh host.example mysql -pfixturePass8",
     "sshpass -p [redacted:secret] ssh host.example mysql -p[redacted:secret]"),
    ("curl https://host.example/?sig=fixturePass9#section",
     "curl https://host.example/?sig=[redacted:secret]#section"),
    ("mysql --version\nssh -p2222 host.example", "mysql --version\nssh -p2222 host.example"),
    ("mysql --version|ssh -p2222 host.example", "mysql --version|ssh -p2222 host.example"),
])
def test_command_context_survives(text, expected):
    result = scrub(text)
    assert result == expected
    assert scrub(result) == result


@pytest.mark.parametrize("quote", ["'", '"'])
def test_digest_header_keeps_outer_quote_and_suffix(quote, tmp_path):
    header = 'Authorization: Digest username="fixture", nonce="fixturePass9", response="abc123"'
    if quote == '"':
        header = header.replace('"', '\\"')
    text = f"curl -H {quote}{header}{quote} https://host.example --head"
    result = assert_stored_scrubbed(tmp_path, text, ["fixturePass9", "abc123"])
    assert result == f"curl -H {quote}Authorization: [redacted:auth]{quote} https://host.example --head"
