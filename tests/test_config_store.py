"""ui.config_store: ruamel round-trip 读写 (保注释) + 字段编辑。"""
import pytest

pytest.importorskip("ruamel.yaml")

from ui import config_store  # noqa: E402


SAMPLE = """\
# 顶层注释必须保留
strategy:
  pairs: [BTC-USDT-SWAP]
  position_pct: 0.10   # 行内注释
system:
  log_level: INFO
  db_path: data/trades.db
accounts:
  - account_name: acc1
    enabled: true
    env_adapt: demo
    strategy_name: v3-mixed
    api_key: "k"
    pairs: [BTC-USDT-SWAP]
    strategy:
      signal_bar: 6H
      pair_overrides:
        BTC-USDT-SWAP: { mode: trend, tp_pct: 0.008 }
  - account_name: acc2
    enabled: false
    api_key: ""
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr(config_store, "APP_ROOT", tmp_path)
    return cfg


def test_roundtrip_preserves_comments(store):
    data = config_store.load_raw()
    config_store.save_raw(data)
    out = store.read_text(encoding="utf-8")
    assert "# 顶层注释必须保留" in out
    assert "# 行内注释" in out
    assert store.with_suffix(".yaml.bak").exists()


def test_list_accounts(store):
    data = config_store.load_raw()
    accs = config_store.list_accounts(data)
    assert [a["name"] for a in accs] == ["acc1", "acc2"]
    assert accs[0]["enabled"] is True
    assert accs[1]["enabled"] is False


def test_toggle_enabled_persists(store):
    data = config_store.load_raw()
    config_store.set_account_enabled(data, 0, False)
    config_store.save_raw(data)
    data2 = config_store.load_raw()
    assert config_store.list_accounts(data2)[0]["enabled"] is False
    # 注释仍在
    assert "# 行内注释" in store.read_text(encoding="utf-8")


def test_pair_override_edit_and_delete(store):
    data = config_store.load_raw()
    config_store.set_pair_override_field(data, 0, "BTC-USDT-SWAP", "sl_pct", 0.03)
    config_store.set_pair_override_field(data, 0, "BTC-USDT-SWAP", "tp_pct", None)
    config_store.save_raw(data)
    po = config_store.get_pair_overrides(config_store.load_raw(), 0)
    assert po["BTC-USDT-SWAP"]["sl_pct"] == 0.03
    assert "tp_pct" not in po["BTC-USDT-SWAP"]


def test_advanced_create_edit_delete(store):
    data = config_store.load_raw()
    assert config_store.get_advanced(data) == {}
    config_store.set_advanced_field(data, "reconcile_interval_sec", 30)
    config_store.save_raw(data)
    data2 = config_store.load_raw()
    assert config_store.get_advanced(data2)["reconcile_interval_sec"] == 30
    config_store.set_advanced_field(data2, "reconcile_interval_sec", None)
    config_store.save_raw(data2)
    assert config_store.get_advanced(config_store.load_raw()) == {}


def test_account_strategy_field(store):
    data = config_store.load_raw()
    config_store.set_account_strategy_field(data, 1, "position_pct", 0.05)
    config_store.save_raw(data)
    got = config_store.get_account_strategy_fields(config_store.load_raw(), 1)
    assert got["position_pct"] == 0.05


ENV_SAMPLE = """\
strategy: {pairs: [BTC-USDT-SWAP]}
system: {log_level: INFO, db_path: data/trades.db}
accounts:
  - {account_name: live1, enabled: false, env_adapt: real, api_key: "k1"}
  - {account_name: live2, enabled: false, env_adapt: real, api_key: "k2"}
  - {account_name: demo1, enabled: true, env_adapt: demo, api_key: "k3"}
  - {account_name: demo2, enabled: true, env_adapt: demo, api_key: "k4"}
  - {account_name: nokey, enabled: false, env_adapt: real, api_key: ""}
"""


@pytest.fixture
def env_store(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(ENV_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(config_store, "APP_ROOT", tmp_path)
    return cfg


def test_switch_env_to_live_enables_live_disables_demo(env_store):
    data = config_store.load_raw()
    n = config_store.set_env_enabled(data, "live")
    assert n == 2  # nokey 没 api_key 不启用
    accs = {a["name"]: a["enabled"] for a in config_store.list_accounts(data)}
    assert accs == {"live1": True, "live2": True,
                    "demo1": False, "demo2": False, "nokey": False}


def test_switch_env_back_to_demo(env_store):
    data = config_store.load_raw()
    config_store.set_env_enabled(data, "live")
    n = config_store.set_env_enabled(data, "demo")
    assert n == 2
    accs = {a["name"]: a["enabled"] for a in config_store.list_accounts(data)}
    assert accs["demo1"] and accs["demo2"]
    assert not accs["live1"] and not accs["live2"]


def test_switch_env_no_candidates_leaves_config_untouched(env_store):
    import copy
    data = config_store.load_raw()
    # 删掉全部 live 账户的 key → live 无候选
    for a in data["accounts"]:
        if str(a.get("env_adapt")) == "real":
            a["api_key"] = ""
    before = copy.deepcopy([dict(a) for a in data["accounts"]])
    n = config_store.set_env_enabled(data, "live")
    assert n == 0
    assert [dict(a) for a in data["accounts"]] == before  # 未动 enabled


def test_switch_env_normalizes_real_prod_to_live(env_store):
    data = config_store.load_raw()
    # env_adapt: real 应被归一为 live 匹配
    assert config_store.set_env_enabled(data, "live") == 2
