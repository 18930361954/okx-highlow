"""
持仓快照备份 - 定期保存持仓状态到独立文件

用于灾难恢复场景（数据库损坏、误删除等）
"""
import json
import logging
from datetime import datetime, UTC
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


class PositionSnapshot:
    """持仓快照管理器"""

    def __init__(self, snapshot_dir: str = "data/snapshots"):
        """
        Args:
            snapshot_dir: 快照保存目录
        """
        self.snapshot_dir = Path(snapshot_dir)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    def save_snapshot(self, runtime, snapshot_type: str = "scheduled") -> Optional[str]:
        """保存当前持仓快照

        Args:
            runtime: AccountRuntime 实例
            snapshot_type: 快照类型 (scheduled/manual/pre_update)

        Returns:
            快照文件路径（失败返回 None）
        """
        try:
            timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            filename = f"{runtime.name}_{timestamp}_{snapshot_type}.json"
            filepath = self.snapshot_dir / filename

            # 查询当前未平仓持仓
            open_trades = runtime.db.execute("""
                SELECT id, pair, side, signal_date, signal_bar,
                       entry_price, entry_time, created_at, algo_order_id
                FROM trades
                WHERE account=? AND exit_price IS NULL
                ORDER BY id
            """, (runtime.name,)).fetchall()

            # 获取账户余额
            balance = runtime.account.get_balance()

            # 获取 OKX 实际持仓（用于校验）
            okx_positions = []
            try:
                for pair in runtime.cfg.pairs:
                    pos_list = runtime.okx.get_positions(pair)
                    for pos in pos_list:
                        okx_positions.append({
                            'instId': pos.get('instId'),
                            'posSide': pos.get('posSide'),
                            'pos': pos.get('pos'),
                            'avgPx': pos.get('avgPx'),
                            'upl': pos.get('upl'),
                            'uplRatio': pos.get('uplRatio'),
                        })
            except Exception as e:
                logger.warning(f"[snapshot] 获取 OKX 持仓失败: {e}")

            snapshot_data = {
                'metadata': {
                    'account': runtime.name,
                    'timestamp': datetime.now(UTC).isoformat(),
                    'snapshot_type': snapshot_type,
                    'strategy_name': runtime.cfg.strategy_name,
                    'env': runtime.cfg.env,
                },
                'balance': {
                    'db': balance,
                },
                'trades': {
                    'count': len(open_trades),
                    'items': [
                        {
                            'id': t[0],
                            'pair': t[1],
                            'side': t[2],
                            'signal_date': t[3],
                            'signal_bar': t[4],
                            'entry_price': t[5],
                            'entry_time': t[6],
                            'created_at': t[7],
                            'algo_order_id': t[8],
                        }
                        for t in open_trades
                    ]
                },
                'okx_positions': {
                    'count': len(okx_positions),
                    'items': okx_positions
                }
            }

            # 写入文件
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(snapshot_data, f, indent=2, ensure_ascii=False)

            logger.info(
                f"[snapshot] 快照已保存: {filename} "
                f"(持仓 {len(open_trades)} 笔, 余额 {balance:.2f} USDT)"
            )
            return str(filepath)

        except Exception as e:
            logger.error(f"[snapshot] 保存快照失败: {e}")
            return None

    def cleanup_old_snapshots(self, keep_days: int = 30) -> int:
        """清理过期快照

        Args:
            keep_days: 保留天数

        Returns:
            删除的快照数量
        """
        try:
            cutoff = datetime.now(UTC).timestamp() - (keep_days * 86400)
            deleted = 0

            for filepath in self.snapshot_dir.glob("*.json"):
                if filepath.stat().st_mtime < cutoff:
                    filepath.unlink()
                    deleted += 1

            if deleted:
                logger.info(f"[snapshot] 已清理 {deleted} 个过期快照（超过 {keep_days} 天）")

            return deleted

        except Exception as e:
            logger.error(f"[snapshot] 清理过期快照失败: {e}")
            return 0

    def list_snapshots(self, account: Optional[str] = None, limit: int = 10) -> list[dict]:
        """列出快照文件

        Args:
            account: 账户名（None 表示全部）
            limit: 最大返回数量

        Returns:
            快照列表（按时间倒序）
        """
        try:
            pattern = f"{account}_*.json" if account else "*.json"
            files = sorted(
                self.snapshot_dir.glob(pattern),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )[:limit]

            snapshots = []
            for filepath in files:
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    snapshots.append({
                        'filename': filepath.name,
                        'filepath': str(filepath),
                        'size': filepath.stat().st_size,
                        'mtime': filepath.stat().st_mtime,
                        'account': data['metadata']['account'],
                        'timestamp': data['metadata']['timestamp'],
                        'snapshot_type': data['metadata']['snapshot_type'],
                        'trades_count': data['trades']['count'],
                        'balance': data['balance']['db'],
                    })
                except Exception as e:
                    logger.warning(f"[snapshot] 读取快照失败 {filepath.name}: {e}")

            return snapshots

        except Exception as e:
            logger.error(f"[snapshot] 列出快照失败: {e}")
            return []

    def restore_from_snapshot(self, filepath: str, runtime, dry_run: bool = True) -> bool:
        """从快照恢复持仓（灾难恢复用）

        Args:
            filepath: 快照文件路径
            runtime: AccountRuntime 实例
            dry_run: 仅验证不执行恢复

        Returns:
            是否恢复成功
        """
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                snapshot = json.load(f)

            account = snapshot['metadata']['account']
            if account != runtime.name:
                logger.error(
                    f"[snapshot] 账户不匹配: 快照={account} 当前={runtime.name}"
                )
                return False

            logger.info(
                f"[snapshot] 准备从快照恢复: {Path(filepath).name}\n"
                f"  快照时间: {snapshot['metadata']['timestamp']}\n"
                f"  持仓数量: {snapshot['trades']['count']}\n"
                f"  余额: {snapshot['balance']['db']:.2f} USDT\n"
                f"  模式: {'演练' if dry_run else '实际恢复'}"
            )

            if dry_run:
                logger.info("[snapshot] 演练模式，不执行实际恢复")
                return True

            # 实际恢复逻辑（需谨慎使用）
            # TODO: 实现恢复逻辑（插入缺失的持仓记录、同步余额等）
            logger.warning("[snapshot] 实际恢复功能待实现，当前仅支持演练模式")
            return False

        except Exception as e:
            logger.error(f"[snapshot] 从快照恢复失败: {e}")
            return False


def create_snapshot(runtime, snapshot_type: str = "scheduled") -> Optional[str]:
    """快捷函数：创建快照

    Args:
        runtime: AccountRuntime 实例
        snapshot_type: 快照类型

    Returns:
        快照文件路径（失败返回 None）
    """
    snapshot_dir = runtime.cfg.system_config.get('snapshot_dir', 'data/snapshots')
    manager = PositionSnapshot(snapshot_dir)
    return manager.save_snapshot(runtime, snapshot_type)
