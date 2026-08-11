"""启动时全量对账模块 - 修复断网/重启期间的 db 与 OKX 不一致

核心场景:
1. 断网期间 OKX 服务端 TP/SL 自动平仓，但 bot 未记录
2. OKX 错误风暴 (50001) 导致 reconciler 持续失败，入场/平仓事件丢失
3. 桶末撤单成功但 db 未同步标记
4. 重启后直接进入正常对账，永远不会发现历史遗留问题

设计原则:
- 启动时一次性全量对账，修复所有不一致
- 不依赖 reconciler 的增量对账逻辑
- 异常不阻塞启动，记录日志供人工介入
"""
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.multi_account import AccountRuntime


UTC = timezone.utc


def startup_full_reconcile(runtime: "AccountRuntime") -> None:
    """启动时全量对账: 修复断网/重启/OKX错误期间的所有不一致

    执行顺序:
    1. 检查 OKX 持仓是否在 db 有记录
    2. 检查 db open trades 是否在 OKX 存在
    3. 扫描最近 7 天 OKX 历史持仓，回填缺失记录
    4. 余额校验与同步
    """
    logger = runtime.logger
    logger.info("[startup-sync] ========== 启动全量对账 ==========")

    # === 第一步：检查 OKX 持仓是否在 db 有记录 ===
    _check_okx_positions_in_db(runtime)

    # === 第二步：检查 db open trades 是否在 OKX 存在 ===
    _check_db_trades_in_okx(runtime)

    # === 第三步：扫描 OKX 历史持仓，回填缺失记录 ===
    _recover_missing_trades_from_okx(runtime, days=7)

    # === 第四步：余额校验 ===
    _validate_and_sync_balance(runtime)

    logger.info("[startup-sync] ========== 全量对账完成 ==========")


def _check_okx_positions_in_db(runtime: "AccountRuntime") -> None:
    """检查 OKX 当前持仓是否在 db 有对应的 open trade 记录"""
    logger = runtime.logger
    logger.info("[startup-sync] 1/4 检查 OKX 持仓...")

    try:
        okx_positions = runtime.okx.get_positions()
    except Exception as e:
        logger.error(f"[startup-sync] 获取 OKX 持仓失败: {e}")
        return

    for pos in okx_positions:
        pos_size = float(pos.get("pos", 0) or 0)
        if pos_size == 0:
            continue

        pair = pos.get("instId")
        pos_side = pos.get("posSide")  # long / short
        avg_px = float(pos.get("avgPx", 0) or 0)

        # 查询 db 是否有该持仓的 open trade
        try:
            with runtime.db._conn() as conn:
                open_trade = conn.execute("""
                    SELECT id, entry_price FROM trades
                    WHERE account=? AND pair=? AND side=? AND exit_price IS NULL
                    LIMIT 1
                """, (runtime.name, pair, pos_side)).fetchone()
        except Exception as e:
            logger.warning(f"[startup-sync] 查询 db trade 失败: {e}")
            continue

        if not open_trade:
            # 🔴 严重：OKX 有持仓但 db 无记录
            logger.error(
                f"[startup-sync] 🔴 {pair} {pos_side} OKX 有持仓但 db 无记录！"
                f" pos={abs(pos_size)} avgPx={avg_px}"
            )
            # 尝试从 OKX 历史订单回填（下一步会处理）
            logger.warning(f"[startup-sync] 将尝试从 OKX 历史记录回填...")
        else:
            logger.debug(
                f"[startup-sync] ✓ {pair} {pos_side} 持仓记录匹配 (trade#{open_trade[0]})"
            )


