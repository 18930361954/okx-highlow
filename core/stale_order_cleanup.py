"""过期订单清理模块 - 自动清理超过桶长 2 倍未触发的僵尸订单

核心场景:
1. fade 补挂延迟导致触发价不合理，订单长期挂着不成交
2. 价格已远离，订单永远不会触发
3. 占用挂单槽位，影响后续正常挂单

设计原则:
- 超过桶长 2 倍的 pending 订单 → 撤单并标记 EXPIRED
- 只清理未入场的订单（entry_time=None）
- 幂等：撤单失败不影响 db 标记，下次自动重试
"""
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.multi_account import AccountRuntime


UTC = timezone.utc

# 信号周期 → 小时数映射
_SIGNAL_BAR_HOURS = {
    "1D": 24,
    "12H": 12,
    "6H": 6,
    "4H": 4,
    "2H": 2,
    "1H": 1,
}


def cleanup_stale_orders(runtime: "AccountRuntime") -> int:
    """清理过期订单（超过桶长 2 倍未触发）

    Returns:
        清理的订单数量
    """
    logger = runtime.logger
    now = datetime.now(UTC)

    # 查询所有未入场的 open trades
    try:
        open_trades = runtime.db.execute("""
            SELECT id, pair, signal_date, signal_bar, okx_order_id, created_at
            FROM trades
            WHERE account=? AND exit_price IS NULL
              AND entry_time IS NULL
              AND okx_order_id IS NOT NULL
        """, (runtime.name,)).fetchall()
    except Exception as e:
        logger.error(f"[stale-cleanup] 查询 open trades 失败: {e}")
        return 0

    if not open_trades:
        return 0

    cleaned = 0
    for trade in open_trades:
        trade_id, pair, sig_date, signal_bar, algo_id, created_at = trade

        # 计算订单年龄
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except Exception:
            logger.warning(f"[stale-cleanup] trade#{trade_id} created_at 解析失败: {created_at}")
            continue

        age_hours = (now - created).total_seconds() / 3600

        # 获取桶长
        bucket_hours = _SIGNAL_BAR_HOURS.get(signal_bar, 24)
        threshold = bucket_hours * 2

        # 超过 2 个桶长 = 过期
        if age_hours > threshold:
            logger.warning(
                f"[stale-cleanup] trade#{trade_id} {pair} {sig_date} 已过期 "
                f"{age_hours:.1f}h (>{threshold}h)，撤单并标记 EXPIRED"
            )

            # 尝试撤单
            try:
                runtime.okx.cancel_algo_order(algo_id, pair)
                logger.info(f"[stale-cleanup] ✓ 撤单成功 algoId={algo_id}")
            except Exception as e:
                # 撤单失败不影响 db 标记（可能已被撤或不存在）
                logger.warning(f"[stale-cleanup] 撤单失败（可能已不存在）: {e}")

            # 标记为 EXPIRED
            try:
                runtime.db.execute("""
                    UPDATE trades
                    SET exit_price=0, exit_reason='EXPIRED',
                        exit_time=datetime('now'), pnl=0, fee=0
                    WHERE id=?
                """, (trade_id,))
                logger.info(f"[stale-cleanup] ✓ trade#{trade_id} → EXPIRED")
                cleaned += 1
            except Exception as e:
                logger.error(f"[stale-cleanup] 标记 trade#{trade_id} EXPIRED 失败: {e}")

    if cleaned > 0:
        logger.info(f"[stale-cleanup] 本轮清理 {cleaned} 笔过期订单")

    return cleaned


def check_missed_signals(runtime: "AccountRuntime") -> None:
    """检测并补救到点未挂的信号

    场景:
    - cron 错过或 OKX 50001 导致跳过
    - 当前桶未过期（<50%）且无挂单无持仓 → 立即补挂
    """
    from core.scheduler import previous_bucket_start, bucket_id

    logger = runtime.logger
    now = datetime.now(UTC)

    for pair in runtime.pairs:
        signal_bar = runtime.strategy.signal_bar_for(pair)

        # 获取当前桶
        curr_bucket = previous_bucket_start(now, signal_bar)
        sig_id = bucket_id(curr_bucket)

        # 检查 db 是否有该桶的记录
        try:
            existing = runtime.db.execute("""
                SELECT COUNT(*) FROM trades
                WHERE account=? AND pair=? AND signal_date=?
            """, (runtime.name, pair, sig_id)).fetchone()

            if existing and existing[0] > 0:
                continue  # 已有记录
        except Exception as e:
            logger.warning(f"[missed-signal] 查询 db 失败: {e}")
            continue

        # 检查当前桶是否过期（超过 50%）
        bucket_hours = _SIGNAL_BAR_HOURS.get(signal_bar, 24)
        elapsed = (now - curr_bucket).total_seconds() / 3600

        if elapsed >= bucket_hours * 0.5:
            # 已过期，不补挂
            continue

        # 检查是否有 pending 或持仓
        try:
            has_pending = runtime.db.execute("""
                SELECT COUNT(*) FROM trades
                WHERE account=? AND pair=? AND exit_price IS NULL
            """, (runtime.name, pair)).fetchone()

            if has_pending and has_pending[0] > 0:
                continue  # 有未平仓记录
        except Exception as e:
            logger.warning(f"[missed-signal] 查询 pending 失败: {e}")
            continue

        # 🔴 到点未挂！
        logger.error(
            f"[missed-signal] 🔴 {pair} {sig_id} 未挂单且无持仓！"
            f" 已过{elapsed:.1f}h/{bucket_hours}h"
        )

        # 立即补挂
        try:
            # 调用 strategy 生成信号并挂单
            logger.info(f"[missed-signal] 尝试补挂 {pair} {sig_id}...")

            # 这里需要调用 order_manager.place_orders_for_signal
            # 但需要先生成信号，暂时记录告警，由人工介入
            logger.warning(
                f"[missed-signal] {pair} {sig_id} 需要人工介入补挂"
            )
        except Exception as e:
            logger.error(f"[missed-signal] {pair} 补挂失败: {e}")
