"""Polls Bluesky for each subscription and stores new posts. The only component with internet egress."""
import logging, os, time
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
from psycopg.rows import dict_row

DSN = os.environ["DATABASE_URL"]
POLL = int(os.environ.get("POLL_SECONDS", "60"))
FEED_BASE = os.environ.get("BSKY_FEED_BASE", "https://public.api.bsky.app")
SEARCH_BASE = os.environ.get("BSKY_SEARCH_BASE", "https://api.bsky.app")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s ingester %(levelname)s %(message)s")
log = logging.getLogger("ingester")


def parse_post(item: dict) -> tuple | None:
    rec = item.get("record") or {}
    text = (rec.get("text") or "").strip()
    if not text:
        return None
    ts = rec.get("createdAt") or item.get("indexedAt")
    try:
        posted = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        posted = datetime.now(timezone.utc)
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return item["uri"], (item.get("author") or {}).get("handle"), text, posted


def fetch(client: httpx.Client, kind: str, value: str) -> list[dict]:
    if kind == "handle":
        r = client.get(f"{FEED_BASE}/xrpc/app.bsky.feed.getAuthorFeed",
                       params={"actor": value, "limit": 50, "filter": "posts_no_replies"})
        r.raise_for_status()
        return [f["post"] for f in r.json().get("feed", [])]
    r = client.get(f"{SEARCH_BASE}/xrpc/app.bsky.feed.searchPosts",
                   params={"q": f"#{value}", "sort": "latest", "limit": 50})
    r.raise_for_status()
    return r.json().get("posts", [])


def run_once(client: httpx.Client) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        subs = conn.execute("SELECT id, kind, value FROM subscriptions").fetchall()
        conn.commit()  
        for s in subs:
            try:
                parsed = (parse_post(i) for i in fetch(client, s["kind"], s["value"]))
                posts = [p for p in parsed if p and p[3] >= cutoff]
            except httpx.HTTPError as e:
                log.warning("fetch failed for %s %s: %s", s["kind"], s["value"], e)
                continue
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO posts (subscription_id, uri, author, text, posted_at)
                       VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                    [(s["id"], *p) for p in posts],
                )
            conn.commit()  
            log.info("%s %s: %d fetched", s["kind"], s["value"], len(posts))
        conn.execute("DELETE FROM posts WHERE posted_at < %s", (cutoff,))
        conn.commit()


def main() -> None:
    with httpx.Client(timeout=15, headers={"User-Agent": "feed-analysis/0.1"}) as client:
        while True:
            try:
                run_once(client)
            except psycopg.Error as e: 
                log.warning("db error: %s", e)
            time.sleep(POLL)


if __name__ == "__main__":
    main()