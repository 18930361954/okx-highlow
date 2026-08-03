"""config.yaml 读写 (GUI 配置页后端)。

ruamel.yaml round-trip: 保留注释与键顺序, GUI 只改值不重排文件。
写盘前自动备份 config.yaml.bak。核心原则: 只改用户在界面上动过的字段。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ruamel.yaml import YAML

from utils.paths import APP_ROOT

_yaml = YAML()  # round-trip 模式
_yaml.preserve_quotes = True
_yaml.width = 4096  # 防长行被折行破坏注释


def config_path() -> Path:
    return APP_ROOT / "config.yaml"


def load_raw() -> dict:
    """round-trip 加载 (CommentedMap, 可原样写回)。"""
    with open(config_path(), "r", encoding="utf-8") as f:
        return _yaml.load(f)


def save_raw(data) -> None:
    """备份后原子写回。"""
    cfg = config_path()
    bak = cfg.with_suffix(".yaml.bak")
    shutil.copy(cfg, bak)
    tmp = cfg.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        _yaml.dump(data, f)
    tmp.replace(cfg)


def list_accounts(data) -> list[dict]:
    """账户概要 (供 GUI 表格): name/enabled/env/strategy_name/pairs。"""
    out = []
    for i, raw in enumerate(data.get("accounts") or []):
        out.append({
            "index": i,
            "name": str(raw.get("account_name") or raw.get("name") or f"#{i}"),
            "enabled": bool(raw.get("enabled", True)),
            "env": str(raw.get("env") or raw.get("env_adapt") or ""),
            "strategy_name": str(raw.get("strategy_name") or ""),
            "pairs": list(raw.get("pairs") or []),
        })
    return out


def set_account_enabled(data, index: int, enabled: bool) -> None:
    data["accounts"][index]["enabled"] = bool(enabled)


def _norm_env(v) -> str:
    v = str(v or "").strip().lower()
    if v in ("live", "real", "prod", "production"):
        return "live"
    return "demo"


def set_env_enabled(data, env: str) -> int:
    """一键环境切换: 启用目标环境(demo/live)全部有 api_key 的账户, 禁用其余。
    返回启用数; 0 = 目标环境无可启用账户(未做任何修改)。"""
    target = _norm_env(env)
    accounts = data.get("accounts") or []
    candidates = [
        a for a in accounts
        if _norm_env(a.get("env") or a.get("env_adapt")) == target
        and str(a.get("api_key") or "").strip()
    ]
    if not candidates:
        return 0
    for a in accounts:
        a["enabled"] = a in candidates
    return len(candidates)


def get_account_strategy_fields(data, index: int) -> dict:
    """账户级常用参数 (position_pct 等, 未覆盖时给 None 表示沿用全局)。"""
    s = data["accounts"][index].get("strategy") or {}
    return {
        "position_pct": s.get("position_pct"),
        "signal_bar": s.get("signal_bar"),
        "mode": s.get("mode"),
    }


def set_account_strategy_field(data, index: int, key: str, value) -> None:
    """value=None 时删除该覆盖 (回退全局默认)。"""
    acc = data["accounts"][index]
    s = acc.get("strategy")
    if s is None:
        if value is None:
            return
        acc["strategy"] = {}
        s = acc["strategy"]
    if value is None:
        s.pop(key, None)
    else:
        s[key] = value


def get_pair_overrides(data, index: int) -> dict:
    s = data["accounts"][index].get("strategy") or {}
    return dict(s.get("pair_overrides") or {})


def set_pair_override_field(data, index: int, pair: str, key: str, value) -> None:
    acc = data["accounts"][index]
    s = acc.setdefault("strategy", {})
    po = s.setdefault("pair_overrides", {})
    ov = po.setdefault(pair, {})
    if value is None:
        ov.pop(key, None)
    else:
        ov[key] = value


def get_advanced(data) -> dict:
    return dict(data.get("advanced") or {})


def set_advanced_field(data, key: str, value) -> None:
    """value=None 删除键 (回默认); advanced 段不存在时按需创建。"""
    adv = data.get("advanced")
    if adv is None:
        if value is None:
            return
        data["advanced"] = {}
        adv = data["advanced"]
    if value is None:
        adv.pop(key, None)
    else:
        adv[key] = value
