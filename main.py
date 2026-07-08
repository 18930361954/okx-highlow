"""HighLow Bot 多账户入口。

- 单账户历史配置(无 accounts 段) → 自动合成一个 'default' 账户,行为等价旧 main.py
- 多账户配置 → 一个进程内并行跑 N 个账户,共享 data/trades.db,共享 scheduler
"""
import os
import signal
import sys
import time
from datetime import timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from core.multi_account import AccountRuntime, load_accounts
from core.scheduler import add_account_jobs
from apscheduler.schedulers.background import BackgroundScheduler
from data.db import DB
from execution.position_monitor import PositionMonitor
from utils.logger import get_logger
from utils.time_helper import utc_now, yesterday_utc_date


UTC = timezone.utc
PROJECT_ROOT = Path(__file__).resolve().parent


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


def daily_signal_and_place(rt: AccountRuntime) -> None:
    """每日 00:00 UTC (per-account):拉前一日 24 根 1H → 生成信号 → 下单。
    挂单前查 OKX 持仓,有持仓则跳过;平仓后由 Reconciler._catchup_after_exit 补挂。"""
    logger = rt.logger
    logger.info("[scheduler] daily_signal_and_place fired")
    now = utc_now()
    sig_date = yesterday_utc_date(now)

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
            logger.info(f"[skip] {pair}:当前有持仓,暂不挂单(平仓后由对账器补挂)")
            continue

        try:
            raw = rt.okx.get_candles(pair, bar="1H", limit=24)
        except Exception as e:
            logger.error(f"get_candles({pair}) failed: {e}")
            continue

        if not raw:
            logger.warning(f"{pair}: no candles returned")
            continue

        signal_dict = rt.strategy.compute_signal(pair, raw, signal_date=sig_date)
        if not signal_dict:
            logger.info(f"{pair}: no signal (flat or insufficient data)")
            continue

        margin, mode = rt.account.compute_margin(bal, pair=pair)
        leverage = rt.account.leverage_for(pair)
        logger.info(f"[signal] {signal_dict['reason']} margin={margin:.2f} ({mode}) lev={leverage}x")

        if placed_count > 0 and place_gap_sec > 0:
            time.sleep(place_gap_sec)

        algo_id = rt.order_manager.place_algo_orders(signal_dict, margin=margin, leverage=leverage)
        placed_count += 1
        if algo_id:
            logger.info(f"[order] {pair} algoId={algo_id}")
        else:
            logger.error(f"[order] {pair} place_algo_orders returned no id")


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
    """启动时若已过 signal_time_utc 且今日该 pair 尚未挂过单,补跑一次 daily_signal_and_place。"""
    logger = rt.logger
    config_strat = rt.cfg.strategy_config
    now = utc_now()
    sig_h, sig_m = map(int, str(config_strat.get("signal_time_utc", "00:00")).split(":"))
    today_signal_ts = now.replace(hour=sig_h, minute=sig_m, second=0, microsecond=0)
    if now < today_signal_ts:
        logger.info(f"[catchup] now {now.strftime('%H:%M')} < signal_time "
                    f"{sig_h:02d}:{sig_m:02d},无需补挂")
        return

    sig_date = yesterday_utc_date(now)
    existing_db = {
        r["pair"] for r in rt.db.list_trades_by_date(sig_date.isoformat(), account=rt.name)
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
            logger.info(f"[catchup] {pair} 今日已处理(db 有记录),跳过")
        elif pair in existing_okx:
            logger.info(f"[catchup] {pair} OKX 已有 pending/持仓(db 缺记录),跳过")
        else:
            pending.append(pair)

    if not pending:
        logger.info("[catchup] 所有 pair 今日均已处理,跳过补挂")
        return

    logger.info(f"[catchup] 补挂 pairs: {pending}")
    original = list(rt.cfg.pairs)
    try:
        rt.cfg.pairs = pending
        daily_signal_and_place(rt)
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
        # 预设杠杆
        for pair in rt.cfg.pairs:
            lev = rt.account.leverage_for(pair)
            try:
                rt.okx.set_leverage(pair, lev, mgnMode=rt.cfg.td_mode)
                rt.logger.info(f"[lev] {pair} = {lev}x ({rt.cfg.td_mode}) ok")
            except Exception as e:
                rt.logger.warning(
                    f"set_leverage {pair} {lev}x {rt.cfg.td_mode} failed (可到 OKX 手动设): {e}"
                )
        ok_runtimes.append(rt)

    if not ok_runtimes:
        base_logger.error("所有账户 OKX 连接均失败,退出")
        sys.exit(2)

    # 终端面板:多账户下只显示第一个账户(避免 rich Live 冲突)。
    # 想看某个账户可以在 config 里把它放到 accounts 首位。
    monitor = PositionMonitor(ok_runtimes[0].okx, db, ok_runtimes[0].account,
                              ok_runtimes[0].cfg.to_legacy_config(), logger=base_logger)
    monitor.start()

    # 调度器
    sched = BackgroundScheduler(timezone=UTC)
    rep_h, rep_m = map(int, str(config["system"]["daily_report_time_utc"]).split(":"))

    for rt in ok_runtimes:
        sig_h, sig_m = map(int, str(rt.cfg.strategy_config.get("signal_time_utc", "00:00")).split(":"))
        add_account_jobs(
            sched,
            account_name=rt.name,
            daily_signal_fn=lambda rt=rt: daily_signal_and_place(rt),
            # 各账户不再各自出报告,统一由 all_report_job 出总报告
            daily_report_fn=lambda: None,
            daily_cancel_fn=lambda rt=rt: daily_cancel(rt),
            reconcile_fn=rt.reconcile_tick,
            reconcile_interval_seconds=20,
            signal_hour=sig_h, signal_minute=sig_m,
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

    base_logger.info("[ready] HighLow Bot 系统就绪,等待 00:00 UTC 触发首次信号")

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
