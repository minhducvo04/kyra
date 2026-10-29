"""Seatbelt wall for Grok/Muse processes and all their descendants.

Source: macOS sandbox-exec(1), sandbox_init(3), and the shipped SBPL examples
in /usr/share/sandbox. No fallback to an unsandboxed process is permitted.
"""

import json
import os
import shutil
import stat
import urllib.parse
from pathlib import Path


def _reject_hardlinks(root: Path) -> None:
    """Seatbelt checks paths; inherited hardlinks can alias an outside inode.

    Refuse them before launch. The running wall also prevents creating a link
    to an outside file. Symlinks are not followed here; Seatbelt checks targets.
    """
    pending = [root]
    while pending:
        path = pending.pop()
        try:
            entry = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SystemExit(f"worker send FAILED: cannot inspect writable path {path}: {exc}") from exc
        if stat.S_ISREG(entry.st_mode) and entry.st_nlink > 1:
            raise SystemExit(f"worker send FAILED: inherited hardlink in writable path: {path}")
        if stat.S_ISDIR(entry.st_mode):
            try:
                with os.scandir(path) as children:
                    pending.extend(Path(child.path) for child in children)
            except OSError as exc:
                raise SystemExit(f"worker send FAILED: cannot inspect writable path {path}: {exc}") from exc


def _main_checkout_writes(worktree: Path, main: Path) -> list[str]:
    """Reopen only the worker tree, including when it is inside the main checkout.

    The strict write allowlist excludes shared Git metadata. Linked-worktree
    staging/commits therefore fail closed; the coordinator owns those writes.
    """
    quote = json.dumps
    return [f"(deny file-write* (subpath {quote(str(main))}))",
            f"(allow file-write* (subpath {quote(str(worktree))}))",
            f"(deny file-write* (subpath {quote(str(main / '.git'))}))"]


def profile(worktree: Path, main: Path, home: Path, *, research: bool = False, kind: str | None = None,
            temp_dir: Path | None = None) -> str:
    worktree, main, home = (p.resolve() for p in (worktree, main, home))
    quote = json.dumps
    roots = (main / ".claude/worktrees", home / "kyra-grok")
    denied = {main / "data", worktree / "data", home / ".ssh", home / "Library/Keychains",
              home / ".git-credentials", home / ".gitconfig", home / ".netrc",
              home / ".config/git", home / ".config/gh"}
    # Resolve data aliases before launch, as well as denying their lexical paths.
    for root in roots:
        if root.is_dir():
            denied.update(child / "data" for child in root.iterdir() if child.is_dir())
    denied |= {path.resolve() for path in denied}
    # Grok runs with --sandbox off inside this wall. Its nested sandbox cannot
    # apply here (sandbox-exec exits 71). This profile is the only write wall.
    lines = ["(version 1)", "(allow default)", "(deny file-write*)",
             '(allow file-write* (literal "/dev/null"))']
    _reject_hardlinks(worktree)
    if temp_dir is not None:
        _reject_hardlinks(temp_dir.resolve())
        lines.append(f"(allow file-write* (subpath {quote(str(temp_dir.resolve()))}))")
    # Explicit provider state only. Never grant the <user-home>/.grok tree (config, hooks
    # and hooks-paths stay denied), <user-home>/.claude, a whole XDG root, /private/tmp,
    # or /private/var/folders.
    if kind == "grok":
        group = urllib.parse.quote(str(worktree), safe="")
        if len(group) > 255:
            # Grok uses an undocumented slug/hash algorithm here (.cwd marker).
            # Our discovery only supports encoded paths; never grant all sessions.
            raise SystemExit("worker send FAILED: Grok session directory needs a shorter worktree path")
        sessions = home / ".grok/sessions" / group
        _reject_hardlinks(sessions)
        lines.append(f"(allow file-write* (subpath {quote(str(sessions))}))")
        # The file login owns these two files. Config, hooks and hooks-paths
        # stay read-only: they can persist code or widen this wall.
        for name in ("auth.json", "auth.json.lock"):
            path = home / ".grok" / name
            _reject_hardlinks(path)
            lines.append(f"(allow file-write* (literal {quote(str(path))}))")
    elif kind == "muse":
        # The file login backend owns this file. The neighboring settings,
        # trust and skills paths must stay read-only: they can persist code.
        auth = home / MUSE_CONFIG / "muse/auth.json"
        _reject_hardlinks(auth)
        lines.append(f"(allow file-write* (literal {quote(str(auth))}))")
        # Sessions live in this run's TMPDIR/muse-state/muse/sessions.
    elif kind in {"cursor", "antigravity"}:
        # the owner, 2026-10-21: only each CLI's own file-backed token. No parent
        # directory, settings, Keychain, or other provider receives a grant.
        relative = (".cursor/auth.json" if kind == "cursor" else
                    ".gemini/antigravity-cli/antigravity-oauth-token")
        auth = home / relative
        _reject_hardlinks(auth)
        lines.append(f"(allow file-write* (literal {quote(str(auth))}))")
    lines += _main_checkout_writes(worktree, main)
    lines += ['(deny file-read* file-write* (regex #"(^|/)\\.env(\\.[^/]*)?(/|$)"))']
    lines += [f"(deny file-read* file-write* (subpath {quote(str(path))}))"
              for path in sorted(denied)]
    # Grok's git discovery stats <user-home>/.gitconfig; a denied stat fails discovery and every shell call then hits
    # Grok's confirmation floor, which headless dontAsk turns into a silently cancelled turn (2026-10-16 probe).
    # The file's contents stay denied; only its existence and size are visible.
    lines.append(f"(allow file-read-metadata (literal {quote(str(home / '.gitconfig'))}))")
    # pytest 9 stats every root entry during collection; a denied stat of the tracked .env.example fails it (exit 4,
    # "found no collectors"). the owner yes, 2026-10-17: metadata only, in the worker's own worktree; contents stay denied.
    lines.append(f"(allow file-read-metadata (literal {quote(str(worktree / '.env.example'))}))")
    for root in roots:
        # Python/pytest and Git walk these parents. Only existing siblings are
        # denied; a sibling created later is covered at the next worker launch.
        lines.append("(allow file-read-metadata file-read-data "
                     f"(literal {quote(str(root))}))")
        if root.is_dir():
            siblings = {path for child in root.iterdir() if child.resolve() != worktree
                        for path in (child, child.resolve())}
            lines += [f"(deny file-read* file-write* (subpath {quote(str(path))}))"
                      for path in sorted(siblings)]
    # Do not delegate reads/network/exec to unsandboxed XPC or Apple Event helpers.
    # This includes the SecurityServer keychain API, not just its on-disk files.
    # Build/research share the same wall; only their CLI web-tool flags differ.
    # DNS uses the system resolver, never unrestricted UDP/TCP port 53. Its
    # queries cannot be tied to a subsequent 443 connection by Seatbelt.
    lines += ["(deny mach-lookup)", "(deny appleevent-send)", "(deny network*)",
              '(allow mach-lookup (global-name "com.apple.dnssd.service"))',
              '(allow network-outbound (remote tcp "*:443") '
              '(remote unix-socket (literal "/private/var/run/mDNSResponder")))']
    # Muse workers sign in with their own file-backed Meta login in MUSE_CONFIG (the owner, 2026-10-17: his paid plan is on
    # the login; the .env key has no billing). The Keychain stays closed for every kind (lane 1 review: opening it
    # exposed the GitHub and Claude Code items too). Only a muse worker may read that folder; nobody reads the
    # interactive CLI's own <user-home>/.config/muse.
    own = [home / ".config/muse"] + ([] if kind == "muse" else [home / MUSE_CONFIG])
    lines += [f"(deny file-read* file-write* (subpath {quote(str(path))}))" for path in own]
    return "\n".join(lines) + "\n"


