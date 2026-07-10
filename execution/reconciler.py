"""对账器：REST 轮询 OKX，把未闭合 db.trade 的 entry/exit 状态回填，
并触发 account_state.on_trade_filled 完成余额/连亏/熔断结算。

设计：幂等。db 里 exit_price is None 才处理；重复轮询无副作用。
匹配 key：db.trades.okx_order_id 存的是主 algo 单的 algoId；OKX orders-history
的每条普通订单都带 algoId 字段（触发后落地的入场单 & tp/sl 平仓单都指向同一 algoId）。
"""
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from core.okx_client import OKXError
from data.db import DEFAULT_ACCOUNT

UTC = timezone.utc


# 秒数,与 core.scheduler.SIGNAL_BAR_HOURS 保持一致
_BUCKET_SECS = {
    "1D": 86400, "12H": 43200, "6H": 21600, "4H": 14400,
    "2H": 7200, "1H": 3600,
}


def _parse_sig_id(sig_id: str) -> datetime | None:
    """把 db.signal_date(可能是 '2026-07-08' 或 '2026-07-08T04:00Z')解析成桶起始 UTC。"""
    if not sig_id:
        return None
    try:
        s = sig_id.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _cl_ord_id_prefix(coin: str, sig_id: str) -> str:
    """与 order_manager.py:138 的 clOrdId 生成保持一致(不含 dir+attempt 后缀)。
    孤儿改绑的硬校验依据:orphan 的 algoClOrdId 必须以此前缀开头才认。"""
    sd = "".join(ch for ch in sig_id if ch.isalnum())[:12]
    return f"hl{coin}{sd}"


def _dedup_orphans_by_cl_ord_id(orphans: list[dict], logger,
                                 cancel_fn) -> list[dict]:
    """相同 algoClOrdId 的孤儿只保留 cTime 最早的一张,其余立即撤单。
    OKX 幂等键异常场景:同 clOrdId 本应只建 1 张,若出现多张就是真重复,必撤。
    cancel_fn(pair, order_dict) 由调用方注入。返回 survivor 列表。"""
    def _c(o: dict) -> int:
        try:
            return int(o.get("cTime") or 0)
        except (TypeError, ValueError):
            return 0

    by_cl: dict[str, list[dict]] = {}
    no_cl: list[dict] = []
    for o in orphans:
        cl = o.get("algoClOrdId") or ""
        if cl:
            by_cl.setdefault(cl, []).append(o)
        else:
            no_cl.append(o)

    survivors: list[dict] = []
    for cl, group in by_cl.items():
        group.sort(key=_c)
        if len(group) > 1 and logger:
            for extra in group[1:]:
                logger.error(
                    f"[reconcile] DUPLICATE clOrdId={cl} on OKX "
                    f"→ cancel extra algoId={extra.get('algoId')} "
                    f"cTime={extra.get('cTime')}"
                )
        survivors.append(group[0])
        for extra in group[1:]:
            cancel_fn(extra.get("instId"), extra)
    survivors.extend(no_cl)
    return survivors


def _ms_to_iso(ms: Any) -> str:
    try:
        ts = int(ms)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ts / 1000, tz=UTC).isoformat()


def _is_reduce_only(o: dict) -> bool:
    """判断一条订单是不是"平仓/减仓"性质。OKX 字段：reduceOnly='true'/'false',
    或 category='full_liquidation'/'partial_liquidation'/'adl' 等。
    保守：reduceOnly='true' 才算 exit；其它都当 entry。"""
    v = str(o.get("reduceOnly", "")).lower()
    return v == "true"


def _classify_orders(orders: list[dict]) -> tuple[dict | None, dict | None]:
    """把一组同 algoId 的 filled 订单分成 (entry, exit)。
    - reduceOnly=true 的是 exit
    - 剩下的按 fillTime 最早的是 entry
    - 找不到 exit 就返回 (entry, None)
    """
    if not orders:
        return None, None
    exits = [o for o in orders if _is_reduce_only(o)]
    entries = [o for o in orders if not _is_reduce_only(o)]

    def _t(o: dict) -> int:
        try:
            return int(o.get("fillTime") or o.get("uTime") or o.get("cTime") or 0)
        except (TypeError, ValueError):
            return 0

    entry = min(entries, key=_t) if entries else None
    exit_ = max(exits, key=_t) if exits else None
    return entry, exit_


def _infer_exit_reason(exit_order: dict) -> str:
    """从 exit 订单字段推断是 TP 还是 SL。
    OKX orders-history 里通常有 category / execType 等字段；不同版本略有差异，
    这里尽量用多种字段兜底，最后落到 'EXIT'。"""
    for key in ("category", "algoOrdType", "execType"):
        v = str(exit_order.get(key, "")).lower()
        if "tp" in v or "take" in v:
            return "TP"
        if "sl" in v or "stop" in v:
            return "SL"
    return "EXIT"


