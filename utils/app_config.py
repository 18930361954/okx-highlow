"""advanced/network 配置段读取与启动前轻校验。

原则: 每个键的默认值 == 提升前的硬编码常量, 整段缺失 = 行为与历史版本完全一致。
exe 用户手改 yaml 容错: 类型转换失败回默认并 warning, 不崩。
"""
from __future__ import annotations

import logging

# 键 → (默认值, 原硬编码位置)。默认值绝不可改 — 它们是历史行为的契约。
ADVANCED_DEFAULTS: dict = {
    "slip_pct": 0.0001,                # order_manager.SLIP_PCT
    "fresh_pending_grace_ms": 30000,   # reconciler._FRESH_PENDING_GRACE_MS
    "rearm_cooldown_sec": 300,         # reconciler 重挂保护冷却 5*60
    "recon_net_fail_threshold": 3,     # multi_account._RECON_NET_FAIL_THRESHOLD
    "recon_backoff_base_sec": 30,      # multi_account._RECON_BACKOFF_BASE_SECS
    "recon_backoff_max_sec": 300,      # multi_account._RECON_BACKOFF_MAX_SECS
    "reconcile_interval_sec": 20,      # main reconcile job 间隔
    "account_second_offset_step": 3,   # main 多账户信号秒偏移步长
    "signal_cron_minute": 2,           # scheduler 信号 cron minute
    "signal_misfire_grace_sec": 300,   # scheduler 信号 job misfire 宽限
    "catchup_skip_bucket_ratio": 0.5,  # main 启动补挂: 桶已过比例阈值
    "panel_refresh_sec": 5.0,          # position_monitor 刷新间隔
    "db_busy_timeout_sec": 30,         # data/db sqlite busy timeout
}

NETWORK_DEFAULTS: dict = {
    "okx_base_url": "https://www.okx.com",  # okx_client.OKX_BASE_URL
    "http_timeout_sec": 15,                 # okx_client timeout
}

# GUI 显示用中文名 (yaml 键保持英文不变, 保配置兼容)
ADVANCED_LABELS: dict = {
    "slip_pct": "挂单穿价偏移",
    "fresh_pending_grace_ms": "新挂单对账宽限(毫秒)",
    "rearm_cooldown_sec": "保护重挂冷却(秒)",
    "recon_net_fail_threshold": "网络熔断阈值(次)",
    "recon_backoff_base_sec": "熔断退避基数(秒)",
    "recon_backoff_max_sec": "熔断退避上限(秒)",
    "reconcile_interval_sec": "对账轮询间隔(秒)",
    "account_second_offset_step": "账户秒偏移步长",
    "signal_cron_minute": "信号触发分钟",
    "signal_misfire_grace_sec": "信号错过宽限(秒)",
    "catchup_skip_bucket_ratio": "补挂跳过比例",
    "panel_refresh_sec": "面板刷新间隔(秒)",
    "db_busy_timeout_sec": "数据库锁超时(秒)",
}

_log = logging.getLogger("hl-bot")


def _get(section: dict, defaults: dict, key: str):
    default = defaults[key]
    raw = (section or {}).get(key, default)
    if raw is default:
        return default
    try:
        return type(default)(raw)
    except (TypeError, ValueError):
        _log.warning(f"[config] {key}={raw!r} 类型无效, 回退默认 {default!r}")
        return default


def adv(top_cfg: dict, key: str):
    """advanced.<key>, 缺失/类型错回默认。"""
    return _get(top_cfg.get("advanced") or {}, ADVANCED_DEFAULTS, key)


def net(top_cfg: dict, key: str):
    """network.<key>, 缺失/类型错回默认。"""
    return _get(top_cfg.get("network") or {}, NETWORK_DEFAULTS, key)


def validate_config(top_cfg: dict) -> list[str]:
    """启动前轻校验, 返回人类可读错误列表 (空 = 通过)。
    只校验缺失会导致 KeyError 崩溃的事实必填键, 不改变任何消费逻辑。"""
    errors: list[str] = []
    if not isinstance(top_cfg, dict) or not top_cfg:
        return ["config.yaml 为空或格式错误"]

    system = top_cfg.get("system")
    if not isinstance(system, dict):
        errors.append("缺少 system 段")
    else:
        for k in ("log_level", "db_path"):
            if not system.get(k):
                errors.append(f"system.{k} 缺失")

    strategy = top_cfg.get("strategy")
    if not isinstance(strategy, dict):
        errors.append("缺少 strategy 段")
    else:
        # AccountState/HighLowStrategy 构造器直接下标, 缺了就 KeyError
        for k in ("position_pct", "max_consecutive_losses", "cooldown_hours",
                  "fixed_mode_threshold", "fixed_mode_margin",
                  "float_pct", "tp_pct", "sl_pct"):
            if strategy.get(k) is None:
                errors.append(f"strategy.{k} 缺失 (必填)")
        if not strategy.get("pairs"):
            errors.append("strategy.pairs 缺失或为空")

    accounts = top_cfg.get("accounts")
    if accounts is not None:
        if not isinstance(accounts, list):
            errors.append("accounts 必须是列表")
        else:
            for i, raw in enumerate(accounts):
                if not isinstance(raw, dict):
                    errors.append(f"accounts[{i}] 必须是映射")
                    continue
                name = raw.get("account_name") or raw.get("name") or f"#{i}"
                if bool(raw.get("enabled", True)) and not raw.get("api_key"):
                    errors.append(f"账户 {name}: enabled=true 但 api_key 为空")

    # 未知 advanced/network 键提示 (拼错键会静默用默认, 给个 warning 文案)
    for section, defaults in (("advanced", ADVANCED_DEFAULTS),
                              ("network", NETWORK_DEFAULTS)):
        seg = top_cfg.get(section)
        if isinstance(seg, dict):
            known = set(defaults) | {"proxy_enabled", "proxy_url"}
            for k in seg:
                if k not in known:
                    errors.append(f"{section}.{k} 不是已知配置项 (拼写错误?)")
    return errors
