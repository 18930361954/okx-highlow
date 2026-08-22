"""API Key 有效性周检。

OKX 的 API Key 有有效期(默认创建时可选,到期自动失效),过期后所有私有接口
返回 50100/50101 类错误 —— 挂单、平仓、对账全部瘫掉。等到信号触发时才发现
已经太晚(可能已有裸仓)。这里每周主动探活一次,提前发现。

关键设计: 检查 config.yaml 里**全部**账户的 key, 不只是 enabled=true 的。
停用账户的 key 一样会静默过期, 等哪天切回实盘才发现就晚了。所以本模块直接读
config, 自建临时 OKXClient, 不依赖运行时的 AccountRuntime 列表。

探活用 /api/v5/account/balance (私有接口, 只读)。不用 /public/time ——
那是公开接口, key 失效也照样 200, 探不出问题。
"""
from __future__ import annotations

from datetime import datetime, timezone

import yaml

from core.okx_client import OKXClient, OKXError
from utils.paths import APP_ROOT

UTC = timezone.utc

# key 失效/权限相关的 OKX 错误码。命中这些 → 明确判定 key 有问题。
# 50100 APIKey 不存在 / 50101 APIKey 与环境不匹配 / 50102 时间戳过期
# 50103 请求头缺 APIKey / 50104 passphrase 错 / 50105 passphrase 校验失败
# 50111 APIKey 无效 / 50112 APIKey 已过期 / 50113 APIKey 签名错
# 50114 无效授权 / 50115 无效请求类型 / 50119 APIKey 不存在
_KEY_ERROR_CODES = {
    "50100", "50101", "50102", "50103", "50104", "50105",
    "50111", "50112", "50113", "50114", "50119",
}


def _classify(exc: Exception) -> tuple[str, str]:
    """返回 (状态, 说明)。状态: KEY_INVALID / NETWORK / UNKNOWN。

    只有明确命中 key 类错误码才判 KEY_INVALID —— 网络抖动/代理挂了不能误报,
    否则每周一封假警报, 真出事时反而被忽略。
    """
    msg = str(exc)
    if isinstance(exc, OKXError):
        code = str(getattr(exc, "code", "") or "")
        if code in _KEY_ERROR_CODES:
            return "KEY_INVALID", f"sCode={code} {msg}"
        for c in _KEY_ERROR_CODES:
            if f"sCode={c}" in msg or f"code={c}" in msg:
                return "KEY_INVALID", msg
        return "UNKNOWN", f"OKX 报错但非 key 类: {msg}"
    low = msg.lower()
    for kw in ("timeout", "timed out", "connection", "proxy", "ssl",
               "resolve", "unreachable", "reset"):
        if kw in low:
            return "NETWORK", f"网络/代理问题, 非 key 问题: {msg}"
    return "UNKNOWN", msg


def check_all_api_keys(logger=None, config: dict | None = None) -> list[dict]:
    """探活 config.yaml 里全部账户的 API key(含 enabled=false 的)。

    返回每账户一条: {name, env, enabled, status, detail}
      status: OK / KEY_INVALID / NETWORK / UNKNOWN / NO_KEY
    """
    if config is not None:
        cfg = config
    else:
        # 不 import main.load_config —— core 模块导入 main 会循环导入
        with open(APP_ROOT / "config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    accounts = cfg.get("accounts") or []
    net = cfg.get("network") or {}
    proxy = str(net.get("proxy_url") or "").strip() if net.get("proxy_enabled") else None

    results: list[dict] = []
    for i, raw in enumerate(accounts):
        name = str(raw.get("account_name") or raw.get("name") or f"acc{i}")
        env_raw = str(raw.get("env") or raw.get("env_adapt") or "demo").lower()
        env = "demo" if env_raw in ("demo", "sim", "simulate", "simulation") else "live"
        enabled = bool(raw.get("enabled", True))
        api_key = str(raw.get("api_key") or "").strip()

        if not api_key:
            results.append({"name": name, "env": env, "enabled": enabled,
                            "status": "NO_KEY", "detail": "api_key 为空"})
            continue

        try:
            client = OKXClient(
                api_key=api_key,
                secret_key=str(raw.get("secret_key") or ""),
                passphrase=str(raw.get("passphrase") or ""),
                env=env, proxy_url=proxy, timeout=20,
            )
            bal = client.get_balance("USDT")
            results.append({"name": name, "env": env, "enabled": enabled,
                            "status": "OK", "detail": f"余额 {bal:.2f} USDT"})
        except Exception as e:
            status, detail = _classify(e)
            results.append({"name": name, "env": env, "enabled": enabled,
                            "status": status, "detail": detail})

    if logger:
        _log_results(results, logger)
    return results


def _log_results(results: list[dict], logger) -> None:
    bad = [r for r in results if r["status"] == "KEY_INVALID"]
    warn = [r for r in results if r["status"] in ("NETWORK", "UNKNOWN", "NO_KEY")]
    ok = [r for r in results if r["status"] == "OK"]

    logger.info(
        f"[apikey-check] ===== API Key 周检 "
        f"({datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC) ====="
    )
    for r in results:
        tag = "启用" if r["enabled"] else "停用"
        line = (f"[apikey-check] [{r['status']}] {r['name']} "
                f"({r['env']}/{tag}): {r['detail']}")
        if r["status"] == "KEY_INVALID":
            logger.error(line)
        elif r["status"] == "OK":
            logger.info(line)
        else:
            logger.warning(line)

    if bad:
        names = ", ".join(r["name"] for r in bad)
        logger.error(
            f"[apikey-check] ⚠ {len(bad)} 个账户的 API Key 已失效/过期: {names} "
            f"—— 这些账户无法挂单/平仓/对账, 需去 OKX 重新创建 Key 并更新 config.yaml"
        )
    logger.info(
        f"[apikey-check] ===== 完成: 正常 {len(ok)} / 失效 {len(bad)} / "
        f"待查 {len(warn)} ====="
    )


def notify_if_invalid(results: list[dict], config: dict, logger=None) -> None:
    """有 key 失效时发 webhook 告警(webhook 未配置则跳过)。"""
    bad = [r for r in results if r["status"] == "KEY_INVALID"]
    if not bad:
        return
    wh = ((config.get("system") or {}).get("webhook") or {})
    if not wh.get("enabled") or not wh.get("url"):
        return
    try:
        from core.notifier import Notifier
        n = Notifier(str(wh["url"]), str(wh.get("channel") or "generic"),
                     int(wh.get("timeout") or 5))
        detail = "\n".join(
            f"- {r['name']} ({r['env']}): {r['detail']}" for r in bad)
        n.send(
            title="API Key 失效告警",
            message=(f"{len(bad)} 个账户的 OKX API Key 已失效或过期, "
                     f"无法挂单/平仓/对账:\n{detail}\n\n"
                     f"处理: 去 OKX 重建 API Key 并更新 config.yaml 后重启。"),
            level="CRITICAL",
        )
    except Exception as e:
        if logger:
            logger.warning(f"[apikey-check] webhook 告警发送失败: {e}")


def run_weekly_check(config: dict, logger=None) -> list[dict]:
    """调度器入口: 探活 + 日志 + 告警。"""
    results = check_all_api_keys(logger=logger, config=config)
    notify_if_invalid(results, config, logger=logger)
    return results
