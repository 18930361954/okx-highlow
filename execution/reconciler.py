"""对账器：REST 轮询 OKX，把未闭合 db.trade 的 entry/exit 状态回填，
并触发 account_state.on_trade_filled 完成余额/连亏/熔断结算。

设计：幂等。db 里 exit_price is None 才处理；重复轮询无副作用。
匹配 key：db.trades.okx_order_id 存的是主 algo 单的 algoId；OKX orders-history
的每条普通订单都带 algoId 字段（触发后落地的入场单 & tp/sl 平仓单都指向同一 algoId）。
"""
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from core.buckets import bucket_id, previous_bucket_start
from core.okx_client import OKXError
from data.db import DEFAULT_ACCOUNT

UTC = timezone.utc


# 秒数,与 core.scheduler.SIGNAL_BAR_HOURS 保持一致
_BUCKET_SECS = {
    "1D": 86400, "12H": 43200, "6H": 21600, "4H": 14400,
    "2H": 7200, "1H": 3600,
}

# 新挂 pending 的宽限窗口 (毫秒)。cTime > now - 此值 → 不判"孤儿"。
# 场景:order_manager.place_algo_order 返回 algoId 后, insert_trade 完成前有 ~100ms 窗口,
# reconciler 20s 一轮撞上就会把新单当孤儿撤 (2026-07-12 事故根因)。
_FRESH_PENDING_GRACE_MS = 30_000


