"""
从 OKX 拉指定周期 K 线到 csv_data/（分页）。
  python scripts/fetch_bar.py --pair BTC-USDT-SWAP --bar 4H --days 400
  python scripts/fetch_bar.py --pair ETH-USDT-SWAP --bar 15m --days 400
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


BAR_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1H": 60, "2H": 120, "4H": 240, "6H": 360, "12H": 720, "1D": 1440,
}


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="BTC-USDT-SWAP")
    ap.add_argument("--bar", default="1H")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out", default=None)
    ap.add_argument("--env", default="live")
    args = ap.parse_args()

    if args.bar not in BAR_MINUTES:
        raise SystemExit(f"unsupported bar {args.bar}, choose from {list(BAR_MINUTES)}")

    minutes = BAR_MINUTES[args.bar]
    bars_needed = int(args.days * 24 * 60 / minutes) + 20
    page_size = 100  # history-candles 上限 100

    key = os.getenv("OKX_API_KEY", "")
    sec = os.getenv("OKX_SECRET_KEY", "")
    pp = os.getenv("OKX_PASSPHRASE", "")
    okx = OKXClient(key, sec, pp, env=args.env)

    all_rows: list[list[str]] = []
    after: str | None = None
    seen_ts: set[str] = set()
    empty_streak = 0
    while len(all_rows) < bars_needed:
        rows = okx.get_history_candles(args.pair, bar=args.bar, limit=page_size, after=after)
        if not rows:
            empty_streak += 1
            if empty_streak >= 3:
                break
            time.sleep(0.5)
            continue
        empty_streak = 0
        new = [r for r in rows if r[0] not in seen_ts]
        if not new:
            break
        seen_ts.update(r[0] for r in new)
        all_rows.extend(new)
        after = rows[-1][0]
        if len(all_rows) % 1000 == 0 or len(all_rows) < 200:
            print(f"  fetched {len(all_rows)} / {bars_needed} …")
        time.sleep(0.12)

    all_rows.sort(key=lambda r: int(r[0]))

    tag = args.pair.replace("-USDT-SWAP", "")
    out_path = Path(args.out) if args.out else (
        ROOT / "csv_data" / f"{tag}_USDT_SWAP_{args.bar}_{args.days}d.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume",
                    "volume_ccy", "volume_ccy_quote", "confirm"])
        for r in all_rows:
            w.writerow(r[:9] if len(r) >= 9 else r + [""] * (9 - len(r)))

    from datetime import datetime, timezone
    if all_rows:
        t0 = datetime.fromtimestamp(int(all_rows[0][0]) / 1000, tz=timezone.utc)
        t1 = datetime.fromtimestamp(int(all_rows[-1][0]) / 1000, tz=timezone.utc)
        print(f"saved {len(all_rows)} rows [{t0} ~ {t1}] → {out_path}")
    else:
        print("no rows fetched")


if __name__ == "__main__":
    main()
