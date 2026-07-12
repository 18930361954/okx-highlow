import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


DEFAULT_ACCOUNT = "default"


_SCHEMA_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL DEFAULT 'default',
    signal_date TEXT NOT NULL,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    exit_reason TEXT,
    margin REAL,
    mode TEXT,
    pnl REAL,                    -- OKX 净盈亏(realizedPnl,已扣手续费+资金费,与 UI 一致)
    pnl_gross REAL DEFAULT 0.0,  -- OKX 名义盈亏(positions-history.pnl,权威值,不做本地反推)
    fee REAL DEFAULT 0.0,        -- 该笔手续费(绝对值,来源 OKX positions-history.fee,仅供展示)
    funding REAL DEFAULT 0.0,    -- 该笔资金费(带符号:正=收/负=付,来源 OKX positions-history.fundingFee,仅供展示)
    entry_time TEXT,
    exit_time TEXT,
    okx_order_id TEXT,
    attempt INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_SCHEMA_STATE = """
CREATE TABLE IF NOT EXISTS state (
    account TEXT NOT NULL DEFAULT 'default',
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account, key)
);
"""

_INDEX_TRADES_DATE = "CREATE INDEX IF NOT EXISTS idx_trades_signal_date ON trades(signal_date);"
_INDEX_TRADES_PAIR = "CREATE INDEX IF NOT EXISTS idx_trades_pair ON trades(pair);"
_INDEX_TRADES_ACC = "CREATE INDEX IF NOT EXISTS idx_trades_account ON trades(account);"


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
            # trades 迁移：既有库补 attempt / account / fee 列
            cols = {r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
            if "attempt" not in cols:
                c.execute("ALTER TABLE trades ADD COLUMN attempt INTEGER DEFAULT 1")
            if "account" not in cols:
                c.execute("ALTER TABLE trades ADD COLUMN account TEXT NOT NULL DEFAULT 'default'")
            if "fee" not in cols:
                c.execute("ALTER TABLE trades ADD COLUMN fee REAL DEFAULT 0.0")
            # funding 列(资金费):新拆出的字段。老数据的 fee 里可能混着 funding,
            # 已发生的历史数据不回补 —— funding 列填 0,fee 保持原值(视为"含 funding 的老 fee")。
            if "funding" not in cols:
                c.execute("ALTER TABLE trades ADD COLUMN funding REAL DEFAULT 0.0")
            # pnl_gross 列:直接存 OKX positions-history.pnl (名义/毛盈亏, 权威值),
            # 避免本地 net + fee - funding 反推带来的浮点/舍入不一致 (与 OKX 界面对不上)。
            if "pnl_gross" not in cols:
                c.execute("ALTER TABLE trades ADD COLUMN pnl_gross REAL DEFAULT 0.0")

            # state 迁移：老表主键是 key,单账户;新表主键 (account, key)。
            # 检测老 schema 直接改建新表迁数据。
            state_cols = {r[1] for r in c.execute("PRAGMA table_info(state)").fetchall()}
            if state_cols and "account" not in state_cols:
                c.execute("ALTER TABLE state RENAME TO state_old")
                c.execute(_SCHEMA_STATE)
                c.execute(
                    "INSERT INTO state(account, key, value, updated_at) "
                    "SELECT 'default', key, value, updated_at FROM state_old"
                )
                c.execute("DROP TABLE state_old")
            else:
                c.execute(_SCHEMA_STATE)

            c.execute(_INDEX_TRADES_DATE)
            c.execute(_INDEX_TRADES_PAIR)
            c.execute(_INDEX_TRADES_ACC)

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
        account: str = DEFAULT_ACCOUNT,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO trades
                (account, signal_date, pair, side, entry_price, exit_price, exit_reason,
                 margin, mode, pnl, entry_time, exit_time, okx_order_id, attempt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (account, signal_date, pair, side, entry_price, exit_price, exit_reason,
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
        fee: float | None = None,
        funding: float | None = None,
        pnl_gross: float | None = None,
    ) -> None:
        """pnl=净口径 (realizedPnl); pnl_gross=名义口径 (OKX positions-history.pnl, 权威);
        fee=绝对值(成本); funding=带符号(正=收/负=付)。全部 OKX 直返, 不本地算。"""
        with self._conn() as c:
            sets = ["exit_price=?", "exit_reason=?", "pnl=?", "exit_time=?"]
            vals: list[Any] = [exit_price, exit_reason, pnl, exit_time]
            if fee is not None:
                sets.append("fee=?"); vals.append(fee)
            if funding is not None:
                sets.append("funding=?"); vals.append(funding)
            if pnl_gross is not None:
                sets.append("pnl_gross=?"); vals.append(pnl_gross)
            vals.append(trade_id)
            c.execute(f"UPDATE trades SET {', '.join(sets)} WHERE id=?", vals)

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

    def list_open_trades(self, account: str | None = None) -> list[dict]:
        """尚未结算的 trades：exit_price 为空即为未闭合。reconciler 用。
        account=None 时返回全部账户;传具体 account 时只返回该账户的。"""
        with self._conn() as c:
            if account is None:
                rows = c.execute(
                    "SELECT * FROM trades WHERE exit_price IS NULL ORDER BY id"
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM trades WHERE exit_price IS NULL AND account=? ORDER BY id",
                    (account,),
                ).fetchall()
            return [dict(r) for r in rows]

    def update_trade_algo_id(self, trade_id: int, new_algo_id: str) -> None:
        """孤儿修复：db 里 algoId 在 OKX 找不到、但 pair 有其它 pending 时改绑。"""
        with self._conn() as c:
            c.execute(
                "UPDATE trades SET okx_order_id=? WHERE id=?",
                (new_algo_id, trade_id),
            )

    def get_state(self, key: str, default: str | None = None,
                  account: str = DEFAULT_ACCOUNT) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM state WHERE account=? AND key=?", (account, key),
            ).fetchone()
            return row["value"] if row else default

    def set_state(self, key: str, value: Any, account: str = DEFAULT_ACCOUNT) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO state(account, key, value, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(account, key) DO UPDATE SET
                     value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                (account, key, str(value)),
            )

    def list_trades_by_date(self, signal_date: str,
                             account: str | None = None) -> list[dict]:
        """account=None 时全账户;否则按账户过滤。"""
        with self._conn() as c:
            if account is None:
                rows = c.execute(
                    "SELECT * FROM trades WHERE signal_date=? ORDER BY id",
                    (signal_date,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM trades WHERE signal_date=? AND account=? ORDER BY id",
                    (signal_date, account),
                ).fetchall()
            return [dict(r) for r in rows]

    def list_trades(self, limit: int = 100,
                    account: str | None = None) -> list[dict]:
        with self._conn() as c:
            if account is None:
                rows = c.execute(
                    "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM trades WHERE account=? ORDER BY id DESC LIMIT ?",
                    (account, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    def list_accounts(self) -> list[str]:
        """已在 db 里出现过的 account 名字集合(union of trades + state)。日报汇总用。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT account FROM trades UNION SELECT account FROM state"
            ).fetchall()
        return sorted({r["account"] for r in rows})
