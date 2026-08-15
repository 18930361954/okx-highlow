"""
通知模块 - 支持 Webhook 推送告警消息

支持的通知渠道:
- 钉钉机器人
- 飞书机器人
- Slack Webhook
- 企业微信机器人
- 通用 Webhook
"""
import logging
import requests
from typing import Literal, Optional
from datetime import datetime


logger = logging.getLogger(__name__)


NotifyLevel = Literal['INFO', 'WARNING', 'ERROR', 'CRITICAL']


class Notifier:
    """统一通知接口"""

    def __init__(self, webhook_url: str, channel: str = 'generic', timeout: int = 5):
        """
        Args:
            webhook_url: Webhook 地址
            channel: 通知渠道类型 (dingtalk/feishu/slack/wecom/generic)
            timeout: 请求超时时间（秒）
        """
        self.webhook_url = webhook_url
        self.channel = channel.lower()
        self.timeout = timeout

    def send(
        self,
        title: str,
        message: str,
        level: NotifyLevel = 'INFO',
        account: Optional[str] = None,
        extra: Optional[dict] = None
    ) -> bool:
        """发送通知

        Args:
            title: 标题
            message: 消息内容
            level: 告警级别
            account: 账户名
            extra: 额外字段

        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            logger.debug("[notifier] webhook_url 为空，跳过发送")
            return False

        try:
            payload = self._build_payload(title, message, level, account, extra)
            resp = requests.post(
                self.webhook_url,
                json=payload,
                timeout=self.timeout,
                headers={'Content-Type': 'application/json'}
            )
            resp.raise_for_status()
            logger.info(f"[notifier] 通知已发送: {title}")
            return True
        except Exception as e:
            logger.error(f"[notifier] 发送失败: {e}")
            return False

    def _build_payload(
        self,
        title: str,
        message: str,
        level: NotifyLevel,
        account: Optional[str],
        extra: Optional[dict]
    ) -> dict:
        """构建不同渠道的 payload"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        level_emoji = {
            'INFO': 'ℹ️',
            'WARNING': '⚠️',
            'ERROR': '❌',
            'CRITICAL': '🚨'
        }
        emoji = level_emoji.get(level, '')

        # 钉钉机器人
        if self.channel == 'dingtalk':
            content = f"{emoji} **{title}**\n\n"
            content += f"**级别**: {level}\n"
            if account:
                content += f"**账户**: {account}\n"
            content += f"**时间**: {timestamp}\n"
            content += f"**详情**: {message}\n"
            if extra:
                content += "\n**附加信息**:\n"
                for k, v in extra.items():
                    content += f"- {k}: {v}\n"

            return {
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": content
                }
            }

        # 飞书机器人
        elif self.channel == 'feishu':
            content = f"{emoji} **{title}**\n"
            content += f"级别: {level}\n"
            if account:
                content += f"账户: {account}\n"
            content += f"时间: {timestamp}\n"
            content += f"详情: {message}\n"
            if extra:
                content += "\n附加信息:\n"
                for k, v in extra.items():
                    content += f"• {k}: {v}\n"

            return {
                "msg_type": "text",
                "content": {
                    "text": content
                }
            }

        # Slack Webhook
        elif self.channel == 'slack':
            fields = [
                {"title": "级别", "value": level, "short": True},
                {"title": "时间", "value": timestamp, "short": True}
            ]
            if account:
                fields.append({"title": "账户", "value": account, "short": True})
            if extra:
                for k, v in extra.items():
                    fields.append({"title": k, "value": str(v), "short": True})

            color_map = {
                'INFO': 'good',
                'WARNING': 'warning',
                'ERROR': 'danger',
                'CRITICAL': 'danger'
            }

            return {
                "attachments": [{
                    "color": color_map.get(level, 'good'),
                    "title": f"{emoji} {title}",
                    "text": message,
                    "fields": fields,
                    "footer": "OKX Highlow Bot",
                    "ts": int(datetime.now().timestamp())
                }]
            }

        # 企业微信机器人
        elif self.channel == 'wecom':
            content = f"{emoji} {title}\n"
            content += f"级别: {level}\n"
            if account:
                content += f"账户: {account}\n"
            content += f"时间: {timestamp}\n"
            content += f"详情: {message}\n"
            if extra:
                content += "\n附加信息:\n"
                for k, v in extra.items():
                    content += f"• {k}: {v}\n"

            return {
                "msgtype": "text",
                "text": {
                    "content": content
                }
            }

        # 通用 Webhook（JSON 格式）
        else:
            return {
                "title": title,
                "message": message,
                "level": level,
                "account": account,
                "timestamp": timestamp,
                "extra": extra or {}
            }


def send_drift_alert(
    notifier: Optional[Notifier],
    account: str,
    alert_type: str,
    severity: str,
    message: str
) -> None:
    """发送数据漂移告警

    Args:
        notifier: 通知器实例（None 时跳过）
        account: 账户名
        alert_type: 告警类型
        severity: 严重程度
        message: 消息内容
    """
    if notifier is None:
        return

    level_map = {
        'LOW': 'INFO',
        'MEDIUM': 'WARNING',
        'HIGH': 'ERROR',
        'CRITICAL': 'CRITICAL'
    }

    notifier.send(
        title=f"数据漂移告警 - {alert_type}",
        message=message,
        level=level_map.get(severity, 'WARNING'),
        account=account,
        extra={
            'alert_type': alert_type,
            'severity': severity
        }
    )


def send_balance_sync_alert(
    notifier: Optional[Notifier],
    account: str,
    db_balance: float,
    okx_balance: float,
    diff: float
) -> None:
    """发送余额同步告警

    Args:
        notifier: 通知器实例（None 时跳过）
        account: 账户名
        db_balance: 数据库余额
        okx_balance: OKX 实际余额
        diff: 差值
    """
    if notifier is None:
        return

    severity = 'HIGH' if abs(diff) > 50 else 'MEDIUM'
    level = 'ERROR' if severity == 'HIGH' else 'WARNING'

    notifier.send(
        title=f"余额偏差告警 - {account}",
        message=f"检测到余额偏差 {diff:+.2f} USDT",
        level=level,
        account=account,
        extra={
            'db_balance': f"{db_balance:.2f} USDT",
            'okx_balance': f"{okx_balance:.2f} USDT",
            'diff': f"{diff:+.2f} USDT",
            'severity': severity
        }
    )
