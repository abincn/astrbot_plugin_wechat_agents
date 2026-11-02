"""Evidence references, independent approval and draft-only Outbox handoff."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

from .identity import group_session
from .router_core import RouterCore, _identity, _required


class EvidenceOperations(RouterCore):
    def create_ticket_draft(self, owner: str, room: str, ticket_id: str, *,
                            actor: str) -> int:
        """Create an idempotent draft from an evidence-backed completed Ticket.

        This records a draft_only Outbox row. It never calls the Hook or sends.
        """
        group_session(owner, room)
        actor = _required(actor, "actor", 120)
        with self._transaction():
            ticket = self._ticket(ticket_id, owner, room)
            if ticket["status"] != "completed" or not ticket["result"]:
                raise ValueError("Only completed Tickets can create an Outbox draft")
            evidence_count = self.conn.execute(
                "SELECT COUNT(*) FROM router_evidence WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()[0]
            if not evidence_count:
                raise ValueError("Outbox draft requires an evidence-backed Ticket")
            thread = self._thread(ticket["thread_id"], owner, room)
            source_event_id = thread["trigger_event_id"]
            intent_id = "ticket-draft-" + _identity(ticket_id)
            content = ticket["result"][:30000]
            existing = self.conn.execute(
                "SELECT id,owner,room,content,status,source_event_id FROM outbound "
                "WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            if existing:
                if (
                    existing["owner"] != owner
                    or existing["room"] != room
                    or existing["content"] != content
                    or existing["source_event_id"] != source_event_id
                ):
                    attempt_hash = hashlib.sha256("\x1f".join((
                        intent_id, owner, room, source_event_id, content,
                    )).encode("utf-8")).hexdigest()
                    self.conn.execute(
                        "INSERT OR IGNORE INTO outbound_conflicts"
                        "(intent_id,existing_outbound_id,requested_owner,requested_room,"
                        "requested_source_event_id,requested_content,detected_at,attempt_hash) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (intent_id, existing["id"], owner, room, source_event_id,
                         content, int(self._clock()), attempt_hash),
                    )
                    self._audit(ticket["thread_id"], ticket_id,
                                "outbound_draft_conflict", actor,
                                f"outbound_id={existing['id']}")
                    raise ValueError("Ticket draft intent conflicts with existing Outbox content")
                return int(existing["id"])
            cursor = self.conn.execute(
                "INSERT INTO outbound(owner,room,content,status,created_at,intent_id,source_event_id) "
                "VALUES(?,?,?,'draft_only',?,?,?)",
                (owner, room, content, int(self._clock()), intent_id, source_event_id),
            )
            outbound_id = int(cursor.lastrowid)
            self._audit(ticket["thread_id"], ticket_id, "outbound_draft_created",
                        actor, f"outbound_id={outbound_id};evidence_count={evidence_count}")
            return outbound_id

    def record_evidence(self, owner: str, room: str, ticket_id: str,
                        source_type: str, source_ref: str, summary: str, *,
                        actor: str) -> str:
        """Attach a human-curated, independently inspectable reference to a candidate."""
        group_session(owner, room)
        source_type = _required(source_type, "source_type", 32)
        source_ref = _required(source_ref, "source_ref", 1024)
        summary = _required(summary, "summary", 2000)
        actor = _required(actor, "actor", 120)
        if source_type not in ("inbox_message", "https_url", "sha256"):
            raise ValueError("Unsupported evidence source_type")
        if source_type == "https_url":
            parsed = urlsplit(source_ref)
            if (
                parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError("Evidence URL must be an HTTPS URL without credentials or fragment")
        elif source_type == "sha256" and not re.fullmatch(
            r"[0-9a-fA-F]{64}", source_ref
        ):
            raise ValueError("sha256 evidence must be a 64-character hexadecimal digest")

        evidence_id = "evidence-" + _identity(ticket_id, source_type, source_ref)
        with self._transaction():
            row = self._ticket(ticket_id, owner, room)
            if (
                row["status"] != "review"
                or row["review_reason"] not in (
                    "candidate_requires_eval", "candidate_rejected"
                )
                or not row["result"]
            ):
                raise ValueError("Ticket has no reviewable candidate")
            if source_type == "inbox_message":
                source = self.conn.execute(
                    "SELECT 1 FROM inbox WHERE event_id=? AND owner=? AND room=? "
                    "AND event_id<>?",
                    (source_ref, owner, room, row["trigger_event_id"]),
                ).fetchone()
                if source is None:
                    raise ValueError(
                        "Evidence message must be a different persisted message in this account/group"
                    )
            old = self.conn.execute(
                "SELECT evidence_id,summary FROM router_evidence "
                "WHERE ticket_id=? AND source_type=? AND source_ref=?",
                (ticket_id, source_type, source_ref),
            ).fetchone()
            if old:
                if old["summary"] != summary:
                    raise ValueError("Conflicting summary for immutable evidence reference")
                return old["evidence_id"]
            self.conn.execute(
                "INSERT INTO router_evidence"
                "(evidence_id,ticket_id,source_type,source_ref,summary,actor,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (evidence_id, ticket_id, source_type, source_ref, summary,
                 actor, int(self._clock())),
            )
            self._audit(row["thread_id"], ticket_id, "evidence_recorded",
                        actor, f"type={source_type};id={evidence_id}")
        return evidence_id

    def list_evidence(self, owner: str, room: str, ticket_id: str) -> list[dict]:
        group_session(owner, room)
        with self._lock:
            self._ticket(ticket_id, owner, room)
            return [dict(row) for row in self.conn.execute(
                "SELECT evidence_id,source_type,source_ref,summary,actor,created_at "
                "FROM router_evidence WHERE ticket_id=? ORDER BY created_at,evidence_id",
                (ticket_id,),
            )]

    def approve_candidate(self, owner: str, room: str, ticket_id: str, *,
                          reviewer: str, reason: str) -> None:
        """Approve only after at least one independent evidence reference is attached."""
        group_session(owner, room)
        reviewer = _required(reviewer, "reviewer", 120)
        reason = _required(reason, "reason", 1000)
        with self._transaction():
            row = self._ticket(ticket_id, owner, room)
            if (
                row["status"] != "review"
                or row["review_reason"] not in (
                    "candidate_requires_eval", "candidate_rejected"
                )
                or not row["result"]
            ):
                raise ValueError("Ticket has no reviewable candidate")
            evidence = self.conn.execute(
                "SELECT COUNT(*) AS total FROM router_evidence WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()["total"]
            if not evidence:
                raise ValueError("Candidate approval requires independent evidence")
            self.conn.execute(
                "UPDATE router_tickets SET status='completed',review_reason='',"
                "updated_at=? WHERE ticket_id=? AND status='review'",
                (int(self._clock()), ticket_id),
            )
            self._audit(row["thread_id"], ticket_id, "candidate_approved",
                        reviewer, f"evidence_count={evidence};{reason}")
