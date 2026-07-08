"""多账户运行时。

核心原则:
  - 所有账户 **共享** 同一个 DB 实例(data/trades.db)。trades/state 表按 account 列/主键区分。
  - 每账户独立: OKXClient(独立 API 三件套) + AccountState + Strategy + OrderManager + Reconciler
  - 日志走同一个 logger,但每条业务日志加 [<account>] 前缀
  - 单账户 (config 无 accounts 段) → 自动合成一个名为 'default' 的账户,行为完全等价历史 main.py

多账户配置示例见 config.yaml 的 accounts 段。
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.account_state import AccountState
from core.okx_client import OKXClient
from data.db import DB, DEFAULT_ACCOUNT
from execution.order_manager import OrderManager
from execution.reconciler import Reconciler
from strategy.high_low import HighLowStrategy


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ${VAR} 或 ${VAR:default} 占位符 — 私仓一般直接写 key,占位符是备用
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::([^}]*))?\}")


def _expand_env(s: Any) -> Any:
    """字符串里的 ${VAR} / ${VAR:default} 从 os.environ 展开;非字符串原样返回。"""
    if not isinstance(s, str):
        return s

    def _sub(m: re.Match) -> str:
        name = m.group(1)
        default = m.group(2)
        return os.getenv(name, default if default is not None else m.group(0))

    return _ENV_RE.sub(_sub, s)


def _deep_merge(base: dict, override: dict) -> dict:
    """base ← override(dict 递归合并,其它类型直接覆盖)。base 不被修改。"""
    out = copy.deepcopy(base or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


import logging as _logging


class _AccountFilter(_logging.Filter):
    """只放行消息里包含 [account_name] 前缀的日志。"""
    def __init__(self, prefix: str):
        super().__init__()
        self._marker = f"[{prefix}]"

    def filter(self, record: _logging.LogRecord) -> bool:
        try:
            return self._marker in record.getMessage()
        except Exception:
            return False


class _PrefixLogger:
    """给同一个底层 logger 加 [account] 前缀。
    每账户额外注册一个 FileHandler(带 filter)写到 logs/bot_<name>.log,
    主 bot.log 仍收全部日志。
    """

    def __init__(self, base_logger, prefix: str, keep_days: int = 30):
        self._base = base_logger
        self._prefix = prefix
        try:
            from utils.logger import get_account_file_handler
            # 避免同账户重复注册
            _cache: set[str] = getattr(base_logger, "_acc_files", set())
            if prefix not in _cache:
                handler = get_account_file_handler(prefix, keep_days=keep_days)
                handler.addFilter(_AccountFilter(prefix))
                base_logger.addHandler(handler)
                _cache.add(prefix)
                base_logger._acc_files = _cache
        except Exception:
            pass

    def _fmt(self, msg: str) -> str:
        return f"[{self._prefix}] {msg}"

    def info(self, msg: str, *a, **kw): self._base.info(self._fmt(msg), *a, **kw)
    def warning(self, msg: str, *a, **kw): self._base.warning(self._fmt(msg), *a, **kw)
    def error(self, msg: str, *a, **kw): self._base.error(self._fmt(msg), *a, **kw)
    def debug(self, msg: str, *a, **kw): self._base.debug(self._fmt(msg), *a, **kw)
    def exception(self, msg: str, *a, **kw): self._base.exception(self._fmt(msg), *a, **kw)


@dataclass
class AccountConfig:
    name: str
    enabled: bool
    env: str                                     # demo / live
    api_key: str
    secret_key: str
    passphrase: str
    pairs: list[str]
    td_mode: str
    strategy_config: dict                        # 已合并到顶层 strategy 层级
    system_config: dict
    proxy_url: str | None

    def to_legacy_config(self) -> dict:
        """AccountState/HighLowStrategy 仍吃老结构 {'strategy':..., 'system':..., 'account':...}。"""
        return {
            "strategy": self.strategy_config,
            "system": self.system_config,
            "account": {"env": self.env, "td_mode": self.td_mode},
        }


def _resolve_proxy_url(top_cfg: dict) -> str | None:
    net = top_cfg.get("network") or {}
    if not net.get("proxy_enabled"):
        return None
    url = str(net.get("proxy_url") or "").strip()
    return url or None


def _normalize_env(v: str) -> str:
    """把 real/live/prod 归一到 'live';demo/sim/simulate 归一到 'demo'。
    OKXClient 里 env != 'demo' 就走 live(不加模拟盘头)。"""
    v = (v or "").strip().lower()
    if v in ("live", "real", "prod", "production"):
        return "live"
    if v in ("demo", "sim", "simulate", "simulation"):
        return "demo"
    return v or "demo"


def _build_account_config(name: str, raw: dict, top_cfg: dict) -> AccountConfig:
    enabled = bool(raw.get("enabled", True))
    # 兼容字段名:env / env_adapt;取值 real/live/demo 都归一
    env_raw = raw.get("env") or raw.get("env_adapt") or top_cfg.get("account", {}).get("env") or "demo"
    env = _normalize_env(str(env_raw))

    api_key = str(_expand_env(raw.get("api_key", "")) or "")
    secret_key = str(_expand_env(raw.get("secret_key", "")) or "")
    passphrase = str(_expand_env(raw.get("passphrase", "")) or "")

    top_strategy = top_cfg.get("strategy") or {}
    ov_strategy = raw.get("strategy") or {}
    merged_strategy = _deep_merge(top_strategy, ov_strategy)

    pairs = raw.get("pairs") or merged_strategy.get("pairs") or []
    merged_strategy["pairs"] = list(pairs)

    td_mode = str(raw.get("td_mode") or top_cfg.get("account", {}).get("td_mode") or "cross")
    system_cfg = dict(top_cfg.get("system") or {})
    proxy_url = _resolve_proxy_url(top_cfg)

    return AccountConfig(
        name=name, enabled=enabled, env=env,
        api_key=api_key, secret_key=secret_key, passphrase=passphrase,
        pairs=pairs, td_mode=td_mode,
        strategy_config=merged_strategy, system_config=system_cfg,
        proxy_url=proxy_url,
    )


@dataclass
class AccountRuntime:
    """一个账户跑起来所需的全部对象。共享 db。"""
    cfg: AccountConfig
    okx: OKXClient
    db: DB
    account: AccountState
    strategy: HighLowStrategy
    order_manager: OrderManager
    reconciler: Reconciler
    logger: Any = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return self.cfg.name

    def reconcile_tick(self) -> None:
        try:
            n = self.reconciler.run_once()
            if n and self.logger:
                self.logger.info(f"[reconcile] settled {n} trade update(s)")
        except Exception as e:
            if self.logger:
                self.logger.error(f"[reconcile] tick failed: {e}")


def build_runtime(cfg: AccountConfig, db: DB, base_logger) -> AccountRuntime:
    """把 AccountConfig 变成 AccountRuntime。db 是外部注入的共享实例。"""
    logger = _PrefixLogger(base_logger, cfg.name)
    okx = OKXClient(
        cfg.api_key, cfg.secret_key, cfg.passphrase,
        env=cfg.env, logger=logger, proxy_url=cfg.proxy_url,
    )
    legacy_cfg = cfg.to_legacy_config()
    account = AccountState(db, legacy_cfg, logger=logger, account=cfg.name)
    strategy = HighLowStrategy(legacy_cfg, logger=logger)
    order_mgr = OrderManager(okx, db, logger=logger, td_mode=cfg.td_mode, account=cfg.name)
    reconciler = Reconciler(
        okx, db, account, legacy_cfg, logger=logger,
        strategy=strategy, order_manager=order_mgr,
        account_name=cfg.name,
    )
    return AccountRuntime(
        cfg=cfg, okx=okx, db=db, account=account, strategy=strategy,
        order_manager=order_mgr, reconciler=reconciler, logger=logger,
    )


def _synthesize_default_account(top_cfg: dict) -> AccountConfig:
    """无 accounts 段时,把顶层 strategy/account/env 合成为一个名为 'default' 的账户。
    从 .env 读 OKX_API_KEY/OKX_SECRET_KEY/OKX_PASSPHRASE。"""
    raw = {
        "name": DEFAULT_ACCOUNT,
        "enabled": True,
        "env": top_cfg.get("account", {}).get("env", "demo"),
        "api_key": os.getenv("OKX_API_KEY", ""),
        "secret_key": os.getenv("OKX_SECRET_KEY", ""),
        "passphrase": os.getenv("OKX_PASSPHRASE", ""),
        "pairs": (top_cfg.get("strategy") or {}).get("pairs") or [],
        "td_mode": top_cfg.get("account", {}).get("td_mode", "cross"),
    }
    return _build_account_config(DEFAULT_ACCOUNT, raw, top_cfg)


def load_accounts(top_cfg: dict, db: DB, base_logger) -> list[AccountRuntime]:
    """从顶层 config 构建每个账户的 runtime。
    - 有 accounts 段: 逐个建;api_key 空/enabled=False 的跳过
    - 无 accounts 段(或空): 从顶层 strategy/.env 合成一个 'default' 账户(向后兼容)
    """
    accounts_raw = top_cfg.get("accounts") or []
    if not accounts_raw:
        default_cfg = _synthesize_default_account(top_cfg)
        if not default_cfg.api_key:
            raise ValueError(
                "无 accounts 段且 .env 里 OKX_API_KEY 为空,无法启动"
            )
        return [build_runtime(default_cfg, db, base_logger)]

    runtimes: list[AccountRuntime] = []
    seen: set[str] = set()
    for i, raw in enumerate(accounts_raw):
        # 兼容 name / account_name 两种键名
        name = str(raw.get("name") or raw.get("account_name") or f"acc{i}")
        if name in seen:
            raise ValueError(f"duplicate account name: {name}")
        seen.add(name)
        cfg = _build_account_config(name, raw, top_cfg)
        if not cfg.enabled:
            base_logger.info(f"[{name}] 已禁用 (enabled=false),跳过")
            continue
        if not cfg.api_key:
            base_logger.warning(f"[{name}] api_key 为空,跳过")
            continue
        runtimes.append(build_runtime(cfg, db, base_logger))
    return runtimes
