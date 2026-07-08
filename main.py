"""HighLow Bot 多账户入口。

- 单账户历史配置(无 accounts 段) → 自动合成一个 'default' 账户,行为等价旧 main.py
- 多账户配置 → 一个进程内并行跑 N 个账户,共享 data/trades.db,共享 scheduler
"""
import os
import signal
import sys
import time
from datetime import timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from core.multi_account import AccountRuntime, load_accounts
from core.scheduler import add_account_jobs, signal_hours_for
from apscheduler.schedulers.background import BackgroundScheduler
from data.db import DB
from execution.position_monitor import PositionMonitor
from utils.logger import get_logger
from utils.time_helper import utc_now, yesterday_utc_date


UTC = timezone.utc
PROJECT_ROOT = Path(__file__).resolve().parent


# ---------------- signal-bucket helpers ----------------

def current_bucket_start(now, signal_bar: str):
    """给定 now 和 signal_bar,返回「当前正在进行的桶」的起始 UTC datetime。
    1D 桶起始 = 当天 00:00;4H 桶起始 = 最近一个 0/4/8/12/16/20 时。
    """
    hours = signal_hours_for(signal_bar)
    day_start = now.replace(minute=0, second=0, microsecond=0)
    # 找 <= now.hour 的最大 h
    h = max((x for x in hours if x <= now.hour), default=hours[-1] if hours else 0)
    if h > now.hour:
        # 当前 hour 小于最小 signal_hour → 用昨天最后一次
        day_start = day_start - timedelta(days=1)
        h = hours[-1]
    return day_start.replace(hour=h)


def previous_bucket_start(now, signal_bar: str):
    """上一桶(即 signal 依据的那一桶)起始 UTC datetime。"""
    cur = current_bucket_start(now, signal_bar)
    hours = signal_hours_for(signal_bar)
    # 找 cur.hour 前面一个 h
    idx = hours.index(cur.hour)
    if idx == 0:
        prev_day = cur - timedelta(days=1)
        return prev_day.replace(hour=hours[-1])
    return cur.replace(hour=hours[idx - 1])


def bucket_id(start_dt) -> str:
    """桶标识,存到 db.signal_date。短、可读、UTC。"""
    return start_dt.strftime("%Y-%m-%dT%H:00Z")


