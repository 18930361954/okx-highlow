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


# ==================== 分组 ====================

GROUP_SAMPLE = """\
# 顶层注释必须保留
strategy:
  pairs: [BTC-USDT-SWAP]
  position_pct: 0.10
system:
  log_level: INFO
  db_path: data/trades.db
network:
  proxy_enabled: true
  proxy_url: "http://127.0.0.1:18081"
accounts:
  - account_name: a1
    group: 组A
    enabled: true
    api_key: "k1"
  - account_name: a2
    group: 组A
    enabled: false
    api_key: "k2"
  - account_name: b1
    group: 组B
    enabled: true
    api_key: "k3"   # 行内注释B1
  - account_name: nog
    enabled: true
    api_key: "k4"
"""


@pytest.fixture
def gstore(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(GROUP_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(config_store, "APP_ROOT", tmp_path)
    return cfg


def test_list_groups_order_and_membership(gstore):
    groups = config_store.list_groups(config_store.load_raw())
    assert [g["name"] for g in groups] == ["组A", "组B", config_store.UNGROUPED_LABEL]
    assert [a["name"] for a in groups[0]["accounts"]] == ["a1", "a2"]
    assert groups[2]["is_ungrouped"] is True
    assert config_store.group_names(config_store.load_raw()) == ["组A", "组B"]


def test_add_account_inserts_after_same_group(gstore):
    data = config_store.load_raw()
    idx = config_store.add_account(data, "组A", {
        "account_name": "a3", "enabled": True, "api_key": "k5"})
    assert idx == 2  # 紧跟 a2, 不落到列表末尾
    names = [a["name"] for a in config_store.list_accounts(data)]
    assert names == ["a1", "a2", "a3", "b1", "nog"]
    config_store.save_raw(data)
    groups = config_store.list_groups(config_store.load_raw())
    assert [a["name"] for a in groups[0]["accounts"]] == ["a1", "a2", "a3"]


def test_add_account_rejects_when_group_full(gstore):
    data = config_store.load_raw()
    config_store.add_account(data, "组A", {"account_name": "a3", "api_key": "k"})
    with pytest.raises(config_store.GroupFullError):
        config_store.add_account(data, "组A", {"account_name": "a4", "api_key": "k"})


def test_rename_and_set_group(gstore):
    data = config_store.load_raw()
    assert config_store.rename_group(data, "组A", "组A2") == 2
    config_store.set_account_group(data, 3, "组B")     # nog → 组B
    config_store.save_raw(data)
    groups = {g["name"]: [a["name"] for a in g["accounts"]]
              for g in config_store.list_groups(config_store.load_raw())}
    assert groups == {"组A2": ["a1", "a2"], "组B": ["b1", "nog"]}


def test_set_group_enabled(gstore):
    data = config_store.load_raw()
    assert config_store.set_group_enabled(data, "组A", True) == 2
    accs = {a["name"]: a["enabled"] for a in config_store.list_accounts(data)}
    assert accs["a1"] and accs["a2"]
    assert config_store.set_group_enabled(data, "组A", False) == 2
    accs = {a["name"]: a["enabled"] for a in config_store.list_accounts(data)}
    assert not accs["a1"] and not accs["a2"]


# ==================== 软删除 ====================

@pytest.mark.parametrize("pos,gone", [(0, "a1"), (2, "b1"), (3, "nog")])
def test_soft_delete_comments_out_account(gstore, pos, gone):
    """首/中/尾三种位置: 账户从 accounts 消失, 原文以注释块留在文件里。"""
    data = config_store.load_raw()
    config_store.soft_delete_account(data, pos)
    config_store.save_raw(data)

    text = gstore.read_text(encoding="utf-8")
    data2 = config_store.load_raw()            # 仍是合法 yaml
    names = [a["name"] for a in config_store.list_accounts(data2)]
    assert gone not in names
    assert len(names) == 3
    assert "已删除" in text and f"account_name: {gone}" in text
    assert "# 顶层注释必须保留" in text        # 其余注释未丢


@pytest.mark.parametrize("pos,gone", [(0, "a1"), (1, "a2"), (2, "b1"), (3, "nog")])
def test_soft_delete_never_loses_account_silently(gstore, pos, gone):
    """核心安全不变量: 账户要么还在 accounts 里, 要么以注释形式留在文件里。
    绝不允许「从列表里消失 + 文件里也没有」—— 那就是无声丢配置。"""
    data = config_store.load_raw()
    config_store.soft_delete_account(data, pos)
    config_store.save_raw(data)

    text = gstore.read_text(encoding="utf-8")
    names = [a["name"] for a in config_store.list_accounts(config_store.load_raw())]
    still_listed = gone in names
    commented_out = f"account_name: {gone}" in text and "已删除" in text
    assert still_listed or commented_out, f"{gone} 被无声丢掉了"
    # 原文可恢复: api_key 也要在 (不能只留个名字)
    assert f"api_key:" in text


def test_soft_delete_preserves_account_indentation(gstore):
    """写回后 accounts 缩进风格不变 (原文是 '  - ' 两空格)。
    否则每次 GUI 保存都会重排整个文件, .bak 的 diff 完全没法看。"""
    data = config_store.load_raw()
    config_store.soft_delete_account(data, 1)
    config_store.save_raw(data)
    text = gstore.read_text(encoding="utf-8")
    assert "\n  - account_name: a1" in text


@pytest.mark.parametrize("pos,gone", [(0, "a1"), (2, "b1"), (3, "nog")])
def test_soft_deleted_block_is_restorable(gstore, pos, gone):
    """注释块必须是「去掉行首 '# ' 就能恢复」的真原文 —— 这是软删除的全部意义。
    恢复 = 去掉 '#' 与其后的一个空格, 保留其余缩进 (整块 lstrip 会毁掉层级)。"""
    import yaml as pyyaml

    def uncomment(line: str) -> str:
        s = line.strip()
        if not s.startswith("#"):
            return None
        s = s[1:]
        return s[1:] if s.startswith(" ") else s

    data = config_store.load_raw()
    original = dict(data["accounts"][pos])
    config_store.soft_delete_account(data, pos)
    config_store.save_raw(data)

    lines = gstore.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if "已删除" in ln and gone in ln)
    block = []
    for ln in lines[start + 1:]:              # start 那行是标题, 跳过
        out = uncomment(ln)
        if out is None:
            break
        block.append(out)
    restored = pyyaml.safe_load("\n".join(block))
    assert isinstance(restored, list) and len(restored) == 1
    assert restored[0]["account_name"] == gone
    # 关键字段逐项还原, 不是只剩个名字
    for key in ("api_key", "enabled", "group"):
        if key in original:
            assert restored[0][key] == original[key]


def test_soft_delete_keeps_other_inline_comments(gstore):
    data = config_store.load_raw()
    config_store.soft_delete_account(data, 0)
    config_store.save_raw(data)
    assert "# 行内注释B1" in gstore.read_text(encoding="utf-8")


def test_soft_delete_last_remaining_falls_back_to_disable(tmp_path, monkeypatch):
    """只剩一个账户时无处锚定注释 → 兜底保留条目 + enabled:false, 不丢数据。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "strategy: {pairs: [BTC-USDT-SWAP]}\n"
        "system: {log_level: INFO, db_path: data/trades.db}\n"
        "accounts:\n  - {account_name: only, enabled: true, api_key: \"k\"}\n",
        encoding="utf-8")
    monkeypatch.setattr(config_store, "APP_ROOT", tmp_path)
    data = config_store.load_raw()
    config_store.soft_delete_account(data, 0)
    config_store.save_raw(data)
    accs = config_store.list_accounts(config_store.load_raw())
    assert len(accs) == 1 and accs[0]["name"] == "only"
    assert accs[0]["enabled"] is False        # 停用而非丢失
    assert "已删除" in cfg.read_text(encoding="utf-8")


def test_soft_delete_fallback_on_anchor_failure(gstore, monkeypatch):
    """注释锚定抛异常时也必须保住账户条目 (只停用)。"""
    data = config_store.load_raw()
    monkeypatch.setattr(config_store, "_dump_account_as_comment",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    config_store.soft_delete_account(data, 0)
    accs = config_store.list_accounts(data)
    assert [a["name"] for a in accs] == ["a1", "a2", "b1", "nog"]
    assert accs[0]["enabled"] is False
    config_store.save_raw(data)               # 仍可正常写回
    assert len(config_store.list_accounts(config_store.load_raw())) == 4


def test_delete_group_removes_all_members(gstore):
    data = config_store.load_raw()
    assert config_store.delete_group(data, "组A") == 2
    config_store.save_raw(data)
    names = [a["name"] for a in config_store.list_accounts(config_store.load_raw())]
    assert names == ["b1", "nog"]
    assert "已删除" in gstore.read_text(encoding="utf-8")


# ==================== 代理 ====================

def test_proxy_read_edit_clear(gstore):
    data = config_store.load_raw()
    assert config_store.get_proxy(data) == {
        "url": "http://127.0.0.1:18081", "enabled": True}
    config_store.set_proxy(data, "socks5://10.0.0.2:1080", enabled=True)
    config_store.save_raw(data)
    assert config_store.get_proxy(config_store.load_raw())["url"] == \
        "socks5://10.0.0.2:1080"
    data2 = config_store.load_raw()
    config_store.clear_proxy(data2)
    config_store.save_raw(data2)
    assert config_store.get_proxy(config_store.load_raw()) == {"url": "", "enabled": False}


def test_set_proxy_creates_network_section(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("strategy: {pairs: [BTC-USDT-SWAP]}\nsystem: {log_level: INFO}\n",
                   encoding="utf-8")
    monkeypatch.setattr(config_store, "APP_ROOT", tmp_path)
    data = config_store.load_raw()
    config_store.set_proxy(data, "http://127.0.0.1:7890")
    config_store.save_raw(data)
    assert config_store.get_proxy(config_store.load_raw()) == {
        "url": "http://127.0.0.1:7890", "enabled": True}
