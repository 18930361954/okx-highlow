"""utils/app_config: advanced/network 读取容错 + validate_config 文案。"""
from utils.app_config import ADVANCED_DEFAULTS, NETWORK_DEFAULTS, adv, net, validate_config


def test_adv_missing_section_returns_default():
    assert adv({}, "slip_pct") == ADVANCED_DEFAULTS["slip_pct"]
    assert adv({"advanced": None}, "reconcile_interval_sec") == 20


def test_adv_missing_key_returns_default():
    assert adv({"advanced": {"slip_pct": 0.001}}, "rearm_cooldown_sec") == 300


def test_adv_override_and_type_coercion():
    cfg = {"advanced": {"slip_pct": "0.002", "reconcile_interval_sec": "45"}}
    assert adv(cfg, "slip_pct") == 0.002
    assert adv(cfg, "reconcile_interval_sec") == 45


def test_adv_bad_type_falls_back():
    cfg = {"advanced": {"slip_pct": "abc", "db_busy_timeout_sec": [1]}}
    assert adv(cfg, "slip_pct") == ADVANCED_DEFAULTS["slip_pct"]
    assert adv(cfg, "db_busy_timeout_sec") == ADVANCED_DEFAULTS["db_busy_timeout_sec"]


def test_net_defaults_and_override():
    assert net({}, "okx_base_url") == NETWORK_DEFAULTS["okx_base_url"]
    assert net({"network": {"http_timeout_sec": 30}}, "http_timeout_sec") == 30


def _minimal_valid_config():
    return {
        "system": {"log_level": "INFO", "db_path": "data/trades.db"},
        "strategy": {
            "pairs": ["BTC-USDT-SWAP"], "position_pct": 0.1,
            "max_consecutive_losses": 3, "cooldown_hours": 24,
            "fixed_mode_threshold": 800000, "fixed_mode_margin": 1000,
            "float_pct": 0.0015, "tp_pct": 0.012, "sl_pct": 0.005,
        },
    }


def test_validate_ok():
    assert validate_config(_minimal_valid_config()) == []


def test_validate_empty_config():
    assert validate_config({}) == ["config.yaml 为空或格式错误"]
    assert validate_config(None) == ["config.yaml 为空或格式错误"]


def test_validate_missing_required_keys():
    cfg = _minimal_valid_config()
    del cfg["strategy"]["position_pct"]
    del cfg["system"]["db_path"]
    errs = validate_config(cfg)
    assert any("position_pct" in e for e in errs)
    assert any("db_path" in e for e in errs)


def test_validate_enabled_account_without_key():
    cfg = _minimal_valid_config()
    cfg["accounts"] = [
        {"account_name": "a1", "enabled": True, "api_key": ""},
        {"account_name": "a2", "enabled": False, "api_key": ""},  # 禁用的不报
    ]
    errs = validate_config(cfg)
    assert any("a1" in e for e in errs)
    assert not any("a2" in e for e in errs)


def test_validate_unknown_advanced_key_flagged():
    cfg = _minimal_valid_config()
    cfg["advanced"] = {"slip_pcnt": 0.1}  # 拼写错误
    errs = validate_config(cfg)
    assert any("slip_pcnt" in e for e in errs)


def test_validate_network_known_keys_pass():
    cfg = _minimal_valid_config()
    cfg["network"] = {"proxy_enabled": True, "proxy_url": "http://127.0.0.1:1",
                      "okx_base_url": "https://www.okx.com", "http_timeout_sec": 15}
    assert validate_config(cfg) == []
