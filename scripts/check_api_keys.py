"""探活 config.yaml 里全部账户的 OKX API Key(含 enabled=false 的)。

用法:
  hlbot check-keys              # 探活并打印结果
  hlbot check-keys --notify     # 有失效时同时发 webhook 告警

OKX 的 API Key 有有效期,过期后挂单/平仓/对账全部瘫掉。这个命令随时手动跑,
系统也会每周一 02:30 UTC + 每次启动时自动跑一次。
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml

from utils.paths import APP_ROOT


def main() -> int:
    ap = argparse.ArgumentParser(description="探活全部账户的 OKX API Key")
    ap.add_argument("--notify", action="store_true",
                    help="有 Key 失效时发 webhook 告警")
    args = ap.parse_args()

    with open(APP_ROOT / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    from core.apikey_health import check_all_api_keys, notify_if_invalid

    print("正在探活全部账户的 API Key (含停用账户)...\n")
    results = check_all_api_keys(config=cfg)

    label = {"OK": "正常", "KEY_INVALID": "已失效", "NETWORK": "网络问题",
             "UNKNOWN": "待查", "NO_KEY": "未配置"}
    print(f"{'账户':20s} {'环境':6s} {'状态':6s} {'结果':10s} 说明")
    print("-" * 88)
    for r in results:
        print(f"{r['name']:20s} {r['env']:6s} "
              f"{'启用' if r['enabled'] else '停用':6s} "
              f"{label.get(r['status'], r['status']):10s} {r['detail']}")

    bad = [r for r in results if r["status"] == "KEY_INVALID"]
    warn = [r for r in results if r["status"] in ("NETWORK", "UNKNOWN")]
    print()
    print(f"正常 {sum(1 for r in results if r['status'] == 'OK')} / "
          f"失效 {len(bad)} / 待查 {len(warn)} / "
          f"未配置 {sum(1 for r in results if r['status'] == 'NO_KEY')}")

    if bad:
        print()
        print("以下账户的 API Key 已失效或过期，无法挂单/平仓/对账：")
        for r in bad:
            print(f"  - {r['name']} ({r['env']}): {r['detail']}")
        print("处理：去 OKX 重建 API Key，更新 config.yaml 后重启机器人。")
    if warn:
        print()
        print("以下账户未能确认（多为网络/代理问题，不一定是 Key 失效）：")
        for r in warn:
            print(f"  - {r['name']}: {r['detail']}")

    if args.notify:
        notify_if_invalid(results, cfg)

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
