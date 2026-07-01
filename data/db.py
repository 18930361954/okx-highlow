import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_SCHEMA_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date TEXT NOT NULL,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    exit_reason TEXT,
    margin REAL,
    mode TEXT,
    pnl REAL,
    entry_time TEXT,
    exit_time TEXT,
    okx_order_id TEXT,
    attempt INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_SCHEMA_STATE = """
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_INDEX_TRADES_DATE = "CREATE INDEX IF NOT EXISTS idx_trades_signal_date ON trades(signal_date);"
_INDEX_TRADES_PAIR = "CREATE INDEX IF NOT EXISTS idx_trades_pair ON trades(pair);"


class DB:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute(_SCHEMA_TRADES)
            c.execute(_SCHEMA_STATE)
            c.execute(_INDEX_TRADES_DATE)
            c.execute(_INDEX_TRADES_PAIR)
            # 迁移：既有库补 attempt 列
            cols = {r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
            if "attempt" not in cols:
                c.execute("ALTER TABLE trades ADD COLUMN attempt INTEGER DEFAULT 1")

    def insert_trade(
        self,
        signal_date: str,
        pair: str,
        side: str,
        entry_price: float | None = None,
        exit_price: float | None = None,
        exit_reason: str | None = None,
        margin: float | None = None,
        mode: str | None = None,
        pnl: float | None = None,
        entry_time: str | None = None,
        exit_time: str | None = None,
        okx_order_id: str | None = None,
        attempt: int = 1,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO trades
                (signal_date, pair, side, entry_price, exit_price, exit_reason,
                 margin, mode, pnl, entry_time, exit_time, okx_order_id, attempt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (signal_date, pair, side, entry_price, exit_price, exit_reason,
                 margin, mode, pnl, entry_time, exit_time, okx_order_id, attempt),
            )
            return int(cur.lastrowid)

    def update_trade_exit(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        pnl: float,
        exit_time: str,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE trades SET exit_price=?, exit_reason=?, pnl=?, exit_time=?
                   WHERE id=?""",
                (exit_price, exit_reason, pnl, exit_time, trade_id),
            )

    def update_trade_entry(self, trade_id: int, entry_time: str,
                           entry_price: float | None = None) -> None:
        """建仓成交后回填 entry_time；若给了 entry_price 一并回填（实际成交价可能微偏）。"""
        with self._conn() as c:
            if entry_price is not None:
                c.execute(
                    "UPDATE trades SET entry_time=?, entry_price=? WHERE id=?",
                    (entry_time, entry_price, trade_id),
                )
            else:
                c.execute(
                    "UPDATE trades SET entry_time=? WHERE id=?",
                    (entry_time, trade_id),
                )

    def list_open_trades(self) -> list[dict]:
        """尚未结算的 trades：exit_price 为空即为未闭合。reconciler 用。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM trades WHERE exit_price IS NULL ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def update_trade_algo_id(self, trade_id: int, new_algo_id: str) -> None:
        """孤儿修复：db 里 algoId 在 OKX 找不到、但 pair 有其它 pending 时改绑。"""
        with self._conn() as c:
            c.execute(
                "UPDATE trades SET okx_order_id=? WHERE id=?",
                (new_algo_id, trade_id),
            )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO state(key, value, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                (key, str(value)),
            )

    def list_trades_by_date(self, signal_date: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM trades WHERE signal_date=? ORDER BY id",
                (signal_date,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_trades(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
