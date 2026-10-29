"""Real Seatbelt acceptance: absent binary skips; nested sandbox failure FAILS."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from companion.worker_sandbox import profile, worker_environment, wrap


@pytest.mark.parametrize("own_root", ["repo", "grok"])
def test_profile_denies_existing_siblings_without_denying_parents(tmp_path, own_root):
    home = tmp_path / "home"
    main = home / "repo"
    roots = {"repo": main / ".claude/worktrees", "grok": home / "kyra-grok"}
    own = roots[own_root] / "own"
    own.mkdir(parents=True)
    siblings = [root / "other" for root in roots.values()]
    for sibling in siblings:
        sibling.mkdir(parents=True)
    alias_target = home / "aliased-worktree"
    alias_target.mkdir()
    (roots["repo"] / "alias").symlink_to(alias_target, target_is_directory=True)
    (own / "data").symlink_to(main / "data", target_is_directory=True)
    policy = profile(own, main, home)
    assert policy == profile(own, main, home, research=True)
    for sibling in [*siblings, roots["repo"] / "alias", alias_target]:
        assert f"(deny file-read* file-write* (subpath {json.dumps(str(sibling))}))" in policy
    for root in roots.values():
        assert f"(subpath {json.dumps(str(root))})" not in policy
        assert f"(allow file-read-metadata file-read-data (literal {json.dumps(str(root))}))" in policy
    assert f"(deny file-read* file-write* (subpath {json.dumps(str(own))}))" not in policy
    for data in [main / "data", own / "data", *(sibling / "data" for sibling in siblings)]:
        assert f"(deny file-read* file-write* (subpath {json.dumps(str(data))}))" in policy
    # Enumeration is a launch-time snapshot; a fresh launch covers new siblings.
    later = roots[own_root] / "later"
    later.mkdir()
    assert str(later) not in policy
    assert f"(deny file-read* file-write* (subpath {json.dumps(str(later))}))" in profile(own, main, home)


def test_profile_denies_main_checkout_writes_except_own_tree(tmp_path):
    home = tmp_path / "home"
    main = home / "repo"
    git = main / ".git"
    own = main / ".claude/worktrees/own"
    for path in (own, git / "hooks", git / "info", git / "objects", git / "refs", git / "logs",
                 git / "worktrees/own", git / "worktrees/other", home / "elsewhere"):
        path.mkdir(parents=True)
    (git / "config").write_text("[core]\n")
    (own / ".git").write_text(f"gitdir: {git / 'worktrees/own'}\n")
    (git / "worktrees/alias").symlink_to(home / "elsewhere", target_is_directory=True)
    rules = profile(own, main, home).splitlines()

    def at(rule):
        assert rule in rules, rule
        return rules.index(rule)

    broad = at(f"(deny file-write* (subpath {json.dumps(str(main))}))")
    allowed = at(f"(allow file-write* (subpath {json.dumps(str(own))}))")
    git_denied = at(f"(deny file-write* (subpath {json.dumps(str(git))}))")
    # Last match wins: shared Git, own data and .env remain denied.
    private = [at(f"(deny file-read* file-write* (subpath {json.dumps(str(own / 'data'))}))"),
               at('(deny file-read* file-write* (regex #"(^|/)\\.env(\\.[^/]*)?(/|$)"))')]
    assert at("(deny file-write*)") < broad < allowed < git_denied
    assert allowed < min(private)
    for path in (git, git / "objects", git / "refs", git / "logs",
                 git / "packed-refs", git / "worktrees", git / "worktrees/own", git / "worktrees/other"):
        assert f"(allow file-write* (subpath {json.dumps(str(path))}))" not in rules
        assert f"(allow file-write* (literal {json.dumps(str(path))}))" not in rules


@pytest.fixture
def linked_repo(tmp_path):
    if not shutil.which("sandbox-exec"):
        pytest.skip("sandbox-exec is absent")
    home = tmp_path / "home"
    main = home / "repo"
    env = {**worker_environment(), "HOME": str(home)}
    identity = ["-c", "user.name=Alex Rivera", "-c", "user.email=alex@example.invalid"]
    (home / ".ssh").mkdir(parents=True)

    def git(*args, check=True, policy=None):
        prefix = [shutil.which("sandbox-exec"), "-f", str(policy)] if policy else []
        return subprocess.run([*prefix, "git", *identity, *args], env=env, check=check,
                              capture_output=True, text=True, timeout=30)

    git("init", str(main))
    (main / "README").write_text("fixture\n")
    git("-C", str(main), "add", "README")
    git("-C", str(main), "commit", "-m", "fixture")
    own, other = main / ".claude/worktrees/own", main / ".claude/worktrees/other"
    git("-C", str(main), "worktree", "add", "-b", "own", str(own))
    git("-C", str(main), "worktree", "add", "-b", "other", str(other))
    policy = tmp_path / "worker.sb"
    policy.write_text(profile(own, main, home))
    return main, own, policy, git, env


def test_wall_denies_writes_to_the_shared_git_config_hooks_and_main_tree(linked_repo):
    main, own, policy, git, env = linked_repo
    git_dir = main / ".git"
    config, readme = (git_dir / "config").read_text(), (main / "README").read_text()

    def sh(script):
        return subprocess.run([shutil.which("sandbox-exec"), "-f", str(policy), "/bin/sh", "-c", script],
                              cwd=own, env=env, capture_output=True, text=True, timeout=30)

    commit = git("-C", str(own), "commit", "--allow-empty", "-m", "x", check=False, policy=policy)
    hooks_path = git("-C", str(own), "config", "core.hooksPath", "x", check=False, policy=policy)
    hook = sh(f'printf "#!/bin/sh\\n" > "{git_dir / "hooks/x"}"')
    tree = sh(f'printf overwritten > "{main / "README"}"')
    print(f"live exit codes: commit={commit.returncode} config={hooks_path.returncode} "
          f"hook={hook.returncode} readme={tree.returncode}")
    assert commit.returncode != 0 and "Operation not permitted" in commit.stderr
    assert git("-C", str(own), "log", "-1", "--format=%s").stdout.strip() == "fixture"
    assert hooks_path.returncode != 0 and "core.hooksPath" not in (git_dir / "config").read_text()
    assert (git_dir / "config").read_text() == config
    assert hook.returncode != 0 and "Operation not permitted" in hook.stderr
    assert not (git_dir / "hooks/x").exists()
    assert tree.returncode != 0 and "Operation not permitted" in tree.stderr
    assert (main / "README").read_text() == readme
    for target in (main / "src.py", git_dir / "info/attributes", git_dir / "packed-refs",
                   git_dir / "worktrees/other/HEAD", main / ".claude/worktrees/other/README"):
        result = sh(f'printf x >> "{target}"')
        assert result.returncode != 0 and "Operation not permitted" in result.stderr, target
    assert (own / "README").exists() and sh("printf ok > own-file").returncode == 0


def test_ls_shared_git_is_outside_the_write_allowlist(linked_repo):
    main, own, policy, git, env = linked_repo
    for path in (main / ".git/objects/probe", main / ".git/refs/heads/probe",
                 main / ".git/logs/probe", main / ".git/worktrees/own/probe"):
        result = subprocess.run([shutil.which("sandbox-exec"), "-f", str(policy), "/bin/sh", "-c",
                                 'printf x > "$1"', "probe", str(path)],
                                cwd=own, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode != 0 and "Operation not permitted" in result.stderr, path
        assert not path.exists()


@pytest.mark.parametrize("root", ["worktree", "temp", "grok", "grok-auth", "grok-lock", "muse"])
def test_ls_profile_refuses_inherited_hardlinks(tmp_path, root):
    import urllib.parse

    from companion.worker_sandbox import MUSE_CONFIG

    home, main, own, temp = (tmp_path / p for p in ("home", "main", "own", "temp"))
    sessions = home / ".grok/sessions" / urllib.parse.quote(str(own), safe="")
    auth = home / MUSE_CONFIG / "muse/auth.json"
    target = {"worktree": own / "alias", "temp": temp / "alias",
              "grok": sessions / "alias", "grok-auth": home / ".grok/auth.json",
              "grok-lock": home / ".grok/auth.json.lock", "muse": auth}[root]
    for path in (home, own, temp, target.parent):
        path.mkdir(parents=True, exist_ok=True)
    kind = "muse" if root == "muse" else "grok"
    profile(own, main, home, kind=kind, temp_dir=temp)  # Ordinary roots still work.
    outside = home / "outside"
    outside.write_text("synthetic outside file")
    target.hardlink_to(outside)
    with pytest.raises(SystemExit, match="hardlink"):
        profile(own, main, home, kind=kind, temp_dir=temp)
    assert outside.read_text() == "synthetic outside file"
    target.unlink()
    profile(own, main, home, kind=kind, temp_dir=temp)


def test_ls_grok_hashed_session_path_fails_before_launch(tmp_path):
    own = tmp_path / ("x" * 230)
    own.mkdir()
    with pytest.raises(SystemExit, match="shorter worktree path"):
        profile(own, tmp_path / "main", tmp_path / "home", kind="grok")


def test_grok_profile_allows_only_the_auth_files(tmp_path):
    import urllib.parse

    from companion.worker_sandbox import MUSE_CONFIG

    home, main, own = tmp_path / "home", tmp_path / "main", tmp_path / "own"
    own.mkdir()
    grok = profile(own, main, home, kind="grok")
    muse = profile(own, main, home, kind="muse")
    resolved = home.resolve()
    auth = resolved / ".grok" / "auth.json"
    lock = resolved / ".grok" / "auth.json.lock"
    muse_auth = resolved / MUSE_CONFIG / "muse/auth.json"

    def write_literals(policy):
        return [line for line in policy.splitlines() if line.startswith("(allow file-write* (literal ")]

    auth_rule = f"(allow file-write* (literal {json.dumps(str(auth))}))"
    lock_rule = f"(allow file-write* (literal {json.dumps(str(lock))}))"
    muse_rule = f"(allow file-write* (literal {json.dumps(str(muse_auth))}))"
    assert write_literals(grok) == ['(allow file-write* (literal "/dev/null"))', auth_rule, lock_rule]
    assert [line for line in grok.splitlines() if "/.grok/" in line and "(literal " in line] == [auth_rule, lock_rule]
    assert write_literals(muse) == ['(allow file-write* (literal "/dev/null"))', muse_rule]
    assert "/.grok/" not in muse
    group = urllib.parse.quote(str(own.resolve()), safe="")
    sessions = resolved / ".grok/sessions" / group
    assert f"(allow file-write* (subpath {json.dumps(str(sessions))}))" in grok
    assert f"(allow file-write* (subpath {json.dumps(str(resolved / '.grok'))}))" not in grok
    for name in ("config.toml", "managed_config.toml", "requirements.toml", "sandbox.toml",
                 "trusted_folders.toml", "hooks", "hooks-paths"):
        path = json.dumps(str(resolved / ".grok" / name))
        assert f"(allow file-write* (literal {path}))" not in grok
        assert f"(allow file-write* (subpath {path}))" not in grok


@pytest.fixture
def wall(tmp_path):
    if not shutil.which("sandbox-exec"):
        pytest.skip("sandbox-exec is absent")
    home = tmp_path / "home"
    main = home / "repo"
    own = main / ".claude/worktrees/own"
    other = main / ".claude/worktrees/other"
    outside = home / "kyra-grok/other"
    for path in (own, other, outside, main / "data/team", home / ".ssh", home / "Library/Keychains"):
        path.mkdir(parents=True)
    for path in (main / "data/team/config.json", own / ".env", own / ".env.local",
                 home / ".ssh/id_ed25519", home / "Library/Keychains/scratch", other / "private",
                 outside / "private"):
        path.write_text("synthetic fixture\n")
    (own / "data").symlink_to(main / "data", target_is_directory=True)
    (main / ".claude/data").symlink_to(main / "data", target_is_directory=True)
    (own / "alias").symlink_to(other / "private")
    policy = tmp_path / "worker.sb"
    policy.write_text(profile(own, main, home))

    def run(command, *, research=False):
        policy.write_text(profile(own, main, home, research=research))
        return subprocess.run([shutil.which("sandbox-exec"), "-f", str(policy), *command],
                              cwd=own, env={**worker_environment(), "HOME": str(home)},
                              capture_output=True, text=True, timeout=30)

    # Positive control rules out a broken profile or a forbidden nested sandbox.
    control = run(["/usr/bin/true"])
    assert control.returncode == 0, f"Seatbelt cannot start; Board must run outside Codex: {control.stderr}"
    return own, main, home, other, run


@pytest.mark.parametrize("target", ["../../data/team/config.json", "data/team/config.json", ".env", ".env.local",
                                    "$HOME/.ssh/id_ed25519", "$HOME/Library/Keychains/scratch",
                                    "../other/private", "$HOME/kyra-grok/other/private", "alias"])
def test_real_os_denies_private_reads(wall, target):
    own, main, home, other, run = wall
    result = run(["/bin/sh", "-c", f'cat "{target}"'])
    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr, result.stderr
    assert "synthetic fixture" not in result.stdout


@pytest.mark.parametrize("target", [".env", "data/team/config.json", "../other/private"])
def test_real_os_denies_private_writes(wall, target):
    *_, run = wall
    result = run(["/bin/sh", "-c", f'printf overwritten > "{target}"'])
    assert result.returncode != 0 and "Operation not permitted" in result.stderr


@pytest.mark.parametrize("target", ["x", ".zshrc", "Library/LaunchAgents/x", ".claude/x",
                                   ".grok/config.toml", ".grok/hooks/x", "Projects/x"])
def test_ls_real_wall_denies_home_writes_with_worktree_control(wall, target):
    own, main, home, other, run = wall
    path = home / target
    path.parent.mkdir(parents=True, exist_ok=True)
    allowed = run(["/bin/sh", "-c", "printf ok > allowed"])
    assert allowed.returncode == 0 and (own / "allowed").read_text() == "ok"
    denied = run(["/bin/sh", "-c", 'printf x > "$1"', "probe", str(path)])
    assert denied.returncode != 0 and "Operation not permitted" in denied.stderr
    assert not path.exists()


@pytest.mark.parametrize("kind", ["grok", "muse"])
def test_ls_real_wall_allows_only_own_temp_and_provider_state(tmp_path, kind):
    if not shutil.which("sandbox-exec"):
        pytest.skip("sandbox-exec is absent")
    import urllib.parse

    from companion.worker_sandbox import MUSE_CONFIG

    home, main, own = tmp_path / "home", tmp_path / "main", tmp_path / "worktree"
    own.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / f"{kind}.log"
    sessions = home / ".grok/sessions" / urllib.parse.quote(str(own), safe="")
    auth = home / MUSE_CONFIG / "muse"
    for path in (sessions, sessions.parent / "other-worktree", auth / "skills", home / ".claude",
                 home / ".grok/hooks", home / "Library/LaunchAgents", tmp_path / "another-run"):
        path.mkdir(parents=True, exist_ok=True)
    command = wrap([sys.executable, "-c", """
