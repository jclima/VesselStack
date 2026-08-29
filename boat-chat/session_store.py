#!/usr/bin/env python3
"""Durable chat sessions, request audits, and answer feedback."""
from __future__ import annotations
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
import memory_index

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

def normalize_session_id(value: Any) -> str:
    session_id = str(value or "").strip()
    return session_id if SESSION_ID_RE.fullmatch(session_id) else ""

def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or memory_index.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.executescript("""
    create table if not exists chat_sessions (
        session_id text primary key, turns text not null, query_plan text, updated_at integer not null
    );
    create table if not exists chat_requests (
        request_id text primary key, session_id text, question_hash text not null,
        query_plan text, context_profile text, elapsed_ms integer, outcome text not null, created_at integer not null
    );
    create table if not exists chat_feedback (
        id integer primary key autoincrement, request_id text not null, session_id text,
        rating text not null, note text, created_at integer not null
    );
    create table if not exists maintenance_tasks (
        id integer primary key autoincrement,
        title text not null,
        due_date text,
        notes text not null default '',
        completed integer not null default 0,
        updated_at integer not null
    );
    create index if not exists chat_feedback_request on chat_feedback(request_id);
    """)
    conn.commit()
    return conn

def get_session(session_id: str, path: Path | None = None) -> dict[str, Any]:
    session_id = normalize_session_id(session_id)
    if not session_id: return {"turns": [], "query_plan": {}}
    conn = connect(path); row = conn.execute("select turns, query_plan from chat_sessions where session_id=?", (session_id,)).fetchone(); conn.close()
    if not row: return {"turns": [], "query_plan": {}}
    try: turns = json.loads(row["turns"])
    except Exception: turns = []
    try: plan = json.loads(row["query_plan"] or "{}")
    except Exception: plan = {}
    return {"turns": turns[-8:] if isinstance(turns, list) else [], "query_plan": plan if isinstance(plan, dict) else {}}

def append_exchange(session_id: str, message: str, answer: str, query_plan: dict[str, Any], path: Path | None = None) -> None:
    session_id = normalize_session_id(session_id)
    if not session_id: return
    current = get_session(session_id, path)
    turns = (current["turns"] + [{"role":"user","content":message[:4000]}, {"role":"assistant","content":answer[:4000]}])[-8:]
    conn = connect(path)
    with conn:
        conn.execute("insert into chat_sessions(session_id,turns,query_plan,updated_at) values(?,?,?,?) on conflict(session_id) do update set turns=excluded.turns,query_plan=excluded.query_plan,updated_at=excluded.updated_at", (session_id,json.dumps(turns),json.dumps(query_plan,default=str),int(time.time())))
    conn.close()

def clear_session(session_id: str, path: Path | None = None) -> None:
    session_id = normalize_session_id(session_id)
    if not session_id: return
    conn=connect(path)
    with conn: conn.execute("delete from chat_sessions where session_id=?",(session_id,))
    conn.close()

def record_request(request_id: str, session_id: str, question_hash: str, query_plan: dict[str, Any], context_profile: dict[str, Any], elapsed_ms: int, outcome: str, path: Path | None = None) -> None:
    conn=connect(path)
    with conn: conn.execute("insert or replace into chat_requests values(?,?,?,?,?,?,?,?)",(request_id,normalize_session_id(session_id) or None,question_hash,json.dumps(query_plan,default=str),json.dumps(context_profile,default=str),elapsed_ms,outcome,int(time.time())))
    conn.close()

def record_feedback(request_id: str, session_id: str, rating: str, note: str = "", path: Path | None = None) -> None:
    if rating not in {"helpful","incomplete","wrong"}: raise ValueError("rating must be helpful, incomplete, or wrong")
    conn=connect(path)
    if not conn.execute("select 1 from chat_requests where request_id=?", (request_id[:64],)).fetchone():
        conn.close(); raise ValueError("unknown request_id")
    with conn: conn.execute("insert into chat_feedback(request_id,session_id,rating,note,created_at) values(?,?,?,?,?)",(request_id[:64],normalize_session_id(session_id) or None,rating,note[:1000],int(time.time())))
    conn.close()

def status(path: Path | None = None) -> dict[str, int]:
    conn=connect(path)
    result={name:int(conn.execute(f"select count(*) from {name}").fetchone()[0]) for name in ("chat_sessions","chat_requests","chat_feedback")}
    conn.close(); return result


def list_maintenance(path: Path | None = None) -> list[dict[str, Any]]:
    conn = connect(path)
    rows = conn.execute("select id,title,due_date,notes,completed,updated_at from maintenance_tasks order by completed,due_date is null,due_date,id").fetchall()
    conn.close()
    return [{**dict(row), "completed": bool(row["completed"])} for row in rows]

def save_maintenance(payload: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    title = str(payload.get("title", "")).strip()[:160]
    if not title:
        raise ValueError("maintenance title is required")
    due_date = str(payload.get("due_date", "")).strip()[:10] or None
    if due_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_date):
        raise ValueError("due_date must use YYYY-MM-DD")
    notes = str(payload.get("notes", "")).strip()[:1000]
    completed = 1 if payload.get("completed") is True else 0
    task_id = int(payload.get("id") or 0)
    now = int(time.time())
    conn = connect(path)
    with conn:
        if task_id:
            result = conn.execute("update maintenance_tasks set title=?,due_date=?,notes=?,completed=?,updated_at=? where id=?", (title,due_date,notes,completed,now,task_id))
            if not result.rowcount:
                conn.close(); raise ValueError("unknown maintenance task")
        else:
            task_id = int(conn.execute("insert into maintenance_tasks(title,due_date,notes,completed,updated_at) values(?,?,?,?,?)", (title,due_date,notes,completed,now)).lastrowid)
    row = conn.execute("select id,title,due_date,notes,completed,updated_at from maintenance_tasks where id=?", (task_id,)).fetchone()
    conn.close()
    return {**dict(row), "completed": bool(row["completed"])}
