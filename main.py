import os
import signal
import sys
import time
from datetime import timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from core.account_state import AccountState
from core.okx_client import OKXClient
from core.scheduler import build_scheduler
from data.db import DB
from execution.order_manager import OrderManager
from execution.position_monitor import PositionMonitor
from execution.reconciler import Reconciler
from strategy.high_low import HighLowStrategy
from utils.logger import get_logger
from utils.time_helper import utc_now, yesterday_utc_date


UTC = timezone.utc
PROJECT_ROOT = Path(__file__).resolve().parent


def load_config(path: str = "config.yaml") -> dict:
    with open(PROJECT_ROOT / path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def daily_signal_and_place(okx, db, strategy, account, order_mgr, config, logger):
    """每日 00:00 UTC：拉前一日 24 根 1H → 生成信号 → 下单"""
    logger.info("[scheduler] daily_signal_and_place fired")
    now = utc_now()
    sig_date = yesterday_utc_date(now)  # 前一日的数据 → 今日挂单

    ok, reason = account.can_trade(now)
    if not ok:
        logger.warning(f"[skip] cannot trade: {reason}")
        return

    bal = account.get_balance()
    leverage = int(config["strategy"]["leverage"])

    for pair in config["strategy"]["pairs"]:
        try:
            raw = okx.get_candles(pair, bar="1H", limit=24)
        except Exception as e:
            logger.error(f"get_candles({pair}) failed: {e}")
            continue

        if not raw:
            logger.warning(f"{pair}: no candles returned")
            continue

        signal = strategy.compute_signal(pair, raw, signal_date=sig_date)
        if not signal:
            logger.info(f"{pair}: no signal (flat or insufficient data)")
            continue

        # pair 级仓位比例：不同 pair 可以有独立 position_pct
        margin, mode = account.compute_margin(bal, pair=pair)
        logger.info(f"[signal] {signal['reason']} margin={margin:.2f} ({mode})")
        algo_id = order_mgr.place_algo_orders(signal, margin=margin, leverage=leverage)
        if algo_id:
            logger.info(f"[order] {pair} algoId={algo_id}")
        else:
            logger.error(f"[order] {pair} place_algo_orders returned no id")


def daily_report(db, account, config, logger):
    """每日 23:55 UTC：生成 Markdown 报告"""
    logger.info("[scheduler] daily_report fired")
    try:
        from scripts.daily_report import generate_report
        generate_report(db, account, config)
    except Exception as e:
        logger.error(f"daily_report failed: {e}")


def daily_cancel(order_mgr, logger):
    """每日 23:59 UTC：撤所有未触发挂单"""
    logger.info("[scheduler] daily_cancel fired")
    order_mgr.cancel_all_pending()


def startup_catchup_if_needed(okx, db, strategy, account, order_mgr, config, logger):
    """启动时若已过今日 signal_time_utc 且今日该 pair 尚未挂过单，补跑一次 daily_signal_and_place。
    判定：db.trades 里 signal_date=昨日 且 pair=该 pair 的记录不存在 → 视为漏挂。
    补挂时会临时把 config.strategy.pairs 收窄成"待补 pair 列表"，避免对已挂 pair 重复下单。
    """
    now = utc_now()
    sig_h, sig_m = map(int, str(config["strategy"]["signal_time_utc"]).split(":"))
    today_signal_ts = now.replace(hour=sig_h, minute=sig_m, second=0, microsecond=0)
    if now < today_signal_ts:
        logger.info(f"[catchup] now {now.strftime('%H:%M')} < signal_time "
                    f"{sig_h:02d}:{sig_m:02d}, 无需补挂")
        return

    sig_date = yesterday_utc_date(now)  # 今日挂的单 db 里 signal_date=昨日
    existing_db = {r["pair"] for r in db.list_trades_by_date(sig_date.isoformat())}

    # 交叉验证 OKX 实际状态：pending algo 单 + 已有持仓都算"今日已处理"，
    # 防止 db 因下单成功但客户端漏写等边界情况导致重复挂单。
    existing_okx: set[str] = set()
    try:
        for o in okx.list_pending_algos(ordType="trigger"):
            inst = o.get("instId")
            if inst:
                existing_okx.add(inst)
    except Exception as e:
        logger.warning(f"[catchup] list_pending_algos 失败，仅按 db 判定: {e}")
    try:
        for p in okx.get_positions():
            if float(p.get("pos", 0) or 0) != 0:
                inst = p.get("instId")
                if inst:
                    existing_okx.add(inst)
    except Exception as e:
        logger.warning(f"[catchup] get_positions 失败，仅按 db 判定: {e}")

    pending: list[str] = []
    for pair in config["strategy"]["pairs"]:
        if pair in existing_db:
            logger.info(f"[catchup] {pair} 今日已处理（db 有记录），跳过")
        elif pair in existing_okx:
            logger.info(f"[catchup] {pair} OKX 已有 pending/持仓（db 缺记录），跳过")
        else:
            pending.append(pair)

    if not pending:
        logger.info("[catchup] 所有 pair 今日均已处理，跳过补挂")
        return

    logger.info(f"[catchup] 补挂 pairs: {pending}")
    original_pairs = config["strategy"]["pairs"]
    try:
        config["strategy"]["pairs"] = pending
        daily_signal_and_place(okx, db, strategy, account, order_mgr, config, logger)
    finally:
        config["strategy"]["pairs"] = original_pairs


def init_balance_if_needed(okx, account, logger):
    """首次启动：余额 == 0 时从 OKX 拉一次实际余额"""
    if account.get_balance() <= 0:
        try:
            bal = okx.get_balance("USDT")
            account.set_balance(bal)
            logger.info(f"[init] balance bootstrapped from OKX: {bal:.2f} USDT")
        except Exception as e:
            logger.error(f"[init] cannot fetch balance: {e}")


def main():
    load_dotenv(PROJECT_ROOT / ".env")
    config = load_config()
    logger = get_logger("hl-bot", level=config["system"]["log_level"],
                        keep_days=int(config["system"]["log_keep_days"]))

    api_key = os.getenv("OKX_API_KEY", "")
    secret = os.getenv("OKX_SECRET_KEY", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")
    if not api_key:
        logger.error("OKX_API_KEY not set in .env — exiting")
        sys.exit(1)

    db = DB(PROJECT_ROOT / config["system"]["db_path"])
    okx = OKXClient(api_key, secret, passphrase, env=config["account"]["env"], logger=logger)

    if not okx.test_connection():
        logger.error("OKX connection failed — exiting")
        sys.exit(2)
    logger.info(f"OKX connected (env={config['account']['env']})")

    strategy = HighLowStrategy(config, logger=logger)
    account = AccountState(db, config, logger=logger)
    td_mode = str(config["account"].get("td_mode", "cross"))
    order_mgr = OrderManager(okx, db, logger=logger, td_mode=td_mode)

    init_balance_if_needed(okx, account, logger)

    # 预设杠杆（cross 全仓：set_leverage 一次设好 long+short）
    lev = int(config["strategy"]["leverage"])
    for pair in config["strategy"]["pairs"]:
        try:
            okx.set_leverage(pair, lev, mgnMode=td_mode)
            logger.info(f"[lev] {pair} = {lev}x ({td_mode}) ok")
        except Exception as e:
            logger.warning(f"set_leverage {pair} {lev}x {td_mode} failed (可到 OKX 手动设): {e}")

    monitor = PositionMonitor(okx, db, account, config, logger=logger)
    monitor.start()

    reconciler = Reconciler(okx, db, account, config, logger=logger,
                            strategy=strategy, order_manager=order_mgr)

    def _reconcile_tick():
        try:
            n = reconciler.run_once()
            if n:
                logger.info(f"[reconcile] settled {n} trade update(s)")
        except Exception as e:
            logger.error(f"[reconcile] tick failed: {e}")

    # 解析 signal/report/cancel 时间
    sig_h, sig_m = map(int, str(config["strategy"]["signal_time_utc"]).split(":"))
    rep_h, rep_m = map(int, str(config["system"]["daily_report_time_utc"]).split(":"))

    sched = build_scheduler(
        daily_signal_fn=lambda: daily_signal_and_place(okx, db, strategy, account, order_mgr, config, logger),
        daily_report_fn=lambda: daily_report(db, account, config, logger),
        daily_cancel_fn=lambda: daily_cancel(order_mgr, logger),
        reconcile_fn=_reconcile_tick,
        reconcile_interval_seconds=20,
        signal_hour=sig_h, signal_minute=sig_m,
        report_hour=rep_h, report_minute=rep_m,
    )
    sched.start()

    # 启动立刻对账一次：如果重启前有未闭合 trade，尽快回填
    _reconcile_tick()

    startup_catchup_if_needed(okx, db, strategy, account, order_mgr, config, logger)

    logger.info("[ready] HighLow Bot 系统就绪，等待 00:00 UTC 触发首次信号")

    stop_evt = {"stop": False}

    def _shutdown(signum, frame):
        if stop_evt["stop"]:
            return
        stop_evt["stop"] = True
        logger.info(f"[shutdown] signal {signum} received, stopping...")
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

    logger.info("[shutdown] bye")


if __name__ == "__main__":
    main()
