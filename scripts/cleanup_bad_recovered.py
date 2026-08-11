#!/usr/bin/env python3
"""清理错误回填的 RECOVERED 记录

问题：v1.0.7 的全量对账会无差别回填最近7天所有 OKX 历史持仓，
      包括策略启动前的历史交易，污染策略验证数据。

本脚本：删除策略首次启动之前的所有 RECOVERED 记录
"""
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from data.db import DB
from datetime import datetime


def cleanup_bad_recovered(db_path: str, dry_run: bool = True):
    """清理策略启动前的 RECOVERED 记录"""
    db = DB(db_path)

    with db._conn() as conn:
        # 查询所有账户的策略启动时间
        accounts = conn.execute("""
            SELECT DISTINCT account FROM trades
        """).fetchall()

        total_deleted = 0

        for (account,) in accounts:
            # 获取该账户策略首次启动时间（第一笔非RECOVERED记录）
            first_trade = conn.execute("""
                SELECT MIN(created_at) FROM trades
                WHERE account=? AND (exit_reason IS NULL OR exit_reason != 'RECOVERED')
            """, (account,)).fetchone()

            if not first_trade or not first_trade[0]:
                print(f"[{account}] 无正常交易记录，跳过")
                continue

            strategy_start = first_trade[0]
            print(f"\n[{account}] 策略启动时间: {strategy_start}")

            # 统计该账户策略启动前的 RECOVERED 记录
            bad_recovered = conn.execute("""
                SELECT COUNT(*), MIN(entry_time), MAX(entry_time)
                FROM trades
                WHERE account=? AND exit_reason='RECOVERED'
                  AND entry_time < ?
            """, (account, strategy_start)).fetchone()

            count, min_time, max_time = bad_recovered
            if count == 0:
                print(f"[{account}] 无需清理")
                continue

            print(f"[{account}] 发现 {count} 条策略启动前的 RECOVERED 记录")
            print(f"[{account}] 时间范围: {min_time} ~ {max_time}")

            if dry_run:
                print(f"[{account}] [DRY-RUN] 将删除 {count} 条记录")
            else:
                conn.execute("""
                    DELETE FROM trades
                    WHERE account=? AND exit_reason='RECOVERED'
                      AND entry_time < ?
                """, (account, strategy_start))
                print(f"[{account}] [OK] 已删除 {count} 条记录")

            total_deleted += count

        print(f"\n{'[DRY-RUN] ' if dry_run else ''}总计: {total_deleted} 条")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="清理错误回填的 RECOVERED 记录")
    parser.add_argument("--db", default="data/trades.db", help="数据库路径")
    parser.add_argument("--apply", action="store_true", help="实际执行删除（默认 dry-run）")
    args = parser.parse_args()

    print(f"数据库: {args.db}")
    print(f"模式: {'APPLY (实际删除)' if args.apply else 'DRY-RUN (仅预览)'}\n")

    cleanup_bad_recovered(args.db, dry_run=not args.apply)
