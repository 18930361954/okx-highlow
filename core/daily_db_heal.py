"""每日 DB 自愈任务 - 深度对账与修复

每日定时运行（23:50），执行完整的数据一致性检查和修复：
1. 标记 db 有但 OKX 不存在的孤儿订单
2. 从 OKX 历史持仓回填缺失记录
3. 余额严重偏差告警

与启动全量对账的区别：
- 启动对账：快速修复，7 天历史
- 每日自愈：深度检查，30 天历史，更详细的日志
"""
from datetime import datetime, timedelta, timezone


UTC = timezone.utc


def daily_db_heal(runtime):
    """每日 DB 自愈 - 完整数据一致性检查和修复

    Args:
        runtime: AccountRuntime 实例
    """
    logger = runtime.logger
    logger.info("[daily-heal] ========== 每日 DB 自愈开始 ==========")

    stats = {
        "orphans_marked": 0,
        "trades_recovered": 0,
        "balance_diff": 0.0,
    }

    # === 第一步：标记孤儿订单 ===
    logger.info("[daily-heal] 1/3 标记孤儿订单...")
    stats["orphans_marked"] = _mark_orphan_trades(runtime)

    # === 第二步：回填缺失记录（30 天历史）===
    logger.info("[daily-heal] 2/3 回填缺失记录（30 天历史）...")
    stats["trades_recovered"] = _recover_missing_trades(runtime, days=30)

    # === 第三步：余额校验 ===
    logger.info("[daily-heal] 3/3 余额校验...")
    stats["balance_diff"] = _validate_balance(runtime)

    # === 第四步：持仓快照备份（P3优化）===
    logger.info("[daily-heal] 4/4 持仓快照备份...")
    snapshot_path = None
    try:
        from core.position_snapshot import create_snapshot
        snapshot_path = create_snapshot(runtime, snapshot_type="scheduled")
        if snapshot_path:
            logger.info(f"[daily-heal] 持仓快照已保存: {snapshot_path}")
    except Exception as e:
        logger.debug(f"[daily-heal] 持仓快照保存失败: {e}")

    # === 总结 ===
    logger.info(
        f"[daily-heal] ========== 每日 DB 自愈完成 =========="
    )
    logger.info(
        f"[daily-heal] 孤儿订单: {stats['orphans_marked']} 个, "
        f"回填记录: {stats['trades_recovered']} 个, "
        f"余额差: {stats['balance_diff']:.2f} USDT"
    )

    stats['snapshot_path'] = snapshot_path
    return stats