def _c_time_ms(o: dict) -> int:
    try:
        return int(o.get("cTime") or 0)
    except (TypeError, ValueError):
        return 0


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
                 account_name: str = DEFAULT_ACCOUNT,
                 advanced: dict | None = None):
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
        adv = advanced or {}
        self._grace_ms = int(adv.get("fresh_pending_grace_ms", _FRESH_PENDING_GRACE_MS))
        self._rearm_cooldown_ms = int(adv.get("rearm_cooldown_sec", 300)) * 1000
        # 本轮 run_once 中是否遇到过网络异常。tick 层据此做熔断退避,防止 DNS/断网时刷屏。
        # 每轮 run_once 开头重置。
        self.last_run_had_net_error: bool = False
        # trade_id → 上次重挂保护的时间戳 ms (防 pending 索引延迟导致重复挂)
        self._rearm_at: dict[int, int] = {}

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

    def _check_network_recovery_and_heal(self) -> None:
        """P2优化: 网络故障恢复后主动触发 mini_heal 补齐漂移数据。
        检测逻辑: 前次有网络错误 + 本轮成功 → 判定为恢复,触发对账。"""
        if not hasattr(self, '_prev_had_net_error'):
            self._prev_had_net_error = False

        # 本轮开始前保存上一轮状态
        had_error_before = self._prev_had_net_error

        # 检测恢复: 前次错误 + 本轮成功(目前尚未执行任何网络请求,默认成功)
        if had_error_before and not self.last_run_had_net_error:
            if self.logger:
                self.logger.warning("[network-recovery] 检测到网络恢复,触发 mini_heal 补齐数据")
            try:
                from tools.daily_db_heal import mini_heal
                mini_heal(self.account_name, self.logger)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"[network-recovery] mini_heal 失败: {e}")

        # 更新状态供下一轮使用
        self._prev_had_net_error = self.last_run_had_net_error

    def _sync_balance_after_exit(self) -> None:
        """平仓结算后把本地余额对齐 OKX 真值,吸收充值/提现等本地感知不到的资金变动。
        用 cashBal(现金余额,不含未实现盈亏),有持仓也能安全同步 ——
        旧逻辑用 eq 且有持仓时跳过,导致某账户长期持仓时余额一直不更新(2026-07-20 反馈)。
        拉取失败沿用本地累加值。
        """
        try:
            okx_bal = float(self.okx.get_cash_balance("USDT"))
        except Exception as e:
            self._mark_if_net_error(e)
            if self.logger:
                self.logger.warning(f"[balance-sync] 拉取 OKX 余额失败,沿用本地值: {e}")
            return
        if okx_bal <= 0:
            return
        local = self.account.get_balance()
        if abs(okx_bal - local) < 0.01:
            return
        self.account.set_balance(okx_bal)
        if self.logger:
            self.logger.info(
                f"[balance-sync] 本地 {local:.2f} → OKX {okx_bal:.2f} USDT "
                f"(差 {okx_bal - local:+.2f}, 含充值/提现等外部变动)"
            )

    def run_once(self) -> int:
        """跑一轮对账。返回本轮结算的 trade 数（含 entry 回填与 exit 结算）。"""
        self.last_run_had_net_error = False
        self._last_pending_algo_ids = None  # 每轮清空,避免 cleanup 异常时用到旧值

        # P2优化: 网络恢复检测
        self._check_network_recovery_and_heal()

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
            self._sweep_zombie_open()
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
                # fade 两腿都入场(OCO 撤单失败)时同 pair 会有一多一空两个平仓单,
                # 必须按 posSide 方向绑对腿, 否则平仓单绑错腿(pnl 记到反方向)。
                t_side = str(t.get("side") or "").lower()  # long / short
                for cand in orders_by_pair.get(pair, []):
                    ord_id = cand.get("ordId") or cand.get("algoId") or ""
                    if ord_id in matched_ord_ids:
                        continue
                    if str(cand.get("reduceOnly", "")).lower() != "true":
                        continue
                    # posSide 校验: 平多单 posSide=long, 平空单 posSide=short。
                    # OKX 有返 posSide 才校验(net 模式可能为空则跳过校验,保持旧行为)。
                    cand_pos = str(cand.get("posSide") or "").lower()
                    if cand_pos and t_side and cand_pos != t_side:
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
                    # fade OCO 撤对向腿不在这里做 —— 统一由 run_once 尾部的
                    # _sweep_fade_oco 幂等扫描处理(本轮立即生效, 失败下轮自动重试)。
                    # 在这里撤会有竞态: 对向腿排在本循环后面时 entry_time 尚未回填,
                    # 若它其实也成交了会被误撤成 CANCELLED → 活仓裸奔。
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
                        # 全部字段直接 OKX positions-history 取, 不本地反推:
                        pnl_net = _num(pos_row, "realizedPnl")     # 净盈亏 (已扣所有成本)
                        pnl_gross = _num(pos_row, "pnl")           # 名义盈亏 (OKX 界面显示的"未扣费"口径)
                        fee_raw = _num(pos_row, "fee")             # OKX 总是负 (成本)
                        funding_raw = _num(pos_row, "fundingFee")  # 有正负: 负=付, 正=收
                        fee_abs = abs(fee_raw)                     # 手续费展示恒正 (成本)
                        funding_signed = funding_raw               # 资金费保留符号
                        src = "positions-history"
                    else:
                        # orders fallback: OKX orders 接口不返 fundingFee,置 0;
                        # 名义 = orders 累加 pnl (与 OKX 界面"每笔 order 的 pnl"口径一致)
                        pnl_gross = sum(_num(o, "pnl") for o in related)
                        fee_raw_sum = sum(_num(o, "fee") for o in related)  # 负值
                        fee_abs = abs(fee_raw_sum)
                        funding_signed = 0.0
                        pnl_net = pnl_gross + fee_raw_sum  # 只能本地反推 (无 positions-history)
                        src = f"orders×{len(related)}(fallback,funding=0)"

                    # db 存 4 个字段全部来自 OKX (positions-history 走优先, orders 走 fallback):
                    #   pnl        = realizedPnl (净, 权威)
                    #   pnl_gross  = pnl (名义, 权威, 与 OKX 界面一致)
                    #   fee        = |fee| (成本)
                    #   funding    = fundingFee (带符号)
                    self.db.update_trade_exit(
                        trade_id=t["id"],
                        exit_price=fill_px,
                        exit_reason=reason,
                        pnl=pnl_net,
                        exit_time=fill_time,
                        fee=fee_abs,
                        funding=funding_signed,
                        pnl_gross=pnl_gross,
                    )
                    if self.logger:
                        self.logger.info(
                            f"[reconcile] exit filled: trade#{t['id']} {t['pair']} "
                            f"{reason} @ {fill_px} 名义={pnl_gross:+.4f} "
                            f"手续费={fee_abs:.4f} 资金费={funding_signed:+.4f} "
                            f"净={pnl_net:+.4f} src={src}"
                        )

                    # 结算账户状态:余额按净 pnl 更新(与 OKX 服务端实际扣减一致)
                    exit_dt = None
                    try:
                        exit_dt = datetime.fromisoformat(fill_time) if fill_time else None
                    except ValueError:
                        exit_dt = None
                    self.account.on_trade_filled(pnl=pnl_net, exit_time=exit_dt)
                    self._sync_balance_after_exit()
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

        # fade OCO: 主 entry/exit 匹配之后统一撤"一腿已成交"组的未成交对向腿。
        # 必须在主循环之后 —— 循环中途 entry_time 未落库会误判对向腿未成交而错撤。
        self._sweep_fade_oco()

        # 活仓保护兜底: TP/SL OCO 触发后落地限价单未成交 → 撤残单重挂 OCO
        # (2026-07-30 ETH V 反事故: TP 触发未成交, OCO 一次性消耗, SL 裸奔 10h)。
        self._sweep_unprotected_positions()

        # 尾部僵尸兜底: 主匹配跑完后,algoId 仍死、bucket 已过、未 entry filled 的 → ORPHAN
        self._sweep_zombie_open()
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
        # reentry 是"SL 后同向重挂": 只数同 pair + 同方向 + 未被 OCO 撤销的腿。
        # fade 会在同桶产生一多一空两行,若把对向腿也计入 already 会翻倍误触上限、抑制重挂;
        # CANCELLED(被 OCO 撤的对向腿)同理不算一次真实入场。
        sl_side = str(sl_trade.get("side") or "").lower()
        same_day = [x for x in self.db.list_trades_by_date(sig_date, account=self.account_name)
                    if x.get("pair") == pair
                    and str(x.get("side") or "").lower() == sl_side
                    and str(x.get("exit_reason") or "").upper() != "CANCELLED"]
        already = len(same_day)
        if already >= len(reentry_floats):
            if self.logger:
                self.logger.info(
                    f"[reentry] {pair} 跳过：当日已入场 {already} 次，达到 reentry_floats 上限"
                )
            return

        # 确保还在"挂单桶"内。挂单桶 = signal 桶后一桶。
        now = (sl_time or datetime.now(UTC)).astimezone(UTC)
        signal_bar = self._signal_bar_for(pair)
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
        - 信号未过期（距离桶起始不超过桶长的 50%）
        """
        if not pair or not self.strategy or not self.order_manager:
            return
        now = (exit_dt or datetime.now(UTC)).astimezone(UTC)
        signal_bar = self._signal_bar_for(pair)
        # 上一桶 (即 signal 依据的那一桶) 起始时间 → 用它作 sig_id
        prev = previous_bucket_start(now, signal_bar)
        sig_id = bucket_id(prev)

        # === 过期检查：距离桶起始超过桶长 50% 不补挂 ===
        bucket_secs = _BUCKET_SECS.get(signal_bar, 3600)
        elapsed_secs = (now - prev).total_seconds()
        if elapsed_secs > bucket_secs * 0.5:
            if self.logger:
                self.logger.info(
                    f"[catchup-exit] {pair} 信号已过期 "
                    f"(elapsed={elapsed_secs:.0f}s > {bucket_secs*0.5:.0f}s), 不补挂"
                )
            return

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
        max_ct = getattr(self.strategy, "max_contracts_for", lambda p: None)(pair)

        # fade 返回 {'legs':[...]} 双腿;trend/reversal 单腿。与 main.bucket_signal_and_place 一致。
        legs = signal.get("legs") or [signal]
        for leg in legs:
            if self.logger:
                self.logger.info(
                    f"[catchup-exit] {pair} 平仓后补挂 attempt=1 dir={leg['direction']} "
                    f"entry={leg['entry_price']} tp={leg['tp_price']} sl={leg['sl_price']} "
                    f"margin={margin:.2f} ({mode}) lev={lev}x"
                )
            algo_id = self.order_manager.place_algo_orders(
                leg, margin=margin, leverage=lev, max_contracts=max_ct,
                leg_group=leg.get("leg_group"))
            if not algo_id and self.logger:
                self.logger.error(f"[catchup-exit] {pair} {leg['direction']} place_algo_orders 未拿到 algoId")

    def _signal_bar_for(self, pair: str | None) -> str:
        """per-pair 信号周期(混周期支持)。strategy 有 signal_bar_for 就用 pair 级,
        否则回退到账户级 signal_bar,再回退 1D。"""
        strat = getattr(self, "strategy", None)
        if strat is None:
            return "1D"
        fn = getattr(strat, "signal_bar_for", None)
        if callable(fn) and pair:
            try:
                return fn(pair)
            except Exception:
                pass
        return getattr(strat, "signal_bar", "1D")

    def _is_past_bucket(self, sig_id: str, pair: str | None = None) -> bool:
        """sig_id 对应桶的"挂单窗口"(sig_bucket + bucket_secs)已完全过完 → True。
        用于孤儿改绑失败时判断是否要把 db trade 标 ORPHAN 平掉,防止脏数据长期挂着。
        pair 给定时按该 pair 的周期算(混周期账户不同 pair 桶长不同)。"""
        sig_dt = _parse_sig_id(sig_id)
        if sig_dt is None:
            return False
        signal_bar = self._signal_bar_for(pair)
        bucket_secs = _BUCKET_SECS.get(signal_bar, 86400)
        # 挂单窗口 = signal 桶后一桶结束时刻
        window_end = sig_dt + timedelta(seconds=bucket_secs * 2)
        return datetime.now(UTC) >= window_end

    def _cancel_residual_order(self, db_t: dict) -> None:
        """标 ORPHAN 前, 撤掉该 algo 触发后落地却没成交的普通限价残单。

        场景(2026-07-16 SOL 事故): algo 触发 → 落地限价入场单没成交 → algo 已不在
        trigger pending, daily_cancel 撤不到; db 标 ORPHAN 后那张限价单仍活在盘口,
        价格回来就会以过期信号开仓。这里按 algoId 反查 orders-pending 精确撤掉。
        失败仅告警(daily_cancel 的残单兜底扫描还会再兜一次)。
        """
        pair = db_t.get("pair")
        algo_id = str(db_t.get("okx_order_id") or "")
        if not pair or not algo_id:
            return
        try:
            for o in self.okx.list_pending_orders(instId=pair):
                if str(o.get("algoId") or "") != algo_id:
                    continue
                # 平仓单(reduceOnly)不撤: 活仓的 TP/SL 落地单, 撤了仓位裸奔
                if str(o.get("reduceOnly", "")).lower() == "true":
                    continue
                ord_id = o.get("ordId")
                if not ord_id:
                    continue
                self.okx.cancel_order(pair, ord_id)
                if self.logger:
                    self.logger.warning(
                        f"[reconcile] trade#{db_t.get('id')} {pair} 标 ORPHAN 前撤掉"
                        f"已触发未成交残单 ordId={ord_id} algoId={algo_id}"
                    )
        except Exception as e:
            self._mark_if_net_error(e)
            if self.logger:
                self.logger.warning(
                    f"[reconcile] trade#{db_t.get('id')} {pair} 撤残单失败"
                    f"(daily_cancel 会再兜底): {e}"
                )

    def _expire_as_orphan(self, db_t: dict) -> None:
        """孤儿改绑失败且信号桶已过 → 把 db trade 标 exit_reason=ORPHAN 平掉。
        pnl=0 fee=0 不影响余额/连亏统计,只是把 open 状态收干净。
        先撤掉可能残留的已触发未成交限价单, 防孤儿单继续挂着以过期信号成交。
        标记前回查 algo 终态:
          - effective = trigger 已触发。入场单若真实成交是活仓(2026-07-28 trade#537
            误标事故: 触发 38 分钟后被标 ORPHAN,16.19U 盈利丢失),回填 entry
            交还正常对账流,绝不标 ORPHAN;
          - order_failed = trigger 触发后下单被拒(典型: 保证金不足) = 漏单,
            验收硬性第三关的统计落点,必须与普通孤儿(canceled 等)区分。"""
        if db_t.get("entry_time"):
            # 已入场 = 活仓或等待平仓匹配, 由正常对账流处理, 绝不 ORPHAN
            if self.logger:
                self.logger.warning(
                    f"[reconcile] trade#{db_t.get('id')} {db_t.get('pair')} "
                    f"entry_time={db_t.get('entry_time')} 已入场, 拒绝标 ORPHAN"
                )
            return

        okx_state = ""
        od = None
        try:
            od = self.okx.get_algo_order(algoId=db_t.get("okx_order_id"))
        except Exception as e:
            # 回查失败不能贸然标 ORPHAN: 若实为已触发活仓, 误标即丢 pnl (trade#537 教训)。
            # 本轮跳过, trade 保持 open, 下轮重试。
            self._mark_if_net_error(e)
            if self.logger:
                self.logger.warning(
                    f"[reconcile] trade#{db_t.get('id')} {db_t.get('pair')} "
                    f"标 ORPHAN 前回查 algo 终态失败, 本轮跳过下轮重试: {e}"
                )
            return
        if od:
            okx_state = str(od.get("state") or "")

        if okx_state in ("effective", "partially_effective") \
                and self._rescue_triggered_entry(db_t):
            return

        self._cancel_residual_order(db_t)
        if okx_state == "order_failed" and od:
            fail_code = od.get("failCode") or od.get("code") or "?"
            if self.logger:
                self.logger.error(
                    f"[reconcile] [漏单] trade#{db_t.get('id')} {db_t.get('pair')} "
                    f"trigger 触发失败(下单被拒) failCode={fail_code} "
                    f"algoId={db_t.get('okx_order_id')} "
                    f"signal_date={db_t.get('signal_date')} —— 核对当时保证金占用"
                )
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
                    f"{f' (OKX 终态={okx_state})' if okx_state else ''}"
                )
        except Exception as e:
            if self.logger:
                self.logger.error(
                    f"[reconcile] _expire_as_orphan trade#{db_t.get('id')} failed: {e}"
                )

    def _rescue_triggered_entry(self, db_t: dict) -> bool:
        """algo state=effective(已触发)时回查入场单是否真实成交。
        成交 → 回填 entry_time/entry_price 交还正常对账流(平仓由主匹配结算),返回 True;
        回查失败 → 返回 True(保守: 本轮不标 ORPHAN,下轮重试);
        确认未成交(触发后落地的限价单没吃到) → 返回 False,走原 ORPHAN 清理。"""
        pair = db_t.get("pair")
        algo_id = str(db_t.get("okx_order_id") or "")
        if not pair or not algo_id:
            return False
        try:
            rows = self.okx.list_order_history(instId=pair, state="filled", limit=100)
        except Exception as e:
            self._mark_if_net_error(e)
            if self.logger:
                self.logger.warning(
                    f"[reconcile] trade#{db_t.get('id')} {pair} algo=effective "
                    f"但回查入场成交失败,本轮不标 ORPHAN,下轮重试: {e}"
                )
            return True
        entry = None
        for o in rows:
            if str(o.get("algoId") or "") == algo_id and not _is_reduce_only(o):
                entry = o
                break
        if entry is None:
            return False
        fill_time = _ms_to_iso(entry.get("fillTime") or entry.get("uTime"))
        try:
            px = float(entry.get("fillPx") or entry.get("avgPx") or 0) or None
        except (TypeError, ValueError):
            px = None
        try:
            self.db.update_trade_entry(db_t["id"], entry_time=fill_time, entry_price=px)
        except Exception as e:
            if self.logger:
                self.logger.error(
                    f"[reconcile] trade#{db_t.get('id')} rescue update_trade_entry "
                    f"failed(本轮不标 ORPHAN): {e}"
                )
            return True
        if self.logger:
            self.logger.warning(
                f"[reconcile] trade#{db_t.get('id')} {pair} algo=effective 已触发成交 "
                f"@ {px or db_t.get('entry_price')} time={fill_time} → 回填 entry "
                f"交还对账流,取消 ORPHAN 标记"
            )
        return True

    def _sweep_fade_oco(self) -> None:
        """fade OCO 幂等扫描(run_once 尾部, 主 entry/exit 匹配之后):
        对每个 leg_group 的 open 腿分组 —
        - 恰好一腿已入场(entry_time 有值) → 撤未入场的对向腿(OKX algo + 残单),
          db 标 CANCELLED。本轮撤失败下轮自动重试(幂等)。
        - 两腿都已入场(20s 竞态窗口内行情双向扫过) → 谁也不撤(撤了就是裸奔活仓),
          ERROR 告警: fade 主动多空双持 = whipsaw 执行版(2026-07-23~26 事故类型),
          各自 TP/SL 结算。
        必须在主循环 entry 回填之后跑 —— 循环中途 db 的 entry_time 还没落, 会把
        实际已成交的对向腿误判成未成交而错撤。"""
        try:
            current_open = self.db.list_open_trades(account=self.account_name)
        except Exception:
            return
        by_group: dict[str, list[dict]] = {}
        for t in current_open:
            lg = t.get("leg_group")
            if lg:
                by_group.setdefault(lg, []).append(t)

        for lg, legs in by_group.items():
            filled = [t for t in legs if t.get("entry_time")]
            unfilled = [t for t in legs if not t.get("entry_time")]
            if len(filled) >= 2:
                if self.logger:
                    ids = ", ".join(f"#{t['id']}({t.get('side')})" for t in filled)
                    self.logger.error(
                        f"[reconcile][fade] leg_group={lg} 两腿都已成交! {ids} "
                        f"同时多空持仓 —— OCO 撤单窗口内行情双向扫过, 各自 TP/SL 结算"
                    )
                continue
            if not unfilled:
                continue  # 没有待撤腿
            if not filled:
                # open 里没有已成交腿 ≠ 对向没成交 —— 对向可能同轮入场+平仓已闭合
                # (TP 秒达)。查全表(含已闭合)兜底, 否则未成交腿漏撤继续裸挂过期信号。
                any_filled = self.db.get_any_filled_sibling(
                    self.account_name, lg, int(unfilled[0]["id"]))
                if not any_filled:
                    continue  # 两腿都没成交, 挂着等
                filled = [any_filled]
            for sib in unfilled:
                self._cancel_fade_leg(filled[0], sib)

    def _cancel_fade_leg(self, filled_trade: dict, sib: dict) -> None:
        """撤 fade 的未成交对向腿: OKX trigger algo + 已触发未成交残单, db 标 CANCELLED。
        pnl=0 fee=0 不影响余额/连亏统计。仿 _expire_as_orphan 收干净。"""
        self._cancel_residual_order(sib)
        aid = str(sib.get("okx_order_id") or "")
        pair = sib.get("pair")
        if aid and pair:
            try:
                self.okx.cancel_algo_order(aid, pair)
            except Exception as e:
                self._mark_if_net_error(e)
                if self.logger:
                    self.logger.warning(
                        f"[reconcile][fade] 撤 sibling algo {aid} 失败(下轮兜底再撤): {e}")
        try:
            self.db.update_trade_exit(
                trade_id=sib["id"],
                exit_price=0.0,
                exit_reason="CANCELLED",
                pnl=0.0,
                exit_time=datetime.now(UTC).isoformat(),
                fee=0.0,
            )
            if self.logger:
                self.logger.info(
                    f"[reconcile][fade] leg_group={sib.get('leg_group')} "
                    f"trade#{filled_trade['id']}({filled_trade.get('side')}) 成交 → "
                    f"撤对向腿 trade#{sib['id']}({sib.get('side')}) algoId={aid} 标 CANCELLED"
                )
        except Exception as e:
            if self.logger:
                self.logger.error(
                    f"[reconcile][fade] _cancel_fade_leg trade#{sib.get('id')} failed: {e}")

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

        # 全局 pending algoId 集合 (供末尾"僵尸 open"兜底扫描用)
        all_pending_algo_ids = {o.get("algoId") for o in all_pending if o.get("algoId")}
        # 新挂宽限阈值:cTime 早于此值才被当"过期孤儿"处理
        fresh_threshold_ms = int(datetime.now(UTC).timestamp() * 1000) - self._grace_ms

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

            # 排除刚挂 <30s 的 pending:order_manager place → insert_trade 有 ~100ms 窗口,
            # 期间 reconciler 会把新单当孤儿撤 (2026-07-12 事故根因)。
            if orphans:
                orphans = [o for o in orphans if _c_time_ms(o) < fresh_threshold_ms]

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
                    if self._is_past_bucket(expected_sig, pair):
                        self._expire_as_orphan(db_t)

            # 步骤 3: 剩余未被消化的 survivor 全撤(真正的重复/外部单)
            for o in survivors:
                if o.get("algoId") not in used_orphan_ids:
                    self._cancel_pending(pair, o)

        # 记录本轮 pending algoId 集合供 run_once 尾部 _sweep_zombie_open 使用
        self._last_pending_algo_ids = all_pending_algo_ids

    def _sweep_unprotected_positions(self) -> None:
        """活仓保护兜底: 已入场未平仓 trade, 若 OKX 上既无 pending TP/SL OCO,
        也无活的落地平仓限价单可成交保护 → 说明 OCO 触发过但限价没吃到(V 反),
        撤掉残单并按原 TP/SL 参数重挂独立 OCO。

        2026-07-30 事故: ETH short TP 触发, 限价 1910.55 未成交, OCO 一次性消耗,
        SL 1983.53 随之作废, 活仓无保护裸奔 10 小时。
        2026-08-06 事故: BTC short SL 触发未成交, 现价已越过原 SL, 重挂被 OKX
        51278 拒绝, 每轮重试死循环 12 分钟无保护 → 价格越过触发价时改市价平仓。

        P2增强: 每轮检查持仓与 db 的一致性, 发现"db open 但 OKX 无持仓"的孤儿单。

        判定链(全部来自 OKX 实时状态, 不依赖本地推断):
          1. db open trade 且 entry_time 有值(已入场)
          2. OKX 确认该 pair+posSide 真有持仓(pos != 0)
          3. 主 algo get_algo_order → attachAlgoOrds 取原 TP/SL 参数
          4. pending oco/conditional 里没有该 posSide 的 reduceOnly 保护单
          5. 落地的平仓限价残单(reduceOnly, 带 OCO algoId)一并撤掉再重挂
        重挂用 place_oco_order(sz=当前持仓量), 5 分钟冷却防 pending 索引延迟重复挂。
        重挂被 51277-51280 拒(现价已越过 TP/SL 触发价, 原价永远挂不回去) →
        把越线一侧收敛到现价 ±0.2% 重挂(触发即市价): 保护立刻恢复、亏损锁在
        现价附近, 又保留 V 反弹回 TP 的机会; 收敛仍失败才市价平仓兜底。
        """
        try:
            open_trades = self.db.list_open_trades(account=self.account_name)
        except Exception:
            return
        entered = [t for t in open_trades if t.get("entry_time")]
        if not entered:
            return

        # P2增强: 按 pair 查询实际持仓,检测 db open 但 OKX 无持仓的孤儿单
        pairs_to_check = {t.get("pair") for t in entered if t.get("pair")}
        okx_positions: dict[tuple[str, str], float] = {}  # (pair, side) -> pos
        for pair in pairs_to_check:
            try:
                pos_rows = self.okx.get_positions(instId=pair)
                for p in pos_rows:
                    side = str(p.get("posSide") or "").lower()
                    pos_val = float(p.get("pos") or 0)
                    if side in ("long", "short"):
                        okx_positions[(pair, side)] = pos_val
            except Exception as e:
                self._mark_if_net_error(e)
                if self.logger:
                    self.logger.warning(f"[reconcile] _sweep_unprotected get_positions({pair}) failed: {e}")

        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        for t in entered:
            tid = t.get("id")
            pair = t.get("pair")
            side = str(t.get("side") or "").lower()  # long/short = posSide
            algo_id = str(t.get("okx_order_id") or "")
            if not pair or not algo_id or side not in ("long", "short"):
                continue

            # P2增强: 持仓校验 - db open 但 OKX 无持仓 → 孤儿单,触发 mini_heal
            okx_pos = okx_positions.get((pair, side), 0)
            if okx_pos == 0:
                if self.logger:
                    self.logger.error(
                        f"[reconcile] [orphan-position] trade#{tid} {pair} {side} "
                        f"db 显示 open 但 OKX 无持仓 → 触发 mini_heal 补齐历史数据"
                    )
                try:
                    from tools.daily_db_heal import mini_heal
                    mini_heal(self.account_name, self.logger)
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"[reconcile] mini_heal 失败: {e}")
                continue  # 本轮跳过,等 mini_heal 同步后下轮处理

            if now_ms - self._rearm_at.get(tid, 0) < self._rearm_cooldown_ms:
                continue
            try:
                pos_rows = self.okx.get_positions(instId=pair)
                pos = next((p for p in pos_rows
                            if str(p.get("posSide") or "").lower() == side
                            and float(p.get("pos") or 0) != 0), None)
                if pos is None:
                    continue  # 无持仓: 平仓匹配流程会处理

                # 有无 pending 的 OCO/conditional 保护单 (posSide 对齐)
                protected = False
                for typ in ("oco", "conditional"):
                    for o in self.okx.list_pending_algos(instId=pair, ordType=typ):
                        if str(o.get("posSide") or "").lower() == side:
                            protected = True
                            break
                    if protected:
                        break
                if protected:
                    continue

                # 主 algo 的 attachAlgoOrds = 原始 TP/SL 参数 (权威源)
                od = self.okx.get_algo_order(algoId=algo_id)
                attach = (od or {}).get("attachAlgoOrds") or []
                a = attach[0] if attach and isinstance(attach[0], dict) else {}
                tp_trig = str(a.get("tpTriggerPx") or "")
                sl_trig = str(a.get("slTriggerPx") or "")
                if not tp_trig and not sl_trig:
                    continue  # 原单就没带 TP/SL, 不属于本兜底范围

                # 撤掉 OCO 触发后落地未成交的平仓残单 (reduceOnly), 释放冻结仓位
                for o in self.okx.list_pending_orders(instId=pair):
                    if str(o.get("posSide") or "").lower() != side:
                        continue
                    if str(o.get("reduceOnly", "")).lower() != "true":
                        continue
                    ord_id = o.get("ordId")
                    if ord_id:
                        self.okx.cancel_order(pair, ord_id)
                        if self.logger:
                            self.logger.warning(
                                f"[reconcile] [protect] trade#{tid} {pair} 撤触发未成交"
                                f"平仓残单 ordId={ord_id} (重挂完整 OCO 前清场)"
                            )

                close_side = "sell" if side == "long" else "buy"
                sz = str(pos.get("pos") or "")
                mgn_mode = str(pos.get("mgnMode") or "cross")
                tp_px = str(a.get("tpOrdPx") or "") or None
                sl_px = str(a.get("slOrdPx") or "") or None

                # 现价已越过原触发价时原价挂不回去(OKX 51277-51280): 把越线一侧
                # 收敛到现价 ±0.2% 触发即市价 —— 保护立即恢复, 逆行 0.2% 内止出,
                # V 反弹回来则仓位保住。2026-08-06 BTC 事故: 原 SL 被穿越, 原价
                # 重挂每 20s 被 51278 拒, 裸奔 12 分钟靠价格回落才恢复。
                last = float(pos.get("last") or pos.get("markPx") or 0)
                converged = []
                if last > 0:
                    up, down = round(last * 1.002, 6), round(last * 0.998, 6)
                    if tp_trig:
                        v = float(tp_trig)
                        if (v <= last) if side == "long" else (v >= last):
                            tp_trig, tp_px = str(up if side == "long" else down), "-1"
                            converged.append(f"tp→{tp_trig}")
                    if sl_trig:
                        v = float(sl_trig)
                        if (v >= last) if side == "long" else (v <= last):
                            sl_trig, sl_px = str(down if side == "long" else up), "-1"
                            converged.append(f"sl→{sl_trig}")
                try:
                    self.okx.place_oco_order(
                        instId=pair,
                        tdMode=mgn_mode,
                        side=close_side,
                        sz=sz,
                        posSide=side,
                        tpTriggerPx=tp_trig or None,
                        tpOrdPx=tp_px,
                        slTriggerPx=sl_trig or None,
                        slOrdPx=sl_px,
                    )
                except OKXError as e:
                    if not any(f"sCode={c}" in str(e)
                               for c in ("51277", "51278", "51279", "51280")):
                        raise
                    # 收敛后仍越线被拒(极速行情竞态) → 市价平仓兜底, 绝不留裸仓
                    self.okx.close_position(pair, mgn_mode, posSide=side)
                    self._rearm_at[tid] = now_ms
                    if self.logger:
                        self.logger.error(
                            f"[reconcile] [protect] trade#{tid} {pair} {side} "
                            f"重挂 OCO 触发价仍越线被拒({e}) → 已市价平仓, "
                            f"等对账回填盈亏"
                        )
                    continue
                self._rearm_at[tid] = now_ms
                if self.logger:
                    extra = f" (越线收敛: {', '.join(converged)})" if converged else ""
                    self.logger.error(
                        f"[reconcile] [protect] trade#{tid} {pair} {side} 活仓无 TP/SL "
                        f"保护(OCO 触发未成交后消耗) → 已重挂 OCO tp={tp_trig} sl={sl_trig} "
                        f"sz={sz}{extra}"
                    )
            except Exception as e:
                self._mark_if_net_error(e)
                if self.logger:
                    self.logger.warning(
                        f"[reconcile] [protect] trade#{tid} {pair} 保护检查失败"
                        f"(下轮重试): {e}"
                    )

    def _sweep_zombie_open(self) -> None:
        """兜底扫 db.open trade: algoId 不在 OKX pending 且信号桶已过 → 标 ORPHAN。

        覆盖场景:
          - daily_cancel 撤了 OKX 单但 db 未同步 (老代码路径遗留)
          - 同 pair 后续无新 pending → _cleanup_duplicate_pending 的 pair-loop 无法触发兜底

        必须在 run_once 主 entry/exit 匹配之后才能跑,否则会误伤刚 entry filled 但 db
        还没回填 entry_time 的 open trade。
        """
        pending_ids = getattr(self, "_last_pending_algo_ids", None)
        if pending_ids is None:
            return  # cleanup 因异常提前 return 了, 本轮跳过
        try:
            current_open = self.db.list_open_trades(account=self.account_name)
        except Exception:
            return
        for t in current_open:
            aid = t.get("okx_order_id")
            if not aid or aid in pending_ids:
                continue
            if t.get("entry_time"):
                continue  # 已入场是活持仓, 由 exit 匹配流程处理
            sig = t.get("signal_date") or ""
            if not self._is_past_bucket(sig, t.get("pair")):
                continue
            self._expire_as_orphan(t)

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
