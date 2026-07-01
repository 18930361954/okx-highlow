from typing import Callable


# 各品种合约面值。OKX SWAP 张数与标的数量换算用。
DEFAULT_CT_VAL = {
    "BTC-USDT-SWAP": 0.01,
    "ETH-USDT-SWAP": 0.1,
}

# 触发价相对触发价的限价小步长（用于 trigger 后的限价委托价）。
# 触发后立刻挂限价，价格放宽 SLIP_PCT 以提高成交概率（但仍是限价）。
SLIP_PCT = 0.0005  # 0.05%


class OrderManager:
    """
    OKX algo 单：
      - 触发后下「限价」单（orderPx 用实际价格，不用 -1）
      - TP/SL 也用「限价」单（tpOrdPx / slOrdPx 用实际价格，不用 -1）
      - 账户模式：cross（全仓）—— isolated 在 demo 上 set_leverage 容易被旧 TP/SL 单卡住
    一次下单 → 触发后自动入场 + 服务端绑 TP/SL，程序断网不影响。
    """

    def __init__(self, okx_client, db, logger=None, ct_val: dict | None = None,
                 td_mode: str = "cross"):
        self.okx = okx_client
        self.db = db
        self.logger = logger
        self.ct_val = {**DEFAULT_CT_VAL, **(ct_val or {})}
        self.td_mode = td_mode
        self._fill_callback: Callable | None = None

    # ---------- helpers ----------

    def _calc_size(self, pair: str, margin_usdt: float, leverage: int,
                   entry_price: float) -> str:
        """
        size 单位：张数（OKX SWAP）
        notional = margin × leverage
        coin_qty = notional / entry_price
        contracts = coin_qty / ct_val
        """
        ct_val = self.ct_val.get(pair, 0.01)
        notional = margin_usdt * leverage
        coin_qty = notional / entry_price
        contracts = max(1, int(coin_qty / ct_val))
        return str(contracts)

    def _ensure_leverage(self, pair: str, leverage: int) -> None:
        """
        cross 模式下 set_leverage 不需要 posSide（一次设好 long+short）。
        失败仅警告：可能是因为该品种已有持仓/挂单，需用户手动到 OKX 调整。
        """
        try:
            self.okx.set_leverage(pair, leverage, mgnMode=self.td_mode)
            if self.logger:
                self.logger.info(f"[lev] {pair} = {leverage}x ({self.td_mode}) ok")
        except Exception as e:
            if self.logger:
                self.logger.warning(
                    f"set_leverage {pair} lev={leverage} mode={self.td_mode} failed: {e}"
                )

    # ---------- algo orders ----------

    def place_algo_orders(
        self,
        signal: dict,
        margin: float,
        leverage: int,
        td_mode: str | None = None,
    ) -> str | None:
        pair = signal["pair"]
        direction = signal["direction"]
        entry_price = float(signal["entry_price"])
        tp_price = float(signal["tp_price"])
        sl_price = float(signal["sl_price"])

        mode = td_mode or self.td_mode
        self._ensure_leverage(pair, leverage)

        side = "buy" if direction == "long" else "sell"
        pos_side = "long" if direction == "long" else "short"

        sz = self._calc_size(pair, margin, leverage, entry_price)

        # 触发后的限价价格：稍微放宽 SLIP_PCT 保证能成交，但仍是限价不是市价。
        if direction == "long":
            order_px = round(entry_price * (1 + SLIP_PCT), 6)
        else:
            order_px = round(entry_price * (1 - SLIP_PCT), 6)

        # 幂等键：pair+signal_date+direction 唯一。OKX 保证同 clOrdId 不重复建单。
        # 只允许字母数字/-/_/.，最长 32 字符。BTC-USDT-SWAP → BTC；ETH-USDT-SWAP → ETH。
        coin = pair.split("-")[0]  # 如 BTC / ETH
        sd = signal.get("signal_date", "").replace("-", "")  # 20260630
        algo_cl_ord_id = f"hl{coin}{sd}{direction[0]}"[:32]  # 如 hlBTC20260630s

        if self.logger:
            self.logger.info(
                f"[order] place algo {pair} dir={direction} entry={entry_price} "
                f"orderPx={order_px} tp={tp_price} sl={sl_price} "
                f"margin={margin:.2f} lev={leverage} sz={sz} mode={mode} "
                f"clOrdId={algo_cl_ord_id}"
            )

        try:
            resp = self.okx.place_algo_order(
                instId=pair,
                tdMode=mode,
                side=side,
                ordType="trigger",
                sz=sz,
                triggerPx=str(entry_price),
                orderPx=str(order_px),            # 限价！不是 -1
                triggerPxType="last",
                posSide=pos_side,
                tpTriggerPx=str(tp_price),
                tpOrdPx=str(tp_price),            # TP 限价
                slTriggerPx=str(sl_price),
                slOrdPx=str(sl_price),            # SL 限价
                algoClOrdId=algo_cl_ord_id,
            )
        except Exception as e:
            if self.logger:
                self.logger.error(f"place_algo_order {pair} failed: {e}")
            return None

        data = resp.get("data", []) if isinstance(resp, dict) else []
        algo_id = data[0].get("algoId") if data else None

        # 边界：resp 结构不带 algoId（比如返回格式异常）。回查 pending 兜底，
        # 有单就把 algoId 取回来，避免 db 缺记录导致下次 catchup 重复挂。
        if not algo_id:
            if self.logger:
                self.logger.warning(
                    f"[order] {pair} place resp 未含 algoId，回查 pending：resp={resp}"
                )
            try:
                for o in self.okx.list_pending_algos(instId=pair, ordType="trigger"):
                    algo_id = o.get("algoId") or algo_id
                    if algo_id:
                        break
            except Exception as e:
                if self.logger:
                    self.logger.error(f"[order] 回查 pending 失败: {e}")

        self.db.insert_trade(
            signal_date=signal.get("signal_date", ""),
            pair=pair,
            side=direction,
            entry_price=entry_price,
            margin=margin,
            mode="FIXED" if margin >= 1000 else "PCT",
            okx_order_id=algo_id,
            entry_time=None,
        )
        return algo_id

    def cancel_all_pending(self, pair: str | None = None) -> int:
        """撤当前所有未触发 algo 单。返回撤掉的数量。"""
        try:
            pending = self.okx.list_pending_algos(instId=pair, ordType="trigger")
        except Exception as e:
            if self.logger:
                self.logger.error(f"list_pending_algos failed: {e}")
            return 0
        cnt = 0
        for o in pending:
            algo_id = o.get("algoId")
            inst = o.get("instId")
            if not algo_id or not inst:
                continue
            try:
                self.okx.cancel_algo_order(algo_id, inst)
                cnt += 1
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"cancel {algo_id} failed: {e}")
        if self.logger:
            self.logger.info(f"[cancel] cancelled {cnt} pending algos (pair={pair or 'ALL'})")
        return cnt

    def on_order_filled(self, callback: Callable) -> None:
        self._fill_callback = callback

    def _notify_filled(self, *args, **kwargs):
        if self._fill_callback:
            self._fill_callback(*args, **kwargs)
