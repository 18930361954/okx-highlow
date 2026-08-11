"""版本清理脚本 - 打包新版本后清理旧版本目录

使用场景:
1. build.bat 打包完成后
2. 数据同步到新版本完成后
3. 清理所有旧版本目录，只保留最新版本和备份

避免排查问题时找错地方！
"""
import shutil
import sys
from pathlib import Path
from datetime import datetime


PROJECT_ROOT = Path(__file__).parent.parent
DIST_DIR = PROJECT_ROOT / "dist"
BACKUP_SUFFIX = "_backup"


def list_version_dirs():
    """列出所有版本目录（排除备份目录和生产目录）"""
    if not DIST_DIR.exists():
        return []

    EXCLUDE_DIRS = {"hlbot-live"}  # 生产目录永不清理
    version_dirs = []
    for d in DIST_DIR.iterdir():
        if d.is_dir() and not d.name.endswith(BACKUP_SUFFIX) and d.name not in EXCLUDE_DIRS:
            version_dirs.append(d)

    return sorted(version_dirs, key=lambda x: x.stat().st_mtime, reverse=True)


def cleanup_old_versions(keep_latest: int = 1, dry_run: bool = False):
    """清理旧版本目录

    Args:
        keep_latest: 保留最新的 N 个版本
        dry_run: 只打印不删除
    """
    version_dirs = list_version_dirs()

    if len(version_dirs) <= keep_latest:
        print(f"[OK] 只有 {len(version_dirs)} 个版本，无需清理")
        return

    to_keep = version_dirs[:keep_latest]
    to_remove = version_dirs[keep_latest:]

    print(f"保留版本 ({len(to_keep)}):")
    for d in to_keep:
        mtime = datetime.fromtimestamp(d.stat().st_mtime)
        print(f"  [KEEP] {d.name} (修改时间: {mtime.strftime('%Y-%m-%d %H:%M:%S')})")

    print(f"\n清理版本 ({len(to_remove)}):")
    for d in to_remove:
        mtime = datetime.fromtimestamp(d.stat().st_mtime)
        print(f"  [DEL] {d.name} (修改时间: {mtime.strftime('%Y-%m-%d %H:%M:%S')})")

        if not dry_run:
            try:
                shutil.rmtree(d)
                print(f"    → 已删除")
            except Exception as e:
                print(f"    → 删除失败: {e}")
        else:
            print(f"    → [dry-run] 将删除")

    if dry_run:
        print("\n⚠️ 这是 dry-run 模式，没有实际删除任何文件")
        print("   执行实际清理请运行: python scripts/cleanup_old_versions.py --confirm")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="清理旧版本目录")
    parser.add_argument(
        "--keep",
        type=int,
        default=1,
        help="保留最新的 N 个版本（默认: 1）"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="确认执行删除（不加此参数为 dry-run 模式）"
    )

    args = parser.parse_args()

    print("=" * 60)
    print("版本清理脚本")
    print("=" * 60)
    print(f"dist 目录: {DIST_DIR}")
    print(f"保留版本数: {args.keep}")
    print(f"执行模式: {'实际删除' if args.confirm else 'dry-run（预览）'}")
    print("=" * 60)
    print()

    cleanup_old_versions(
        keep_latest=args.keep,
        dry_run=not args.confirm
    )

    print()
    print("=" * 60)
    print("[OK] 清理完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
