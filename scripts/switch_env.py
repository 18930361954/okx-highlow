"""一键切换运行环境: 启用目标环境(demo/live)全部账户, 禁用其余。

用法:
  python scripts/switch_env.py demo          # 切到模拟盘
  python scripts/switch_env.py live          # 切到实盘
  python scripts/switch_env.py               # 只显示当前各账户 env/enabled 状态

写盘走 ruamel round-trip (保注释), 自动备份 config.yaml.bak。改完需重启机器人。
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from utils.paths import APP_ROOT as ROOT  # noqa: E402,F811 frozen 下指向 exe 旁

from ui import config_store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="一键切换 demo/live 环境")
    ap.add_argument("env", nargs="?", choices=["demo", "live"],
                    help="目标环境; 缺省只显示当前状态")
    ap.add_argument("--yes", action="store_true", help="切实盘时跳过确认")
    args = ap.parse_args()

    data = config_store.load_raw()
    accounts = config_store.list_accounts(data)
    if not accounts:
        print("config.yaml 无 accounts 段")
        return 1

    print("当前状态:")
    for a in accounts:
        mark = "[启用]" if a["enabled"] else "[禁用]"
        print(f"  {mark} {a['name']}  env={a['env']}  strategy={a['strategy_name']}")

    if not args.env:
        return 0

    if args.env == "live" and not args.yes:
        ans = input("\n切到实盘将启用全部实盘账户 (真实资金!), 输入 yes 确认: ")
        if ans.strip().lower() != "yes":
            print("已取消")
            return 1

    n = config_store.set_env_enabled(data, args.env)
    if n == 0:
        print(f"\n没有可启用的 {args.env} 账户 (env 不匹配或 api_key 为空), 未修改")
        return 1
    config_store.save_raw(data)
    print(f"\n已切到 {args.env}: 启用 {n} 个账户 (备份 config.yaml.bak), 重启机器人生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