def load_config(path: str = "config.yaml") -> dict:
    with open(PROJECT_ROOT / path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _pairs_with_open_position(okx, logger) -> set[str]:
    """查该账户 OKX 当前有真实持仓的 pair 集合(pos != 0)。异常返回空 set。"""
    have: set[str] = set()
    try:
        for p in okx.get_positions():
            if float(p.get("pos", 0) or 0) != 0:
                inst = p.get("instId")
                if inst:
                    have.add(inst)
    except Exception as e:
        if logger:
            logger.warning(f"[skip-check] get_positions 失败,本轮不按持仓跳过: {e}")
    return have


def bucket_signal_and_place(rt: AccountRuntime) -> None:
    """信号桶触发 (per-account):在每个信号桶起始时刻拉「上一桶」K,生成信号,下单。
    信号周期由 rt.strategy.signal_bar 决定 (1D/12H/6H/4H/2H/1H)。
    挂单前查 OKX 持仓,有持仓则跳过;平仓后由 Reconciler._catchup_after_exit 补挂。
    """
    logger = rt.logger
    now = utc_now()
    signal_bar = rt.strategy.signal_bar
    prev_bkt = previous_bucket_start(now, signal_bar)
    sig_id = bucket_id(prev_bkt)
    logger.info(f"[scheduler] bucket_signal fired signal_bar={signal_bar} prev_bucket={sig_id}")

    ok, reason = rt.account.can_trade(now)
    if not ok:
        logger.warning(f"[skip] cannot trade: {reason}")
        return

    bal = rt.account.get_balance()
    place_gap_sec = float(rt.cfg.strategy_config.get("place_gap_sec", 1.0))
    placed_count = 0
    held_pairs = _pairs_with_open_position(rt.okx, logger)

    for pair in rt.cfg.pairs:
        if pair in held_pairs:
            logger.info(f"[skip] {pair}: 当前有持仓,暂不挂单(平仓后由对账器补挂)")
            continue

        # 1D 用 24 根 1H 聚合(与旧行为一致);其它周期直接拉 bar=signal_bar limit=2 取上一桶
        try:
            if signal_bar == "1D":
                raw = rt.okx.get_candles(pair, bar="1H", limit=24)
            else:
                raw = rt.okx.get_candles(pair, bar=signal_bar, limit=2)
                if raw and len(raw) >= 2:
                    # OKX 按时间倒序:[最新, 次新]。次新才是上一桶
                    raw = [raw[1]]
        except Exception as e:
            logger.error(f"get_candles({pair}) failed: {e}")
            continue

        if not raw:
            logger.warning(f"{pair}: no candles returned")
            continue

        signal_dict = rt.strategy.compute_signal(pair, raw, signal_date=sig_id)
        if not signal_dict:
            logger.info(f"{pair}: no signal (flat or insufficient data)")
            continue

        margin, mode = rt.account.compute_margin(bal, pair=pair)
        leverage = rt.account.leverage_for(pair)
        max_ct = rt.strategy.max_contracts_for(pair)
        logger.info(f"[signal] {signal_dict['reason']} margin={margin:.2f} ({mode}) lev={leverage}x max_ct={max_ct}")

        if placed_count > 0 and place_gap_sec > 0:
            time.sleep(place_gap_sec)

        algo_id = rt.order_manager.place_algo_orders(signal_dict, margin=margin, leverage=leverage,
                                                     max_contracts=max_ct)
        placed_count += 1
        if algo_id:
            logger.info(f"[order] {pair} algoId={algo_id}")
        else:
            logger.error(f"[order] {pair} place_algo_orders returned no id")


# 向后兼容名 (旧引用/测试用)
daily_signal_and_place = bucket_signal_and_place


def daily_report_all(runtimes: list[AccountRuntime], config: dict, base_logger) -> None:
    """每日 23:55 UTC (全局共 1 次): 生成一份主报告 = 总览 + 各账户拆分表。"""
    base_logger.info("[scheduler] daily_report fired (all accounts)")
    try:
        from scripts.daily_report import generate_multi_account_report
        out = generate_multi_account_report(runtimes, config)
        base_logger.info(f"[report] saved → {out}")
    except Exception as e:
        base_logger.error(f"daily_report failed: {e}")


def daily_cancel(rt: AccountRuntime) -> None:
    rt.logger.info("[scheduler] daily_cancel fired")
    rt.order_manager.cancel_all_pending()


def startup_catchup_if_needed(rt: AccountRuntime) -> None:
    """启动时判断当前信号桶该 pair 是否已挂单,未挂则补跑。
    多信号周期下无"是否已过 00:00"的固定判断,直接按当前桶来。

    保护:若当前桶已过半(> 50% 时间),跳过本桶补挂 —— 现价可能已远离信号触发价,
    挂着大概率不成交。等下桶自然 cron。
    """
    logger = rt.logger
    now = utc_now()
    signal_bar = rt.strategy.signal_bar
    cur_bkt = current_bucket_start(now, signal_bar)
    sig_id = bucket_id(previous_bucket_start(now, signal_bar))

    # 判断当前桶经过时长
    from core.scheduler import SIGNAL_BAR_HOURS
    hours = SIGNAL_BAR_HOURS.get(signal_bar, [0])
    bucket_hours = 24 // len(hours) if len(hours) >= 1 else 24
    bucket_secs = bucket_hours * 3600
    elapsed = (now - cur_bkt).total_seconds()
    if elapsed > bucket_secs * 0.5:
        logger.info(
            f"[catchup] 当前桶 {cur_bkt.strftime('%H:%M')} 已过 "
            f"{elapsed/60:.0f}/{bucket_secs/60:.0f} 分钟(>50%),跳过补挂,等下桶"
        )
        return

    existing_db = {
        r["pair"] for r in rt.db.list_trades_by_date(sig_id, account=rt.name)
    }

    existing_okx: set[str] = set()
    try:
        for o in rt.okx.list_pending_algos(ordType="trigger"):
            inst = o.get("instId")
            if inst:
                existing_okx.add(inst)
    except Exception as e:
        logger.warning(f"[catchup] list_pending_algos 失败,仅按 db 判定: {e}")
    try:
        for p in rt.okx.get_positions():
            if float(p.get("pos", 0) or 0) != 0:
                inst = p.get("instId")
                if inst:
                    existing_okx.add(inst)
    except Exception as e:
        logger.warning(f"[catchup] get_positions 失败,仅按 db 判定: {e}")

    pending: list[str] = []
    for pair in rt.cfg.pairs:
        if pair in existing_db:
            logger.info(f"[catchup] {pair} 桶 {sig_id} 已处理(db 有记录),跳过")
        elif pair in existing_okx:
            logger.info(f"[catchup] {pair} OKX 已有 pending/持仓(db 缺记录),跳过")
        else:
            pending.append(pair)

    if not pending:
        logger.info(f"[catchup] 桶 {sig_id} 所有 pair 均已处理,跳过补挂")
        return

    logger.info(f"[catchup] 补挂 pairs: {pending} (当前桶起始 {cur_bkt.isoformat()})")
    original = list(rt.cfg.pairs)
    try:
        rt.cfg.pairs = pending
        bucket_signal_and_place(rt)
    finally:
        rt.cfg.pairs = original


def init_balance_if_needed(rt: AccountRuntime) -> None:
    if rt.account.get_balance() <= 0:
        try:
            bal = rt.okx.get_balance("USDT")
            rt.account.set_balance(bal)
            rt.logger.info(f"[init] balance bootstrapped from OKX: {bal:.2f} USDT")
        except Exception as e:
            rt.logger.error(f"[init] cannot fetch balance: {e}")


def main():
    load_dotenv(PROJECT_ROOT / ".env")
    config = load_config()
    base_logger = get_logger(
        "hl-bot", level=config["system"]["log_level"],
        keep_days=int(config["system"]["log_keep_days"]),
    )

    db_path = PROJECT_ROOT / config["system"]["db_path"]
    db = DB(db_path)

    # 加载账户 (无 accounts 段 → 自动合成 default,行为等价旧 main.py)
    try:
        runtimes = load_accounts(config, db, base_logger)
    except Exception as e:
        base_logger.error(f"load_accounts failed: {e}")
        sys.exit(1)

    if not runtimes:
        base_logger.error("没有可运行的账户,退出")
        sys.exit(1)

    base_logger.info(f"[boot] 启用 {len(runtimes)} 个账户: {[rt.name for rt in runtimes]}")

    # 逐账户连通性检查 + 初始化
    ok_runtimes: list[AccountRuntime] = []
    for rt in runtimes:
        if not rt.okx.test_connection():
            rt.logger.error("OKX 连接失败,该账户跳过启动")
            continue
        rt.logger.info(f"OKX connected (env={rt.cfg.env})")
        init_balance_if_needed(rt)
        # 预设杠杆(并登记到 order_manager 缓存,避免每次挂单重复 set)
        for pair in rt.cfg.pairs:
            lev = rt.account.leverage_for(pair)
            try:
                rt.okx.set_leverage(pair, lev, mgnMode=rt.cfg.td_mode)
                rt.order_manager.mark_leverage_confirmed(pair, lev)
                rt.logger.info(f"[lev] {pair} = {lev}x ({rt.cfg.td_mode}) ok")
            except Exception as e:
                rt.logger.warning(
                    f"set_leverage {pair} {lev}x {rt.cfg.td_mode} failed (可到 OKX 手动设): {e}"
                )
        ok_runtimes.append(rt)

    if not ok_runtimes:
        base_logger.error("所有账户 OKX 连接均失败,退出")
        sys.exit(2)

    # 终端面板:多账户版,显示所有账户余额、挂单、持仓、今日成交
    monitor = PositionMonitor(runtimes=ok_runtimes, db=db, logger=base_logger)
    monitor.start()

    # 调度器
    sched = BackgroundScheduler(timezone=UTC)
    rep_h, rep_m = map(int, str(config["system"]["daily_report_time_utc"]).split(":"))

    for rt in ok_runtimes:
        signal_bar = rt.strategy.signal_bar
        base_logger.info(f"[{rt.name}] signal_bar={signal_bar}, "
                          f"每天 {len(signal_hours_for(signal_bar))} 次挂单")
        add_account_jobs(
            sched,
            account_name=rt.name,
            daily_signal_fn=lambda rt=rt: bucket_signal_and_place(rt),
            daily_report_fn=lambda: None,   # 各账户不各自出报告,统一由 daily_report_all 出总报告
            daily_cancel_fn=lambda rt=rt: daily_cancel(rt),
            reconcile_fn=rt.reconcile_tick,
            reconcile_interval_seconds=20,
            signal_bar=signal_bar,
            report_hour=rep_h, report_minute=rep_m,
        )

    # 全局 1 次总报告
    from apscheduler.triggers.cron import CronTrigger
    sched.add_job(
        lambda: daily_report_all(ok_runtimes, config, base_logger),
        trigger=CronTrigger(hour=rep_h, minute=rep_m, timezone=UTC),
        id="daily_report_all",
        misfire_grace_time=300, coalesce=True, max_instances=1, replace_existing=True,
    )

    sched.start()

    # 启动立刻各账户跑一次 reconcile + catchup
    for rt in ok_runtimes:
        try:
            rt.reconcile_tick()
        except Exception as e:
            rt.logger.error(f"启动 reconcile 失败: {e}")
        try:
            startup_catchup_if_needed(rt)
        except Exception as e:
            rt.logger.error(f"startup catchup 失败: {e}")

    base_logger.info("[ready] HighLow Bot 系统就绪,等待下一次信号桶触发")

    stop_evt = {"stop": False}

    def _shutdown(signum, frame):
        if stop_evt["stop"]:
            return
        stop_evt["stop"] = True
        base_logger.info(f"[shutdown] signal {signum} received, stopping...")
        try:
            sched.shutdown(wait=False)
        except Exception:
            pass
        monitor.stop()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        while not stop_evt["stop"]:
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)

    base_logger.info("[shutdown] bye")


if __name__ == "__main__":
    main()