def _mark_orphan_trades(runtime) -> int:
    """标记 db 有但 OKX 不存在的孤儿订单

    Returns:
        标记的孤儿订单数量
    """
    logger = runtime.logger

    # 查询 db 所有 open trades
    with runtime.db._conn() as conn:
        rows = conn.execute("""
            SELECT id, pair, okx_order_id, signal_date
            FROM trades
            WHERE account=? AND exit_price IS NULL AND okx_order_id IS NOT NULL
        """, (runtime.name,)).fetchall()
    open_trades = list(rows)

    if not open_trades:
        logger.info("[daily-heal] 无未平仓记录")
        return 0

    # 查询 OKX 所有 pending algo
    okx_algos = {}
    for pair in runtime.cfg.pairs:
        try:
            algos = runtime.okx.list_pending_algos(ordType="trigger", instId=pair)
            for a in algos:
                okx_algos[a['algoId']] = a
        except Exception as e:
            logger.warning(f"[daily-heal] 查询 {pair} pending 失败: {e}")
            return 0  # API 失败时不标记，避免误杀

    # 标记孤儿
    orphan_count = 0
    for trade_id, pair, algo_id, sig_date in open_trades:
        if algo_id not in okx_algos:
            # db 有但 OKX 没有，查询订单详细状态
            try:
                order_info = runtime.okx._request('GET', '/api/v5/trade/order-algo',
                                                 params={'algoId': algo_id})
                if order_info.get('data'):
                    state = order_info['data'][0]['state']
                    if state == 'canceled':
                        logger.info(
                            f"[daily-heal] trade#{trade_id} {pair} {sig_date} "
                            f"OKX 已撤但 db 未标记，修复为 CANCELLED"
                        )
                        with runtime.db._conn() as conn:
                            conn.execute("""
                                UPDATE trades
                                SET exit_price=0, exit_reason='CANCELLED',
                                    exit_time=datetime('now'), pnl=0
                                WHERE id=?
                            """, (trade_id,))
                        orphan_count += 1
                    elif state == 'effective':
                        # 仍有效但不在 pending 列表，可能是 API 延迟
                        logger.debug(f"[daily-heal] trade#{trade_id} state=effective 但不在 pending")
                    else:
                        logger.warning(
                            f"[daily-heal] trade#{trade_id} {pair} {sig_date} "
                            f"state={state}，标记为 ORPHAN"
                        )
                        with runtime.db._conn() as conn:
                            conn.execute("""
                                UPDATE trades
                                SET exit_price=0, exit_reason='ORPHAN',
                                    exit_time=datetime('now'), pnl=0
                                WHERE id=?
                            """, (trade_id,))
                        orphan_count += 1
                else:
                    # OKX 查不到订单（可能已删除）
                    logger.warning(
                        f"[daily-heal] trade#{trade_id} {pair} {sig_date} "
                        f"OKX 查不到订单，标记为 ORPHAN"
                    )
                    with runtime.db._conn() as conn:
                        conn.execute("""
                            UPDATE trades
                            SET exit_price=0, exit_reason='ORPHAN',
                                exit_time=datetime('now'), pnl=0
                            WHERE id=?
                        """, (trade_id,))
                    orphan_count += 1
            except Exception as e:
                logger.warning(f"[daily-heal] 查询 trade#{trade_id} 失败: {e}")

    return orphan_count


def _recover_missing_trades(runtime, days=30, rate_limit_delay=0.5) -> int:
    """从 OKX 历史持仓回填 db 缺失的交易

    只回填策略启动时间之后的持仓，避免污染验证数据。
    使用 begin 参数 + 开仓时间二次过滤，与 startup_reconcile 保持一致。

    Args:
        runtime: AccountRuntime 实例
        days: 回溯天数（但不早于策略启动时间）
        rate_limit_delay: 每次 API 调用后的延迟（秒）

    Returns:
        回填的记录数量
    """
    import time
    from datetime import datetime, timezone
    logger = runtime.logger
    UTC = timezone.utc

    # 策略启动时间：优先读配置，否则不回填历史
    strategy_start_ms = None
    config_start_date = getattr(runtime.cfg, 'strategy_start_date', None)
    if config_start_date:
        try:
            strategy_start = datetime.fromisoformat(config_start_date).replace(tzinfo=UTC)
            strategy_start_ms = int(strategy_start.timestamp() * 1000)
            logger.info(f"[daily-heal] 回填起点: {strategy_start.isoformat()}")
        except Exception as e:
            logger.warning(f"[daily-heal] strategy_start_date 格式错误: {e}")

    if strategy_start_ms is None:
        logger.info("[daily-heal] 未配置 strategy_start_date，跳过历史回填（防止数据污染）")
        return 0

    since = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)
    # 取两者较大值：不早于策略启动时间
    since = max(since, strategy_start_ms)

    recovered_count = 0

    for pair in runtime.cfg.pairs:
        try:
            # 限速：避免触发 OKX API 限流
            time.sleep(rate_limit_delay)

            resp = runtime.okx._request('GET', '/api/v5/account/positions-history', params={
                'instType': 'SWAP',
                'instId': pair,
                'begin': str(strategy_start_ms),  # 从策略启动时间开始（平仓时间过滤）
                'limit': '100'
            })

            if not resp.get('data'):
                continue

            for pos in resp['data']:
                open_ts = int(pos['cTime'])
                close_ts = int(pos['uTime'])

                # 二次过滤：只回填开仓时间 >= strategy_start 的持仓
                # OKX positions-history 的 begin 参数过滤的是平仓时间，不是开仓时间
                if open_ts < strategy_start_ms:
                    continue

                open_time = datetime.fromtimestamp(open_ts/1000, tz=UTC).isoformat()
                close_time = datetime.fromtimestamp(close_ts/1000, tz=UTC).isoformat()

                # 检查 db 是否有该持仓（宽松匹配：入场时间±5分钟）
                with runtime.db._conn() as conn:
                    existing = conn.execute("""
                        SELECT id FROM trades
                        WHERE account=? AND pair=?
                          AND abs(julianday(entry_time) - julianday(?)) < 0.0035
                        LIMIT 1
                    """, (runtime.name, pair, open_time)).fetchone()

                if existing:
                    continue  # 已存在

                # db 缺失，回填
                logger.warning(
                    f"[daily-heal] {pair} posId={pos['posId']} db 缺失，回填 "
                    f"开仓={open_time[:16]} 平仓={close_time[:16]} pnl={pos.get('realizedPnl')}"
                )

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
                        'long' if pos['posSide'] == 'long' else 'short',
                        'RECOVERED', 'UNKNOWN',
                        float(pos.get('openAvgPx', 0)),
                        float(pos.get('closeAvgPx', 0)),
                        'RECOVERED',
                        open_time, close_time,
                        float(pos.get('realizedPnl', 0)),
                        float(pos.get('pnl', 0)),
                        abs(float(pos.get('fee', 0))),
                        float(pos.get('fundingFee', 0))
                    ))

                recovered_count += 1

        except Exception as e:
            logger.error(f"[daily-heal] {pair} 回填失败: {e}")

    return recovered_count


