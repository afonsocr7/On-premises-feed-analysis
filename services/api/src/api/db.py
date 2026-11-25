import os, time
import psycopg
from psycopg.rows import dict_row

DSN = os.environ["DATABASE_URL"]


SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
  id         SERIAL PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('handle','hashtag')),
  value      TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (kind, value)
);
CREATE TABLE IF NOT EXISTS posts (
  uri             TEXT NOT NULL,
  subscription_id INT  NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
  author          TEXT,
  text            TEXT NOT NULL,
  posted_at       TIMESTAMPTZ NOT NULL,
  sentiment       TEXT CHECK (sentiment IN ('positive','neutral','negative')),
  analyzed_at     TIMESTAMPTZ,
  PRIMARY KEY (subscription_id, uri)
);
-- additive migrations (safe to re-run; old and new pods overlap during a rolling update)
ALTER TABLE posts ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS posts_pending ON posts (posted_at DESC) WHERE sentiment IS NULL;
CREATE INDEX IF NOT EXISTS posts_window ON posts (subscription_id, posted_at DESC) WHERE sentiment IS NOT NULL;
"""

def connect():
    return psycopg.connect(DSN, row_factory=dict_row)

def init_schema(retries: int = 120):
    for i in range(retries):
        try:
            with connect() as c:
                c.execute("SELECT pg_advisory_lock(42)") 
                c.execute(SCHEMA)
                c.execute("SELECT pg_advisory_unlock(42)")
            return
        except psycopg.OperationalError:
            time.sleep(2)
    raise RuntimeError("database unavailable")