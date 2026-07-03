"""最近 30 天信号日振幅分布，评估 [1%, 4%] 阈值是否合理。"""
import sys
import io
from pathlib import Path
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent

CFGS = [
    ("BTC", "csv_data/BTC_1H_recent.csv"),
    ("ETH", "csv_data/ETH_1H_recent.csv"),
]

MIN_AMP = 0.01
MAX_AMP = 0.04


def daily_amp(csv):
    df = pd.read_csv(csv)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    df["date"] = df["ts"].dt.date
    # 只保留完整日：每日 K 数 = 24
    g = df.groupby("date").agg(
        open_=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        n=("open", "count"),
    ).reset_index()
    g = g[g["n"] == 24].copy()
    g["amp"] = (g["high"] - g["low"]) / g["open_"]
    g["dir"] = g.apply(lambda r: "阳" if r["close"] > r["open_"] else "阴" if r["close"] < r["open_"] else "平", axis=1)
    g["in_range"] = (g["amp"] >= MIN_AMP) & (g["amp"] <= MAX_AMP)
    return g


def analyze(label, csv):
    g = daily_amp(csv)
    last30 = g.tail(30)
    total = len(last30)
    in_range = int(last30["in_range"].sum())
    too_small = int((last30["amp"] < MIN_AMP).sum())
    too_big = int((last30["amp"] > MAX_AMP).sum())
    med = last30["amp"].median() * 100
    mean = last30["amp"].mean() * 100
    p25, p50, p75, p90 = (last30["amp"].quantile(q) * 100 for q in [0.25, 0.5, 0.75, 0.9])
    print(f"\n=== {label} 最近 {total} 个完整信号日 ===")
    print(f"命中 [1%, 4%]:  {in_range}/{total} ({in_range/total*100:.0f}%)")
    print(f"过小 <1%:       {too_small}/{total}")
    print(f"过大 >4%:       {too_big}/{total}")
    print(f"振幅分位数：    p25={p25:.2f}%  p50={p50:.2f}%  p75={p75:.2f}%  p90={p90:.2f}%")
    print(f"均值/中位数：   mean={mean:.2f}%  median={med:.2f}%")
    print("\n最近 30 天逐日：")
    for _, r in last30.iterrows():
        flag = "OK" if r["in_range"] else ("TOO_SMALL" if r["amp"] < MIN_AMP else "TOO_BIG")
        print(f"  {r['date']}  amp={r['amp']*100:5.2f}%  {r['dir']}  {flag}")

    # 假设放宽上限试算
    for cap in [0.05, 0.06, 0.08, 0.10]:
        n_ok = int(((last30["amp"] >= MIN_AMP) & (last30["amp"] <= cap)).sum())
        print(f"  若把上限调到 {cap*100:.0f}%: 可交易日 = {n_ok}/{total} ({n_ok/total*100:.0f}%)")


if __name__ == "__main__":
    for lab, csv in CFGS:
        analyze(lab, csv)
