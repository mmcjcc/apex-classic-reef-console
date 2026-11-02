"""Who is calling. One of three modes, set by REEF_AUTH:

- basic (the default when REEF_AUTH_URL is not set): reef-web's own users file (REEF_USERS_FILE, default
  /app/config/users.txt), one line per user, `name:role:hash`, read again whenever it changes. A browser signs in with
  HTTP Basic: the page itself asks, so the browser keeps the sign-in for everything under /reef/. A script or an MCP
  client sends `Authorization: Bearer name:password`. Make a line with `python -m reefweb.auth NAME ROLE` (it reads the
  password). Basic sends the password with every request: use HTTPS (a reverse proxy) beyond a trusted LAN.
- forward: another service holds the sign-in (a homelab console, an SSO proxy). reef-web asks it, GET REEF_AUTH_URL
  with the caller's session cookie (REEF_AUTH_COOKIE) or bearer token, and expects 200 with JSON
  `{"name", "role", "kind", "can_request"}`, or 401/403. A browser without a session is sent to REEF_SIGN_IN_URL.
  Put a reverse proxy that checks every request (forward_auth) in front, so the app's files are not served to strangers.
- none: no sign-in; every caller is "local", an admin. Only on a network where everyone may read and record.

Roles: viewer reads; agent and admin may also add records (water tests, journal entries). A write from a browser
session must carry the header X-Requested-With: reef-console: browsers never add a custom header to a cross-site form
post (CSRF), while they do attach cookies and Basic credentials on their own. A bearer token is never sent by a
browser on its own, so a request with one needs no such header. Answers are kept for a minute."""

from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import hmac
import logging
import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status

CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "reef-console"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CACHE_SECONDS = 60.0
CACHE_MAX = 500
ROLES = ("viewer", "agent", "admin")
WRITERS = ("agent", "admin")
HASH_SCHEME = "pbkdf2_sha256"
HASH_ITERATIONS = 600_000
REALM = "reef-console"
log = logging.getLogger("reefweb.auth")


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    kind: str  # "human" (a browser session) or "token"
    can_write: bool  # may add records: agent and admin


class IdentityUnavailable(Exception):
    pass


class _Cache:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, Principal | None]] = {}

    @staticmethod
    def key(*parts: str) -> str:
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()

    def get(self, key: str) -> tuple[bool, Principal | None]:
        hit = self._entries.get(key)
        if hit is not None and time.monotonic() - hit[0] < CACHE_SECONDS:
            return True, hit[1]
        return False, None

    def put(self, key: str, principal: Principal | None) -> None:
        if len(self._entries) >= CACHE_MAX:
            self._entries.clear()
        self._entries[key] = (time.monotonic(), principal)

    def clear(self) -> None:
        self._entries.clear()


class Identity:
    """forward mode: another service answers who the caller is (the class keeps this name for the tests and callers)."""

    mode = "forward"
    protects_pages = False  # basic: the page itself asks for the sign-in, so the browser keeps it for /reef/
    challenge: str | None = None  # the WWW-Authenticate value sent with a 401

    def __init__(self, url: str, cookie: str, transport: httpx.AsyncBaseTransport | None = None, sign_in: str = "") -> None:
        self.url = url
        self.cookie = cookie
        self.sign_in = sign_in
        self._cache = _Cache()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(5.0), transport=transport)

    @classmethod
    def from_env(cls) -> Identity:
        return identity_from_env()

    async def lookup(self, authorization: str | None, cookie: str | None) -> Principal | None:
        """The principal behind a bearer token or a session cookie; None when there is none or the service says no."""
        if authorization and authorization[:7].lower() == "bearer ":
            headers = {"Authorization": authorization}
        elif cookie:
            headers = {"Cookie": f"{self.cookie}={cookie}"}
        else:
            return None
        if not self.url:
            return None
        key = self._cache.key(*sorted(f"{k}={v}" for k, v in headers.items()))
        hit, principal = self._cache.get(key)
        if hit:
            return principal
        try:
            response = await self._client.get(self.url, headers=headers)
        except httpx.HTTPError as exc:
            raise IdentityUnavailable(f"the sign-in check did not answer: {exc.__class__.__name__}") from exc
        if response.status_code in (401, 403):
            principal = None
        elif response.status_code != 200:
            raise IdentityUnavailable(f"the sign-in check answered HTTP {response.status_code}")
        else:
            data = response.json()
            principal = Principal(str(data["name"]), str(data["role"]), str(data.get("kind", "")), bool(data.get("can_request")))
        self._cache.put(key, principal)
        return principal

    async def close(self) -> None:
        await self._client.aclose()


def hash_password(password: str, iterations: int = HASH_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{HASH_SCHEME}${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != HASH_SCHEME:
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, binascii.Error):
        return False


def parse_users(text: str) -> dict[str, tuple[str, str]]:
    """name -> (role, hash) from the users file; a malformed line stops the start, so a typo never locks out quietly."""
    users: dict[str, tuple[str, str]] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 2)
        if len(parts) != 3 or not parts[0] or parts[1] not in ROLES or not parts[2].startswith(HASH_SCHEME + "$"):
            raise ValueError(f"users file line {number}: expected name:role:hash with a role of {', '.join(ROLES)}")
        users[parts[0]] = (parts[1], parts[2])
    return users


