import time
from typing import Callable

from core.okx_client import OKXError
from data.db import DEFAULT_ACCOUNT


# 各品种合约面值。OKX SWAP 张数与标的数量换算用。
DEFAULT_CT_VAL = {
    "BTC-USDT-SWAP": 0.01,
    "ETH-USDT-SWAP": 0.1,
    "SOL-USDT-SWAP": 1.0,
}

# 触发价相对触发价的限价小步长（用于 trigger 后的限价委托价）。
# 触发后立刻挂限价，价格放宽 SLIP_PCT 以提高成交概率（但仍是限价）。
# 0.01% = 保守偏移，绝大部分情况能作为 maker 挂单；极少数快速行情才会跨过成为 taker。
SLIP_PCT = 0.0001  # 0.01%

# 51149/code=1 挂单超时后按 algoClOrdId 回查的退避序列(秒)。
# OKX 侧实际已用幂等键建单成功但响应超时,pending 索引落库需要一小段时间。
# 4 轮共 ~3.2s,覆盖绝大多数落库延迟;仍找不到才算真正失败。
_POLL_BACKOFFS = (0.2, 0.5, 1.0, 1.5)


class OrderManager:
    """
    OKX algo 单：
      - 触发后下「限价」单（orderPx 用实际价格，不用 -1）
      - TP/SL 也用「限价」单（tpOrdPx / slOrdPx 用实际价格，不用 -1）
      - 账户模式：cross（全仓）—— isolated 在 demo 上 set_leverage 容易被旧 TP/SL 单卡住
    一次下单 → 触发后自动入场 + 服务端绑 TP/SL，程序断网不影响。
    """

    def __init__(self, okx_client, db, logger=None, ct_val: dict | None = None,
                 td_mode: str = "cross", account: str = DEFAULT_ACCOUNT):
        self.okx = okx_client
        self.db = db
        self.logger = logger
        self.ct_val = {**DEFAULT_CT_VAL, **(ct_val or {})}
        self.td_mode = td_mode
        self.account = account
        # 缓存 pair → 已确认设置成功的 leverage,避免每次挂单都调 set_leverage。
        # main.py 启动时会预设一次,同 leverage 时 place 就跳过 set。
        self._lev_confirmed: dict[str, int] = {}
        self._fill_callback: Callable | None = None

    def mark_leverage_confirmed(self, pair: str, leverage: int) -> None:
        """外部(如 main.py boot)成功 set 过后调用此方法登记,让后续 place 跳过重复 set。"""
        self._lev_confirmed[pair] = leverage

    # ---------- helpers ----------

    def _calc_size(self, pair: str, margin_usdt: float, leverage: int,
                   entry_price: float, max_contracts: int | None = None) -> str:
        """
        size 单位:张数(OKX SWAP)
        notional = margin × leverage
        coin_qty = notional / entry_price
        contracts = coin_qty / ct_val,再按 max_contracts 截断
        """
        ct_val = self.ct_val.get(pair, 0.01)
        notional = margin_usdt * leverage
        coin_qty = notional / entry_price
        contracts = max(1, int(coin_qty / ct_val))
        if max_contracts and contracts > max_contracts:
            if self.logger:
                self.logger.info(
                    f"[order] {pair} 张数从 {contracts} 封顶到 {max_contracts}"
                )
            contracts = max_contracts
        return str(contracts)

    def _ensure_leverage(self, pair: str, leverage: int) -> None:
        """
        缓存 pair 已确认的 leverage,同值时跳过 API 调用。
        cross 模式下 set_leverage 不需要 posSide(一次设好 long+short)。
        失败仅警告:可能是因为该品种已有持仓/挂单,需用户手动到 OKX 调整。
        """
        if self._lev_confirmed.get(pair) == leverage:
            return  # 已确认过,跳过
        try:
            self.okx.set_leverage(pair, leverage, mgnMode=self.td_mode)
            self._lev_confirmed[pair] = leverage
            if self.logger:
                self.logger.info(f"[lev] {pair} = {leverage}x ({self.td_mode}) ok")
        except OKXError as e:
            # 59669: 已有 pending trigger 单,无法调整杠杆。假设已在目标档位,登记缓存跳过。
            if e.code == "59669":
                self._lev_confirmed[pair] = leverage
                if self.logger:
                    self.logger.info(
                        f"[lev] {pair} = {leverage}x ({self.td_mode}) 已有 pending 单,跳过 set"
                    )
            elif self.logger:
                self.logger.warning(
                    f"set_leverage {pair} lev={leverage} mode={self.td_mode} failed: {e}"
                )
        except Exception as e:
            if self.logger:
                self.logger.warning(
                    f"set_leverage {pair} lev={leverage} mode={self.td_mode} failed: {e}"
                )

    # ---------- algo orders ----------

    def _poll_algo_by_cl_ord_id(self, pair: str, algo_cl_ord_id: str) -> str | None:
        """51149/code=1 timeout 后按幂等键回查 algoId。
        每轮先调 get_algo_order(状态无关),失败再回退 list_pending_algos 扫。
        任一轮拿到 algoId 立即返回;全轮 miss 返回 None。
        _POLL_BACKOFFS 总耗时约 3.2s,阻塞挂单主流程,但比错误 insert None 安全。
        """
        for i, wait in enumerate(_POLL_BACKOFFS):
            time.sleep(wait)
            # 首选:单据接口,任意状态可查
            try:
                row = self.okx.get_algo_order(algoClOrdId=algo_cl_ord_id)
                if row and row.get("algoId"):
                    return row["algoId"]
            except Exception as e:
                if self.logger:
                    self.logger.warning(
                        f"[order] poll get_algo_order attempt {i+1} failed: {e}"
                    )
            # 兜底:pending 列表扫
            try:
                for o in self.okx.list_pending_algos(instId=pair, ordType="trigger"):
                    if o.get("algoClOrdId") == algo_cl_ord_id:
                        aid = o.get("algoId")
                        if aid:
                            return aid
            except Exception:
                pass
        return None

    def place_algo_orders(
        self,
        signal: dict,
        margin: float,
        leverage: int,
        td_mode: str | None = None,
        attempt: int = 1,
        max_contracts: int | None = None,
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

        sz = self._calc_size(pair, margin, leverage, entry_price,
                              max_contracts=max_contracts)

        # 触发后的限价价格：稍微放宽 SLIP_PCT 保证能成交，但仍是限价不是市价。
        if direction == "long":
            order_px = round(entry_price * (1 + SLIP_PCT), 6)
        else:
            order_px = round(entry_price * (1 - SLIP_PCT), 6)

        # 幂等键: pair + signal_id + direction + attempt 唯一。
        # OKX 限制 [A-Za-z0-9_-.]{1,32}。sig_id 可能是 '2026-07-08' 或 '2026-07-08T04:00Z'
        # (bucket id),清理特殊字符 + 截断到 32。
        coin = pair.split("-")[0]  # BTC / ETH / SOL
        sd_raw = str(signal.get("signal_date", ""))
        # 只留数字字母,把日期时间戳压成 YYYYMMDDHH 之类
        sd = "".join(ch for ch in sd_raw if ch.isalnum())[:12]  # 20260708T04 → 20260708T04
        algo_cl_ord_id = f"hl{coin}{sd}{direction[0]}{attempt}"[:32]

        if self.logger:
            self.logger.info(
                f"[order] place algo {pair} dir={direction} entry={entry_price} "
                f"orderPx={order_px} tp={tp_price} sl={sl_price} "
                f"margin={margin:.2f} lev={leverage} sz={sz} mode={mode} "
                f"clOrdId={algo_cl_ord_id}"
            )

        place_err: Exception | None = None
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
            place_err = e
            resp = None
            # 51149 下单超时 / code=1 空响应：OKX 侧用 algoClOrdId 幂等键建单成功但响应异常。
            # 静默不打 log,走下面的 clOrdId 回查兜底;回查成功后 INFO 提示,失败才 ERROR

        data = resp.get("data", []) if isinstance(resp, dict) else []
        algo_id = data[0].get("algoId") if data else None

        # 边界:resp 未含 algoId(返回格式异常 / 请求侧异常,比如 51149 下单超时)。
        # 挂单带了 algoClOrdId 幂等键 → 按幂等键多轮延时回查,覆盖 OKX pending 索引落库延迟。
        # 关键:仍未拿到 algoId 时,直接 return None 且不写 db —— 避免 reconciler 后续把
        # algoId=None 的脏 db trade 误消化到无关孤儿 pending 上(2026-07-09 ETH 事故根因)。
        if not algo_id:
            if self.logger and not place_err:
                self.logger.warning(
                    f"[order] {pair} place resp 未含 algoId,回查 clOrdId: resp={resp}"
                )
            algo_id = self._poll_algo_by_cl_ord_id(pair, algo_cl_ord_id)

            if algo_id and self.logger:
                self.logger.info(
                    f"[order] {pair} 首次超时/空响应,通过 clOrdId 多轮兜底拿到 algoId={algo_id}"
                )
            elif not algo_id:
                if self.logger:
                    err_desc = f": {place_err}" if place_err else ""
                    self.logger.error(
                        f"place_algo_order {pair} failed 且 clOrdId={algo_cl_ord_id} "
                        f"多轮回查未命中,不写 db (避免脏数据){err_desc}"
                    )
                # 不写 db,直接返回。下轮 scheduler/catchup 用同 clOrdId 重试;
                # OKX 幂等键保证同键不会重复建仓。
                return None

        self.db.insert_trade(
            signal_date=signal.get("signal_date", ""),
            pair=pair,
            side=direction,
            entry_price=entry_price,
            margin=margin,
            mode="FIXED" if margin >= 1000 else "PCT",
            okx_order_id=algo_id,
            entry_time=None,
            attempt=attempt,
            account=self.account,
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
