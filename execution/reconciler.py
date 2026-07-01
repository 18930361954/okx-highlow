"""对账器：REST 轮询 OKX，把未闭合 db.trade 的 entry/exit 状态回填，
并触发 account_state.on_trade_filled 完成余额/连亏/熔断结算。

设计：幂等。db 里 exit_price is None 才处理；重复轮询无副作用。
匹配 key：db.trades.okx_order_id 存的是主 algo 单的 algoId；OKX orders-history
的每条普通订单都带 algoId 字段（触发后落地的入场单 & tp/sl 平仓单都指向同一 algoId）。
"""
from datetime import datetime, timezone
from typing import Any

UTC = timezone.utc


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


class Reconciler:
    def __init__(self, okx_client, db, account_state, config: dict, logger=None):
        self.okx = okx_client
        self.db = db
        self.account = account_state
        self.config = config
        self.logger = logger
        self.pairs: list[str] = list(config["strategy"]["pairs"])
        self.leverage = int(config["strategy"]["leverage"])

    def run_once(self) -> int:
        """跑一轮对账。返回本轮结算的 trade 数（含 entry 回填与 exit 结算）。"""
        try:
            open_trades = self.db.list_open_trades()
        except Exception as e:
            if self.logger:
                self.logger.error(f"[reconcile] list_open_trades failed: {e}")
            return 0

        # 每轮先做一次"同 pair 多张 pending 清理"（异常状态自愈）。
        # 即使 open_trades 为空也要跑，因为可能存在孤儿单需要撤。
        self._cleanup_duplicate_pending(open_trades)

        if not open_trades:
            return 0

        # 按 pair 分组拉 orders-history，减少 API 调用（每 pair 一次）
        pairs = {t["pair"] for t in open_trades if t.get("pair")}
        orders_by_algo: dict[str, list[dict]] = {}
        for pair in pairs:
            try:
                rows = self.okx.list_order_history(instId=pair, state="filled", limit=100)
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"[reconcile] list_order_history({pair}) failed: {e}")
                continue
            for o in rows:
                aid = o.get("algoId") or ""
                if aid:
                    orders_by_algo.setdefault(aid, []).append(o)

        processed = 0
        for t in open_trades:
            algo_id = t.get("okx_order_id")
            if not algo_id:
                # 挂单时未拿到 algoId → 没法关联；跳过。（下次挂单流程已加回查兜底）
                continue
            orders = orders_by_algo.get(algo_id, [])
            entry, exit_ = _classify_orders(orders)

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

                    # pnl 优先取 OKX 提供的实际 pnl（含手续费/资金费口径），
                    # 拿不到就按 margin*lev*pct 估算（与回测一致）。
                    pnl_raw = exit_.get("pnl")
                    try:
                        pnl = float(pnl_raw) if pnl_raw not in (None, "") else None
                    except (TypeError, ValueError):
                        pnl = None
                    if pnl is None:
                        entry_px = float(t.get("entry_price") or 0)
                        margin = float(t.get("margin") or 0)
                        if entry_px > 0 and margin > 0:
                            if t.get("side") == "long":
                                pct = (fill_px - entry_px) / entry_px
                            else:
                                pct = (entry_px - fill_px) / entry_px
                            pnl = margin * self.leverage * pct
                        else:
                            pnl = 0.0

                    self.db.update_trade_exit(
                        trade_id=t["id"],
                        exit_price=fill_px,
                        exit_reason=reason,
                        pnl=pnl,
                        exit_time=fill_time,
                    )
                    if self.logger:
                        self.logger.info(
                            f"[reconcile] exit filled: trade#{t['id']} {t['pair']} "
                            f"{reason} @ {fill_px} pnl={pnl:+.4f}"
                        )

                    # 结算账户状态：余额/连亏/熔断
                    exit_dt = None
                    try:
                        exit_dt = datetime.fromisoformat(fill_time) if fill_time else None
                    except ValueError:
                        exit_dt = None
                    self.account.on_trade_filled(pnl=pnl, exit_time=exit_dt)
                    processed += 1
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"[reconcile] settle trade#{t.get('id')} failed: {e}")

        return processed

    def _cleanup_duplicate_pending(self, open_trades: list[dict]) -> None:
        """扫每个策略 pair 的 pending algo：>1 张就撤到只剩 1 张。
        保留优先级：
          1. db 里 open trade 记录的 algoId
          2. 否则 cTime 最早的那张（更可能是真的、后来的重复）
        若 db 的 algoId 在 OKX 找不到，但同 pair 还有 pending → 改绑到保留下来那张。
        """
        try:
            all_pending = self.okx.list_pending_algos(ordType="trigger")
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[reconcile] cleanup: list_pending_algos failed: {e}")
            return

        # 按 pair 分组
        pending_by_pair: dict[str, list[dict]] = {}
        for o in all_pending:
            inst = o.get("instId")
            if inst in self.pairs:
                pending_by_pair.setdefault(inst, []).append(o)

        # 反查 db：pair → 该 pair 目前 open 且带 algoId 的 trade（应至多 1 条）
        open_by_pair: dict[str, dict] = {}
        for t in open_trades:
            pair = t.get("pair")
            if pair and t.get("okx_order_id"):
                open_by_pair[pair] = t  # 若同 pair 多条 open trade，取最后一个即可

        for pair, orders in pending_by_pair.items():
            if len(orders) <= 1:
                # 顺便处理孤儿：db 记录的 algoId 不在 pending 里 → 可能已触发建仓（不管）
                # 或已被撤（reconciler 走对账 exit 分支处理）。这里不额外操作。
                continue

            db_trade = open_by_pair.get(pair)
            db_algo_id = db_trade.get("okx_order_id") if db_trade else None

            # 选保留者
            keep = None
            if db_algo_id:
                keep = next((o for o in orders if o.get("algoId") == db_algo_id), None)
            if keep is None:
                # db 里的 algoId 不在 OKX pending 里，或 db 根本没记录 → 保留 cTime 最早那张
                def _c(o: dict) -> int:
                    try:
                        return int(o.get("cTime") or 0)
                    except (TypeError, ValueError):
                        return 0
                keep = min(orders, key=_c)
                # 若 db 有 open trade 但 algoId 对不上，改绑到保留下来这张，
                # 保证后续 orders-history 对账时 algoId 能匹配上
                if db_trade and db_algo_id != keep.get("algoId"):
                    try:
                        self.db.update_trade_algo_id(db_trade["id"], keep["algoId"])
                        if self.logger:
                            self.logger.warning(
                                f"[reconcile] cleanup {pair}: db trade#{db_trade['id']} "
                                f"algoId {db_algo_id} → {keep['algoId']}（原 algoId 已不在 OKX pending）"
                            )
                    except Exception as e:
                        if self.logger:
                            self.logger.error(f"[reconcile] cleanup update algoId failed: {e}")

            # 撤其余
            for o in orders:
                aid = o.get("algoId")
                if not aid or aid == keep.get("algoId"):
                    continue
                if self.logger:
                    self.logger.warning(
                        f"[reconcile] cleanup {pair}: 发现重复 pending algo "
                        f"algoId={aid} cTime={o.get('cTime')}，撤单（保留 {keep.get('algoId')}）"
                    )
                try:
                    self.okx.cancel_algo_order(aid, pair)
                except Exception as e:
                    if self.logger:
                        self.logger.error(
                            f"[reconcile] cleanup cancel {aid} failed: {e}"
                        )