class BasicIdentity(Identity):
    """basic mode: reef-web's own users file."""

    mode = "basic"
    protects_pages = True
    challenge = f'Basic realm="{REALM}", charset="UTF-8"'

    def __init__(self, path: Path) -> None:
        self.path = path
        self.cookie = ""
        self.sign_in = ""
        self._cache = _Cache()
        self._stamp: tuple[float, int] | None = None
        self._users: dict[str, tuple[str, str]] = {}
        self._load()

    def _load(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            if self._stamp != (0.0, 0):
                log.warning("no users file at %s: nobody can sign in (python -m reefweb.auth NAME ROLE makes a line)", self.path)
            self._stamp, self._users = (0.0, 0), {}
            self._cache.clear()
            return
        stamp = (stat.st_mtime, stat.st_size)
        if stamp == self._stamp:
            return
        try:
            users = parse_users(self.path.read_text(encoding="utf-8"))
        except ValueError:
            if self._stamp is None:
                raise  # at start: stop, so a typo is seen at once
            log.exception("the users file changed and has a mistake: keeping the users read before")
            self._stamp = stamp
            return
        self._users, self._stamp = users, stamp
        self._cache.clear()

    async def lookup(self, authorization: str | None, cookie: str | None) -> Principal | None:
        if not authorization:
            return None
        scheme, _, value = authorization.partition(" ")
        scheme = scheme.lower()
        if scheme == "basic":
            try:
                pair, kind = base64.b64decode(value.strip(), validate=True).decode("utf-8"), "human"
            except (binascii.Error, UnicodeDecodeError):
                return None
        elif scheme == "bearer":
            pair, kind = value.strip(), "token"
        else:
            return None
        name, colon, password = pair.partition(":")
        if not colon:
            return None
        self._load()
        key = self._cache.key(kind, name, password)
        hit, principal = self._cache.get(key)
        if hit:
            return principal
        entry = self._users.get(name)
        principal = None
        if entry is not None and verify_password(password, entry[1]):
            principal = Principal(name, entry[0], kind, entry[0] in WRITERS)
        self._cache.put(key, principal)
        return principal

    async def close(self) -> None:
        return None


class OpenIdentity(Identity):
    """none mode: everyone is the local admin."""

    mode = "none"

    def __init__(self) -> None:
        self.cookie = ""
        self.sign_in = ""

    async def lookup(self, authorization: str | None, cookie: str | None) -> Principal | None:
        bearer = (authorization or "")[:7].lower() == "bearer "
        return Principal("local", "admin", "token" if bearer else "human", True)

    async def close(self) -> None:
        return None


def identity_from_env() -> Identity:
    url = os.environ.get("REEF_AUTH_URL", "")
    mode = os.environ.get("REEF_AUTH", "forward" if url else "basic").strip().lower()
    if mode == "forward":
        return Identity(url, os.environ.get("REEF_AUTH_COOKIE", "session"), sign_in=os.environ.get("REEF_SIGN_IN_URL", ""))
    if mode == "basic":
        return BasicIdentity(Path(os.environ.get("REEF_USERS_FILE", "/app/config/users.txt")))
    if mode == "none":
        log.warning("REEF_AUTH=none: no sign-in, every caller may read and record")
        return OpenIdentity()
    raise ValueError(f"REEF_AUTH is basic, forward or none, not {mode!r}")


def unauthorized(identity: Identity, detail: str = "not signed in") -> HTTPException:
    headers = {}
    if identity.challenge:
        headers["WWW-Authenticate"] = identity.challenge
    if identity.sign_in:
        headers["X-Sign-In"] = identity.sign_in  # the app sends the browser there
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers=headers or None)


async def caller(request: Request) -> Principal | None:
    """The signed-in caller, or None."""
    identity: Identity = request.app.state.identity
    try:
        cookie = request.cookies.get(identity.cookie) if identity.cookie else None
        return await identity.lookup(request.headers.get("authorization"), cookie)
    except IdentityUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


async def reader(request: Request) -> Principal:
    """FastAPI dependency: a signed-in caller, with the CSRF header on a browser session's writes."""
    principal = await caller(request)
    if principal is None:
        raise unauthorized(request.app.state.identity)
    authorization = request.headers.get("authorization") or ""
    bearer = authorization[:7].lower() == "bearer "
    if not bearer and request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing X-Requested-With header")
    return principal


async def writer(principal: Annotated[Principal, Depends(reader)]) -> Principal:
    if not principal.can_write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{principal.name} ({principal.role}) may read but not add records")
    return principal


def main(argv: list[str]) -> int:
    """python -m reefweb.auth NAME ROLE: prints a users-file line; the password is read from the terminal or stdin."""
    if len(argv) != 2 or argv[1] not in ROLES or ":" in argv[0] or not argv[0]:
        print(f"usage: python -m reefweb.auth NAME ROLE   (ROLE: {', '.join(ROLES)})", file=sys.stderr)
        return 2
    password = getpass.getpass("password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\r\n")
    if len(password) < 8:
        print("a password of at least 8 characters, please", file=sys.stderr)
        return 2
    print(f"{argv[0]}:{argv[1]}:{hash_password(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
