"""批量拉取 OKX 多 pair × 多周期历史 K 到 csv_data/。

用法:
  python scripts/fetch_multi.py --pairs BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \
      --bars 1D,12H,6H,4H,2H,1H,30m,15m,5m --days 730

OKX history-candles 单页最多 100 根；这里按周期自动分页并存盘。
每 pair × 周期一份独立 CSV：{coin}_USDT_SWAP_{bar}_{days}d.csv
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


# OKX bar → 每根 K 的秒数（用于估算需要多少根）
BAR_SECONDS = {
    "1D": 86400,
    "12H": 12 * 3600,
    "6H": 6 * 3600,
    "4H": 4 * 3600,
    "2H": 2 * 3600,
    "1H": 3600,
    "30m": 1800,
    "15m": 900,
    "5m": 300,
    "3m": 180,
    "1m": 60,
}


def _fetch_one(okx: OKXClient, pair: str, bar: str, days: int, sleep_s: float,
                logger=None) -> list[list[str]]:
    """拉一个 pair 的一个周期。分页向"更早"翻页,直到覆盖 days 或返回为空。"""
    seconds = BAR_SECONDS.get(bar, 3600)
    bars_needed = int(days * 86400 / seconds) + 10  # 多留点余量
    all_rows: list[list[str]] = []
    after: str | None = None
    page_size = 100

    while len(all_rows) < bars_needed:
        try:
            rows = okx.get_history_candles(pair, bar=bar, limit=page_size, after=after)
        except Exception as e:
            if logger:
                logger(f"[{pair} {bar}] request failed at rows={len(all_rows)}: {e}; sleep 5s retry")
            time.sleep(5)
            continue
        if not rows:
            break
        all_rows.extend(rows)
        after = rows[-1][0]
        time.sleep(sleep_s)

    all_rows.sort(key=lambda r: int(r[0]))
    return all_rows


def _write_csv(rows: list[list[str]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close",
                    "volume", "volume_ccy", "volume_ccy_quote", "confirm"])
        for r in rows:
            w.writerow(r[:9] if len(r) >= 9 else r + [""] * (9 - len(r)))


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP")
    ap.add_argument("--bars", default="1D,12H,6H,4H,2H,1H,30m,15m,5m")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--env", default="live",
                    help="live 才能拿完整历史；demo 上历史通常有截断")
    ap.add_argument("--sleep", type=float, default=0.15,
                    help="每次分页请求间隔秒数（防 429）")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--force", action="store_true",
                    help="强制覆盖已存在文件；默认跳过已存在的 pair×bar")
    ap.add_argument("--proxy", default=None, help="覆盖 config 里的代理,如 http://127.0.0.1:18081")
    args = ap.parse_args()

    key = os.getenv("OKX_API_KEY", "")
    sec = os.getenv("OKX_SECRET_KEY", "")
    pp = os.getenv("OKX_PASSPHRASE", "")

    # 优先读 CLI 参数,再读 config
    proxy_url = args.proxy
    if not proxy_url:
        try:
            import yaml
            cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
            net = cfg.get("network") or {}
            if net.get("proxy_enabled"):
                proxy_url = str(net.get("proxy_url") or "") or None
        except Exception:
            pass

    okx = OKXClient(key, sec, pp, env=args.env, proxy_url=proxy_url)

    outdir = Path(args.outdir) if args.outdir else (ROOT / "csv_data")
    outdir.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    bars = [b.strip() for b in args.bars.split(",") if b.strip()]

    total = len(pairs) * len(bars)
    log(f"start: {len(pairs)} pairs × {len(bars)} bars = {total} tasks, days={args.days}, "
        f"env={args.env}, proxy={proxy_url or 'off'}")

    done = 0
    for pair in pairs:
        coin = pair.split("-")[0]
        for bar in bars:
            done += 1
            out_path = outdir / f"{coin}_USDT_SWAP_{bar}_{args.days}d.csv"
            if out_path.exists() and not args.force:
                log(f"({done}/{total}) skip existing {out_path.name}")
                continue

            log(f"({done}/{total}) fetch {pair} {bar} days={args.days}...")
            t0 = time.time()
            try:
                rows = _fetch_one(okx, pair, bar, args.days, args.sleep, logger=log)
            except Exception as e:
                log(f"({done}/{total}) {pair} {bar} FAILED: {e}")
                continue
            _write_csv(rows, out_path)
            log(f"({done}/{total}) saved {len(rows)} rows → {out_path.name} "
                f"({time.time()-t0:.1f}s)")

    log("all done")


if __name__ == "__main__":
    main()
