"""Review 4 regressions with synthetic credentials and real temporary stores."""
import base64
import json

import pytest

from companion.agent_control import Event, EventStore, scrub
from companion.agent_control import redact as redact_module

SECRET = "fixturePass9"
# Synthetic bytes, not a key. Slashes defeat the unrelated entropy fallback.
BODY = base64.b64encode(bytes(range(191, 255))).decode()
assert "/" in BODY and "+" in BODY


def stored_target(tmp_path, text, secrets=(SECRET,)):
    store = EventStore(tmp_path)
    event = Event(ts=1767323045, source="git_diff", source_ref="fix4/1",
                  actor="worker", action="command", target=text)
    assert store.append(event)
    reopened = EventStore(tmp_path)
    assert reopened.verify_chain().ok
    target = reopened.read_events()[0].target
    files = [path for path in store.root.rglob("*") if path.is_file()]
    assert files
    for secret in secrets:
        assert all(secret not in path.read_text() for path in files)
        assert secret not in target
    assert reopened.append(event) is False
    assert scrub(target) == target
    return target


@pytest.mark.parametrize("separator", [" ", r"\n"])
def test_pgp_private_key_block_never_reaches_store(tmp_path, separator):
    block = (f"-----BEGIN PGP PRIVATE KEY BLOCK-----{separator}{BODY}"
             f"{separator}-----END PGP PRIVATE KEY BLOCK-----")
    text = f"echo '{block}' | gpg --import"
    assert stored_target(tmp_path, text, (BODY,)) == (
        "echo '[redacted:private_key]' | gpg --import")


def test_truncated_pgp_block_never_reaches_store(tmp_path):
    text = f"-----BEGIN PGP PRIVATE KEY BLOCK----- {BODY}"
    assert stored_target(tmp_path, text, (BODY,)) == "[redacted:private_key]"


@pytest.mark.parametrize(("options", "expected"), [
    (f"--user fixture:{SECRET}", "--user [redacted:secret]"),
    (f"--user=fixture:{SECRET}", "--user=[redacted:secret]"),
    (f"--user 'fixture:{SECRET}'", "--user '[redacted:secret]'"),
    (f'--user="fixture:{SECRET}"', '--user="[redacted:secret]"'),
    (f"--proxy-user fixture:{SECRET}", "--proxy-user [redacted:secret]"),
    (f"--proxy-user=fixture:{SECRET}", "--proxy-user=[redacted:secret]"),
    (f"--proxy-user='fixture:{SECRET}'", "--proxy-user='[redacted:secret]'"),
    (f"-U fixture:{SECRET}", "-U [redacted:secret]"),
    (f"-Ufixture:{SECRET}", "-U[redacted:secret]"),
    (f"-sU'fixture:{SECRET}'", "-sU'[redacted:secret]'"),
    (f"--user fixture:public\\ {SECRET}", "--user [redacted:secret]"),
    (f'--user="fixture:public"{SECRET}', "--user=[redacted:secret]"),
    (f"--user 'fixture:curl mysql {SECRET}'", "--user '[redacted:secret]'"),
])
def test_curl_credentials_never_reach_store(tmp_path, options, expected):
    command = f"/usr/bin/curl {options} https://host.example --head"
    assert stored_target(tmp_path, command) == (
        f"/usr/bin/curl {expected} https://host.example --head")


@pytest.mark.parametrize("command", ["mysqldump", "mariadb", "mysqladmin"])
@pytest.mark.parametrize("value", [SECRET, f"'{SECRET}'", f'"public "{SECRET}'])
def test_mysql_sibling_passwords_never_reach_store(tmp_path, command, value):
    text = f"/usr/bin/{command} -uroot -p{value} fixture"
    target = stored_target(tmp_path, text)
    assert target.endswith(" fixture")
    assert "-uroot -p" in target


@pytest.mark.parametrize("name", ["X-Api-Key", "X-Token"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_secret_header_preserves_quote_and_following_arguments(tmp_path, name, quote):
    text = f"curl -H {quote}{name}: {SECRET}{quote} https://host.example -o out"
    assert stored_target(tmp_path, text) == (
        f"curl -H {quote}{name}: [redacted:secret]{quote} https://host.example -o out")


def test_secret_header_with_inner_quotes_keeps_context(tmp_path):
    text = f"""curl -H 'X-Api-Key: "public {SECRET}"' https://host.example --head"""
    assert stored_target(tmp_path, text) == (
        "curl -H 'X-Api-Key: [redacted:secret]' https://host.example --head")


@pytest.mark.parametrize("command", ["mysqldump", "mariadb", "mysqladmin"])
def test_mysql_sibling_bare_password_flag_is_a_prompt(tmp_path, command):
    text = f"{command} -p fixture"
    assert stored_target(tmp_path, text) == text


@pytest.mark.parametrize("text", [
    "tool --user fixture:role",
    "curl --user-agent fixture-browser https://host.example",
    "curl --user fixture https://host.example",
    "curl --proxy-user fixture https://host.example",
    "curl -U fixture https://host.example",
    "curl --user= https://host.example",
    "curl --proxy-user= https://host.example",
    "curl -H 'Content-Type: application/json' https://host.example",
    "-----BEGIN PGP PUBLIC KEY BLOCK----- fixture -----END PGP PUBLIC KEY BLOCK-----",
])
def test_noncredential_context_survives_store(tmp_path, text):
    assert stored_target(tmp_path, text) == text


def test_nested_json_curl_credentials_remain_decodable(tmp_path):
    text = json.dumps({"command": f"curl --user fixture:{SECRET} https://host.example"})
    result = stored_target(tmp_path, text)
    assert json.loads(result) == {
        "command": "curl --user [redacted:secret] https://host.example",
    }


@pytest.mark.parametrize("argument", [
    """--data '{"command":"mysql status"}'""",
    """-H 'X-Trace: mysql status'""",
    "mysql",
    "--url mysql",
])
def test_command_words_inside_arguments_do_not_hide_later_credentials(tmp_path, argument):
    text = f"curl {argument} --user fixture:{SECRET} https://host.example"
    assert stored_target(tmp_path, text) == (
        f"curl {argument} --user [redacted:secret] https://host.example")


@pytest.mark.parametrize("pattern", ["curl ", "mysql -p "])
def test_repeated_commands_have_linear_token_scan_work(monkeypatch, pattern):
    # Count token scans, not wall time: this exposes the review's repeated-tail
    # work without depending on host load or building a benchmark harness.
    original = redact_module._value_end
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(redact_module, "_value_end", counted)
    repetitions = 240
    text = pattern * repetitions
    assert scrub(text) == text
    assert calls <= 4 * repetitions, f"{calls} token scans for {repetitions} command words"