MUSE_CONFIG = ".config/kyra-muse"  # relative to HOME; the owner signed in once with the file credential backend here


# One credential per worker kind, passed only to that kind. Muse had META_API_KEY until 2026-10-17; it now signs in
# with the file-backed login in MUSE_CONFIG, and the key (no billing) would take priority over that login, so no kind
# gets it.
KIND_SECRETS: dict[str, frozenset[str]] = {}

WALL_MARKER = "KYRA_WORKER_WALL"  # set in every walled worker's environment, never read by the wall itself


def worker_environment(source: dict[str, str] | None = None, kind: str | None = None) -> dict[str, str]:
    """Do not inherit credential agents, Git overrides, or arbitrary secret variables. A worker of ``kind`` also
    keeps that kind's own credential (KIND_SECRETS) and no other kind's: a grok, claude or codex worker never gets
    META_API_KEY even when the caller's environment holds it."""
    source = os.environ if source is None else source
    allowed = {"HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
               "USER", "LOGNAME", "SHELL", "MUSE_NO_AUTO_UPDATE",
               "PYTEST_ADDOPTS"}  # the courier's pytest cap (-n 2, or -n 1 while a priority job runs), no secret
    allowed |= KIND_SECRETS.get(kind, frozenset())
    env = {key: value for key, value in source.items() if key in allowed}
    # Homebrew tools (node for the browser-free JS tests) live outside the minimal PATH a worker inherits.
    parts = [part for part in env.get("PATH", "").split(":") if part]
    env["PATH"] = ":".join(parts + [d for d in ("/opt/homebrew/bin", "/usr/local/bin") if d not in parts])
    # node/npm run inside the wall (the owner yes, 2026-10-17: no new network). npm only runs existing scripts and
    # node_modules: offline, no update check, no audit or funding call. Node itself uses the same 443 rule as before.
    env.update(NPM_CONFIG_OFFLINE="true", NPM_CONFIG_UPDATE_NOTIFIER="false",
               NPM_CONFIG_AUDIT="false", NPM_CONFIG_FUND="false")
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0="credential.helper", GIT_CONFIG_VALUE_0="")
    # Tells tests they run walled (tests/worker_wall.py); a marker only, it opens nothing.
    env[WALL_MARKER] = kind or "1"
    if kind == "muse":
        home = Path(env.get("HOME") or Path.home())
        env.update(TBH_CREDENTIAL_BACKEND="file", XDG_CONFIG_HOME=str(home / MUSE_CONFIG))
    elif kind == "cursor":
        env.update(AGENT_CLI_CREDENTIAL_STORE="file", NO_OPEN_BROWSER="1")
    return env


def wrap(argv: list[str], log: Path, worktree: Path, main: Path, *,
         research: bool = False, kind: str | None = None, home: Path | None = None) -> list[str]:
    binary = shutil.which("sandbox-exec")
    if not binary:
        raise SystemExit("worker send FAILED: sandbox-exec is required; refusing unsandboxed launch")
    policy = log.with_suffix(".sb")
    # One unique log per launch, so this is one private temp directory per run.
    # The courier also sets TMPDIR to this path for the CLI and its descendants.
    temp_dir = log.with_suffix(".tmp")
    temp_dir.mkdir(mode=0o700)
    # A unique worker log gives the profile a unique name. Never follow an old symlink.
    with policy.open("x") as stream:
        stream.write(profile(worktree, main, home or Path.home(), research=research, kind=kind,
                             temp_dir=temp_dir))
    return [binary, "-f", str(policy), *argv]
