"""Single-host, crash-safe admission of side effects. Contains no tool payloads."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Literal

from .types import Email, Execution, ToolRequest


class LedgerUnavailable(RuntimeError):
    """The ledger could not establish or record an execution state."""


class SQLiteLedger:
    """Use one stable secret per database and one trusted scope per caller."""

    def __init__(self, path: str | Path, *, secret: bytes, scope: str):
        self._configure(path, secret, scope)
        with self._connection():
            pass

    def _configure(self, path: str | Path, secret: bytes, scope: str) -> None:
        if type(secret) is not bytes or len(secret) < 32:
            raise ValueError("Use a ledger secret of at least 32 bytes.")
        if type(scope) is not str or not scope.strip():
            raise ValueError("A stable application and principal scope is required.")
        self._uri = Path(path).resolve().as_uri() + "?mode=rw"
        self._secret = secret
        self._scope = scope

    @classmethod
    def create(cls, path: str | Path, *, secret: bytes, scope: str) -> SQLiteLedger:
        """Provision a new database once. An existing file is never overwritten."""
        ledger = cls.__new__(cls)
        ledger._configure(path, secret, scope)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(ledger._uri, uri=True)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE identity (id INTEGER PRIMARY KEY CHECK(id=1), tag BLOB NOT NULL)")
            db.execute("INSERT INTO identity VALUES (1, ?)", (ledger._digest("identity-v1"),))
            db.execute("""CREATE TABLE executions (
                key BLOB PRIMARY KEY, action BLOB NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('started','succeeded','failed','expired'))
            ) WITHOUT ROWID""")
        return ledger

    def _digest(self, *parts: str) -> bytes:
        payload = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        return hmac.new(self._secret, payload, hashlib.sha256).digest()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=2)) as db, db:
                db.execute("PRAGMA synchronous=FULL")
                identity = db.execute("SELECT tag FROM identity WHERE id=1").fetchone()
                if identity != (self._digest("identity-v1"),):
                    raise LedgerUnavailable("The ledger identity could not be verified.")
                yield db
        except sqlite3.Error:
            raise LedgerUnavailable("The execution ledger is unavailable.") from None

    @staticmethod
    def validate_key(key: str | None) -> None:
        if type(key) is not str or not key.strip() or len(key) > 256:
            raise ValueError("Use a stable idempotency key of 1 to 256 characters.")

    def claim(self, key: str, request: ToolRequest) -> Literal["duplicate_execution", "idempotency_conflict"] | None:
        self.validate_key(key)
        ref = self._digest("key", self._scope, key)
        kind = "email" if isinstance(request.arguments, Email) else "json"
        action = self._digest("action", request.user_request, request.tool_name, kind,
                              request.arguments.preview())
        with self._connection() as db:
            inserted = db.execute("""INSERT INTO executions VALUES (?, ?, 'started')
                                     ON CONFLICT(key) DO NOTHING""", (ref, action))
            if inserted.rowcount == 1:
                return None
            previous = db.execute("SELECT action FROM executions WHERE key=?", (ref,)).fetchone()
            return "duplicate_execution" if previous == (action,) else "idempotency_conflict"

    def finish(self, key: str, outcome: Execution | Literal["expired"]) -> None:
        if outcome not in (Execution.FAILED, Execution.SUCCEEDED, "expired"):
            raise ValueError("A terminal execution outcome is required.")
        with self._connection() as db:
            updated = db.execute("UPDATE executions SET state=? WHERE key=? AND state='started'",
                                 (str(outcome), self._digest("key", self._scope, key)))
            if updated.rowcount != 1:
                raise LedgerUnavailable("The execution outcome could not be recorded.")

    def status(self, key: str) -> str | None:
        """Inspect the recorded state without exposing the action or its arguments."""
        self.validate_key(key)
        with self._connection() as db:
            row = db.execute("SELECT state FROM executions WHERE key=?",
                             (self._digest("key", self._scope, key),)).fetchone()
        return row[0] if row else None
