import asyncio
import hashlib
import sqlite3
from pathlib import Path

from fastapi import HTTPException

from gateway.schemas import SendTextRequest


class DeliveryStore:
    """持久化发送凭证；发送结果不确定时禁止自动重发，防止客户重复收到消息。"""

    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                message_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, provider_message_id TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")

    async def claim(self, request: SendTextRequest) -> str | None:
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        return await asyncio.to_thread(self._claim, request.message_id, fingerprint)

    def _claim(self, message_id: str, fingerprint: str) -> str | None:
        with sqlite3.connect(self.path, timeout=5) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT fingerprint,status,provider_message_id FROM deliveries WHERE message_id=?", (message_id,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise HTTPException(409, "Message ID already used for another payload")
                if row[1] == "SENT":
                    return row[2]
                raise HTTPException(409, "Delivery pending, failed or uncertain; check receipt before resending")
            connection.execute("INSERT INTO deliveries(message_id,fingerprint,status) VALUES(?,?,'SENDING')", (message_id, fingerprint))
        return None

    async def finish(self, message_id: str, status: str, provider_message_id: str | None = None) -> None:
        await asyncio.to_thread(self._finish, message_id, status, provider_message_id)

    def _finish(self, message_id: str, status: str, provider_message_id: str | None) -> None:
        with sqlite3.connect(self.path, timeout=5) as connection:
            connection.execute("UPDATE deliveries SET status=?,provider_message_id=?,updated_at=CURRENT_TIMESTAMP WHERE message_id=?", (status, provider_message_id, message_id))
