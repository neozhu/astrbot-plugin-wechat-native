"""Small transactional journal: IDs/receipts only, never tokens or message bodies.

The AstrBot queue cannot participate in a database transaction. A crash after
handoff is therefore retained for review rather than automatically handed off twice.
"""

import sqlite3
from pathlib import Path


def cursor_number(value: str) -> int:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or len(value) > 19
        or int(value) > 9223372036854775807
    ):
        raise ValueError("invalid_cursor")
    return int(value)


class StateIdentityError(RuntimeError):
    """Stored identity changed; preserve evidence and require operator inspection."""


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY, cursor TEXT NOT NULL);
            INSERT OR IGNORE INTO checkpoint VALUES (1, '0');
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recalls (
                thread TEXT NOT NULL, server_id TEXT NOT NULL, PRIMARY KEY(thread, server_id));
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, thread TEXT NOT NULL, server_id TEXT, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sends (
                key TEXT PRIMARY KEY, message_id TEXT, thread TEXT NOT NULL,
                state TEXT NOT NULL, send_id TEXT);
        """)
        with self.db:
            self.db.execute("UPDATE messages SET status='needs_review' WHERE status='queued'")
            self.db.execute("UPDATE sends SET state='needs_review' WHERE state='submitting'")

    def bind_server(self, server_id: str):
        if not isinstance(server_id, str) or not server_id:
            raise StateIdentityError("Native server identity unavailable; state retained")
        row = self.db.execute("SELECT value FROM settings WHERE key='server_id'").fetchone()
        if row and row[0] != server_id:
            raise StateIdentityError("Native server identity changed; state retained for review")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('server_id', ?)", (server_id,))

    @property
    def cursor(self) -> str:
        return self.db.execute("SELECT cursor FROM checkpoint WHERE id=1").fetchone()[0]

    def _advance(self, cursor: str):
        if cursor_number(cursor) > cursor_number(self.cursor):
            self.db.execute("UPDATE checkpoint SET cursor=? WHERE id=1", (cursor,))

    def advance(self, cursor: str):
        with self.db:
            self._advance(cursor)

    def accept(
        self, cursor: str, message_id: str, thread: str, server_id: str | None, status: str
    ) -> bool:
        existing = self.db.execute(
            "SELECT thread, server_id FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if existing and (existing[0] != thread or existing[1] != server_id):
            self.mark_message(message_id, "needs_review")
            raise StateIdentityError("Native message identity reused; cursor retained for review")
        with self.db:
            recalled = self.db.execute(
                "SELECT 1 FROM recalls WHERE thread=? AND server_id=?", (thread, server_id)
            ).fetchone()
            result = self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?)",
                (message_id, thread, server_id, "recalled" if recalled else status),
            )
            self._advance(cursor)
            return result.rowcount == 1 and not recalled

    def message_status(self, message_id: str) -> str | None:
        row = self.db.execute("SELECT status FROM messages WHERE id=?", (message_id,)).fetchone()
        return row[0] if row else None

    def mark_message(self, message_id: str, status: str):
        with self.db:
            self.db.execute(
                "UPDATE messages SET status=? WHERE id=? AND status!='recalled'",
                (status, message_id),
            )

    def recall(self, thread: str, server_id: str | None, message_id: str | None):
        with self.db:
            if server_id:
                self.db.execute("INSERT OR IGNORE INTO recalls VALUES (?, ?)", (thread, server_id))
            self.db.execute(
                "UPDATE messages SET status='recalled' WHERE thread=? AND "
                "((? IS NOT NULL AND server_id=?) OR (? IS NULL AND id=?))",
                (thread, server_id, server_id, server_id, message_id),
            )
            if message_id:
                self.db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, 'recalled')",
                    (message_id, thread, server_id),
                )

    def begin_send(self, key: str, message_id: str | None, thread: str):
        with self.db:
            self.db.execute(
                "INSERT INTO sends VALUES (?, ?, ?, 'submitting', NULL)", (key, message_id, thread)
            )

    def finish_send(self, key: str, state: str, send_id: str | None):
        with self.db:
            self.db.execute(
                "UPDATE sends SET state=?, send_id=? WHERE key=?", (state, send_id, key)
            )

    def send_record(self, key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM sends WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def close(self):
        self.db.close()
