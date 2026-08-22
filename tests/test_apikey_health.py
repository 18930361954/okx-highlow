"""API Key 周检: 错误分类 + 全账户覆盖 + 告警触发。"""
import pytest

from core.apikey_health import (
    _classify, check_all_api_keys, notify_if_invalid,
)
from core.okx_client import OKXError


def _cfg(accounts):
    return {"accounts": accounts, "network": {"proxy_enabled": False},
            "system": {}}


def _acct(name, key="k1", enabled=True, env="demo"):
    return {"account_name": name, "api_key": key, "secret_key": "s",
            "passphrase": "p", "enabled": enabled, "env_adapt": env}


# ---------------- 错误分类 ----------------

@pytest.mark.parametrize("code", [
    "50100", "50101", "50102", "50103", "50104", "50105",
    "50111", "50112", "50113", "50114", "50119",
])
def test_key_error_codes_classified_invalid(code):
    status, _ = _classify(OKXError("boom", code))
    assert status == "KEY_INVALID"


def test_key_error_code_embedded_in_message():
    """OKXError 未带 code 属性但消息里有 sCode= 时也要认出来。"""
    status, _ = _classify(OKXError("failed sCode=50112 expired"))
    assert status == "KEY_INVALID"


def test_non_key_okx_error_not_invalid():
    """余额不足之类的业务错误不能误报成 Key 失效。"""
    status, detail = _classify(OKXError("insufficient balance", "51008"))
    assert status == "UNKNOWN"
    assert "非 key 类" in detail


@pytest.mark.parametrize("msg", [
    "HTTPSConnectionPool read timed out",
    "proxy connection failed",
    "SSL handshake error",
    "Connection reset by peer",
    "failed to resolve host",
])
def test_network_errors_not_reported_as_key_invalid(msg):
    """网络抖动不能误报 —— 否则每周假警报, 真失效时反被忽略。"""
    status, _ = _classify(RuntimeError(msg))
    assert status == "NETWORK"


# ---------------- 全账户覆盖 ----------------

def test_checks_disabled_accounts_too(monkeypatch):
    """停用账户的 Key 也会静默过期, 必须一起探活。"""
    seen = []

    class FakeClient:
        def __init__(self, api_key, secret_key, passphrase, env="demo",
                     proxy_url=None, timeout=20, **kw):
            seen.append(api_key)

        def get_balance(self, ccy="USDT"):
            return 100.0

    monkeypatch.setattr("core.apikey_health.OKXClient", FakeClient)
    cfg = _cfg([_acct("启用的", "kA", enabled=True),
                _acct("停用的", "kB", enabled=False)])
    res = check_all_api_keys(config=cfg)
    assert seen == ["kA", "kB"]
    assert [r["status"] for r in res] == ["OK", "OK"]
    assert [r["enabled"] for r in res] == [True, False]


def test_empty_key_reported_no_key(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("api_key 为空时不该建 client")

    monkeypatch.setattr("core.apikey_health.OKXClient", _boom)
    res = check_all_api_keys(config=_cfg([_acct("空key", key="")]))
    assert res[0]["status"] == "NO_KEY"


def test_expired_key_reported_invalid(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def get_balance(self, ccy="USDT"):
            raise OKXError("APIKey expired", "50112")

    monkeypatch.setattr("core.apikey_health.OKXClient", FakeClient)
    res = check_all_api_keys(config=_cfg([_acct("过期的")]))
    assert res[0]["status"] == "KEY_INVALID"


def test_one_bad_key_does_not_stop_others(monkeypatch):
    """一个账户挂了不能中断后面的探活。"""
    class FakeClient:
        def __init__(self, api_key, *a, **kw):
            self.key = api_key

        def get_balance(self, ccy="USDT"):
            if self.key == "bad":
                raise OKXError("APIKey does not exist", "50100")
            return 50.0

    monkeypatch.setattr("core.apikey_health.OKXClient", FakeClient)
    cfg = _cfg([_acct("坏的", "bad"), _acct("好的", "good")])
    res = check_all_api_keys(config=cfg)
    assert [r["status"] for r in res] == ["KEY_INVALID", "OK"]


def test_env_normalized(monkeypatch):
    class FakeClient:
        def __init__(self, *a, env="demo", **kw):
            self.env = env

        def get_balance(self, ccy="USDT"):
            return 1.0

    monkeypatch.setattr("core.apikey_health.OKXClient", FakeClient)
    cfg = _cfg([_acct("实盘的", env="real"), _acct("模拟的", env="demo")])
    res = check_all_api_keys(config=cfg)
    assert [r["env"] for r in res] == ["live", "demo"]


# ---------------- 告警 ----------------

def test_notify_skipped_when_all_ok(monkeypatch):
    sent = []
    monkeypatch.setattr("core.notifier.Notifier.send",
                        lambda self, **kw: sent.append(kw) or True)
    cfg = {"system": {"webhook": {"enabled": True, "url": "http://x"}}}
    notify_if_invalid([{"name": "a", "env": "demo", "status": "OK",
                        "detail": ""}], cfg)
    assert sent == []


def test_notify_fires_on_invalid(monkeypatch):
    sent = []

    def _send(self, **kw):
        sent.append(kw)
        return True

    monkeypatch.setattr("core.notifier.Notifier.send", _send)
    cfg = {"system": {"webhook": {"enabled": True, "url": "http://x"}}}
    notify_if_invalid([{"name": "坏账户", "env": "live",
                        "status": "KEY_INVALID", "detail": "sCode=50112"}], cfg)
    assert len(sent) == 1
    assert sent[0]["level"] == "CRITICAL"
    assert "坏账户" in sent[0]["message"]


def test_notify_skipped_when_webhook_disabled(monkeypatch):
    sent = []
    monkeypatch.setattr("core.notifier.Notifier.send",
                        lambda self, **kw: sent.append(kw) or True)
    cfg = {"system": {"webhook": {"enabled": False, "url": "http://x"}}}
    notify_if_invalid([{"name": "a", "env": "live", "status": "KEY_INVALID",
                        "detail": "x"}], cfg)
    assert sent == []


def test_notify_failure_does_not_raise(monkeypatch):
    """告警发送失败不能把周检整个搞崩。"""
    def _boom(self, **kw):
        raise RuntimeError("webhook down")

    monkeypatch.setattr("core.notifier.Notifier.send", _boom)
    cfg = {"system": {"webhook": {"enabled": True, "url": "http://x"}}}
    notify_if_invalid([{"name": "a", "env": "live", "status": "KEY_INVALID",
                        "detail": "x"}], cfg)  # 不抛即通过
