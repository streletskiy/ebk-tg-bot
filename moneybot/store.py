import asyncio
import json
import time

import psycopg
from psycopg.rows import dict_row

from .telegram import Reply
from .replies import styled_reply


class Store:
    LOCK_ID = 5570758412811194196

    def __init__(self, settings, *, worker=False):
        self.work_ready = asyncio.Event()
        self.feedback_ready = asyncio.Event()
        self.db = psycopg.connect(**settings.database(), autocommit=True, row_factory=dict_row)
        try:
            self.db.execute(SCHEMA)
            if worker:
                self.acquire_worker_lock()
                self.share_memories()
                self.db.execute("UPDATE jobs SET status='queued',progress=NULL WHERE status='processing'")
        except BaseException:
            self.db.close()
            raise

    def acquire_worker_lock(self):
        if not self.db.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (self.LOCK_ID,)).fetchone()["acquired"]:
            raise RuntimeError("Another worker or migration is running")

    def transaction(self):
        return self.db.transaction()

    def close(self):
        self.db.close()

    def counts(self, user):
        return self.db.execute("SELECT status,count(*) AS count FROM jobs WHERE user_id=%s GROUP BY status", (user,)).fetchall()

    def meta(self, key, default="0"):
        row = self.db.execute("SELECT value FROM meta WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT INTO meta VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key, str(value)))

    def ingest(self, updates, contacts, bot_id=0):
        with self.transaction():
            for update in updates:
                uid = update["update_id"]
                msg = update.get("message", {})
                callback = update.get("callback_query")
                if callback:
                    self.ingest_callback(uid, callback, contacts, bot_id)
                    self.set_meta("offset", uid + 1)
                    continue
                sender = msg.get("from", {})
                user = sender.get("id")
                # Ignore edited messages, bots, groups, channels and all unknown users before persistence.
                if user in contacts and not sender.get("is_bot") and msg.get("chat", {}).get("type") == "private" and msg["chat"]["id"] == user:
                    self.db.execute("INSERT INTO jobs(id,user_id,payload,created,source_message_id) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING",
                                    (uid, user, json.dumps(msg, ensure_ascii=False), time.time(), msg.get("message_id")))
                self.db.execute("INSERT INTO meta VALUES('offset',%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (str(uid + 1),))
        if updates:
            self.work_ready.set()
            self.feedback_ready.set()

    def next_job(self):
        # Per-user ordering preserves clarification context across retries.
        now = time.time()
        row = self.db.execute("""SELECT * FROM jobs j WHERE status='queued' AND next_attempt<=%s
            AND (created<=%s OR EXISTS(SELECT 1 FROM job_feedback f WHERE f.job_id=j.id))
            AND NOT EXISTS(SELECT 1 FROM jobs older WHERE older.user_id=j.user_id AND older.id<j.id
                AND older.status IN ('queued','processing')) ORDER BY id LIMIT 1""", (now, now - 2)).fetchone()
        if row:
            self.db.execute("UPDATE jobs SET status='processing',attempts=attempts+1,progress=NULL WHERE id=%s", (row["id"],))
            return row

    def finish(self, job, response):
        if isinstance(response, str):
            response = styled_reply(response)
        markup = json.dumps(response.markup, ensure_ascii=False) if isinstance(response, Reply) else "{}"
        text = response.text if isinstance(response, Reply) else response
        entities = json.dumps(response.entities) if isinstance(response, Reply) else '[]'
        self.db.execute("UPDATE jobs SET status='done',response=%s,reply_markup=%s,reply_entities=%s,payload='{}',error=NULL,notified=feedback_disabled,progress=NULL,delivery_after=0 WHERE id=%s", (text, markup, entities, job))
        self.feedback_ready.set()

    def ingest_callback(self, uid, callback, contacts, bot_id):
        sender, msg = callback.get("from", {}), callback.get("message", {})
        user, data = sender.get("id"), callback.get("data", "")
        if (type(user) is not int or user not in contacts or sender.get("is_bot")
                or msg.get("chat", {}).get("type") != "private" or msg["chat"]["id"] != user
                or msg.get("from", {}).get("id") != bot_id or not isinstance(data, str)
                or not 1 <= len(data.encode()) <= 64 or not isinstance(callback.get("id"), str)):
            return
        previous = self.db.execute("""SELECT f.*,j.id AS owner FROM job_feedback f JOIN jobs j ON j.id=f.job_id
            WHERE j.user_id=%s AND f.message_id=%s ORDER BY j.created DESC,j.id DESC LIMIT 1""",
            (user, msg.get("message_id"))).fetchone()
        if not previous:
            return
        markup = json.loads(previous["sent_markup"] or '{}')
        allowed = {b.get("callback_data") for row in markup.get("inline_keyboard", []) for b in row}
        payload = {"_callback": data if data in allowed else "q:stale" if data.startswith("q:") else "stale",
                   "message_id": msg["message_id"]}
        if data == "retry" and data in allowed:
            payload["_retry_job"] = previous["owner"]
        inserted = self.db.execute("""INSERT INTO jobs(id,user_id,payload,created,source_message_id,callback_id)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING RETURNING id""",
            (uid, user, json.dumps(payload), time.time(), msg["message_id"], callback["id"][:200])).fetchone()
        if inserted:
            # Superseded navigation has no financial side effects. Keep every mutation
            # and every callback acknowledgement, but render only the latest destination.
            self.db.execute("""UPDATE jobs SET status='done',response='',payload='{}',progress=NULL
                WHERE user_id=%s AND id<%s AND status='queued'
                AND id IN (SELECT job_id FROM job_feedback WHERE message_id=%s)
                AND (payload::jsonb->>'_callback') ~ '^s:[^:]+:(page|row|review|categories|accounts|questions):'
                AND NOT EXISTS(SELECT 1 FROM jobs barrier WHERE barrier.user_id=jobs.user_id
                    AND barrier.id>jobs.id AND barrier.id<%s AND barrier.status IN ('queued','processing')
                    AND COALESCE(barrier.payload::jsonb->>'_callback','') !~ '^s:[^:]+:(page|row|review|categories|accounts|questions):')
                AND NOT EXISTS(SELECT 1 FROM meta WHERE key='plan:' || jobs.id::text)""", (user, uid, msg["message_id"], uid))
            # Only the latest queued action owns this interactive message. Financial jobs
            # still finish in order, but an older dispatcher result cannot restore stale buttons.
            self.db.execute("""UPDATE jobs SET feedback_disabled=1,notified=1 WHERE user_id=%s AND id<>%s
                AND id IN (SELECT job_id FROM job_feedback WHERE message_id=%s)""", (user, uid, msg["message_id"]))
            self.save_feedback(uid, 0, msg["message_id"], previous["sent_text"], previous["sent_markup"], previous["sent_entities"])

    def pending_callbacks(self):
        return self.db.execute("SELECT id,user_id,callback_id FROM jobs WHERE callback_id IS NOT NULL AND callback_answered=0 ORDER BY created LIMIT 20").fetchall()

    def callback_answered(self, job):
        self.db.execute("UPDATE jobs SET callback_answered=1 WHERE id=%s", (job,))

    def resume_statements(self, contacts):
        for record in self.db.execute("SELECT user_id,content FROM conversations WHERE updated>%s", (time.time()-86400,)).fetchall():
            user = record["user_id"]
            statement = json.loads(record["content"]).get("statement")
            if user not in contacts or not statement or statement.get("ui_version", 0) >= 1:
                continue
            job = self.db.execute("""SELECT id,source_message_id FROM jobs WHERE user_id=%s AND status='done'
                AND response LIKE %s AND NOT EXISTS(SELECT 1 FROM meta WHERE key='plan:' || jobs.id::text)
                ORDER BY id DESC LIMIT 1""", (user, '%: прочитано ' + str(len(statement['rows'])) + ' операций.%')).fetchone()
            if job:
                statement["ui_version"] = 1
                with self.transaction():
                    self.save_context(user, {"statement": statement})
                    self.db.execute("""UPDATE jobs SET status='queued',payload=%s,response=NULL,notified=0,
                        feedback_disabled=0,next_attempt=0,attempts=0 WHERE id=%s""",
                        (json.dumps({"_resume_statement": statement["token"]}), job["id"]))

    def fail(self, job, code, retry, response=None):
        self.db.execute("UPDATE jobs SET status=%s,error=%s,next_attempt=%s,response=%s,notified=feedback_disabled,progress=NULL WHERE id=%s",
                        ("queued" if retry else "failed", code, time.time() + 30, response, job))
        self.feedback_ready.set()

    def refresh_statement_menus(self, contacts):
        for record in self.db.execute("SELECT user_id,content FROM conversations WHERE updated>%s", (time.time()-86400,)).fetchall():
            user = record["user_id"]
            statement = json.loads(record["content"]).get("statement")
            if user not in contacts or not statement or not statement.get("ready") or statement.get("ui_version", 0) >= 4:
                continue
            job = self.db.execute("""SELECT id FROM jobs WHERE user_id=%s AND status='done' AND reply_markup LIKE %s
                AND EXISTS(SELECT 1 FROM job_feedback WHERE job_id=jobs.id)
                AND NOT EXISTS(SELECT 1 FROM meta WHERE key='plan:' || jobs.id::text) ORDER BY id DESC LIMIT 1""",
                (user, '%s:' + statement['token'] + ':%')).fetchone()
            if not job:
                continue
            with self.transaction():
                statement["ui_version"] = 4
                self.save_context(user, {"statement": statement})
                self.db.execute("""UPDATE jobs SET status='queued',payload=%s,response=NULL,notified=0,
                    feedback_disabled=0,next_attempt=0,attempts=0,progress=NULL,delivery_after=0 WHERE id=%s""",
                    (json.dumps({"_refresh_statement_ui": statement["token"]}), job["id"]))

    def progress(self, job, text):
        self.db.execute("UPDATE jobs SET progress=%s WHERE id=%s AND status='processing'", (text[:500], job))
        self.feedback_ready.set()

    def active_queue(self):
        now = time.time()
        rows = [dict(row) for row in self.db.execute(
            "SELECT id,user_id,status,next_attempt FROM jobs WHERE status IN ('queued','processing') ORDER BY id")]
        # A retry delays subsequent messages by that user, but does not block other users.
        available = {}
        for row in rows:
            available[row["user_id"]] = max(available.get(row["user_id"], 0), row["next_attempt"])
            row["ready_at"] = available[row["user_id"]]
        rows.sort(key=lambda row: (0 if row["status"] == "processing" else 1,
                                   max(now, row["ready_at"]), row["id"]))
        return {row["id"]: {**row, "position": i + 1, "total": len(rows)} for i, row in enumerate(rows)}

    def feedback_candidates(self, now):
        return self.db.execute("""SELECT * FROM jobs WHERE feedback_disabled=0 AND delivery_after<=%s
            AND (status IN ('queued','processing') OR (status IN ('done','failed') AND notified=0 AND response IS NOT NULL))
            ORDER BY delivery_after,id LIMIT 100""", (now,)).fetchall()

    def feedback_messages(self, job):
        return {row["part"]: dict(row) for row in self.db.execute("SELECT * FROM job_feedback WHERE job_id=%s", (job,))}

    def save_feedback(self, job, part, message_id, text, markup="{}", entities="[]"):
        self.db.execute("INSERT INTO job_feedback(job_id,part,message_id,sent_text,sent_markup,sent_entities) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id,part) DO UPDATE SET message_id=EXCLUDED.message_id,sent_text=EXCLUDED.sent_text,sent_markup=EXCLUDED.sent_markup,sent_entities=EXCLUDED.sent_entities", (job, part, message_id, text, markup, entities))

    def has_feedback(self):
        return self.db.execute("SELECT EXISTS(SELECT 1 FROM jobs WHERE feedback_disabled=0 AND notified=0) AS pending").fetchone()["pending"]

    def record_delivery(self, job, part, message_id, text, markup, entities):
        self.save_feedback(job, part, message_id, text, markup, entities)
        # A click can arrive while editMessageText is in flight. Its new owner must
        # inherit what Telegram actually displayed, rather than a pre-edit snapshot.
        self.db.execute("""UPDATE job_feedback SET sent_text=%s,sent_markup=%s,sent_entities=%s
            WHERE part=%s AND job_id=(SELECT j.id FROM jobs j JOIN job_feedback f ON f.job_id=j.id
                WHERE j.user_id=(SELECT user_id FROM jobs WHERE id=%s) AND f.message_id=%s
                ORDER BY j.created DESC,j.id DESC LIMIT 1)""", (text, markup, entities, part, job, message_id))

    def defer_feedback(self, job, until):
        self.db.execute("UPDATE jobs SET delivery_after=%s WHERE id=%s", (until, job))

    def disable_feedback(self, job):
        self.db.execute("UPDATE jobs SET feedback_disabled=1,notified=1 WHERE id=%s", (job,))

    def feedback_complete(self, job, response):
        # Processing may have advanced while Telegram was responding. Never acknowledge a newer result.
        self.db.execute("UPDATE jobs SET notified=1 WHERE id=%s AND status IN ('done','failed') AND response=%s", (job, response))

    def retry_last(self, user):
        row = self.db.execute("SELECT id FROM jobs WHERE user_id=%s AND status='failed' ORDER BY id DESC LIMIT 1", (user,)).fetchone()
        if not row:
            return False
        return self.retry_job(user, row["id"])

    def retry_job(self, user, job):
        self.db.execute("""UPDATE jobs SET status='queued',attempts=0,next_attempt=0,progress=NULL,response=NULL,
            reply_markup='{}',notified=0,feedback_disabled=0,delivery_after=0 WHERE id=%s AND user_id=%s AND status='failed'""", (job, user))
        self.work_ready.set()
        self.feedback_ready.set()
        return True

    def notified(self, job):
        self.db.execute("UPDATE jobs SET notified=1 WHERE id=%s", (job,))

    def context(self, user):
        row = self.db.execute("SELECT * FROM conversations WHERE user_id=%s", (user,)).fetchone()
        return json.loads(row["content"]) if row and row["updated"] > time.time() - 86400 else None

    def save_context(self, user, content):
        if content is None:
            self.db.execute("DELETE FROM conversations WHERE user_id=%s", (user,))
        else:
            self.db.execute("INSERT INTO conversations VALUES(%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET content=EXCLUDED.content,updated=EXCLUDED.updated",
                            (user, json.dumps(content, ensure_ascii=False), time.time()))

    def write(self, marker):
        return self.db.execute("SELECT * FROM writes WHERE marker=%s", (marker,)).fetchone()

    def memories(self, user):
        return self.db.execute("SELECT id,kind,merchant,category_id,category_name FROM memories WHERE user_id=0 ORDER BY id").fetchall()

    def share_memories(self):
        """Merge legacy preferences transactionally under the single worker lock."""
        with self.transaction():
            if self.meta("_shared_memory_v1", ""):
                return
            self.db.execute("LOCK TABLE memories IN EXCLUSIVE MODE")
            self.db.execute("""DELETE FROM memories WHERE id NOT IN (
                SELECT DISTINCT ON(kind,merchant) id FROM memories
                ORDER BY kind,merchant,updated DESC,id DESC)""")
            if self.db.execute("SELECT count(*) AS count FROM memories").fetchone()["count"] > 100:
                raise ValueError("Shared category memory exceeds 100 rules; merge was rolled back")
            self.db.execute("UPDATE memories SET user_id=0")
            self.set_meta("_shared_memory_v1", "1")

    def remember(self, user, kind, merchant, category_id, category_name):
        self.db.execute("""INSERT INTO memories(user_id,kind,merchant,category_id,category_name,updated)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,kind,merchant) DO UPDATE
            SET category_id=EXCLUDED.category_id,category_name=EXCLUDED.category_name,updated=EXCLUDED.updated""",
            (0, kind, merchant, category_id, category_name, time.time()))

    def forget(self, user, rule_id):
        return self.db.execute("DELETE FROM memories WHERE user_id=0 AND id=%s RETURNING id", (rule_id,)).fetchone() is not None

    def forget_merchant(self, user, kind, merchant):
        self.db.execute("DELETE FROM memories WHERE user_id=0 AND kind=%s AND merchant=%s", (kind, merchant))

    def payment_marker(self, fingerprint):
        return self.meta("payment:" + fingerprint, "")

    def claim_payment(self, fingerprint, marker):
        previous = self.payment_marker(fingerprint)
        if previous and previous != marker:
            raise RuntimeError("Payment identity is already claimed")
        self.set_meta("payment:" + fingerprint, marker)

    def prepare(self, marker, job, payload):
        self.db.execute("INSERT INTO writes VALUES(%s,%s,%s,'sending',NULL,%s)",
                        (marker, job, json.dumps(payload, ensure_ascii=False), time.time()))

    def write_result(self, marker, result):
        self.db.execute("UPDATE writes SET state='done',result=%s WHERE marker=%s", (json.dumps(result), marker))

    def cleanup(self, days):
        cutoff = time.time() - days * 86400
        self.db.execute("DELETE FROM conversations WHERE updated<%s", (time.time()-86400,))
        # Keep update IDs and write markers for deduplication, erase old financial payloads.
        self.db.execute("UPDATE jobs SET response=NULL,payload='{}' WHERE created<%s AND notified=1", (cutoff,))
        self.db.execute("UPDATE writes SET payload='{}',result=NULL WHERE created<%s AND state='done'", (cutoff,))
        self.db.execute("DELETE FROM meta WHERE key IN (SELECT 'plan:' || id::text FROM jobs WHERE created<%s AND notified=1)", (cutoff,))
        self.db.execute("UPDATE job_feedback SET sent_text='',sent_markup='{}' WHERE job_id IN (SELECT id FROM jobs WHERE created<%s AND notified=1)", (cutoff,))
        self.db.execute("UPDATE jobs SET reply_markup='{}',callback_id=NULL WHERE created<%s AND notified=1", (cutoff,))


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
    id BIGINT PRIMARY KEY, user_id BIGINT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
    created DOUBLE PRECISION NOT NULL, next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,
    response TEXT, error TEXT, notified INTEGER NOT NULL DEFAULT 0,
    source_message_id BIGINT, progress TEXT, delivery_after DOUBLE PRECISION NOT NULL DEFAULT 0,
    feedback_disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS writes(
    marker TEXT PRIMARY KEY, job_id BIGINT NOT NULL, payload TEXT NOT NULL,
    state TEXT NOT NULL, result TEXT, created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(
    user_id BIGINT PRIMARY KEY, content TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS job_feedback(
    job_id BIGINT NOT NULL, part INTEGER NOT NULL, message_id BIGINT NOT NULL,
    sent_text TEXT NOT NULL, PRIMARY KEY(job_id,part));
CREATE TABLE IF NOT EXISTS memories(
    id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('expense','income')), merchant TEXT NOT NULL,
    category_id TEXT NOT NULL, category_name TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL,
    UNIQUE(user_id,kind,merchant));
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(status,next_attempt,id);
CREATE INDEX IF NOT EXISTS jobs_user_order ON jobs(user_id,status,id);
CREATE INDEX IF NOT EXISTS jobs_feedback_due ON jobs(delivery_after,id) WHERE notified=0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS reply_markup TEXT NOT NULL DEFAULT '{}';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS callback_id TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS callback_answered INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS reply_entities TEXT NOT NULL DEFAULT '[]';
ALTER TABLE job_feedback ADD COLUMN IF NOT EXISTS sent_entities TEXT NOT NULL DEFAULT '[]';
ALTER TABLE job_feedback ADD COLUMN IF NOT EXISTS sent_markup TEXT NOT NULL DEFAULT '{}';
"""
