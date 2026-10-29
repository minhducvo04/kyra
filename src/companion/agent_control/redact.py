"""Secret redaction for agent-control events.

``scrub`` returns a copy of its input with every key-shaped value replaced
by ``[redacted:<kind>]``. It runs before any write (``store.EventStore``
calls it on append), so a secret that reaches an ingest adapter never
reaches disk. Classification is by the *meaning* of the value, not by one
spelling: known vendor prefixes, Bearer/Basic authorization, URL
credentials, CLI flags and secret-named assignments are all protected. A
name is secret-ish when it contains ``key``, ``token``, ``secret``,
``password``, ``passwd``, ``pass``, ``pwd``, ``auth``, ``signature``,
``sig``, ``credential`` or ``apikey``
/ ``api_key`` anywhere in it, case-insensitively, including a prefix such
as ``OPENAI_API_KEY``, ``MY_TOKEN`` or ``db_password`` and camelCase such
as ``clientSecret``. Token *counts* (``tokens_in``, ``tokens_out``,
``tokens_total``) name a number, not a credential, and pass through.
Plain hashes (hex strings), UUIDs and paths also pass through: they are
the metadata the event model legitimately stores. Nothing here opens a
file, let alone a ``.env`` file.
"""

import json
import math
import re
import shlex
from urllib.parse import unquote