def _check_db_trades_in_okx(runtime: "AccountRuntime") -> None:
    """检查 db 未平仓记录是否在 OKX 仍存在（pending algo 或持仓）"""
    logger = runtime.logger
    logger.info("[startup-sync] 2/4 检查 db 未平仓记录...")

    # 查询 db 所有 open trades
    try:
        with runtime.db._conn() as conn:
            open_trades = conn.execute("""
                SELECT id, pair, side, okx_order_id, signal_date, created_at, entry_time
                FROM trades
                WHERE account=? AND exit_price IS NULL
            """, (runtime.name,)).fetchall()
    except Exception as e:
        logger.error(f"[startup-sync] 查询 db open trades 失败: {e}")
        return

    if not open_trades:
        logger.info("[startup-sync] db 无未闭合记录")
        return

    # 查询 OKX 所有 pending algo（按 pair 分组查询）
    okx_algos = {}
    pairs = {t[1] for t in open_trades if t[1]}  # t[1] = pair
    for pair in pairs:
        try:
            algos = runtime.okx.list_pending_algos(ordType="trigger", instId=pair)
            for a in algos:
                okx_algos[a["algoId"]] = a
        except Exception as e:
            logger.warning(f"[startup-sync] 查询 {pair} pending 失败: {e}")

    # 查询 OKX 当前持仓
    okx_positions = {}
    try:
        for pos in runtime.okx.get_positions():
            if float(pos.get("pos", 0) or 0) != 0:
                pair = pos.get("instId")
                side = pos.get("posSide")
                if pair and side:
                    okx_positions[f"{pair}:{side}"] = pos
    except Exception as e:
        logger.warning(f"[startup-sync] 获取 OKX 持仓失败: {e}")

    # 标记 db 中不存在于 OKX 的订单为 ORPHAN
    for trade in open_trades:
        trade_id, pair, side, algo_id, sig_date, created_at, entry_time = trade

        if not algo_id:
            # 没有 algoId，无法对账（旧数据或异常数据）
            continue

        # 检查是否有入场时间（已成交）
        if entry_time:
            # 已入场，检查是否有持仓
            pos_key = f"{pair}:{side}"
            if pos_key in okx_positions:
                logger.debug(f"[startup-sync] ✓ trade#{trade_id} {pair} {side} 持仓正常")
                continue
            else:
                # db 说已入场，但 OKX 无持仓 → 可能已平仓但 db 未记录
                logger.warning(
                    f"[startup-sync] ⚠️ trade#{trade_id} {pair} {side} "
                    f"db 已入场但 OKX 无持仓，可能断网期间已平仓"
                )
                # 交给历史持仓回填流程处理
                continue

        # 未入场，检查是否在 pending
        if algo_id in okx_algos:
            logger.debug(f"[startup-sync] ✓ trade#{trade_id} {pair} pending 正常")
            continue

        # db 有记录但 OKX 不存在，查询订单状态
        try:
            order_info = runtime.okx._request(
                "GET", "/api/v5/trade/order-algo",
                params={"algoId": algo_id}
            )
            if order_info.get("data"):
                state = order_info["data"][0]["state"]
                if state == "canceled":
                    logger.warning(
                        f"[startup-sync] trade#{trade_id} {pair} {sig_date} "
                        f"OKX 已撤但 db 未标记，修复为 CANCELLED"
                    )
                    _mark_trade_cancelled(runtime, trade_id)
                elif state == "effective":
                    # 在 pending 但刚才没查到？可能延迟，保持不动
                    logger.info(f"[startup-sync] ✓ trade#{trade_id} {pair} pending (延迟)")
                else:
                    logger.warning(
                        f"[startup-sync] trade#{trade_id} {pair} state={state}"
                    )
            else:
                # OKX 查不到订单（可能已删除）
                logger.warning(
                    f"[startup-sync] trade#{trade_id} {pair} {sig_date} "
                    f"OKX 查不到订单，标记为 ORPHAN"
                )
                _mark_trade_orphan(runtime, trade_id)
        except Exception as e:
            logger.warning(f"[startup-sync] 查询 trade#{trade_id} 状态失败: {e}")


