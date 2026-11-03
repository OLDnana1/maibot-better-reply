"""Better Reply：为每个回复目标的第一条文字消息强制添加引用。

MaiBot 1.3.19 的引用由 ``src/maisaka/quote/rules.py`` 在回复生成后按公式
随机抽签，Planner / Replyer 的提示词无法决定。本插件挂在
``send_service.before_send``，在真正发送前覆盖 ``set_reply``：

- 同一个（会话，目标消息）在去重窗口内的第一条文字消息 → 强制引用；
- 同一目标的后续分段 → 强制不引用，避免 A1/A2/A3 每条都带引用；
- 纯表情、纯图片、纯语音消息不占用“首次引用”名额；
- 目标消息换成另一个人的消息（新的 msg_id）时，重新引用。

插件不修改核心的概率公式，也不改配置；它只是在发送前把核心算出的最终
布尔值覆盖掉。核心仍会在监控里记录 ``quote_probability``，但实际发送以
本插件为准。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode


class PluginSectionConfig(PluginConfigBase):
    """插件基础开关。"""

    __ui_label__ = "插件开关"
    __ui_icon__ = "toggle-left"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件", "x-widget": "switch"},
    )
    config_version: str = Field(
        default="1.0.0",
        description="配置版本号",
        json_schema_extra={"label": "配置版本"},
    )


class QuoteSectionConfig(PluginConfigBase):
    """首条引用配置。"""

    __ui_label__ = "首条引用"
    __ui_icon__ = "message-square-quote"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True,
        description="关闭后不干预核心的引用抽签",
        json_schema_extra={"label": "启用首条引用", "x-widget": "switch"},
    )
    ttl_seconds: int = Field(
        default=300,
        ge=1,
        le=86400,
        description="同一个目标消息的滑动去重窗口：连续分段只有第一条引用；停止发送超过该窗口后再次回复会重新引用",
        json_schema_extra={"label": "去重窗口（秒）", "x-widget": "input"},
    )
    max_entries: int = Field(
        default=1000,
        ge=10,
        le=10000,
        description="内存中最多保留多少个目标消息的去重记录",
        json_schema_extra={"label": "最大记录数", "x-widget": "input"},
    )
    platforms: List[str] = Field(
        default_factory=lambda: ["qq"],
        description="生效平台；留空表示所有平台",
        json_schema_extra={"label": "生效平台"},
    )
    only_group: bool = Field(
        default=False,
        description="只处理群聊消息；关闭时私聊也生效",
        json_schema_extra={"label": "仅群聊", "x-widget": "switch"},
    )
    force_no_quote_after_first: bool = Field(
        default=True,
        description="同一目标的后续文字消息强制不引用，覆盖核心的随机结果",
        json_schema_extra={"label": "后续分段不引用", "x-widget": "switch"},
    )
    log_decisions: bool = Field(
        default=True,
        description="在插件日志里记录每次判定",
        json_schema_extra={"label": "记录判定日志", "x-widget": "switch"},
    )


class BetterReplyConfig(PluginConfigBase):
    """Better Reply 插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    quote: QuoteSectionConfig = Field(default_factory=QuoteSectionConfig)


