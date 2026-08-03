import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


DEFAULT_ACCOUNT = "default"


_SCHEMA_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL DEFAULT 'default',
    strategy TEXT,               -- 策略版本名(如 v1-highlow / v2-trend),同账户换策略后数据互不混淆
    signal_bar TEXT,             -- 该笔信号周期(1D/12H/6H/4H)。混周期下同账户不同 pair 周期不同,统计按此分组
    leg_group TEXT,              -- fade 双向挂单的分组键(同 leg_group 的两行=同桶一多一空,OCO 关联)
    signal_date TEXT NOT NULL,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    trigger_price REAL,          -- 下单时触发价(策略计算值)。entry_price 成交后被实际成交价覆盖,两者差=滑点
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
# 同账户同 algoId 只允许一条 trade: 防 clOrdId 回查兜底把已入库的 algoId 二次 insert
# (2026-07-20 ETH 重复入库事故: catchup 补挂与整点 cron 相差 20s 竞态, 同单双录 → pnl 双记)
_INDEX_TRADES_ACC_ALGO_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_acc_algo_uniq "
    "ON trades(account, okx_order_id) WHERE okx_order_id IS NOT NULL;"
)


class DB:
    def __init__(self, db_path: str | Path, busy_timeout: int = 30):
        self.path = Path(db_path)
        self.busy_timeout = busy_timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout,
                               isolation_level=None)
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
            # strategy 列(策略版本名): 换策略后新旧数据按名字区分。
            # 历史数据回填 (v1-highlow / v2-trend) 已于 2026-07-27 一次性完成, 这里只补列。
            if "strategy" not in cols:
                c.execute("ALTER TABLE trades ADD COLUMN strategy TEXT")
            # leg_group 列(fade 双向挂单分组键): 同 leg_group 的两行是同桶一多一空,
            # 一腿成交后 reconciler 据此撤另一腿(OCO)。旧数据留空,不影响单向策略。
            if "leg_group" not in cols:
                c.execute("ALTER TABLE trades ADD COLUMN leg_group TEXT")
            # signal_bar 列(信号周期): 混周期(v3-mixed)下同账户不同 pair 周期不同,
            # 监控/报表按周期分组统计需要行级周期。旧数据留空,报表侧回退账户级周期。
            if "signal_bar" not in cols:
                c.execute("ALTER TABLE trades ADD COLUMN signal_bar TEXT")
            # trigger_price 列: reconciler 回填实际成交价会覆盖 entry_price,
            # 触发价单独存一列才能算滑点(验收软性项: 成交价 vs 触发价均值 <15bp)。
            if "trigger_price" not in cols:
                c.execute("ALTER TABLE trades ADD COLUMN trigger_price REAL")

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
            try:
                c.execute(_INDEX_TRADES_ACC_ALGO_UNIQUE)
            except sqlite3.IntegrityError:
                # 老库存在历史重复 (account, okx_order_id) 时建唯一索引会失败。
                # 不阻塞启动: 无索引时 insert_trade 退化为无幂等保护(与旧版一致),
                # 清理重复后下次启动自动建上。清理: scripts/fix_orphan_trades.py 或手工去重。
                pass

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
        strategy: str | None = None,
        leg_group: str | None = None,
        signal_bar: str | None = None,
        trigger_price: float | None = None,
    ) -> int:
        with self._conn() as c:
            try:
                cur = c.execute(
                    """INSERT INTO trades
                    (account, strategy, leg_group, signal_bar, trigger_price, signal_date, pair, side, entry_price, exit_price, exit_reason,
                     margin, mode, pnl, entry_time, exit_time, okx_order_id, attempt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (account, strategy, leg_group, signal_bar, trigger_price, signal_date, pair, side, entry_price, exit_price, exit_reason,
                     margin, mode, pnl, entry_time, exit_time, okx_order_id, attempt),
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                # 幂等: 同 (account, okx_order_id) 已有记录 → 返回已存在的 id, 不重复入库。
                # 场景: place 用同 clOrdId 幂等键重挂时 OKX 返回同一 algoId
                # (catchup 补挂 vs 整点 cron 竞态, 2026-07-20 ETH 双录事故)
                row = c.execute(
                    "SELECT id FROM trades WHERE account=? AND okx_order_id=?",
                    (account, okx_order_id),
                ).fetchone()
                if row:
                    return int(row["id"])
                raise

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

    def get_open_sibling_leg(self, account: str, leg_group: str,
                             exclude_trade_id: int) -> dict | None:
        """fade OCO 用: 返回同 leg_group、非本行、尚未闭合(exit_price IS NULL)的另一腿。
        一腿成交后 reconciler 据此找到待撤的对向腿。找不到返回 None(可能已成交/已撤)。"""
        if not leg_group:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM trades WHERE account=? AND leg_group=? AND id!=? "
                "AND exit_price IS NULL ORDER BY id LIMIT 1",
                (account, leg_group, exclude_trade_id),
            ).fetchone()
            return dict(row) if row else None

    def get_any_filled_sibling(self, account: str, leg_group: str,
                               exclude_trade_id: int) -> dict | None:
        """fade OCO 用: 返回同 leg_group、非本行、已真实入场(entry_time 非空)的另一腿,
        **不论是否已闭合** —— A 腿可能在同一轮对账里入场+平仓直接闭合,
        只查 open 会漏判"对向已成交",导致本腿漏撤继续裸挂(过期信号风险)。"""
        if not leg_group:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM trades WHERE account=? AND leg_group=? AND id!=? "
                "AND entry_time IS NOT NULL ORDER BY id LIMIT 1",
                (account, leg_group, exclude_trade_id),
            ).fetchone()
            return dict(row) if row else None

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
                             account: str | None = None,
                             strategy: str | None = None) -> list[dict]:
        """account=None 时全账户;否则按账户过滤。strategy 同理(None=不过滤)。"""
        with self._conn() as c:
            sql = "SELECT * FROM trades WHERE signal_date=?"
            vals: list[Any] = [signal_date]
            if account is not None:
                sql += " AND account=?"; vals.append(account)
            if strategy is not None:
                sql += " AND strategy=?"; vals.append(strategy)
            rows = c.execute(sql + " ORDER BY id", vals).fetchall()
            return [dict(r) for r in rows]

    def list_trades(self, limit: int = 100,
                    account: str | None = None,
                    strategy: str | None = None) -> list[dict]:
        with self._conn() as c:
            sql = "SELECT * FROM trades"
            conds: list[str] = []
            vals: list[Any] = []
            if account is not None:
                conds.append("account=?"); vals.append(account)
            if strategy is not None:
                conds.append("strategy=?"); vals.append(strategy)
            if conds:
                sql += " WHERE " + " AND ".join(conds)
            sql += " ORDER BY id DESC LIMIT ?"
            vals.append(limit)
            rows = c.execute(sql, vals).fetchall()
            return [dict(r) for r in rows]

    def list_accounts(self) -> list[str]:
        """已在 db 里出现过的 account 名字集合(union of trades + state)。日报汇总用。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT account FROM trades UNION SELECT account FROM state"
            ).fetchall()
        return sorted({r["account"] for r in rows})
