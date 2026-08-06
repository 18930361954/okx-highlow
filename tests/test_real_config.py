"""真实 config.yaml 的健全性检查 (不是 fixture, 就是仓库里这份)。

exe 用户会手改 config.yaml, 分组/代理这些字段又是 v1.0.2 新加的 ——
这里保证仓库里这份配置随时可启动: 校验通过、账户能构建、分组不超限。
文件不存在时跳过 (CI / 全新克隆)。
"""
import pytest

from utils.paths import APP_ROOT

pytestmark = pytest.mark.skipif(
    not (APP_ROOT / "config.yaml").exists(), reason="无 config.yaml")


@pytest.fixture(scope="module")
def cfg():
    from main import load_config
    return load_config()


def test_real_config_passes_validation(cfg):
    from utils.app_config import validate_config
    assert validate_config(cfg) == []


def test_real_config_accounts_build(cfg):
    """每个账户都能构建成 AccountConfig (group 字段贯通到后端)。"""
    from core.multi_account import _build_account_config
    accounts = cfg.get("accounts") or []
    assert accounts, "config.yaml 没有 accounts 段"
    for raw in accounts:
        name = raw.get("account_name") or raw.get("name")
        acc = _build_account_config(name, raw, cfg)
        assert acc.name
        assert isinstance(acc.group, str)      # 缺字段时是 "" 而非 None
        assert acc.env in ("demo", "live")
        assert acc.pairs


def test_real_config_groups_within_limit(cfg):
    from utils.app_config import GROUP_MAX_ACCOUNTS
    counts: dict[str, int] = {}
    for raw in cfg.get("accounts") or []:
        g = str(raw.get("group") or "")
        if g:
            counts[g] = counts.get(g, 0) + 1
    for g, n in counts.items():
        assert n <= GROUP_MAX_ACCOUNTS, f"组 {g} 有 {n} 个账号"


def test_real_config_readable_by_config_store():
    """GUI 侧 (ruamel round-trip) 能读出分组与代理。"""
    from ui import config_store
    data = config_store.load_raw()
    groups = config_store.list_groups(data)
    assert groups
    for g in groups:
        assert g["accounts"]
    proxy = config_store.get_proxy(data)
    assert set(proxy) == {"url", "enabled"}


def test_example_config_also_valid():
    """config.example.yaml 是首启落地的模板, 必须同样合法。"""
    import yaml
    p = APP_ROOT / "config.example.yaml"
    if not p.exists():
        pytest.skip("无 config.example.yaml")
    from utils.app_config import validate_config
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # 模板里 api_key 是 ${VAR} 占位符, 账户默认 enabled:false → 不该报错
    assert validate_config(data) == []
