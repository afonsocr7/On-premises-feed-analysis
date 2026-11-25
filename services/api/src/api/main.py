import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db

HANDLE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
TAG_RE = re.compile(r"^[\w]{1,64}$")
STATIC = Path(__file__).parent / "static"
WINDOW = int(os.environ.get("SUMMARY_WINDOW", "50"))  # summary = latest N classified posts


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    yield


app = FastAPI(title="Feed analysis", lifespan=lifespan)


class NewSub(BaseModel):
    kind: Literal["handle", "hashtag"]
    value: str


def normalize(kind: str, value: str) -> str:
    v = value.strip().lstrip("@#").lower() if kind == "handle" else value.strip().lstrip("#").lower()
    if not (HANDLE_RE if kind == "handle" else TAG_RE).match(v):
        raise HTTPException(422, f"invalid {kind}: {value!r}")
    return v


def overall(pos: int, neg: int, total: int) -> str:
    if not total:
        return "neutral"
    score = (pos - neg) / total
    return "positive" if score > 0.2 else "negative" if score < -0.2 else "neutral"


@app.get("/livez")
def livez():
    """Liveness: the process is up. Deliberately does NOT check the DB (a DB outage must not restart pods)."""
    return {"ok": True}


@app.get("/healthz")
def healthz():
    try:
        with db.connect() as c:
            c.execute("SELECT 1")
    except psycopg.Error:
        raise HTTPException(503, "db unavailable")
    return {"ok": True}


@app.get("/api/subscriptions")
def list_subscriptions():
    """Summary is derived from the labelled posts at read time, so item_count, counts and
    updated_at always come from the same snapshot (no summary table to go stale or race)."""
    with db.connect() as c:
        rows = c.execute(
            """SELECT s.id, s.kind, s.value, w.positive, w.neutral, w.negative,
                      w.item_count, w.updated_at
               FROM subscriptions s
               LEFT JOIN LATERAL (
                 SELECT count(*) FILTER (WHERE sentiment='positive') AS positive,
                        count(*) FILTER (WHERE sentiment='neutral')  AS neutral,
                        count(*) FILTER (WHERE sentiment='negative') AS negative,
                        count(*) AS item_count,
                        max(analyzed_at) AS updated_at
                 FROM (SELECT sentiment, analyzed_at FROM posts
                       WHERE subscription_id = s.id AND sentiment IS NOT NULL
                       ORDER BY posted_at DESC LIMIT %s) p
               ) w ON true
               ORDER BY s.created_at DESC""", (WINDOW,)).fetchall()
    for r in rows:
        r["label"] = overall(r["positive"], r["negative"], r["item_count"]) if r["item_count"] else None
    return rows


@app.post("/api/subscriptions", status_code=201)
def subscribe(body: NewSub):
    value = normalize(body.kind, body.value)
    with db.connect() as c:
        row = c.execute(
            """INSERT INTO subscriptions (kind, value) VALUES (%s, %s)
               ON CONFLICT (kind, value) DO UPDATE SET kind = EXCLUDED.kind
               RETURNING id, kind, value""",
            (body.kind, value),
        ).fetchone()
    return row


@app.delete("/api/subscriptions/{sub_id}", status_code=204)
def unsubscribe(sub_id: int):
    with db.connect() as c:
        n = c.execute("DELETE FROM subscriptions WHERE id = %s", (sub_id,)).rowcount
    if not n:
        raise HTTPException(404, "not found")
    return Response(status_code=204)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")