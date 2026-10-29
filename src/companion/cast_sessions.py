"""Local device grants and single-use cast credentials; no video or owner tokens.

File locking covers both HTTP listeners and thread-pool calls. Only credential
verifiers persist; a restart never restores an authenticated native connection.
"""
try:
    import fcntl
except ImportError:  # Windows: no POSIX locks; callers fail at call time, not import time
    fcntl = None
import hashlib
import hmac
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path

from companion.paths import write_json


class CastAuthError(ValueError):
    def __init__(self):
        super().__init__("Cast authorization refused")


def bridge_secret(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Publish the complete credential atomically, including across the two listeners.
    lock = path.with_suffix(".lock")
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(secrets.token_urlsafe(32))
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise CastAuthError()
        value = path.read_text().strip()
        if len(value) < 32:
            raise CastAuthError()
        return value


class CastSessionStore:
    SESSION_SECONDS = 3600

    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _state(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists():
                state = json.loads(self.path.read_text())
                if (not isinstance(state, dict) or state.get("version") != 1
                        or not isinstance(state.get("salt"), str)
                        or any(not isinstance(state.get(k), dict)
                               for k in ("enrollments", "devices", "capabilities", "sessions", "tickets"))):
                    raise ValueError("Invalid cast store")
            else:
                state = {"version": 1, "salt": secrets.token_hex(32), "enrollments": {},
                         "devices": {}, "capabilities": {}, "sessions": {}, "tickets": {}}
            yield state
            write_json(self.path, state)
            self.path.chmod(0o600)

    @staticmethod
    def _hash(state, value):
        return hmac.new(state["salt"].encode(), value.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _device(state, device_id):
        device = state["devices"].get(device_id)
        if device is None or device["revoked"]:
            raise CastAuthError()
        return device

    @classmethod
    def _session(cls, state, session_id, now):
        session = state["sessions"].get(session_id)
        if session is None or session["expires_at"] <= now:
            raise CastAuthError()
        cls._device(state, session["device_id"])
        return session

    @staticmethod
    def _prune(state, now):
        for key in ("enrollments", "capabilities", "sessions", "tickets"):
            state[key] = {k: v for k, v in state[key].items() if v["expires_at"] > now}

    def begin_enrollment(self, name, now):
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise CastAuthError()
        with self._state() as state:
            self._prune(state, now)
            if len(state["enrollments"]) >= 16:
                raise CastAuthError()
            enrollment_id, code = secrets.token_urlsafe(32), f"{secrets.randbelow(1000000):06d}"
            claim = secrets.token_urlsafe(32)
            state["enrollments"][enrollment_id] = {"name": name.strip(), "code": self._hash(state, code),
                "claim": self._hash(state, claim), "expires_at": now + 300}
            return {"enrollment_id": enrollment_id, "code": code, "claim": claim}

    def approve(self, enrollment_id, code, now):
        with self._state() as state:
            entry = state["enrollments"].get(enrollment_id)
            if (entry is None or entry["expires_at"] <= now or "device_id" in entry
                    or not hmac.compare_digest(entry["code"], self._hash(state, code))):
                raise CastAuthError()
            device_id = secrets.token_urlsafe(32)
            entry["device_id"] = device_id
            entry["expires_at"] = now + 300
            del entry["code"]
            state["devices"][device_id] = {"device_id": device_id, "name": entry["name"], "enrolled_at": now,
                                          "revoked": False, "verifier": ""}
            return {"device_id": device_id}

    def claim(self, enrollment_id, claim, now):
        with self._state() as state:
            entry = state["enrollments"].get(enrollment_id)
            if (entry is None or entry["expires_at"] <= now or "device_id" not in entry
                    or not hmac.compare_digest(entry["claim"], self._hash(state, claim))):
                raise CastAuthError()
            device = self._device(state, entry["device_id"])
            secret = secrets.token_urlsafe(32)
            device["verifier"] = self._hash(state, secret)
            del state["enrollments"][enrollment_id]
            return {"device_id": device["device_id"], "secret": secret}

    def pending(self, now):
        with self._state() as state:
            self._prune(state, now)
            return [{"enrollment_id": key, "name": e["name"]} for key, e in state["enrollments"].items()
                    if "device_id" not in e]

    def devices(self):
        with self._state() as state:
            return [{k: d[k] for k in ("device_id", "name", "enrolled_at", "revoked")}
                    for d in state["devices"].values()]

    def revoke(self, device_id):
        with self._state() as state:
            device = state["devices"].get(device_id)
            if device is None:
                raise CastAuthError()
            device["revoked"] = True
            for kind in ("capabilities", "sessions", "tickets"):
                state[kind] = {k: v for k, v in state[kind].items() if v["device_id"] != device_id}

    def issue_capability(self, device_id, secret, now):
        with self._state() as state:
            device = self._device(state, device_id)
            if not hmac.compare_digest(device["verifier"], self._hash(state, secret)):
                raise CastAuthError()
            self._prune(state, now)
            cap = secrets.token_urlsafe(32)
            state["capabilities"][self._hash(state, cap)] = {"device_id": device_id, "scope": "view", "expires_at": now + 60}
            return cap

    def redeem_capability(self, capability, now):
        with self._state() as state:
            key = self._hash(state, capability)
            cap = state["capabilities"].get(key)
            if cap is None or cap["expires_at"] <= now or cap["scope"] != "view":
                raise CastAuthError()
            self._device(state, cap["device_id"])
            del state["capabilities"][key]
            session_id = secrets.token_urlsafe(32)
            session = {"session_id": session_id, "device_id": cap["device_id"], "expires_at": now + self.SESSION_SECONDS}
            state["sessions"][session_id] = session
            return session.copy()

    def issue_ticket(self, session_id, stream_id, epoch, now):
        if not stream_id or type(epoch) is not int or not 0 <= epoch <= 0xFFFFFFFF:
            raise CastAuthError()
        with self._state() as state:
            session = self._session(state, session_id, now)
            self._prune(state, now)
            ticket = secrets.token_urlsafe(32)
            state["tickets"][self._hash(state, ticket)] = {"session_id": session_id, "device_id": session["device_id"],
                "stream_id": stream_id, "epoch": epoch, "expires_at": min(now + 60, session["expires_at"])}
            return ticket

    def verify_ticket(self, ticket, stream_id, epoch, now):
        with self._state() as state:
            key = self._hash(state, ticket)
            entry = state["tickets"].get(key)
            if (entry is None or entry["expires_at"] <= now or entry["stream_id"] != stream_id
                    or entry["epoch"] != epoch):
                raise CastAuthError()
            self._session(state, entry["session_id"], now)
            del state["tickets"][key]
            return entry["device_id"]

    def check_session(self, session_id, now):
        with self._state() as state:
            return self._session(state, session_id, now).copy()

    def close_session(self, session_id):
        with self._state() as state:
            state["sessions"].pop(session_id, None)
            state["tickets"] = {k: v for k, v in state["tickets"].items() if v["session_id"] != session_id}
