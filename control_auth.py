"""Bearer-token authentication and account/group capability grants."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from .identity import group_session


CAPABILITIES = frozenset({
    "thread:create",
    "thread:read",
    "thread:complete",
    "ticket:create",
    "ticket:read",
    "ticket:dispatch",
    "ticket:review",
    "ticket:retry",
    "subscription:manage",
    "subscription:dispatch",
    "subscription:read",
    "audit:read",
})


def new_operator_credential() -> tuple[str, str]:
    """Return (bearer_token, sha256_hex); persist only the hash in grant config."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return token, digest


def _required(value: object, label: str, limit: int = 160) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds {limit} bytes")
    return value.strip()


@dataclass(frozen=True)
class _Grant:
    token_sha256: str
    subject: str
    owner: str
    rooms: tuple[str, ...]
    capabilities: frozenset[str]


@dataclass(frozen=True)
class ControlPrincipal:
    """Signed in-memory identity; not a serializable bearer credential."""

    subject: str
    owner: str
    rooms: tuple[str, ...]
    capabilities: frozenset[str]
    _signature: str = field(repr=False)


class ControlAuthenticator:
    """Verify bearer tokens against SHA-256-only, explicitly scoped grants."""

    def __init__(self, grants: Iterable[Mapping[str, object]]):
        self._signing_key = secrets.token_bytes(32)
        self._grants: list[_Grant] = []
        seen: set[str] = set()
        for raw in grants:
            if not isinstance(raw, Mapping):
                raise ValueError("Each operator grant must be an object")
            digest = raw.get("token_sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("token_sha256 must be 64 lowercase hexadecimal characters")
            if digest in seen:
                raise ValueError("Duplicate operator token hash")
            seen.add(digest)
            subject = _required(raw.get("subject"), "subject", 120)
            owner = _required(raw.get("owner"), "owner", 160)
            rooms_raw = raw.get("rooms")
            caps_raw = raw.get("capabilities")
            if not isinstance(rooms_raw, (list, tuple)) or not rooms_raw:
                raise ValueError("rooms must be a non-empty exact allowlist")
            if not isinstance(caps_raw, (list, tuple)) or not caps_raw:
                raise ValueError("capabilities must be a non-empty allowlist")
            rooms = tuple(sorted({
                _required(room, "room", 240) for room in rooms_raw
            }))
            for room in rooms:
                group_session(owner, room)
            capabilities = frozenset(
                _required(cap, "capability", 80) for cap in caps_raw
            )
            unknown = capabilities - CAPABILITIES
            if unknown:
                raise ValueError("Unknown operator capabilities: " + ",".join(sorted(unknown)))
            self._grants.append(_Grant(digest, subject, owner, rooms, capabilities))
        if not self._grants:
            raise ValueError("At least one operator grant is required")

    @classmethod
    def from_json(cls, content: str) -> "ControlAuthenticator":
        if not isinstance(content, str) or len(content.encode("utf-8")) > 65536:
            raise ValueError("Operator grant JSON must be at most 64 KiB")
        data = json.loads(content)
        if not isinstance(data, list):
            raise ValueError("Operator grant config must be a JSON array")
        return cls(data)

    @classmethod
    def from_file(cls, path: str | Path) -> "ControlAuthenticator":
        location = Path(path).expanduser()
        if location.stat().st_size > 65536:
            raise ValueError("Operator grant file must be at most 64 KiB")
        return cls.from_json(location.read_text(encoding="utf-8"))

    def _signature(self, subject: str, owner: str, rooms: tuple[str, ...],
                   capabilities: frozenset[str]) -> str:
        payload = json.dumps(
            [subject, owner, list(rooms), sorted(capabilities)],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._signing_key, payload, hashlib.sha256).hexdigest()

    def authenticate(self, token: str) -> ControlPrincipal:
        if not isinstance(token, str) or not 32 <= len(token) <= 512:
            raise PermissionError("Invalid operator credential")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched = None
        # Compare every configured digest; do not reveal which grant matched.
        for grant in self._grants:
            if hmac.compare_digest(digest, grant.token_sha256):
                matched = grant
        if matched is None:
            raise PermissionError("Invalid operator credential")
        signature = self._signature(
            matched.subject, matched.owner, matched.rooms, matched.capabilities
        )
        return ControlPrincipal(
            matched.subject, matched.owner, matched.rooms,
            matched.capabilities, signature,
        )

    def authorize(self, principal: ControlPrincipal, owner: str, room: str,
                  capability: str) -> None:
        if not isinstance(principal, ControlPrincipal):
            raise PermissionError("Authenticated control principal required")
        expected = self._signature(
            principal.subject, principal.owner,
            principal.rooms, principal.capabilities,
        )
        if not hmac.compare_digest(expected, principal._signature):
            raise PermissionError("Invalid control principal")
        group_session(owner, room)
        if principal.owner != owner or room not in principal.rooms:
            raise PermissionError("Operator is not authorized for this account/group")
        if capability not in principal.capabilities:
            raise PermissionError("Operator lacks capability: " + capability)