_PREFIX_PATTERNS = (
    ("api_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}")),
    ("api_key", re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{8,}")),
    ("api_key", re.compile(r"\bxai-[A-Za-z0-9_-]{8,}")),
    ("api_key", re.compile(r"\bhf_[A-Za-z0-9]{8,}")),
    ("api_key", re.compile(r"\bgsk_[A-Za-z0-9_-]{8,}")),
    ("api_key", re.compile(r"\bAIza[A-Za-z0-9_-]{8,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}")),
    ("github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?"
    r"(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|$)", re.DOTALL,
)
_JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_CLI_COMMAND = re.compile(
    r"(?<![\w./-])(?:[\w./-]+/)?(?P<command>mysql|mysqldump|mariadb|mysqladmin|sshpass|curl)(?=\s)"
)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_BASIC = re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{8,}", re.IGNORECASE)
_HEADER = re.compile(r"\b(?P<name>[A-Za-z][A-Za-z0-9-]*)\s*:\s*")
_URL_QUERY_PREFIX = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]*[?&]$")
_URL_CRED = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://)[^/\s?#@]+@")
_FLAG = re.compile(
    r"(?P<prefix>--(?P<name>[A-Za-z][A-Za-z0-9_-]*)(?:\s*=\s*|\s+))"
)
_ASSIGNMENT = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_])(?P<qname>[\"']?)"
    r"(?P<name>(?:[A-Za-z_]|\\u[0-9a-fA-F]{4}|%[0-9a-fA-F]{2})"
    r"(?:[A-Za-z0-9_.\-]|\\u[0-9a-fA-F]{4}|%[0-9a-fA-F]{2})*)"
    r"(?P=qname)\s*(?P<separator>[:=])\s*)"
)
_MARKER = re.compile(
    r"\[redacted:(?:secret|api_key|slack_token|github_token|aws_access_key|"
    r"private_key|bearer|basic|auth|high_entropy)\]"
)
_CANDIDATE = re.compile(r"[A-Za-z0-9_+\-]{32,}")
_TOKEN = re.compile(r"\S+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_HEX = re.compile(r"^[0-9a-fA-F]+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

ENTROPY_MIN = 4.0  # bits per character; random alphanumerics sit near 5, English words near 3

_SECRET_WORDS = frozenset({
    "key", "token", "secret", "password", "passwd", "pass", "pwd", "auth", "signature", "sig",
    "credential", "credentials", "apikey", "passphrase", "cred", "creds",
})
_STRONG_SUBSTRINGS = ("password", "passwd", "passphrase", "secret", "credential", "apikey", "token")
_COUNT_WORDS = frozenset({"in", "out", "total", "count", "used", "input", "output"})


def _name_parts(name):
    parts = []
    for chunk in re.split(r"[_\-.]+", name):
        parts.extend(match.group(0).lower() for match in _CAMEL.finditer(chunk))
    return parts


def _is_secret_name(name):
    """True when an identifier names a credential, wherever the word sits."""
    lowered = name.lower().strip("-")
    if lowered == "authorization":
        return True
    parts = _name_parts(name)
    if not parts:
        return False
    if parts[0] == "tokens" and all(part in _COUNT_WORDS or part == "tokens" for part in parts):
        return False  # tokens_in and friends count tokens; they are not one
    if any(part in _SECRET_WORDS for part in parts):
        return True
    if "api" in parts and "key" in parts:
        return True
    if any(part.startswith("auth") and not part.startswith("author") or part.endswith("auth")
           for part in parts):
        return True  # authentication, oauth and friends name a credential too
    if any(part not in ("keyboard", "keynote") and (part.endswith("key") or part.startswith("key"))
           for part in parts):
        return True  # accesskey, keyfile and friends
    joined = "".join(parts)
    if joined.startswith("author"):
        return False  # an author is a person, not an auth credential
    return any(word in joined for word in _STRONG_SUBSTRINGS)


def _value_end(text, start, *, json_value=False, shell=False):
    """Consume quoted/escaped shell or JSON values, including adjacent suffixes."""
    if json_value:
        try:
            _value, end = json.JSONDecoder().raw_decode(text, start)
        except ValueError:
            return len(text)  # An incomplete structured secret must not leak children.
        if end == len(text) or text[end].isspace() or text[end] in ",}]":
            return end
        # A quoted value followed by a suffix is not valid JSON; hide that
        # suffix too instead of leaving a partly redacted credential.
    quote = None
    pos = start
    while pos < len(text):
        char = text[pos]
        if char == "\\":
            pos = min(pos + 2, len(text))
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char.isspace() or char in ";&" or (shell and char == "|"):
            break
        pos += 1
    return pos


def _named_redaction(text, pattern):
    # Match names without consuming non-secret values: nested assignments and
    # flags after a boolean flag must still be visited.
    parts = []
    copied = pos = 0
    while match := pattern.search(text, pos):
        pos = match.end()
        groups = match.groupdict()
        name = match.group("name")
        if groups.get("qname") == '"':
            name = json.loads(f'"{name}"')  # Decode JSON key escapes before classification.
        if not groups.get("qname") and groups.get("separator") == ":" and _HEADER.match(text, match.start()):
            continue  # The header pass knows where the enclosing quote ends.
        if not _is_secret_name(unquote(name)):
            # A non-secret wrapper may hold escaped JSON or a quoted command.
            # Decode complete values and scrub their contents, so escaped inner
            # names cannot hide behind an outer assignment.
            try:
                value, end = json.JSONDecoder().raw_decode(text, pos)
            except ValueError:
                continue
            cleaned = scrub(value)
            if cleaned != value:
                parts.extend((text[copied:pos], json.dumps(cleaned)))
                copied = end
            pos = end
            continue
        json_value = groups.get("qname") == '"' and groups.get("separator") == ":"
        if _URL_QUERY_PREFIX.search(text[:match.start()]):
            end = pos
            while end < len(text) and not text[end].isspace() and text[end] not in "&#\"'<>":
                end += 1
        else:
            end = _value_end(text, pos, json_value=json_value)
        value = text[pos:end]
        quoted = len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]
        inner = value[1:-1] if quoted else value
        if _MARKER.fullmatch(inner):
            pos = end
            continue
        replacement = "[redacted:secret]"
        if json_value:
            replacement = json.dumps(replacement)
        elif quoted:
            replacement = value[0] + replacement + value[0]
        parts.extend((text[copied:pos], replacement))
        copied = pos = end
    parts.append(text[copied:])
    return "".join(parts)


def _escaped_json_redaction(match):
    literal = match.group()
    if "\\" not in literal:
        return literal
    try:
        value = json.loads(literal)
    except ValueError:
        # Shell double quotes allow escapes such as Python's \x1b which JSON
        # rejects. Decode only shell escapes, then scan the inner assignments.
        value = re.sub(r'\\(["\\$`])', r'\1', literal[1:-1])
    cleaned = scrub_text(value)
    return json.dumps(cleaned) if cleaned != value else literal


def _cli_redaction(text):
    """Recognized credential options for curl, sshpass and MySQL clients."""
    replacements = []
    scanned_words = set()
    for command in _CLI_COMMAND.finditer(text):
        if command.start() in scanned_words:
            continue  # An argument of an already-scanned command, not a new invocation.
        pos = command.end()
        while pos < len(text):
            while pos < len(text) and text[pos] in " \t":
                pos += 1
            if pos == len(text) or text[pos] in ";|&\r\n":
                break
            # Skip repeated command matches at these bare argument positions.
            # Matches inside quoted arguments remain eligible for their own scan.
            scanned_words.add(pos)
            end = _value_end(text, pos, shell=True)
            word = text[pos:end]
            if word == "--":
                break
            # curl commonly bundles no-argument options before -u, e.g. -su.
            option = (re.match(r"(?:--(?:proxy-)?user(?:=|$)|-[sSfvLk]*[uU])", word)
                      if command["command"] == "curl" else re.match(r"-p", word))
            if not option:
                if command["command"] == "sshpass" and not word.startswith("-"):
                    break  # Its child command's options have different meanings.
                pos = end
                continue
            start = pos + option.end()
            if start == end and not word.endswith("="):
                if command["command"] not in ("curl", "sshpass"):
                    pos = end  # Bare MySQL-family -p prompts; the next word is not a password.
                    continue
                while start < len(text) and text[start] in " \t":
                    start += 1
                end = _value_end(text, start, shell=True)
            value = text[start:end]
            try:
                decoded = "".join(shlex.split(value))
            except ValueError:
                decoded = value
            if decoded and (command["command"] != "curl" or ":" in decoded):
                quoted = len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]
                replacement = "[redacted:secret]"
                if quoted:
                    replacement = value[0] + replacement + value[0]
                replacements.append((start, end, replacement))
                if command["command"] == "sshpass":
                    break
            pos = end
    # A command-looking string inside a credential is already covered by its
    # outer span. Never apply overlapping replacements with stale offsets.
    disjoint = []
    for span in sorted(replacements, key=lambda item: (item[0], -item[1])):
        if not disjoint or span[0] >= disjoint[-1][1]:
            disjoint.append(span)
    for start, end, replacement in reversed(disjoint):
        text = text[:start] + replacement + text[end:]
    return text


