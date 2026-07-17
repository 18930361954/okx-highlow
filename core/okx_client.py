import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any

import requests


OKX_BASE_URL = "https://www.okx.com"


class OKXError(Exception):
    def __init__(self, msg: str, code: str = ""):
        super().__init__(msg)
        self.code = code


class OKXClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str,
        env: str = "demo",
        logger=None,
        timeout: int = 15,
        proxy_url: str | None = None,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.env = env
        self.logger = logger
        self.timeout = timeout
        self._session = requests.Session()
        if proxy_url:
            self._session.proxies = {"http": proxy_url, "https": proxy_url}
            if logger:
                logger.info(f"[net] OKX REST via proxy {proxy_url}")

    # ---------------- signing & request ----------------

    def _ts(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
               f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        message = f"{ts}{method}{path}{body}"
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, ts: str, sign: str) -> dict:
        h = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.env == "demo":
            h["x-simulated-trading"] = "1"
        return h

    # 可安全退避重试的 OKX 业务错误码：
    #   51290 Trading bot engine currently upgrading —— OKX 明确要求重试，纯读端接口重试无副作用；
    #         下单端配合 algoClOrdId 幂等键也安全
    # 注意：51149 曾在这里，但观测到 OKX 侧其实已用 algoClOrdId 幂等键建单，只是响应超时；
    # 底层再 retry 反而把幂等键“用掉”后返回 code=1 空响应，同时留下 2~3 张 pending 需 reconcile 撤。
    # 现在 51149 直接抛给上层 order_manager，由 place_algo_orders 的 clOrdId 回查兜底把 algoId 取回。
    _RETRYABLE_OKX_CODES = {"51290"}

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        body: dict | None = None,
        max_retries: int = 3,
    ) -> dict:
        method = method.upper()
        if params:
            from urllib.parse import urlencode
            query = "?" + urlencode(params)
        else:
            query = ""
        path = endpoint + query
        body_str = json.dumps(body) if body else ""
        url = OKX_BASE_URL + path

        last_err: Exception | None = None
        for attempt in range(max_retries):
            ts = self._ts()
            sign = self._sign(ts, method, path, body_str)
            headers = self._headers(ts, sign)
            try:
                resp = self._session.request(
                    method, url, headers=headers, data=body_str or None,
                    timeout=self.timeout,
                )
                data = resp.json()
                if str(data.get("code", "")) != "0":
                    code = str(data.get("code", ""))
                    top_msg = data.get("msg") or ""
                    # code=1 (batch endpoint 部分失败) 真错因在 data[0].sMsg,顶层 msg 是空。
                    # 例: order-algo posMode 错、余额不足等. 展开子错误好排查。
                    sub_msg = ""
                    rows = data.get("data") or []
                    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                        s_code = rows[0].get("sCode") or ""
                        s_msg = rows[0].get("sMsg") or ""
                        if s_code or s_msg:
                            sub_msg = f" sCode={s_code} sMsg={s_msg}"
                    msg = f"OKX error code={code} msg={top_msg}{sub_msg} endpoint={endpoint}"
                    if self.logger:
                        # 59669：set_leverage 被 pending trigger 单卡住,是上层已知不阻断状态,不打 WARNING
                        # 让 main.py 的 except 分支决定日志级别
                        if code != "59669":
                            self.logger.warning(msg)
                    if code in self._RETRYABLE_OKX_CODES and attempt < max_retries - 1:
                        last_err = OKXError(msg, code=code)
                        wait = 2 ** attempt
                        if self.logger:
                            self.logger.warning(
                                f"OKX retryable code={code} (attempt {attempt+1}); retry in {wait}s"
                            )
                        time.sleep(wait)
                        continue
                    raise OKXError(msg, code=code)
                return data
            except (requests.RequestException, ValueError) as e:
                last_err = e
                wait = 2 ** attempt
                if self.logger:
                    self.logger.warning(f"OKX request failed (attempt {attempt+1}): {e}; retry in {wait}s")
                time.sleep(wait)
            except OKXError:
                raise
        raise OKXError(f"OKX request failed after {max_retries} retries: {last_err}")

    # ---------------- public market ----------------

    def get_candles(self, instId: str, bar: str = "1H", limit: int = 24,
                    after: str | None = None, before: str | None = None) -> list[list[str]]:
        params: dict[str, Any] = {"instId": instId, "bar": bar, "limit": str(limit)}
        if after:
            params["after"] = str(after)
        if before:
            params["before"] = str(before)
        data = self._request("GET", "/api/v5/market/candles", params=params)
        return data.get("data", [])

    def get_history_candles(self, instId: str, bar: str = "1H", limit: int = 100,
                            after: str | None = None, before: str | None = None) -> list[list[str]]:
        params: dict[str, Any] = {"instId": instId, "bar": bar, "limit": str(limit)}
        if after:
            params["after"] = str(after)
        if before:
            params["before"] = str(before)
        data = self._request("GET", "/api/v5/market/history-candles", params=params)
        return data.get("data", [])

    def get_instruments(self, instType: str = "SWAP",
                        instId: str | None = None) -> list[dict]:
        """拉合约元信息:ctVal(面值)、lotSz(下单最小步长)、minSz(最小张数)、
        tickSz(价格步长)等。用于把张数从整张精度放宽到 lotSz。"""
        params: dict[str, Any] = {"instType": instType}
        if instId:
            params["instId"] = instId
        data = self._request("GET", "/api/v5/public/instruments", params=params)
        return data.get("data", [])

    # ---------------- account ----------------

    def get_balance(self, ccy: str = "USDT") -> float:
        data = self._request("GET", "/api/v5/account/balance", params={"ccy": ccy})
        rows = data.get("data", [])
        if not rows:
            return 0.0
        details = rows[0].get("details", [])
        for d in details:
            if d.get("ccy") == ccy:
                return float(d.get("eq", 0) or 0)
        return float(rows[0].get("totalEq", 0) or 0)

    def get_positions(self, instId: str | None = None) -> list[dict]:
        params = {"instType": "SWAP"}
        if instId:
            params["instId"] = instId
        data = self._request("GET", "/api/v5/account/positions", params=params)
        return data.get("data", [])

    def set_leverage(self, instId: str, lever: int, mgnMode: str = "isolated",
                     posSide: str | None = None) -> dict:
        body: dict[str, Any] = {"instId": instId, "lever": str(lever), "mgnMode": mgnMode}
        if posSide:
            body["posSide"] = posSide
        return self._request("POST", "/api/v5/account/set-leverage", body=body)

    # ---------------- algo orders ----------------

    def place_algo_order(
        self,
        instId: str,
        tdMode: str,
        side: str,
        ordType: str,
        sz: str,
        triggerPx: str | None = None,
        orderPx: str | None = None,
        triggerPxType: str = "last",
        posSide: str | None = None,
        tpTriggerPx: str | None = None,
        tpOrdPx: str | None = None,
        slTriggerPx: str | None = None,
        slOrdPx: str | None = None,
        reduceOnly: bool = False,
        ccy: str = "USDT",
        algoClOrdId: str | None = None,
    ) -> dict:
        """
        ordType:
          - 'trigger': 触发后下普通限价/市价单。triggerPx 必填；orderPx=-1 是市价、具体价是限价。
          - 'conditional' / 'oco': 单向 / OCO 止盈止损。
        TP/SL（trigger 单）：必须用 `attachAlgoOrds` 数组挂载到主单上。
        顶层的 tpTriggerPx/slTriggerPx 在 trigger 单上会被服务端静默丢弃 —— 不要用！
        本方法对外接口保留 tpTriggerPx 等参数，内部自动转成 attachAlgoOrds。
        """
        body: dict[str, Any] = {
            "instId": instId,
            "tdMode": tdMode,
            "side": side,
            "ordType": ordType,
            "sz": sz,
            "ccy": ccy,
        }
        if algoClOrdId:
            # OKX 幂等键：同 algoClOrdId 服务端只建一次单，防 HTTP 层重发导致的重复挂单
            body["algoClOrdId"] = algoClOrdId
        if triggerPx is not None:
            body["triggerPx"] = triggerPx
            body["triggerPxType"] = triggerPxType
        if orderPx is not None:
            body["orderPx"] = orderPx
        if posSide:
            body["posSide"] = posSide

        # TP/SL → attachAlgoOrds（trigger 单的正确挂载方式）
        if tpTriggerPx is not None or slTriggerPx is not None:
            attach: dict[str, Any] = {}
            if tpTriggerPx is not None:
                attach["tpTriggerPx"] = tpTriggerPx
                attach["tpOrdPx"] = tpOrdPx if tpOrdPx is not None else "-1"
                attach["tpTriggerPxType"] = triggerPxType
            if slTriggerPx is not None:
                attach["slTriggerPx"] = slTriggerPx
                attach["slOrdPx"] = slOrdPx if slOrdPx is not None else "-1"
                attach["slTriggerPxType"] = triggerPxType
            body["attachAlgoOrds"] = [attach]

        if reduceOnly:
            body["reduceOnly"] = True
        return self._request("POST", "/api/v5/trade/order-algo", body=body)

    def cancel_algo_order(self, algoId: str, instId: str) -> dict:
        body = [{"algoId": algoId, "instId": instId}]
        return self._request("POST", "/api/v5/trade/cancel-algos", body=body)

    def list_pending_orders(self, instType: str = "SWAP",
                            instId: str | None = None) -> list[dict]:
        """未成交的「普通」订单(非 algo)。
        algo trigger 触发后落地的入场限价单若未立刻成交,就挂在这里,
        且带 algoId 字段可反查回主 algo。daily_cancel 只撤未触发 trigger algo,
        这类「已触发未成交」的残单不在 trigger pending 里 → 需单独撤(SOL 事故根因)。"""
        params: dict[str, Any] = {"instType": instType}
        if instId:
            params["instId"] = instId
        data = self._request("GET", "/api/v5/trade/orders-pending", params=params)
        return data.get("data", [])

    def cancel_order(self, instId: str, ordId: str) -> dict:
        """撤单条普通订单(非 algo)。用于撤 algo 触发后落地却没成交的残留限价单。"""
        body = {"instId": instId, "ordId": ordId}
        return self._request("POST", "/api/v5/trade/cancel-order", body=body)

    def list_pending_algos(self, instType: str = "SWAP",
                           instId: str | None = None,
                           ordType: str = "trigger") -> list[dict]:
        params: dict[str, Any] = {"instType": instType, "ordType": ordType}
        if instId:
            params["instId"] = instId
        data = self._request("GET", "/api/v5/trade/orders-algo-pending", params=params)
        return data.get("data", [])

    def list_algo_history(self, instType: str = "SWAP",
                          instId: str | None = None,
                          ordType: str = "trigger",
                          state: str | None = None,
                          limit: int = 100) -> list[dict]:
        """已终态的 algo 单。state: effective(已触发) / canceled / order_failed。
        state 为 None 时按 ordType 拉全部，由调用方自行过滤。"""
        params: dict[str, Any] = {"instType": instType, "ordType": ordType,
                                   "limit": str(limit)}
        if instId:
            params["instId"] = instId
        if state:
            params["state"] = state
        data = self._request("GET", "/api/v5/trade/orders-algo-history", params=params)
        return data.get("data", [])

    def get_algo_order(self, algoClOrdId: str | None = None,
                       algoId: str | None = None) -> dict | None:
        """按 algoClOrdId 或 algoId 查单条 algo 单,任意状态(live/effective/canceled/order_failed)。
        找不到时(空 data 或 OKX 51000/51603 not-found)返回 None,其它错误抛出。
        用于 51149 timeout 后按幂等键回查:pending 索引可能延迟,但单据接口能拿到。"""
        params: dict[str, Any] = {}
        if algoClOrdId:
            params["algoClOrdId"] = algoClOrdId
        if algoId:
            params["algoId"] = algoId
        if not params:
            raise ValueError("get_algo_order requires algoClOrdId or algoId")
        try:
            data = self._request("GET", "/api/v5/trade/order-algo", params=params)
        except OKXError as e:
            # 51000: parameter error (含 not-found); 51603: order not exist
            if e.code in ("51000", "51603"):
                return None
            raise
        rows = data.get("data", [])
        return rows[0] if rows else None

    def list_order_history(self, instType: str = "SWAP",
                           instId: str | None = None,
                           state: str = "filled",
                           limit: int = 100) -> list[dict]:
        """已终态的普通订单（algo 触发后落地的都是普通订单）。
        通过 algoId 字段能反查回主 algo。fillPx / fillTime / pnl / fee 都在。"""
        params: dict[str, Any] = {"instType": instType, "state": state,
                                   "limit": str(limit)}
        if instId:
            params["instId"] = instId
        data = self._request("GET", "/api/v5/trade/orders-history", params=params)
        return data.get("data", [])

    def list_positions_history(self, instType: str = "SWAP",
                                instId: str | None = None,
                                limit: int = 100) -> list[dict]:
        """已平仓的 position 历史。字段 realizedPnl 就是 OKX 界面显示的
        "已实现收益"（净口径,已扣手续费+资金费）。
        字段还有 pnl(=realizedPnl-fee-fundingFee 反向,基本等价)、fee(已扣手续费,负)、
        fundingFee(资金费,负)、closeAvgPx、uTime(平仓时间 ms)、posSide 等。"""
        params: dict[str, Any] = {"instType": instType, "limit": str(limit)}
        if instId:
            params["instId"] = instId
        data = self._request("GET", "/api/v5/account/positions-history", params=params)
        return data.get("data", [])

    # ---------------- health ----------------

    def test_connection(self) -> bool:
        try:
            self._request("GET", "/api/v5/public/time")
            return True
        except Exception as e:
            if self.logger:
                self.logger.error(f"OKX connection test failed: {e}")
            return False
