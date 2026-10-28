"""Dashboard authentication: salted PBKDF2 password, in-memory sessions with CSRF tokens, per-IP login lockout.
The dashboard is served over plain HTTP on the LAN: the password is not encrypted in transit, so use a password you do not reuse."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

from . import config

ITERATIONS = 150_000
SESSION_SECONDS = 8 * 3600
MAX_FAILS, LOCK_SECONDS = 5, 120
MIN_PASSWORD = 10


class Auth:
    def __init__(self, path: Optional[str] = None, clock=time.time):
        self.path, self.clock = path or config.ADMIN_PATH, clock
        self.sessions: Dict[str, dict] = {}
        self.fails: Dict[str, Tuple[int, float]] = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ password
    def configured(self) -> bool:
        try:
            with open(self.path) as f:
                return "hash" in json.load(f)
        except (OSError, ValueError):
            return False

    def set_password(self, pw: str) -> None:
        if len(pw) < MIN_PASSWORD:
            raise ValueError("password must be at least %d characters" % MIN_PASSWORD)
        salt = secrets.token_bytes(16)
        h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITERATIONS)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"salt": salt.hex(), "hash": h.hex(), "iters": ITERATIONS, "created": self.clock()}, f)
        os.replace(tmp, self.path)

    def verify(self, pw: str) -> bool:
        try:
            with open(self.path) as f:
                d = json.load(f)
            h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(d["salt"]), int(d["iters"]))
            return hmac.compare_digest(h.hex(), d["hash"])
        except (OSError, ValueError, KeyError):
            return False

    # ------------------------------------------------------------------ lockout
    def allowed(self, ip: str) -> bool:
        with self.lock:
            n, first = self.fails.get(ip, (0, 0.0))
            if n >= MAX_FAILS and self.clock() - first < LOCK_SECONDS:
                return False
            if n >= MAX_FAILS:
                self.fails.pop(ip, None)
            return True

    def record_fail(self, ip: str) -> None:
        with self.lock:
            n, first = self.fails.get(ip, (0, self.clock()))
            self.fails[ip] = (n + 1, first if n else self.clock())

    def reset_fails(self, ip: str) -> None:
        with self.lock:
            self.fails.pop(ip, None)

    # ------------------------------------------------------------------ sessions
    def login(self, ip: str, pw: str) -> Optional[Tuple[str, str]]:
        """-> (session_token, csrf_token) or None. Always does the hash work so timing does not reveal state."""
        if not self.allowed(ip):
            return None
        if self.verify(pw):
            self.reset_fails(ip)
            return self.new_session(ip)
        self.record_fail(ip)
        return None

    def new_session(self, ip: str) -> Tuple[str, str]:
        tok, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.lock:
            now = self.clock()
            self.sessions = {k: v for k, v in self.sessions.items() if v["exp"] > now}
            self.sessions[tok] = {"exp": now + SESSION_SECONDS, "csrf": csrf, "ip": ip}
        return tok, csrf

    def session(self, tok: Optional[str]) -> Optional[dict]:
        if not tok:
            return None
        with self.lock:
            s = self.sessions.get(tok)
            if s and s["exp"] > self.clock():
                return s
            self.sessions.pop(tok, None)
        return None

    def logout(self, tok: Optional[str]) -> None:
        with self.lock:
            self.sessions.pop(tok or "", None)

    @staticmethod
    def csrf_ok(session: Optional[dict], token: Optional[str]) -> bool:
        return bool(session and token and hmac.compare_digest(session["csrf"], token))