def _header_redaction(text):
    """Consume a secret-named header, preserving its enclosing quote."""
    parts = []
    copied = 0
    for match in _HEADER.finditer(text):
        if match.start() < copied or not _is_secret_name(match["name"]):
            continue
        quote = None
        pos = 0
        while pos < match.start():
            char = text[pos]
            if char == "\\" and quote != "'":
                pos += 2
                continue
            if char == quote:
                quote = None
            elif quote is None and char in "\"'":
                quote = char
            pos += 1
        end = match.end()
        while end < len(text):
            char = text[end]
            if char == "\\" and quote != "'":
                end += 2
                continue
            if (quote and char == quote) or (not quote and char in "\r\n"):
                break
            end += 1
        kind = "auth" if match["name"].lower() == "authorization" else "secret"
        parts.extend((text[copied:match.end()], f"[redacted:{kind}]"))
        copied = end
    parts.append(text[copied:])
    return "".join(parts)


def _entropy(text):
    counts = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    total = len(text)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _is_high_entropy(token):
    if _HEX.match(token) or _UUID.match(token):
        return False  # a content hash or id: metadata the store keeps by design
    classes = sum((
        any(char.islower() for char in token),
        any(char.isupper() for char in token),
        any(char.isdigit() for char in token),
        any(char in "_+-" for char in token),
    ))
    return classes >= 3 and _entropy(token) >= ENTROPY_MIN


def _scan_token(match):
    token = match.group(0)
    if "/" in token:  # only this token is a path; the rest of the string still scans
        return token
    return _CANDIDATE.sub(
        lambda hit: "[redacted:high_entropy]" if _is_high_entropy(hit.group(0)) else hit.group(0),
        token,
    )


def scrub_text(text):
    """One string in, the same string with key-shaped spans redacted out."""
    if not isinstance(text, str):
        raise TypeError("scrub_text: must be a string")
    # Remove PEM spans first: their body may itself resemble another syntax.
    result = _PRIVATE_KEY.sub("[redacted:private_key]", text)
    result = _JSON_STRING.sub(_escaped_json_redaction, result)
    result = _cli_redaction(result)
    result = _BEARER.sub("[redacted:bearer]", result)
    result = _BASIC.sub("[redacted:basic]", result)
    result = _URL_CRED.sub(r"\1[redacted:secret]@", result)
    result = _named_redaction(result, _FLAG)
    result = _named_redaction(result, _ASSIGNMENT)
    result = _header_redaction(result)
    for kind, pattern in _PREFIX_PATTERNS:
        result = pattern.sub(f"[redacted:{kind}]", result)
    result = _TOKEN.sub(_scan_token, result)
    return result


def scrub(value):
    """A redacted deep copy of ``value``; the input is never mutated."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_secret_name(key) and item not in (None, ""):
                cleaned[key] = "[redacted:secret]"
            else:
                cleaned[key] = scrub(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return type(value)(scrub(item) for item in value)
    return value
