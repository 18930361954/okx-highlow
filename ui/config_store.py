"""config.yaml 读写 (GUI 配置页后端)。

ruamel.yaml round-trip: 保留注释与键顺序, GUI 只改值不重排文件。
写盘前自动备份 config.yaml.bak。核心原则: 只改用户在界面上动过的字段。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ruamel.yaml import YAML

from utils.app_config import GROUP_MAX_ACCOUNTS
from utils.paths import APP_ROOT

__all__ = [
    "GROUP_MAX_ACCOUNTS", "UNGROUPED_LABEL", "config_path", "load_raw", "save_raw",
    "list_accounts", "set_account_enabled", "set_env_enabled",
    "get_account_strategy_fields", "set_account_strategy_field",
    "get_pair_overrides", "set_pair_override_field",
    "get_advanced", "set_advanced_field",
    "list_groups", "group_names", "add_account", "update_account",
    "soft_delete_account", "rename_group", "delete_group", "set_account_group",
    "set_group_enabled", "get_proxy", "set_proxy", "clear_proxy",
]

# group 为空的账户在界面上归到这个标签下 (yaml 里仍是缺字段/空串, 不写入这个值)
UNGROUPED_LABEL = "未分组"

_yaml = YAML()  # round-trip 模式
_yaml.preserve_quotes = True
_yaml.width = 4096  # 防长行被折行破坏注释
# 对齐 config.yaml 现有风格 ('  - account_name:' 破折号缩进 2, 内容缩进 4)。
# 不设的话 ruamel 按默认缩进重排整个 accounts 段, 每次保存 .bak 的 diff 都没法看。
_yaml.indent(mapping=2, sequence=4, offset=2)


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
            "group": str(raw.get("group") or ""),
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


# ==================== 分组 (group) ====================
# 「组」只是 accounts[].group 上的一个分类标签: accounts 仍是扁平列表,
# 后端 load_accounts 完全不感知分组。无 group 字段的账户归到 UNGROUPED_LABEL。


def list_groups(data) -> list[dict]:
    """按 accounts 出现顺序分组: [{name, is_ungrouped, accounts: [<list_accounts 条目>]}]。
    组的先后 = 该组第一个账户在文件里的位置。"""
    groups: list[dict] = []
    by_name: dict[str, dict] = {}
    for acc in list_accounts(data):
        g = acc["group"]
        if g not in by_name:
            entry = {
                "name": g or UNGROUPED_LABEL,
                "raw_name": g,
                "is_ungrouped": not g,
                "accounts": [],
            }
            by_name[g] = entry
            groups.append(entry)
        by_name[g]["accounts"].append(acc)
    return groups


def group_names(data) -> list[str]:
    """已存在的真实组名 (不含未分组占位), 保持文件顺序。"""
    return [g["raw_name"] for g in list_groups(data) if not g["is_ungrouped"]]


def _accounts_seq(data):
    """取 accounts 列表, 不存在时创建。"""
    if data.get("accounts") is None:
        data["accounts"] = []
    return data["accounts"]


def group_count(data, group: str) -> int:
    g = str(group or "")
    return sum(1 for a in list_accounts(data) if a["group"] == g)


def _last_index_of_group(data, group: str) -> int | None:
    g = str(group or "")
    idxs = [a["index"] for a in list_accounts(data) if a["group"] == g]
    return idxs[-1] if idxs else None


# 新账户写入 yaml 时的键顺序 (可读性; ruamel 按插入顺序 dump)
_ACCOUNT_KEY_ORDER = (
    "account_name", "group", "enabled", "strategy_name",
    "api_key", "secret_key", "passphrase", "env_adapt", "pairs",
)


class GroupFullError(Exception):
    """目标组已满 GROUP_MAX_ACCOUNTS 个账号。"""


def add_account(data, group: str, fields: dict) -> int:
    """新增账户, 插到同组最后一个账户之后 (保证同组在文件里连续)。
    组为空 → 追加到列表末尾。返回新账户的 index。"""
    g = str(group or "")
    if g and group_count(data, g) >= GROUP_MAX_ACCOUNTS:
        raise GroupFullError(f"组 {g} 已有 {GROUP_MAX_ACCOUNTS} 个账号")

    from ruamel.yaml.comments import CommentedMap
    acc = CommentedMap()
    payload = dict(fields or {})
    payload["group"] = g
    for key in _ACCOUNT_KEY_ORDER:
        if key in payload:
            acc[key] = payload.pop(key)
    for key, val in payload.items():      # strategy 等剩余键
        acc[key] = val

    seq = _accounts_seq(data)
    last = _last_index_of_group(data, g)
    pos = len(seq) if last is None else last + 1
    seq.insert(pos, acc)
    return pos


def update_account(data, index: int, fields: dict) -> None:
    """更新账户顶层字段。value=None → 删除该键。strategy 子字段请用
    set_account_strategy_field / set_pair_override_field。"""
    acc = data["accounts"][index]
    for key, val in (fields or {}).items():
        if val is None:
            acc.pop(key, None)
        else:
            acc[key] = val


def set_account_group(data, index: int, group: str) -> None:
    g = str(group or "")
    acc = data["accounts"][index]
    if g:
        if group_count(data, g) >= GROUP_MAX_ACCOUNTS and str(acc.get("group") or "") != g:
            raise GroupFullError(f"组 {g} 已有 {GROUP_MAX_ACCOUNTS} 个账号")
        acc["group"] = g
    else:
        acc.pop("group", None)


def rename_group(data, old: str, new: str) -> int:
    """批量改组名, 返回受影响账户数。"""
    old, new = str(old or ""), str(new or "")
    n = 0
    for acc in data.get("accounts") or []:
        if str(acc.get("group") or "") == old:
            if new:
                acc["group"] = new
            else:
                acc.pop("group", None)
            n += 1
    return n


def set_group_enabled(data, group: str, enabled: bool) -> int:
    """组内全部账户一键启用/停用, 返回受影响账户数。"""
    g = str(group or "")
    n = 0
    for acc in list_accounts(data):
        if acc["group"] == g:
            set_account_enabled(data, acc["index"], enabled)
            n += 1
    return n


def delete_group(data, group: str) -> int:
    """软删除组内全部账户 (从尾往前删, 避免 index 位移)。返回删除数。"""
    g = str(group or "")
    idxs = [a["index"] for a in list_accounts(data) if a["group"] == g]
    for i in reversed(idxs):
        soft_delete_account(data, i)
    return len(idxs)


# ==================== 软删除 (注释掉, 不丢数据) ====================


def _dump_text(data) -> str:
    import io
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _dump_account_as_comment(acc, name: str, stamp: str) -> str:
    """账户条目的**未加 # 前缀**原文块 (含标题行)。

    注意: ruamel 的 yaml_set_comment_before_after_key 会自己给每行加 '# ',
    所以这里不能预先加 —— 否则输出成 '# # - account_name: ...', 去掉一个 #
    仍是注释, 就恢复不出来了。手写 CommentToken 的那条路径需要自己加前缀。
    """
    import io
    buf = io.StringIO()
    _yaml.dump([acc], buf)                       # 以单元素列表 dump, 保留 '- ' 结构
    body = buf.getvalue().rstrip("\n")
    title = _deleted_marker(name, stamp) + " — 去掉行首 # 即可恢复 ----"
    return "\n".join([title] + body.split("\n"))


def _commented(block: str) -> str:
    """给每行加 '# ' —— 只在手写 CommentToken 时用。"""
    return "\n".join(f"# {ln}" if ln.strip() else "#" for ln in block.split("\n"))


def _deleted_marker(name: str, stamp: str) -> str:
    return f"---- [已删除 {stamp}] 账户 {name}"


def _fallback_disable(acc, name: str, stamp: str) -> None:
    """兜底: 注释锚定失败时保留条目, 只停用并打标记 — 绝不丢账户数据。"""
    acc["enabled"] = False
    try:
        acc.yaml_add_eol_comment(f"[已删除 {stamp}] {name}", "enabled")
    except Exception:
        pass


def _snapshot_ca(seq) -> dict:
    return {k: (list(v) if isinstance(v, list) else v)
            for k, v in seq.ca.items.items()}


def _restore_ca(seq, snap: dict) -> None:
    seq.ca.items.clear()
    seq.ca.items.update({k: (list(v) if isinstance(v, list) else v)
                         for k, v in snap.items()})


def _anchor_before_next(seq, idx: int, block: str) -> bool:
    """注释挂在「接替该位置的元素」前面 —— 删中间/开头时用这个。"""
    if idx >= len(seq):
        return False
    seq.yaml_set_comment_before_after_key(idx, before=block, indent=2)
    return True


def _anchor_after_prev(seq, idx: int, block: str) -> bool:
    """删的是最后一个 → 挂到前一个元素之后 (ruamel 官方 API)。"""
    if idx == 0 or not seq:
        return False
    seq.yaml_set_comment_before_after_key(idx - 1, after=block, indent=2)
    return True


def _anchor_after_prev_raw(seq, idx: int, block: str) -> bool:
    """同上, 但直接塞 CommentToken 到前一元素的 EOL 槽 ——
    ruamel 解析「列表项后面跟一段注释」时正是存在这个位置。
    官方 API 在序列上对 after= 不总生效, 这条是兜底。
    这条路径 ruamel 不会自动加 '#', 得自己加。"""
    if idx == 0 or not seq:
        return False
    from ruamel.yaml.error import CommentMark
    from ruamel.yaml.tokens import CommentToken
    text = "\n" + "\n".join("  " + ln for ln in _commented(block).split("\n")) + "\n"
    slot = seq.ca.items.setdefault(idx - 1, [None, None, None, None])
    slot[0] = CommentToken(text, CommentMark(2), None)
    return True


_ANCHOR_STRATEGIES = (_anchor_before_next, _anchor_after_prev, _anchor_after_prev_raw)


def _block_is_restorable(text: str, marker: str) -> bool:
    """注释块真的落进文件, 且「去掉一个 '# ' 就能恢复」。

    双重前缀 ('# # ---- [已删除...') 是最阴的失败模式: 标记字符串在文件里,
    看着成功了, 但用户去掉一个 # 之后仍是注释 —— 等于账户永久丢了。
    """
    for line in text.splitlines():
        if marker not in line:
            continue
        s = line.strip()
        if not s.startswith("#"):
            return False
        rest = s[1:].lstrip()
        return not rest.startswith("#")     # 第二个 # = 被注释了两层
    return False


def soft_delete_account(data, index: int) -> None:
    """从 accounts 移除该账户, 但把原文以注释块写回同一位置。
    DB 里的历史成交不动 —— 只是配置层面下线。

    注释锚定必须**验证真的落进了输出文本**: ruamel 在序列上挂注释的行为
    随版本/位置而变, 静默不生效就等于账户被无声删掉。任何一步失败都退回
    「保留条目 + enabled:false」, 绝不丢数据。
    """
    from datetime import datetime

    seq = data["accounts"]
    acc = seq[index]
    name = str(acc.get("account_name") or acc.get("name") or f"#{index}")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    try:
        block = _dump_account_as_comment(acc, name, stamp)
    except Exception:
        _fallback_disable(acc, name, stamp)
        return

    marker = _deleted_marker(name, stamp)
    del seq[index]
    base_ca = _snapshot_ca(seq)

    for strategy in _ANCHOR_STRATEGIES:
        _restore_ca(seq, base_ca)
        try:
            if not strategy(seq, index, block):
                continue
            text = _dump_text(data)
            if not _block_is_restorable(text, marker):
                continue                  # 没落进文件, 或被双重注释成 '# # ' → 换一种锚法
            _yaml.load(text)              # 结构仍是合法 yaml
            return
        except Exception:
            continue

    # 全部锚法都不行 → 条目放回原位, 只停用
    _restore_ca(seq, base_ca)
    seq.insert(index, acc)
    _fallback_disable(acc, name, stamp)


# ==================== 代理 (network 段, 全局唯一 1 个) ====================


def get_proxy(data) -> dict:
    net = data.get("network") or {}
    return {
        "url": str(net.get("proxy_url") or ""),
        "enabled": bool(net.get("proxy_enabled", False)),
    }


def set_proxy(data, url: str, enabled: bool = True) -> None:
    if data.get("network") is None:
        data["network"] = {}
    data["network"]["proxy_url"] = str(url or "")
    data["network"]["proxy_enabled"] = bool(enabled)


def clear_proxy(data) -> None:
    """删除代理 = 置空 URL 并停用 (保留键, 让用户知道这里可以配)。"""
    net = data.get("network")
    if net is None:
        return
    net["proxy_url"] = ""
    net["proxy_enabled"] = False