import json, os
from pathlib import Path
out = {}
for label, path in json.loads(os.environ['PROBE_PATHS']).items():
    try:
        Path(path).write_text('synthetic probe')
        out[label] = 'allowed'
    except PermissionError:
        out[label] = 'denied'
print(json.dumps(out))
"""], log, own, main, kind=kind, home=home)
    (own / "home-link").symlink_to(home, target_is_directory=True)
    paths = {"worktree": own / "x", "temp": log.with_suffix(".tmp") / "x",
             "home": home / "x", "launchagent": home / "Library/LaunchAgents/x",
             "claude": home / ".claude/x", "hooks": home / ".grok/hooks/x",
             "alias": own / "home-link/x", "other-temp": tmp_path / "another-run/x",
             "log": log, "profile": log.with_suffix(".sb"),
             "grok-own": sessions / "x", "grok-other": sessions.parent / "other-worktree/x",
             "muse-auth": auth / "auth.json", "muse-settings": auth / "settings.json",
             "muse-trust": auth / "trust.json", "muse-skill": auth / "skills/x"}
    env = dict(worker_environment(kind=kind), HOME=str(home), TMPDIR=str(log.with_suffix(".tmp")),
               PROBE_PATHS=json.dumps({key: str(path) for key, path in paths.items()}))
    result = subprocess.run(command, cwd=own, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    allowed = {"worktree", "temp", "grok-own" if kind == "grok" else "muse-auth"}
    assert observed == {key: "allowed" if key in allowed else "denied" for key in paths}, observed


def test_real_os_build_writes_and_tests_but_leaves_shared_git_read_only(wall):
    own, main, home, other, run = wall
    # Real linked-worktree build/test succeeds; shared Git mutations are refused.
    env = {**worker_environment(), "HOME": str(home)}
    for args in (["init", str(main)], ["-C", str(main), "-c", "user.name=Alex Rivera",
                 "-c", "user.email=alex@example.invalid", "commit", "--allow-empty", "-m", "fixture"]):
        subprocess.run(["git", *args], env=env, check=True, capture_output=True)
    linked = main / ".claude/worktrees/build"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-b", "build", str(linked)],
                   env=env, check=True, capture_output=True)
    policy = main.parent / "build.sb"
    temp = main.parent / "build-tmp"
    temp.mkdir()
    env["TMPDIR"] = str(temp)
    policy.write_text(profile(linked, main, home, temp_dir=temp))
    script = ('printf "def test_build():\\n    assert 2 + 2 == 4\\n" > test_build.py\n'
              '"$1" -m venv --without-pip .venv\n'
              '"$1" -m pytest -q test_build.py\n'
              'if git add test_build.py; then exit 99; fi\n'
              'if git -c user.name="Alex Rivera" -c user.email=alex@example.invalid commit '
              '--allow-empty -m "sandbox build"; then exit 98; fi\n')
    result = subprocess.run([shutil.which("sandbox-exec"), "-f", str(policy), "/bin/sh", "-ec", script,
                             "build", sys.executable], cwd=linked, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout and (linked / ".venv/bin/python").exists()
    assert subprocess.check_output(["git", "-C", str(linked), "log", "-1", "--format=%s"],
                                   env=env, text=True).strip() == "fixture"


@pytest.mark.parametrize("research", [False, True])
@pytest.mark.parametrize("code", [
    "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0))",
    "import socket; s=socket.socket(type=socket.SOCK_DGRAM); s.sendto(b'fixture', ('127.0.0.1', 443))",
])
def test_real_os_denies_bind_and_udp(wall, research, code):
    *_, run = wall
    denied = run([sys.executable, "-c", code], research=research)
    assert denied.returncode != 0 and "Operation not permitted" in denied.stderr


@pytest.mark.parametrize("research", [False, True])
def test_real_os_denies_plain_http(wall, research):
    *_, run = wall
    # Numeric loopback needs neither DNS nor a live network to prove the denial.
    result = run(["/usr/bin/curl", "-q", "--noproxy", "*", "-v", "--connect-timeout", "5",
                  "http://127.0.0.1:80/"], research=research)
    assert result.returncode != 0 and "Operation not permitted" in result.stderr, result.stderr


@pytest.mark.parametrize("research", [False, True])
def test_real_os_https_connect(wall, research):
    _, _, home, _, run = wall
    # No proxy, credentials, redirects or uploads. -k isolates TCP/TLS reachability
    # from local certificate trust services; this is not a certificate audit.
    command = ["/usr/bin/curl", "-q", "--noproxy", "*", "-4", "-k", "-sS",
               "--connect-timeout", "5", "--max-time", "10", "-o", "/dev/null",
               "-w", "%{time_connect}", "https://example.com/"]
    control = subprocess.run(command, env={**worker_environment(), "HOME": str(home)},
                             capture_output=True, text=True, timeout=15)
    if control.returncode in {6, 7, 28} and float(control.stdout or "0") == 0:
        pytest.skip(f"offline: unsandboxed HTTPS control could not connect: {control.stderr}")
    assert float(control.stdout or "0") > 0, control.stderr
    result = run(command, research=research)
    assert "Operation not permitted" not in result.stderr, result.stderr
    # A remote HTTP/TLS failure is OK only after the TCP connection succeeded.
    assert float(result.stdout or "0") > 0, result.stderr


def test_real_os_lists_worktree_parents(wall):
    own, main, home, _, run = wall
    code = ("import os, pathlib, sys; "
            "[(pathlib.Path(p).stat(), os.listdir(p)) for p in sys.argv[1:]]; "
            "pathlib.Path('allowed').write_text('fixture')")
    result = run([sys.executable, "-c", code, str(main / ".claude/worktrees"), str(home / "kyra-grok")])
    assert result.returncode == 0, result.stderr
    assert (own / "allowed").read_text() == "fixture"


def test_environment_drops_credentials_and_inherited_overrides():
    env = worker_environment({"PATH": "/bin", "HOME": "/scratch", "GH_TOKEN": "synthetic",
                              "SSH_AUTH_SOCK": "/scratch/agent", "GIT_CONFIG_COUNT": "9",
                              "AWS_SECRET_ACCESS_KEY": "synthetic", "MUSE_PERMISSION_PROFILE_FAIL_OPEN": "1"})
    assert not {"GH_TOKEN", "SSH_AUTH_SOCK", "AWS_SECRET_ACCESS_KEY", "MUSE_PERMISSION_PROFILE_FAIL_OPEN"} & env.keys()
    assert env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_VALUE_0"] == ""


def test_missing_sandbox_refuses_without_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(SystemExit, match="refusing unsandboxed"):
        wrap(["true"], tmp_path / "worker.log", tmp_path, tmp_path)
    assert not list(tmp_path.glob("*.sb"))


def test_profile_saved_next_to_log_and_never_overwritten(tmp_path):
    if not shutil.which("sandbox-exec"):
        pytest.skip("sandbox-exec is absent")
    command = wrap(["true"], tmp_path / "worker.log", tmp_path, tmp_path)
    assert command[1:] == ["-f", str(tmp_path / "worker.sb"), "true"]
    assert (tmp_path / "worker.sb").read_text() == profile(
        tmp_path, tmp_path, Path.home(), temp_dir=tmp_path / "worker.tmp")
    with pytest.raises(FileExistsError):
        wrap(["true"], tmp_path / "worker.log", tmp_path, tmp_path)


@pytest.mark.parametrize("name", ["grok", "muse"])
@pytest.mark.parametrize("research", [False, True])
def test_courier_wraps_entire_process_before_launch(tmp_path, monkeypatch, name, research):
    from companion import worker_sandbox

    spec = importlib.util.spec_from_file_location("wall_lane_test", Path(__file__).parents[1] / "scripts/lane_send.py")
    lane = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lane)
    monkeypatch.setattr(worker_sandbox.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(lane, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(lane, "_main_checkout", lambda: tmp_path / "main")
    calls = []

    class Process:
        pid = 123

        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

        def poll(self):
            return 0

    monkeypatch.setattr(lane.subprocess, "Popen", Process)
    lane._start_worker(name, ["synthetic-cli", "build"], tmp_path, 0, research=research,
                       env={"HOME": str(tmp_path), "GH_TOKEN": "synthetic"})
    argv, kwargs = calls[0]
    policy = Path(argv[2])
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-f"] and argv[3:] == ["synthetic-cli", "build"]
    assert policy.with_suffix(".log").exists()
    rules = policy.read_text().splitlines()
    assert "(deny network*)" in rules
    assert [rule for rule in rules if rule.startswith("(allow network")] == [
        '(allow network-outbound (remote tcp "*:443") '
        '(remote unix-socket (literal "/private/var/run/mDNSResponder")))'
    ]
    assert "GH_TOKEN" not in kwargs["env"]
    assert Path(kwargs["env"]["TMPDIR"]) == policy.with_suffix(".tmp").resolve()
    if name == "muse":
        assert Path(kwargs["env"]["XDG_DATA_HOME"]) == policy.with_suffix(".tmp").resolve() / "muse-state"
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_real_os_allows_gitconfig_stat_but_not_its_contents(wall):
    # Grok's git discovery stats <user-home>/.gitconfig; a denied stat cancelled every Grok shell call (2026-10-16).
    own, main, home, other, run = wall
    (home / ".gitconfig").write_text("[user]\n\tname = synthetic fixture\n")
    stat = run(["/bin/sh", "-c", 'test -e "$HOME/.gitconfig" && echo present'])
    assert stat.returncode == 0 and "present" in stat.stdout, stat.stderr
    read = run(["/bin/sh", "-c", 'cat "$HOME/.gitconfig"'])
    assert read.returncode != 0 and "synthetic fixture" not in read.stdout


def test_worker_environment_path_reaches_homebrew_node():
    parts = worker_environment({"PATH": "/usr/bin:/bin"})["PATH"].split(":")
    assert "/opt/homebrew/bin" in parts and "/usr/local/bin" in parts and parts[:2] == ["/usr/bin", "/bin"]
    assert "/opt/homebrew/bin" in worker_environment({})["PATH"]


def test_worker_environment_keeps_npm_offline():
    env = worker_environment({})
    assert (env["NPM_CONFIG_OFFLINE"], env["NPM_CONFIG_UPDATE_NOTIFIER"]) == ("true", "false")
    assert env["NPM_CONFIG_AUDIT"] == "false" and env["NPM_CONFIG_FUND"] == "false"


@pytest.mark.parametrize("kind", [None, "grok", "muse"])
def test_real_os_runs_node_and_npm_offline(wall, kind):
    # Earlier workers could not run JavaScript tests in the wall (2026-10-17).
    own, main, home, other, run = wall
    node = shutil.which("node", path=worker_environment()["PATH"])
    if not node:
        pytest.skip("node is absent")
    (own / "package.json").write_text('{"name":"fixture","scripts":{"test":"node -e \\"console.log(40+2)\\""}}')
    env = {**worker_environment(kind=kind), "HOME": str(home)}
    policy = main.parent / "node.sb"
    policy.write_text(profile(own, main, home, kind=kind))
    for command in ([node, "-e", "console.log(1)"], ["npm", "test"]):
        result = subprocess.run([shutil.which("sandbox-exec"), "-f", str(policy), *command], cwd=own, env=env,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
    assert "42" in result.stdout
    fetch = subprocess.run([shutil.which("sandbox-exec"), "-f", str(policy), "npm", "install", "left-pad"],
                           cwd=own, env=env, capture_output=True, text=True, timeout=60)
    assert fetch.returncode != 0 and not (own / "node_modules").exists()