class BetterReplyPlugin(MaiBotPlugin):
    """按目标消息强制首条引用的插件。"""

    config_model = BetterReplyConfig

    def __init__(self) -> None:
        super().__init__()
        self._quoted: Dict[Tuple[str, str], float] = {}
        self._lock: Optional[asyncio.Lock] = None

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        """加载时初始化去重状态。"""

        self._quoted.clear()
        self._lock = asyncio.Lock()
        self.ctx.logger.info(
            "[BetterReply] 已加载：首条引用=%s，去重窗口=%ss，平台=%s",
            "开" if self.config.quote.enabled else "关",
            self.config.quote.ttl_seconds,
            self.config.quote.platforms or "全部",
        )

    async def on_unload(self) -> None:
        """卸载时清空状态。"""

        self._quoted.clear()
        self._lock = None

    async def on_config_update(self, scope: str, config_data: Dict[str, object], version: str) -> None:
        """配置热更新后清空状态，避免旧窗口影响新配置。"""

        del scope, config_data, version
        self._quoted.clear()

    # ===== 发送 Hook =====

    @HookHandler(
        "send_service.before_send",
        name="first_quote",
        description="为每个回复目标的第一条文字消息强制添加引用，并让后续分段不引用",
        mode=HookMode.BLOCKING,
        timeout_ms=3000,
    )
    async def force_first_quote(self, **kwargs: Any) -> Dict[str, Any]:
        """在发送前覆盖核心的 set_reply 结果。"""

        try:
            plugin_cfg = self.config.plugin
            cfg = self.config.quote
        except RuntimeError:
            self.ctx.logger.warning("[BetterReply] 配置尚未注入，跳过本次发送")
            return {"action": "continue", "modified_kwargs": kwargs}

        if not plugin_cfg.enabled or not cfg.enabled:
            return {"action": "continue", "modified_kwargs": kwargs}

        message = kwargs.get("message")
        if not isinstance(message, dict):
            return {"action": "continue", "modified_kwargs": kwargs}

        platform = str(message.get("platform") or "").strip().lower()
        platforms = {
            str(item).strip().lower()
            for item in (cfg.platforms or [])
            if str(item).strip()
        }
        if platforms and platform not in platforms:
            return {"action": "continue", "modified_kwargs": kwargs}

        if message.get("is_notify") or message.get("is_command"):
            return {"action": "continue", "modified_kwargs": kwargs}

        if cfg.only_group and not (message.get("message_info") or {}).get("group_info"):
            return {"action": "continue", "modified_kwargs": kwargs}

        target = str(kwargs.get("reply_message_id") or message.get("reply_to") or "").strip()
        if not target:
            return {"action": "continue", "modified_kwargs": kwargs}

        if not self._has_text_segment(message):
            return {"action": "continue", "modified_kwargs": kwargs}

        session_id = str(message.get("session_id") or "")
        key = (session_id, target)
        now = time.monotonic()

        async with self._get_lock():
            previous = self._quoted.get(key)
            first = previous is None or (now - previous) >= float(cfg.ttl_seconds)
            # 每条消息都刷新时间，连续分段会共享同一个窗口。
            self._quoted[key] = now
            self._prune_locked(now, cfg.ttl_seconds, cfg.max_entries)

        if first:
            kwargs["set_reply"] = True
            kwargs["reply_message_id"] = target
        elif cfg.force_no_quote_after_first:
            kwargs["set_reply"] = False

        if cfg.log_decisions:
            self.ctx.logger.info(
                "[BetterReply] session=%s target=%s first=%s set_reply=%s",
                session_id, target, first, kwargs.get("set_reply"),
            )
        return {"action": "continue", "modified_kwargs": kwargs}

    # ===== 内部工具 =====

    def _get_lock(self) -> asyncio.Lock:
        """延迟创建锁，避免跨事件循环复用。"""

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @staticmethod
    def _has_text_segment(message: Dict[str, Any]) -> bool:
        """判断消息是否至少包含一个非空文字段。"""

        raw_segments = message.get("raw_message") or []
        if isinstance(raw_segments, dict):
            raw_segments = raw_segments.get("components") or []
        if not isinstance(raw_segments, list):
            return False
        for segment in raw_segments:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") != "text":
                continue
            if str(segment.get("data") or "").strip():
                return True
        return False

    def _prune_locked(self, now: float, ttl_seconds: int, max_entries: int) -> None:
        """清理过期记录，并在超限时删除最旧的记录。调用方需持有锁。"""

        expire_before = now - max(float(ttl_seconds) * 4.0, 60.0)
        expired = [key for key, timestamp in self._quoted.items() if timestamp < expire_before]
        for key in expired:
            self._quoted.pop(key, None)
        while len(self._quoted) > max_entries:
            oldest_key = min(self._quoted, key=lambda item: self._quoted[item])
            self._quoted.pop(oldest_key, None)


def create_plugin() -> BetterReplyPlugin:
    """创建插件实例。"""

    return BetterReplyPlugin()
