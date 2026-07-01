"""
从 OKX 拉历史 1H K 线，分页存到 csv_data/。
  python scripts/fetch_history.py --pair BTC-USDT-SWAP --days 180

注意 OKX 单次最多 1440 根（≈60 天 1H），所以会自动分页。
"""
import argparse
import csv
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.okx_client import OKXClient  # noqa: E402


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="BTC-USDT-SWAP")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--out", default=None)
    ap.add_argument("--env", default="demo")
    args = ap.parse_args()

    key = os.getenv("OKX_API_KEY", "")
    sec = os.getenv("OKX_SECRET_KEY", "")
    pp = os.getenv("OKX_PASSPHRASE", "")
    okx = OKXClient(key, sec, pp, env=args.env)

    bars_needed = args.days * 24
    page_size = 100  # OKX history-candles 单次上限 100
    all_rows: list[list[str]] = []
    after: str | None = None
    while len(all_rows) < bars_needed:
        rows = okx.get_history_candles(args.pair, bar="1H", limit=page_size, after=after)
        if not rows:
            break
        all_rows.extend(rows)
        # OKX 返回按时间倒序，pagination 用最早一根的 ts 作为下次 after
        after = rows[-1][0]
        time.sleep(0.2)

    all_rows.sort(key=lambda r: int(r[0]))

    out_path = Path(args.out) if args.out else (
        ROOT / "csv_data" / f"{args.pair.replace('-USDT-SWAP', '')}_USDT_SWAP_1H_{args.days}d.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume", "volume_ccy", "volume_ccy_quote", "confirm"])
        for r in all_rows:
            w.writerow(r[:9] if len(r) >= 9 else r + [""] * (9 - len(r)))

    print(f"saved {len(all_rows)} rows → {out_path}")


if __name__ == "__main__":
    main()