def _validate_balance(runtime) -> float:
    """余额校验

    Returns:
        余额差值（OKX - db）
    """
    logger = runtime.logger

    try:
        okx_balance = runtime.okx.get_cash_balance("USDT")
        db_balance = runtime.account.get_balance()
        diff = okx_balance - db_balance

        logger.info(
            f"[daily-heal] 余额对比: db={db_balance:.2f} okx={okx_balance:.2f} 差={diff:.2f}"
        )

        # P2优化: 数据漂移告警 - 余额偏差超过阈值时记录告警
        if abs(diff) > 10:
            logger.error(
                f"[daily-heal] ⚠️ 数据漂移告警: 余额偏差超过 10 USDT！"
                f"db={db_balance:.2f} okx={okx_balance:.2f} 差={diff:+.2f}"
            )
            # 记录告警到数据库
            try:
                from datetime import datetime
                with runtime.db._conn() as c:
                    c.execute("""
                        INSERT INTO drift_alerts (account, alert_type, severity, message, created_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        runtime.name,
                        'BALANCE_DRIFT',
                        'HIGH' if abs(diff) > 50 else 'MEDIUM',
                        f"余额偏差 {diff:+.2f} USDT (db={db_balance:.2f} okx={okx_balance:.2f})",
                        datetime.now().isoformat()
                    ))
            except Exception as e:
                logger.warning(f"[daily-heal] 记录漂移告警失败: {e}")

            # P3优化: Webhook 通知
            try:
                from core.notifier import send_balance_sync_alert
                notifier = getattr(runtime, 'notifier', None)
                if notifier:
                    send_balance_sync_alert(notifier, runtime.name, db_balance, okx_balance, diff)
            except Exception as e:
                logger.debug(f"[daily-heal] Webhook 通知失败: {e}")

        # 同步到 OKX 余额
        runtime.account.set_balance(okx_balance)
        logger.info(f"[daily-heal] 余额已同步到 OKX 真值: {okx_balance:.2f} USDT")

        return diff

    except Exception as e:
        logger.error(f"[daily-heal] 余额校验失败: {e}")
        return 0.0
