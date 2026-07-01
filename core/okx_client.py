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
    pass


class OKXClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str,
        env: str = "demo",
        logger=None,
        timeout: int = 15,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.env = env
        self.logger = logger
        self.timeout = timeout
        self._session = requests.Session()

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
                    msg = f"OKX error code={data.get('code')} msg={data.get('msg')} endpoint={endpoint}"
                    if self.logger:
                        self.logger.warning(msg)
                    raise OKXError(msg)
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

    # ---------------- health ----------------

    def test_connection(self) -> bool:
        try:
            self._request("GET", "/api/v5/public/time")
            return True
        except Exception as e:
            if self.logger:
                self.logger.error(f"OKX connection test failed: {e}")
            return False