def _recover_missing_trades_from_okx(runtime: "AccountRuntime", days: int = 7) -> None:
    """从 OKX 历史持仓回填 db 缺失的交易记录

    重要：只回填策略首次启动之后的持仓，避免同步策略启动前的历史交易
    """
    logger = runtime.logger
    logger.info(f"[startup-sync] 3/4 扫描最近 {days} 天 OKX 历史持仓...")

    # 查询该账户策略首次启动时间（第一笔非RECOVERED记录）
    try:
        with runtime.db._conn() as conn:
            first_trade = conn.execute("""
                SELECT MIN(created_at) FROM trades
                WHERE account=? AND (exit_reason IS NULL OR exit_reason != 'RECOVERED')
            """, (runtime.name,)).fetchone()

        if first_trade and first_trade[0]:
            strategy_start = datetime.fromisoformat(first_trade[0])
            logger.info(f"[startup-sync] 策略首次启动: {strategy_start.isoformat()}")
        else:
            # 没有正常交易记录，说明是全新账户，不回填任何历史
            logger.info(f"[startup-sync] 账户无正常交易记录，跳过历史持仓回填")
            return
    except Exception as e:
        logger.error(f"[startup-sync] 查询策略启动时间失败: {e}")
        return

    since_ms = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)
    # 限制回填时间不早于策略启动时间
    strategy_start_ms = int(strategy_start.timestamp() * 1000)
    since_ms = max(since_ms, strategy_start_ms)

    # 从 strategy_config 获取 pairs
    pairs = runtime.cfg.strategy_config.get("pairs", [])
    for pair in pairs:
        try:
            resp = runtime.okx._request(
                "GET", "/api/v5/account/positions-history",
                params={
                    "instType": "SWAP",
                    "instId": pair,
                    "after": str(since_ms),
                    "limit": "100"
                }
            )

            if not resp.get("data"):
                continue

            for pos in resp["data"]:
                open_ts = int(pos["cTime"])
                close_ts = int(pos["uTime"])

                open_time = datetime.fromtimestamp(open_ts / 1000, tz=UTC).isoformat()
                close_time = datetime.fromtimestamp(close_ts / 1000, tz=UTC).isoformat()

                # 检查 db 是否有该持仓（宽松匹配：入场时间±5分钟）
                try:
                    with runtime.db._conn() as conn:
                        existing = conn.execute("""
                            SELECT id FROM trades
                            WHERE account=? AND pair=?
                              AND abs(julianday(entry_time) - julianday(?)) < 0.0035
                            LIMIT 1
                        """, (runtime.name, pair, open_time)).fetchone()
                except Exception as e:
                    logger.warning(f"[recover] 查询 db 失败: {e}")
                    existing = None

                if existing:
                    continue  # 已存在

                # db 缺失，回填
                pos_id = pos.get("posId", "")
                pnl = float(pos.get("realizedPnl", 0) or 0)

                logger.warning(
                    f"[recover] {pair} posId={pos_id} db 缺失，回填 "
                    f"开仓={open_time[:16]} 平仓={close_time[:16]} pnl={pnl:.2f}"
                )

                try:
                    with runtime.db._conn() as conn:
                        conn.execute("""
                            INSERT INTO trades (
                                account, pair, side, signal_date, signal_bar,
                                entry_price, exit_price, exit_reason,
                                entry_time, exit_time,
                                pnl, pnl_gross, fee, funding,
                                created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        """, (
                            runtime.name, pair,
                            "long" if pos["posSide"] == "long" else "short",
                            "RECOVERED", "UNKNOWN",
                            float(pos.get("openAvgPx", 0) or 0),
                            float(pos.get("closeAvgPx", 0) or 0),
                            "RECOVERED",
                            open_time, close_time,
                            pnl,
                            float(pos.get("pnl", 0) or 0),
                            abs(float(pos.get("fee", 0) or 0)),
                            float(pos.get("fundingFee", 0) or 0)
                        ))
                    logger.info(f"[recover] ✓ {pair} posId={pos_id} 已回填")
                except Exception as e:
                    logger.error(f"[recover] {pair} posId={pos_id} 回填失败: {e}")

        except Exception as e:
            logger.error(f"[recover] {pair} 扫描失败: {e}")


def _validate_and_sync_balance(runtime: "AccountRuntime") -> None:
    """余额校验与同步"""
    logger = runtime.logger
    logger.info("[startup-sync] 4/4 余额校验...")

    try:
        okx_balance = float(runtime.okx.get_cash_balance("USDT"))
    except Exception as e:
        logger.error(f"[startup-sync] 获取 OKX 余额失败: {e}")
        return

    db_balance = runtime.account.get_balance()
    diff = okx_balance - db_balance

    logger.info(
        f"[startup-sync] 余额对比: db={db_balance:.2f} okx={okx_balance:.2f} 差={diff:+.2f}"
    )

    if abs(diff) > 10:
        logger.warning(
            f"[startup-sync] ⚠️ 余额偏差超过 10 USDT，可能有未记录交易！"
        )

    # 同步到 OKX 余额
    runtime.account.set_balance(okx_balance)
    logger.info(f"[startup-sync] 余额已同步到 OKX 真值: {okx_balance:.2f} USDT")


def _mark_trade_cancelled(runtime: "AccountRuntime", trade_id: int) -> None:
    """标记 trade 为 CANCELLED"""
    try:
        with runtime.db._conn() as conn:
            conn.execute("""
                UPDATE trades
                SET exit_price=0, exit_reason='CANCELLED',
                    exit_time=datetime('now'), pnl=0, fee=0
                WHERE id=?
            """, (trade_id,))
        runtime.logger.info(f"[startup-sync] ✓ trade#{trade_id} → CANCELLED")
    except Exception as e:
        runtime.logger.error(f"[startup-sync] 标记 trade#{trade_id} CANCELLED 失败: {e}")


def _mark_trade_orphan(runtime: "AccountRuntime", trade_id: int) -> None:
    """标记 trade 为 ORPHAN"""
    try:
        with runtime.db._conn() as conn:
            conn.execute("""
                UPDATE trades
                SET exit_price=0, exit_reason='ORPHAN',
                    exit_time=datetime('now'), pnl=0, fee=0
                WHERE id=?
            """, (trade_id,))
        runtime.logger.info(f"[startup-sync] ✓ trade#{trade_id} → ORPHAN")
    except Exception as e:
        runtime.logger.error(f"[startup-sync] 标记 trade#{trade_id} ORPHAN 失败: {e}")
