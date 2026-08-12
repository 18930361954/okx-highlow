"""到点未挂检测 - 每小时检测是否漏挂信号

检测逻辑：
1. 遍历所有配置的 pair
2. 计算当前应该有的信号桶（上一个完整桶）
3. 检查 db 是否有该信号的记录
4. 检查 OKX 是否有对应的 pending 订单
5. 如果两者都没有，且未熔断，则判定为漏挂

补救措施：
- 立即补挂信号（复用 reconciler 的 _catchup_after_exit 逻辑）
- 记录日志告警
"""
from datetime import datetime, timezone
from core.buckets import bucket_id, previous_bucket_start


UTC = timezone.utc


def check_missing_signals(runtime):
    """检测到点未挂的信号并补挂

    Args:
        runtime: AccountRuntime 实例

    Returns:
        补挂的信号数量
    """
    logger = runtime.logger
    logger.info("[missing-signal] ========== 检测到点未挂信号 ==========")

    补挂计数 = 0
    now = datetime.now(UTC)

    for pair_cfg in runtime.cfg.pairs:
        pair = pair_cfg if isinstance(pair_cfg, str) else pair_cfg.get('symbol')
        signal_bar = runtime.strategy.signal_bar_for(pair)

        # 上一个完整桶的起始时间
        prev_bucket = previous_bucket_start(now, signal_bar)
        sig_id = bucket_id(prev_bucket)

        # 检查 db 是否有该信号的记录
        trades = runtime.db.list_trades_by_date(sig_id, account=runtime.name)
        has_db_record = any(t.get('pair') == pair for t in trades)

        # 检查 OKX 是否有 pending 订单
        has_pending = False
        try:
            algos = runtime.okx.list_pending_algos(ordType="trigger", instId=pair)
            has_pending = len(algos) > 0
        except Exception as e:
            logger.warning(f"[missing-signal] {pair} 查询 pending 失败: {e}")
            continue

        # 检查是否有持仓
        has_position = False
        try:
            positions = runtime.okx.get_positions(instId=pair)
            has_position = any(float(p.get('pos', 0) or 0) != 0 for p in positions)
        except Exception as e:
            logger.warning(f"[missing-signal] {pair} 查询持仓失败: {e}")
            continue

        # 判断是否漏挂
        if not has_db_record and not has_pending and not has_position:
            # 检查账户是否熔断
            can_trade, reason = runtime.account.can_trade(now)
            if not can_trade:
                logger.info(
                    f"[missing-signal] {pair} {sig_id} 漏挂但账户熔断({reason}), 不补挂"
                )
                continue

            # 检查信号是否过期（距离桶起始超过桶长 50%）
            bucket_secs = {
                "1D": 86400, "12H": 43200, "6H": 21600,
                "4H": 14400, "2H": 7200, "1H": 3600,
            }.get(signal_bar, 3600)

            elapsed_secs = (now - prev_bucket).total_seconds()
            if elapsed_secs > bucket_secs * 0.5:
                logger.info(
                    f"[missing-signal] {pair} {sig_id} 漏挂但信号已过期 "
                    f"(elapsed={elapsed_secs:.0f}s > {bucket_secs*0.5:.0f}s), 不补挂"
                )
                continue

            # 判定为漏挂，触发补挂
            logger.warning(
                f"[missing-signal] ⚠️ {pair} {sig_id} 检测到漏挂! "
                f"(db={has_db_record} pending={has_pending} pos={has_position})"
            )

            # 调用 reconciler 的补挂逻辑
            if hasattr(runtime, 'reconciler') and runtime.reconciler:
                try:
                    # 直接调用 _catchup_after_exit（假装刚平仓）
                    runtime.reconciler._catchup_after_exit(pair, now)
                    补挂计数 += 1
                    logger.info(f"[missing-signal] {pair} {sig_id} 补挂完成")
                except Exception as e:
                    logger.error(f"[missing-signal] {pair} {sig_id} 补挂失败: {e}")
            else:
                logger.error(f"[missing-signal] {pair} {sig_id} reconciler 未注入，无法补挂")

    logger.info(
        f"[missing-signal] ========== 检测完成，补挂 {补挂计数} 个信号 =========="
    )

    return 补挂计数