def _infer_exit_reason_by_price(side: str, entry_price: float, exit_price: float,
                                 tp_pct: float, sl_pct: float) -> str:
    """字段兜底：按平仓价距 tp/sl 目标价的差距分类。
    long: tp = entry*(1+tp_pct), sl = entry*(1-sl_pct)
    short: tp = entry*(1-tp_pct), sl = entry*(1+sl_pct)
    取离 exit_price 最近的那个当 reason。差距完全一致时按盈亏方向定。"""
    if entry_price <= 0 or exit_price <= 0:
        return "EXIT"
    if side == "long":
        tp_target = entry_price * (1 + tp_pct)
        sl_target = entry_price * (1 - sl_pct)
    elif side == "short":
        tp_target = entry_price * (1 - tp_pct)
        sl_target = entry_price * (1 + sl_pct)
    else:
        return "EXIT"
    d_tp = abs(exit_price - tp_target)
    d_sl = abs(exit_price - sl_target)
    if d_tp < d_sl:
        return "TP"
    if d_sl < d_tp:
        return "SL"
    # 完全相等：按盈亏方向兜底
    if side == "long":
        return "TP" if exit_price >= entry_price else "SL"
    return "TP" if exit_price <= entry_price else "SL"


class Reconciler:
    def __init__(self, okx_client, db, account_state, config: dict, logger=None,
                 strategy=None, order_manager=None,
                 account_name: str = DEFAULT_ACCOUNT):
        self.okx = okx_client
        self.db = db
        self.account = account_state
        self.config = config
        self.logger = logger
        self.strategy = strategy         # 用于日内重挂计算入场价
        self.order_manager = order_manager  # 用于挂重挂单
        self.account_name = account_name    # 多账户下限定 db 查询范围
        self.pairs: list[str] = list(config["strategy"]["pairs"])
        # 全局默认杠杆（兼容旧代码）；实际用 account.leverage_for(pair) 拿 per-pair
        self.leverage = int(config["strategy"]["leverage"])
        # 本轮 run_once 中是否遇到过网络异常。tick 层据此做熔断退避,防止 DNS/断网时刷屏。
        # 每轮 run_once 开头重置。
        self.last_run_had_net_error: bool = False

    @staticmethod
    def _match_position_history(rows: list[dict], side: str, close_px: float,
                                  close_time_iso: str) -> dict | None:
        """在 positions-history 里找与当前 exit 对应的那条仓位。
        匹配键: posSide + closeAvgPx≈fill_px(相对 5bp) + uTime≈fill_time(±5min)。
        posSide 用 db.side("long"/"short") 直接对齐; 都命不中返回 None。
        """
        if not rows or close_px <= 0:
            return None
        try:
            close_ms = int(datetime.fromisoformat(close_time_iso).timestamp() * 1000)
        except (ValueError, TypeError):
            close_ms = 0

        best: tuple[int, dict] | None = None  # (score_lower_better, row)
        for r in rows:
            if str(r.get("posSide", "")).lower() != side.lower():
                continue
            try:
                cap = float(r.get("closeAvgPx") or 0)
            except (TypeError, ValueError):
                cap = 0
            if cap <= 0:
                continue
            # 5bp 容差; 触发价与实际成交常有 1~2bp 滑点
            if abs(cap - close_px) / close_px > 5e-4:
                continue
            try:
                u_ms = int(r.get("uTime") or 0)
            except (TypeError, ValueError):
                u_ms = 0
            if close_ms and u_ms and abs(u_ms - close_ms) > 5 * 60 * 1000:
                continue
            # 越接近的越优
            score = abs(u_ms - close_ms) if close_ms and u_ms else 0
            if best is None or score < best[0]:
                best = (score, r)
        return best[1] if best else None

    def _mark_if_net_error(self, exc: BaseException) -> None:
        """OKX 业务错误不算网络问题;requests 层的连接/DNS/超时算。tick 层据此熔断退避。"""
        if isinstance(exc, OKXError):
            return
        if isinstance(exc, requests.RequestException):
            self.last_run_had_net_error = True

    def run_once(self) -> int:
        """跑一轮对账。返回本轮结算的 trade 数（含 entry 回填与 exit 结算）。"""
        self.last_run_had_net_error = False
        try:
            open_trades = self.db.list_open_trades(account=self.account_name)
        except Exception as e:
            if self.logger:
                self.logger.error(f"[reconcile] list_open_trades failed: {e}")
            return 0

        # 每轮先做一次"同 pair 多张 pending 清理"（异常状态自愈）。
        # 即使 open_trades 为空也要跑，因为可能存在孤儿单需要撤。
        self._cleanup_duplicate_pending(open_trades)

        if not open_trades:
            return 0

        # 按 pair 分组拉 orders-history。同时构建两份索引：
        # 1) by_algo: 主 algo 直接下的入场订单（其 algoId == 主 algo 的 algoId）
        # 2) by_pair: 该 pair 全部已成交订单（按 fillTime 升序），用于 TP/SL 平仓匹配
        #    因为 OKX 的 TP/SL attach 触发后会生成独立的 algoId，跟主 algo 无关联字段。
        # 3) pos_hist_by_pair: 该 pair 的历史仓位（含 realizedPnl 净口径），
        #    用于把 db.pnl 对齐到 OKX 界面显示。
        pairs = {t["pair"] for t in open_trades if t.get("pair")}
        orders_by_algo: dict[str, list[dict]] = {}
        orders_by_pair: dict[str, list[dict]] = {}
        pos_hist_by_pair: dict[str, list[dict]] = {}
        for pair in pairs:
            try:
                rows = self.okx.list_order_history(instId=pair, state="filled", limit=100)
            except Exception as e:
                self._mark_if_net_error(e)
                if self.logger:
                    self.logger.warning(f"[reconcile] list_order_history({pair}) failed: {e}")
                continue
            for o in rows:
                aid = o.get("algoId") or ""
                if aid:
                    orders_by_algo.setdefault(aid, []).append(o)
            # 按 fillTime 升序，方便按时间窗口匹配
            def _ft(o: dict) -> int:
                try:
                    return int(o.get("fillTime") or o.get("uTime") or 0)
                except (TypeError, ValueError):
                    return 0
            orders_by_pair[pair] = sorted(rows, key=_ft)
            # 拉一次仓位历史（净收益权威源）。失败不阻塞对账,回退到 orders 累加口径。
            try:
                pos_hist_by_pair[pair] = self.okx.list_positions_history(
                    instId=pair, limit=100
                )
            except Exception as e:
                self._mark_if_net_error(e)
                if self.logger:
                    self.logger.warning(
                        f"[reconcile] list_positions_history({pair}) failed: {e}"
                    )
                pos_hist_by_pair[pair] = []

        # 已被匹配过的 order（避免同一平仓订单匹配到多个 open trade）
        matched_ord_ids: set[str] = set()

        processed = 0
        for t in open_trades:
            algo_id = t.get("okx_order_id")
            if not algo_id:
                # 挂单时未拿到 algoId → 没法关联；跳过。（下次挂单流程已加回查兜底）
                continue

            # Step 1: 主 algoId 直接匹配（entry 单大部分能命中）
            orders = list(orders_by_algo.get(algo_id, []))
            entry, exit_ = _classify_orders(orders)

            # Step 2: TP/SL 触发的平仓单 algoId 是独立的 → 按 pair+时间窗口兜底
            # 触发条件：db 已知 entry_time，且从 orders_by_algo 里没找到 reduceOnly=true 的平仓订单
            if exit_ is None and t.get("entry_time"):
                pair = t["pair"]
                try:
                    entry_dt = datetime.fromisoformat(t["entry_time"])
                    entry_ms = int(entry_dt.timestamp() * 1000)
                except (ValueError, TypeError):
                    entry_ms = 0
                # 找同 pair、fillTime > entry_time、reduceOnly=true、未被其它 trade 匹配的最早一条
                for cand in orders_by_pair.get(pair, []):
                    ord_id = cand.get("ordId") or cand.get("algoId") or ""
                    if ord_id in matched_ord_ids:
                        continue
                    if str(cand.get("reduceOnly", "")).lower() != "true":
                        continue
                    try:
                        ft = int(cand.get("fillTime") or cand.get("uTime") or 0)
                    except (TypeError, ValueError):
                        ft = 0
                    if ft <= entry_ms:
                        continue
                    exit_ = cand
                    matched_ord_ids.add(ord_id)
                    if self.logger:
                        self.logger.info(
                            f"[reconcile] {pair} trade#{t['id']} 通过时间窗口匹配到平仓订单 "
                            f"algoId={cand.get('algoId')} fillPx={cand.get('fillPx')}"
                        )
                    break

            entry_source = entry or exit_  # exit 存在但 entry 分类失败时兜底用 exit 时间
            if entry_source and not t.get("entry_time"):
                try:
                    fill_time = _ms_to_iso(
                        entry_source.get("fillTime") or entry_source.get("uTime")
                    )
                    # 若能取到 entry 分类的 fillPx 就更新，否则保留 db 里原 entry_price
                    entry_px_arg: float | None = None
                    if entry:
                        try:
                            entry_px_arg = float(
                                entry.get("fillPx") or entry.get("avgPx") or 0
                            ) or None
                        except (TypeError, ValueError):
                            entry_px_arg = None
                    self.db.update_trade_entry(t["id"], entry_time=fill_time,
                                                entry_price=entry_px_arg)
                    if self.logger:
                        self.logger.info(
                            f"[reconcile] entry filled: trade#{t['id']} {t['pair']} "
                            f"@ {entry_px_arg or t['entry_price']} time={fill_time}"
                        )
                    processed += 1
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"[reconcile] update_trade_entry failed: {e}")

            if exit_:
                try:
                    fill_time = _ms_to_iso(exit_.get("fillTime") or exit_.get("uTime"))
                    fill_px = float(exit_.get("fillPx") or exit_.get("avgPx") or 0)
                    if fill_px <= 0:
                        continue
                    reason = _infer_exit_reason(exit_)
                    # 字段兜底失败落到 "EXIT" 时：按 pair 级 tp/sl_pct + 平仓价距离分类。
                    # 关键：SL 分类正确才能触发 _try_reentry。
                    if reason == "EXIT" and self.strategy is not None:
                        entry_px = float(t.get("entry_price") or 0)
                        try:
                            tp_pct, sl_pct = self.strategy.tp_sl_for(t.get("pair", ""))
                            reason = _infer_exit_reason_by_price(
                                side=t.get("side", ""),
                                entry_price=entry_px,
                                exit_price=fill_px,
                                tp_pct=tp_pct,
                                sl_pct=sl_pct,
                            )
                        except Exception:
                            pass

                    # ============================================================
                    # PnL / Fee 全部从 OKX 拿真值,不本地估算
                    # ============================================================
                    # 首选: positions-history.realizedPnl (OKX 界面显示的净收益,
                    #   已扣手续费 + 资金费,与 UI 完全一致)。
                    # 兜底: 累加 orders 的 pnl - |fee| (若 positions-history 未返回或
                    #   匹配不上,例如老仓位、API 限流)。
                    def _num(d: dict, k: str) -> float:
                        v = d.get(k)
                        if v in (None, ""):
                            return 0.0
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            return 0.0

                    related: list[dict] = list(orders_by_algo.get(algo_id, []))
                    if exit_ and exit_ not in related:
                        related.append(exit_)

                    # ---- 首选口径:positions-history 匹配 ----
                    pos_row = self._match_position_history(
                        pos_hist_by_pair.get(t["pair"], []),
                        side=t.get("side", ""),
                        close_px=fill_px,
                        close_time_iso=fill_time,
                    )
                    if pos_row is not None:
                        pnl_net = _num(pos_row, "realizedPnl")
                        fee_raw = _num(pos_row, "fee")           # 负值
                        funding_raw = _num(pos_row, "fundingFee")  # 负值
                        fee_total = abs(fee_raw) + abs(funding_raw)
                        pnl_gross = pnl_net + abs(fee_raw) + abs(funding_raw)
                        src = "positions-history"
                    else:
                        pnl_gross = sum(_num(o, "pnl") for o in related)
                        fee_raw_sum = sum(_num(o, "fee") for o in related)  # 负值
                        fee_total = abs(fee_raw_sum)
                        pnl_net = pnl_gross + fee_raw_sum
                        src = f"orders×{len(related)}(fallback)"

                    # db.pnl 存净口径(与 OKX 界面显示的收益一致);fee 仅供展示
                    self.db.update_trade_exit(
                        trade_id=t["id"],
                        exit_price=fill_px,
                        exit_reason=reason,
                        pnl=pnl_net,
                        exit_time=fill_time,
                        fee=fee_total,
                    )
                    if self.logger:
                        self.logger.info(
                            f"[reconcile] exit filled: trade#{t['id']} {t['pair']} "
                            f"{reason} @ {fill_px} 名义={pnl_gross:+.4f} 手续费={fee_total:.4f} "
                            f"净={pnl_net:+.4f} src={src}"
                        )

                    # 结算账户状态:余额按净 pnl 更新(与 OKX 服务端实际扣减一致)
                    exit_dt = None
                    try:
                        exit_dt = datetime.fromisoformat(fill_time) if fill_time else None
                    except ValueError:
                        exit_dt = None
                    self.account.on_trade_filled(pnl=pnl_net, exit_time=exit_dt)
                    processed += 1

                    # 日内重挂：只有 SL 平仓 + pair 启用 reentry_floats + attempt<最大 + 当日 UTC 未跨天
                    if reason == "SL":
                        try:
                            self._try_reentry(t, exit_dt)
                        except Exception as e:
                            if self.logger:
                                self.logger.error(
                                    f"[reconcile] reentry after trade#{t.get('id')} failed: {e}"
                                )

                    # 平仓后补挂：daily_signal_and_place 因"当时有持仓"跳过挂单时，
                    # 平仓（无论 TP/SL/EXIT）后应尝试跑一次今日 signal 首挂 attempt=1。
                    # 判定：今日 signal_date 下该 pair 在 db 里无任何记录 → 说明确实被跳过了。
                    try:
                        self._catchup_after_exit(t.get("pair"), exit_dt)
                    except Exception as e:
                        if self.logger:
                            self.logger.error(
                                f"[reconcile] catchup after trade#{t.get('id')} failed: {e}"
                            )
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"[reconcile] settle trade#{t.get('id')} failed: {e}")

        return processed

    def _try_reentry(self, sl_trade: dict, sl_time: datetime | None) -> None:
        """SL 平仓后决定是否日内重挂。前提：
        - strategy 和 order_manager 都已注入
        - pair 在 config 启用 reentry_floats
        - 当日已入场次数 < len(reentry_floats)
        - 当前 UTC 与该 signal_date 对应的"挂单日"是同一天（signal_date + 1）
        - 账户未熔断
        """
        pair = sl_trade.get("pair") or "?"
        if not self.strategy or not self.order_manager:
            if self.logger:
                self.logger.info(f"[reentry] {pair} 跳过：strategy/order_manager 未注入")
            return
        if not sl_trade.get("pair"):
            return
        reentry_floats = self.strategy.reentry_floats_for(pair)
        if len(reentry_floats) < 2:
            if self.logger:
                self.logger.info(f"[reentry] {pair} 跳过：未配置 reentry_floats")
            return

        # 判定当日已入场几次（用 signal_date 分组）
        sig_date = sl_trade.get("signal_date")
        if not sig_date:
            return
        same_day = [x for x in self.db.list_trades_by_date(sig_date, account=self.account_name) if x.get("pair") == pair]
        already = len(same_day)
        if already >= len(reentry_floats):
            if self.logger:
                self.logger.info(
                    f"[reentry] {pair} 跳过：当日已入场 {already} 次，达到 reentry_floats 上限"
                )
            return

        # 确保还在"挂单桶"内。挂单桶 = signal 桶后一桶。
        now = (sl_time or datetime.now(UTC)).astimezone(UTC)
        signal_bar = getattr(self.strategy, "signal_bar", "1D")
        bucket_secs = _BUCKET_SECS.get(signal_bar, 86400)
        sig_dt = _parse_sig_id(sig_date)
        if sig_dt is None:
            return
        trade_bkt_start = sig_dt + timedelta(seconds=bucket_secs)
        trade_bkt_end = trade_bkt_start + timedelta(seconds=bucket_secs)
        if not (trade_bkt_start <= now < trade_bkt_end):
            if self.logger:
                self.logger.info(
                    f"[reconcile] {pair} SL 但已跨挂单桶(now={now} trade_bkt={trade_bkt_start})不重挂"
                )
            return

        # 熔断/可交易检查
        ok, why = self.account.can_trade(now)
        if not ok:
            if self.logger:
                self.logger.info(f"[reconcile] {pair} SL 但账户不可交易({why}),不重挂")
            return

        # 拉当前挂单桶开始至今的细粒度 K,重算入场价
        try:
            # 1D 保持旧行为(1H K);其它周期直接用 signal_bar K
            k_bar = "1H" if signal_bar == "1D" else signal_bar
            k_limit = 24 if signal_bar == "1D" else max(2, int(bucket_secs / 3600))
            raw = self.okx.get_candles(pair, bar=k_bar, limit=k_limit)
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[reconcile] get_candles({pair}) 重挂前失败: {e}")
            return
        if not raw:
            return

        from strategy.high_low import _normalize_candle
        normed = [_normalize_candle(c) for c in raw]
        bkt_start_ms = int(trade_bkt_start.timestamp() * 1000)
        today_bars = [c for c in normed if c["ts"] >= bkt_start_ms]
        if not today_bars:
            if self.logger:
                self.logger.info(f"[reconcile] {pair} 当前桶无 K 线,不重挂")
            return

        direction = sl_trade.get("side")
        attempt = already + 1
        new_sig = self.strategy.compute_reentry_signal(
            pair=pair, direction=direction, day_candles_so_far=today_bars,
            attempt=attempt, signal_date=sig_date,
        )
        if not new_sig:
            return

        # 计算保证金 + 杠杆（都是 pair 级：SOL 50x、BTC/ETH 100x）
        bal = self.account.get_balance()
        margin, mode = self.account.compute_margin(bal, pair=pair)
        lev = self.account.leverage_for(pair)

        if self.logger:
            self.logger.info(
                f"[reentry] {pair} attempt={attempt} SL 后重挂 "
                f"dir={direction} entry={new_sig['entry_price']} "
                f"tp={new_sig['tp_price']} sl={new_sig['sl_price']} margin={margin:.2f}"
            )

        max_ct = getattr(self.strategy, "max_contracts_for", lambda p: None)(pair)
        algo_id = self.order_manager.place_algo_orders(
            new_sig, margin=margin, leverage=lev, attempt=attempt, max_contracts=max_ct
        )
        if not algo_id and self.logger:
            self.logger.error(f"[reentry] {pair} attempt={attempt} 挂单失败")

    def _catchup_after_exit(self, pair: str | None, exit_dt: datetime | None) -> None:
        """平仓后当前信号桶内补挂 attempt=1(对应「有持仓所以 signal 被跳过」的场景)。
        判定:
        - strategy/order_manager 已注入
        - db 里当前信号桶 sig_id 该 pair 无任何 trade
        - OKX 无 pending / 无持仓 该 pair
        - 账户未熔断
        """
        if not pair or not self.strategy or not self.order_manager:
            return
        now = (exit_dt or datetime.now(UTC)).astimezone(UTC)
        signal_bar = getattr(self.strategy, "signal_bar", "1D")
        # 上一桶 (即 signal 依据的那一桶) 起始时间 → 用它作 sig_id
        try:
            from main import previous_bucket_start, bucket_id
            prev = previous_bucket_start(now, signal_bar)
            sig_id = bucket_id(prev)
        except Exception:
            # main 未加载时兜底回退到 1D 语义
            sig_id = (now.date() - timedelta(days=1)).isoformat()

        # 已有当前桶记录 → 不补
        same_bkt = [x for x in self.db.list_trades_by_date(sig_id, account=self.account_name) if x.get("pair") == pair]
        if same_bkt:
            return

        # 账户/熔断
        ok, why = self.account.can_trade(now)
        if not ok:
            if self.logger:
                self.logger.info(f"[catchup-exit] {pair} 账户不可交易({why}),不补挂")
            return

        try:
            for o in self.okx.list_pending_algos(instId=pair, ordType="trigger"):
                if o.get("instId") == pair:
                    if self.logger:
                        self.logger.info(f"[catchup-exit] {pair} 已有 pending,跳过")
                    return
        except Exception as e:
            self._mark_if_net_error(e)
            if self.logger:
                self.logger.warning(f"[catchup-exit] {pair} list_pending 失败: {e}")
            return
        try:
            for p in self.okx.get_positions(instId=pair):
                if float(p.get("pos", 0) or 0) != 0:
                    if self.logger:
                        self.logger.info(f"[catchup-exit] {pair} 仍有持仓,跳过")
                    return
        except Exception as e:
            self._mark_if_net_error(e)
            if self.logger:
                self.logger.warning(f"[catchup-exit] {pair} get_positions 失败: {e}")
            return

        # 按 prev_bkt ts 精挑上一桶 K,防 OKX 桶延迟返回错位到上上一桶
        from utils.time_helper import fetch_prev_bucket_candles, to_ms
        try:
            raw = fetch_prev_bucket_candles(self.okx, pair, signal_bar, prev, self.logger)
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[catchup-exit] {pair} get_candles 失败: {e}")
            return
        if not raw:
            if self.logger:
                self.logger.warning(f"[catchup-exit] {pair} 未拿到 prev-bucket K,不补挂")
            return

        # sanity check:确保挑到的 K 线 ts 与算出的 prev 一致
        expected_ms = to_ms(prev)
        actual_ms = min(int(k[0]) for k in raw) if signal_bar == "1D" else int(raw[0][0])
        if actual_ms != expected_ms:
            if self.logger:
                self.logger.error(
                    f"[catchup-exit] {pair} K 线 ts 不匹配 expected={expected_ms}({sig_id}) "
                    f"actual={actual_ms},不补挂"
                )
            return

        signal = self.strategy.compute_signal(pair, raw, signal_date=sig_id)
        if not signal:
            if self.logger:
                self.logger.info(f"[catchup-exit] {pair} compute_signal 无结果，不补挂")
            return

        bal = self.account.get_balance()
        margin, mode = self.account.compute_margin(bal, pair=pair)
        lev = self.account.leverage_for(pair)

        if self.logger:
            self.logger.info(
                f"[catchup-exit] {pair} 平仓后补挂 attempt=1 "
                f"entry={signal['entry_price']} tp={signal['tp_price']} sl={signal['sl_price']} "
                f"margin={margin:.2f} ({mode}) lev={lev}x"
            )

        max_ct = getattr(self.strategy, "max_contracts_for", lambda p: None)(pair)
        algo_id = self.order_manager.place_algo_orders(signal, margin=margin, leverage=lev,
                                                       max_contracts=max_ct)
        if not algo_id and self.logger:
            self.logger.error(f"[catchup-exit] {pair} place_algo_orders 未拿到 algoId")

    def _is_past_bucket(self, sig_id: str) -> bool:
        """sig_id 对应桶的"挂单窗口"(sig_bucket + bucket_secs)已完全过完 → True。
        用于孤儿改绑失败时判断是否要把 db trade 标 ORPHAN 平掉,防止脏数据长期挂着。"""
        sig_dt = _parse_sig_id(sig_id)
        if sig_dt is None:
            return False
        signal_bar = getattr(getattr(self, "strategy", None), "signal_bar", "1D")
        bucket_secs = _BUCKET_SECS.get(signal_bar, 86400)
        # 挂单窗口 = signal 桶后一桶结束时刻
        window_end = sig_dt + timedelta(seconds=bucket_secs * 2)
        return datetime.now(UTC) >= window_end

    def _expire_as_orphan(self, db_t: dict) -> None:
        """孤儿改绑失败且信号桶已过 → 把 db trade 标 exit_reason=ORPHAN 平掉。
        pnl=0 fee=0 不影响余额/连亏统计,只是把 open 状态收干净。"""
        try:
            self.db.update_trade_exit(
                trade_id=db_t["id"],
                exit_price=0.0,
                exit_reason="ORPHAN",
                pnl=0.0,
                exit_time=datetime.now(UTC).isoformat(),
                fee=0.0,
            )
            if self.logger:
                self.logger.error(
                    f"[reconcile] trade#{db_t.get('id')} {db_t.get('pair')} "
                    f"signal_date={db_t.get('signal_date')} algoId={db_t.get('okx_order_id')} "
                    f"已过桶且 OKX 无安全匹配孤儿 → 标记 ORPHAN 平"
                )
        except Exception as e:
            if self.logger:
                self.logger.error(
                    f"[reconcile] _expire_as_orphan trade#{db_t.get('id')} failed: {e}"
                )

    def _cleanup_duplicate_pending(self, open_trades: list[dict]) -> None:
        """扫每个策略 pair 的 pending algo,撤"重复"/无归属的。
        改绑硬校验:orphan 必须同时满足 posSide 方向 + algoClOrdId 前缀含
        db_trade 的 signal_date 桶,才能"消化"该 db trade。宁可把 db trade
        标 ORPHAN 收干净,也不做方向/桶错配的改绑(2026-07-09 ETH 事故根因:
        short trade 被错绑到 long algo)。
        另:同 algoClOrdId 出现 >1 张 pending → 保留 cTime 最早,其余立即撤(OKX 幂等键异常自愈)。
        """
        try:
            all_pending = self.okx.list_pending_algos(ordType="trigger")
        except Exception as e:
            self._mark_if_net_error(e)
            if self.logger:
                self.logger.warning(f"[reconcile] cleanup: list_pending_algos failed: {e}")
            return

        # 按 pair 分组
        pending_by_pair: dict[str, list[dict]] = {}
        for o in all_pending:
            inst = o.get("instId")
            if inst in self.pairs:
                pending_by_pair.setdefault(inst, []).append(o)

        # 反查 db：pair → 该 pair 目前 open 且带 algoId 的 trades（可能多条 —— 日内重挂）
        open_algos_by_pair: dict[str, dict[str, dict]] = {}
        for t in open_trades:
            pair = t.get("pair")
            aid = t.get("okx_order_id")
            if pair and aid:
                open_algos_by_pair.setdefault(pair, {})[aid] = t

        for pair, orders in pending_by_pair.items():
            coin = pair.split("-")[0]
            db_algo_map = open_algos_by_pair.get(pair, {})
            db_algo_ids = set(db_algo_map.keys())

            # 分类：合法（在 db）/ 孤儿（不在 db）
            legit = [o for o in orders if o.get("algoId") in db_algo_ids]
            orphans = [o for o in orders if o.get("algoId") not in db_algo_ids]

            if not orphans:
                continue

            # 步骤 1: 同 algoClOrdId 去重(OKX 幂等键异常自愈)
            survivors = _dedup_orphans_by_cl_ord_id(
                orphans, self.logger, self._cancel_pending
            )

            if not db_algo_map:
                # db 里啥都没:1 张 survivor → 保留观察;多张(clOrdId 各异) → 保留 cTime 最早、撤其余
                if len(survivors) <= 1:
                    continue
                def _c(o: dict) -> int:
                    try:
                        return int(o.get("cTime") or 0)
                    except (TypeError, ValueError):
                        return 0
                survivors_sorted = sorted(survivors, key=_c)
                for o in survivors_sorted[1:]:
                    self._cancel_pending(pair, o)
                continue

            # 步骤 2: db 有缺失 trade → 用 survivors 硬校验后改绑
            missing_db = [t for aid, t in db_algo_map.items()
                          if aid not in {o.get("algoId") for o in legit}]
            used_orphan_ids: set[str] = set()
            for db_t in missing_db:
                expected_dir = str(db_t.get("side") or "").lower()  # long/short
                expected_sig = db_t.get("signal_date") or ""
                expected_prefix = _cl_ord_id_prefix(coin, expected_sig)
                match = None
                for o in survivors:
                    if o.get("algoId") in used_orphan_ids:
                        continue
                    if str(o.get("posSide") or "").lower() != expected_dir:
                        continue
                    if not str(o.get("algoClOrdId") or "").startswith(expected_prefix):
                        continue
                    match = o
                    break
                if match:
                    try:
                        self.db.update_trade_algo_id(db_t["id"], match["algoId"])
                        used_orphan_ids.add(match["algoId"])
                        if self.logger:
                            self.logger.warning(
                                f"[reconcile] cleanup {pair}: SAFE rebind db trade#{db_t['id']} "
                                f"({expected_dir}, sig={expected_sig}) "
                                f"algoId {db_t.get('okx_order_id')} → {match['algoId']} "
                                f"(clOrdId={match.get('algoClOrdId')})"
                            )
                    except Exception as e:
                        if self.logger:
                            self.logger.error(
                                f"[reconcile] cleanup update algoId failed: {e}"
                            )
                else:
                    if self.logger:
                        self.logger.error(
                            f"[reconcile] cleanup {pair}: trade#{db_t['id']} "
                            f"algoId {db_t.get('okx_order_id')} 已不在 OKX pending, "
                            f"且无匹配孤儿(需 posSide={expected_dir}, clOrdId 前缀 "
                            f"{expected_prefix}) → 不改绑"
                        )
                    if self._is_past_bucket(expected_sig):
                        self._expire_as_orphan(db_t)

            # 步骤 3: 剩余未被消化的 survivor 全撤(真正的重复/外部单)
            for o in survivors:
                if o.get("algoId") not in used_orphan_ids:
                    self._cancel_pending(pair, o)

    def _cancel_pending(self, pair: str, o: dict) -> None:
        aid = o.get("algoId")
        if not aid:
            return
        if self.logger:
            self.logger.warning(
                f"[reconcile] cleanup {pair}: 发现重复 pending algo "
                f"algoId={aid} cTime={o.get('cTime')}，撤单"
            )
        try:
            self.okx.cancel_algo_order(aid, pair)
        except Exception as e:
            if self.logger:
                self.logger.error(f"[reconcile] cleanup cancel {aid} failed: {e}")

    def startup_orphan_scan(self) -> int:
        """启动时全 pair 扫一遍 pending,同 algoClOrdId 出现 >1 张就撤晚的。
        专防挂单 timeout 期间 OKX 幂等键异常留下的历史重复单——不撤会一起触发,
        造成同向持仓翻倍。返回撤单数量。
        """
        try:
            all_pending = self.okx.list_pending_algos(ordType="trigger")
        except Exception as e:
            self._mark_if_net_error(e)
            if self.logger:
                self.logger.warning(f"[startup] orphan_scan list_pending_algos failed: {e}")
            return 0

        # 复用 _dedup 逻辑,但要按 pair 分组分别调用(cancel_fn 需要 pair 参数)
        cancelled_before = self._cancel_count if hasattr(self, "_cancel_count") else 0
        self._cancel_count = cancelled_before

        by_pair: dict[str, list[dict]] = {}
        for o in all_pending:
            inst = o.get("instId")
            if inst:
                by_pair.setdefault(inst, []).append(o)

        cancelled = 0
        for pair, orders in by_pair.items():
            def _cancel_and_count(p: str, ord_dict: dict) -> None:
                nonlocal cancelled
                aid = ord_dict.get("algoId")
                if not aid:
                    return
                try:
                    self.okx.cancel_algo_order(aid, p or pair)
                    cancelled += 1
                except Exception as e:
                    if self.logger:
                        self.logger.error(
                            f"[startup] cancel duplicate {aid} failed: {e}"
                        )
            _dedup_orphans_by_cl_ord_id(orders, self.logger, _cancel_and_count)
        if self.logger and cancelled:
            self.logger.warning(
                f"[startup] orphan_scan cancelled {cancelled} duplicate algo(s)"
            )
        return cancelled
