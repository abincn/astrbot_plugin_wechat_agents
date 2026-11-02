"""Bounded, scoped read models for the AstrBot task view; never return claims."""
from __future__ import annotations


def task_snapshot(control, token: str, owner: str, room: str) -> dict:
    principal = control.authenticator.authenticate(token)
    result = {"messages": [], "threads": [], "tickets": [], "subscriptions": []}
    # Authorize each dataset separately. A dispatch-only grant cannot read it.
    with control.router._lock:
        conn = control.router.conn
        if "thread:read" in principal.capabilities:
            control.authenticator.authorize(principal, owner, room, "thread:read")
            result["messages"] = [dict(row) for row in conn.execute(
                "SELECT event_id,sender,substr(content,1,4000) AS content,"
                "kind,status,created_at FROM inbox WHERE owner=? AND room=? "
                "ORDER BY sequence DESC LIMIT 60", (owner, room),
            )]
            result["threads"] = [dict(row) for row in conn.execute(
                "SELECT thread_id,trigger_event_id,goal,status,created_at "
                "FROM router_threads WHERE owner=? AND room=? "
                "ORDER BY created_at DESC,thread_id LIMIT 80", (owner, room),
            )]
        if "ticket:read" in principal.capabilities:
            control.authenticator.authorize(principal, owner, room, "ticket:read")
            result["tickets"] = [dict(row) for row in conn.execute(
                "SELECT t.ticket_id,t.thread_id,t.task,t.status,t.review_reason,"
                "t.created_at,t.updated_at FROM router_tickets t "
                "JOIN router_threads h ON h.thread_id=t.thread_id "
                "WHERE h.owner=? AND h.room=? "
                "ORDER BY t.updated_at DESC,t.ticket_id LIMIT 160", (owner, room),
            )]
        if "subscription:read" in principal.capabilities:
            control.authenticator.authorize(principal, owner, room, "subscription:read")
            result["subscriptions"] = [dict(row) for row in conn.execute(
                "SELECT subscription_id,thread_id,task,due_at,status,"
                "ticket_id,review_reason FROM router_subscriptions "
                "WHERE owner=? AND room=? ORDER BY due_at DESC LIMIT 80",
                (owner, room),
            )]
    return result


def thread_detail(control, token: str, owner: str, room: str, thread_id: str) -> dict:
    thread = control.get_thread(token, owner, room, thread_id)
    principal = control.authenticator.authenticate(token)
    result = {"thread": thread, "tickets": [], "audit": []}
    with control.router._lock:
        if "ticket:read" in principal.capabilities:
            control.authenticator.authorize(principal, owner, room, "ticket:read")
            result["tickets"] = [dict(row) for row in control.router.conn.execute(
                "SELECT ticket_id,thread_id,task,status,review_reason,updated_at "
                "FROM router_tickets WHERE thread_id=? "
                "ORDER BY updated_at DESC,ticket_id LIMIT 60", (thread_id,),
            )]
        if "audit:read" in principal.capabilities:
            control.authenticator.authorize(principal, owner, room, "audit:read")
            result["audit"] = [dict(row) for row in control.router.conn.execute(
                "SELECT action,actor,detail,created_at FROM router_audit "
                "WHERE thread_id=? ORDER BY id DESC LIMIT 80", (thread_id,),
            )]
    return result
