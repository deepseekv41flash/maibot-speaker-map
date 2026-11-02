"""发言者身份对照插件。

在 Maisaka 的 Planner / Replyer 请求发出前做三件事：

1. 给 ``<message ...>`` 开标签补 ``uid``（稳定身份）和缺失的 ``group_card``；
2. 给自己发出的消息补 ``is_self_message="true"``（插件同步进历史的消息常常缺这个标记）；
3. 注入「发言者身份对照」提示块，说明同一 uid 的多个名字是同一个人、
   以及可选的“事实校正”，压过记忆/摘要里被绑错人的说法。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .speaker_map import (
    SpeakerDirectory,
    build_legend,
    collect_self_names,
    collect_uids,
    enrich_items,
    extract_bot_names,
    extract_system_text,
    insert_legend,
    items_have_legend,
)

BOT_ACCOUNT_CACHE_SECONDS = 300.0
# 单次取最近消息的内部超时：留出余量，避免整个 Hook 被宿主 5s 超时干掉。
# 超时后本轮退化为“只有规则/事实校正、没有 uid 对照表”，不会阻断聊天。
RECENT_FETCH_TIMEOUT_SECONDS = 3.5


class PluginSection(PluginConfigBase):
    """插件基础开关。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用发言者对照注入")
    config_version: str = Field(default="1.0.0", description="配置版本")


class IdentitySection(PluginConfigBase):
    """身份对照与补丁开关。"""

    __ui_label__ = "身份对照"
    __ui_icon__ = "users"
    __ui_order__ = 1

    enrich_message_tags: bool = Field(
        default=True,
        description="给 <message> 开标签补 uid，并给自己发的消息补 is_self_message",
    )
    fill_group_card: bool = Field(
        default=True,
        description="消息缺少 group_card 时，用该条消息记录里的群名片补上",
    )
    inject_legend: bool = Field(
        default=True,
        description="在请求末尾注入「发言者身份对照」提示块",
    )
    legend_position: str = Field(
        default="last_user",
        description="对照表插入位置：last_user（最后一条用户消息，省缓存）或 system（系统消息）",
    )
    max_people_in_legend: int = Field(
        default=8,
        description="身份表最多列多少人（机器人自己不计入）",
    )
    authoritative_facts: list[str] = Field(
        default_factory=list,
        description="优先于记忆/摘要的事实校正，一行一条，例如：Boss（uid=100000001）是我的管理员",
    )
    authoritative_facts_file: str = Field(
        default="local_facts.txt",
        description="数据目录下的事实校正文件（一行一条，# 开头忽略）；放不适合写进仓库的本地事实，留空则关闭",
    )


class RuntimeSection(PluginConfigBase):
    """运行参数。"""

    __ui_label__ = "运行"
    __ui_icon__ = "settings"
    __ui_order__ = 2

    recent_message_limit: int = Field(
        default=80,
        description="每次建立身份索引时读取的最近消息条数；越大 RPC 负载越大，建议 60–120",
    )
    cache_ttl_seconds: int = Field(
        default=30,
        description="同一会话身份索引的缓存秒数；越小越实时，越大越省数据库查询",
    )
    debug_log: bool = Field(
        default=False,
        description="每次注入都打一条 info 日志（排障用）",
    )


class SpeakerMapConfig(PluginConfigBase):
    """插件配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    identity: IdentitySection = Field(default_factory=IdentitySection)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)


class SpeakerMapPlugin(MaiBotPlugin):
    """发言者身份对照插件。"""

    config_model = SpeakerMapConfig

    def __init__(self) -> None:
        super().__init__()
        self._directory_cache: dict[str, tuple[float, SpeakerDirectory]] = {}
        self._bot_ids: set[str] = set()
        self._bot_ids_expire_at: float = 0.0
        self._bot_names: list[str] = []
        self._self_names: set[str] = set()
        self._local_facts: list[str] = []
        self._local_facts_mtime: float = 0.0

    # ------------------------------------------------------------------ 本地事实

    def _load_local_facts(self) -> list[str]:
        """读取数据目录下的本地事实文件；文件不在或读失败时返回空列表。"""

        filename = Path(str(self.config.identity.authoritative_facts_file or "").strip()).name
        if not filename:
            return []
        paths = getattr(self.ctx, "paths", None)
        data_dir = getattr(paths, "data_dir", None)
        if data_dir is None:
            return []

        path = data_dir / filename
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            self._local_facts = []
            self._local_facts_mtime = 0.0
            return []
        except OSError as exc:
            self.ctx.logger.debug(f"读取本地事实文件失败: {path} error={exc}")
            return self._local_facts
        if mtime == self._local_facts_mtime:
            return self._local_facts

        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            self.ctx.logger.debug(f"读取本地事实文件失败: {path} error={exc}")
            return self._local_facts

        facts = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
        self._local_facts = facts
        self._local_facts_mtime = mtime
        return facts

    def _collect_authoritative_facts(self) -> list[str]:
        """合并配置里的事实与本地事实文件，去重并保持顺序。"""

        facts: list[str] = []
        for fact in [*self.config.identity.authoritative_facts, *self._load_local_facts()]:
            normalized = str(fact).strip()
            if normalized and normalized not in facts:
                facts.append(normalized)
        return facts

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        """插件加载。"""

        self.ctx.logger.info("发言者身份对照插件已加载")

    async def on_unload(self) -> None:
        """插件卸载。"""

        self._directory_cache.clear()
        self.ctx.logger.info("发言者身份对照插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载时清空索引缓存。"""

        del scope, config_data, version
        self._directory_cache.clear()
        self._bot_ids_expire_at = 0.0

    # ------------------------------------------------------------------ Hook

    @HookHandler(
        "maisaka.planner.before_request",
        name="speaker_map_planner",
        description="Planner 请求前补发言者 uid/自我标记，并注入身份对照表",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=5000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_planner_before_request(self, **kwargs: Any) -> Any:
        """Planner 请求前处理。"""

        return await self._handle_prompt_hook(kwargs, label="planner")

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="speaker_map_replyer",
        description="Replyer 请求前补发言者 uid/自我标记，并注入身份对照表",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=5000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_replyer_before_model_request(self, **kwargs: Any) -> Any:
        """Replyer 请求前处理。"""

        return await self._handle_prompt_hook(kwargs, label="replyer")

    async def _handle_prompt_hook(self, kwargs: dict[str, Any], *, label: str) -> Any:
        if not self.config.plugin.enabled:
            return {"action": "continue", "modified_kwargs": kwargs}

        items = kwargs.get("items")
        if not isinstance(items, list) or not items:
            return {"action": "continue", "modified_kwargs": kwargs}

        enrich_enabled = bool(self.config.identity.enrich_message_tags)
        legend_enabled = bool(self.config.identity.inject_legend)
        if not enrich_enabled and not legend_enabled:
            return {"action": "continue", "modified_kwargs": kwargs}

        session_id = str(kwargs.get("session_id") or "").strip()
        directory = await self._get_directory(session_id)

        system_text = extract_system_text(items)
        names = extract_bot_names(system_text)
        for name in names:
            if name not in self._bot_names:
                self._bot_names.append(name)
        self._self_names.update(names)
        self._self_names.update(collect_self_names(items))
        bot_ids = set(self._bot_ids)

        stats: dict[str, int] = {}
        if enrich_enabled:
            stats = enrich_items(
                items,
                directory,
                bot_ids,
                self._self_names,
                fill_group_card=self.config.identity.fill_group_card,
            )

        legend_inserted = False
        if legend_enabled and not items_have_legend(items):
            used_uids = collect_uids(items, directory)
            legend = build_legend(
                directory,
                used_uids,
                bot_ids,
                self._bot_names or sorted(self._self_names),
                max_people=max(1, int(self.config.identity.max_people_in_legend)),
                authoritative_facts=self._collect_authoritative_facts(),
            )
            position = str(self.config.identity.legend_position or "last_user").strip().lower()
            if position not in {"last_user", "system"}:
                position = "last_user"
            legend_inserted = insert_legend(items, legend, position)

        changed = (
            legend_inserted
            or stats.get("uid", 0) > 0
            or stats.get("group_card", 0) > 0
            or stats.get("self", 0) > 0
        )
        if not changed:
            return {"action": "continue", "modified_kwargs": kwargs}

        kwargs["items"] = items
        if self.config.runtime.debug_log:
            self.ctx.logger.info(
                "发言者对照注入: hook=%s session=%s uid+%s card+%s self+%s legend=%s people=%s",
                label,
                session_id or "-",
                stats.get("uid", 0),
                stats.get("group_card", 0),
                stats.get("self", 0),
                legend_inserted,
                len(directory.people) if directory is not None else 0,
            )
        return {"action": "continue", "modified_kwargs": kwargs}

    # ------------------------------------------------------------------ 身份索引

    async def _get_directory(self, session_id: str) -> SpeakerDirectory | None:
        """按会话取身份索引，带短缓存。"""

        if not session_id:
            return None
        now = time.monotonic()
        cached = self._directory_cache.get(session_id)
        if cached is not None and cached[0] > now:
            return cached[1]

        await self._refresh_bot_ids()
        messages = await self._fetch_recent_messages(session_id)
        directory = SpeakerDirectory.from_recent(messages)
        ttl = max(1, int(self.config.runtime.cache_ttl_seconds))
        self._directory_cache[session_id] = (now + ttl, directory)
        return directory

    async def _fetch_recent_messages(self, session_id: str) -> list[Any]:
        """读取会话最近消息；失败时返回空列表，不打断聊天。"""

        limit = max(20, int(self.config.runtime.recent_message_limit))
        try:
            result = await asyncio.wait_for(
                self.ctx.message.get_recent(session_id, limit=limit),
                timeout=RECENT_FETCH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            self.ctx.logger.warning(
                f"读取最近消息超时（>{RECENT_FETCH_TIMEOUT_SECONDS}s），本轮只注入规则与事实校正: session={session_id} limit={limit}"
            )
            return []
        except Exception as exc:  # noqa: BLE001 - 宿主能力失败不应影响聊天
            self.ctx.logger.debug(f"读取最近消息失败: session={session_id} error={exc}")
            return []

        if isinstance(result, dict):
            if result.get("success") is False:
                self.ctx.logger.debug(f"读取最近消息失败: session={session_id} error={result.get('error')}")
                return []
            messages = result.get("messages")
            return messages if isinstance(messages, list) else []
        if isinstance(result, list):
            return result
        return []

    async def _refresh_bot_ids(self) -> None:
        """读取机器人自己的平台账号，用于判定 is_self_message。"""

        now = time.monotonic()
        if self._bot_ids and now < self._bot_ids_expire_at:
            return
        try:
            rows = await self.ctx.db.get("BotPlatformAccount", filters={}, limit=200)
        except Exception as exc:  # noqa: BLE001 - 失败时退回按名字判定
            self.ctx.logger.debug(f"读取机器人账号失败: error={exc}")
            self._bot_ids_expire_at = now + 60.0
            return

        if isinstance(rows, dict):
            rows = rows.get("result")
        if not isinstance(rows, list):
            self._bot_ids_expire_at = now + 60.0
            return

        discovered: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or row.get("disabled"):
                continue
            account_id = str(row.get("account_id") or "").strip()
            if account_id:
                discovered.add(account_id)
        if discovered:
            self._bot_ids = discovered
        self._bot_ids_expire_at = now + BOT_ACCOUNT_CACHE_SECONDS

    # ------------------------------------------------------------------ 命令

    @Command(
        "speaker",
        description="查看当前会话识别到的发言者身份对照",
        pattern=r"^/speaker$",
    )
    async def cmd_speaker(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """输出当前身份索引，方便排障。"""

        del kwargs
        session_id = str(stream_id or "").strip()
        if not session_id:
            return False, "缺少会话 ID", 2

        self._directory_cache.pop(session_id, None)
        directory = await self._get_directory(session_id)
        if directory is None or not directory.people:
            text = "当前会话没有拿到身份索引（最近消息为空或读取失败）。"
        else:
            lines = [f"发言者身份索引（{len(directory.people)} 人）:"]
            for person in sorted(directory.people.values(), key=lambda p: p.last_seen, reverse=True)[:20]:
                marker = "（我自己）" if person.uid in self._bot_ids else ""
                aliases = person.aliases()
                detail = f"uid={person.uid} 昵称={person.nickname or '-'} 名片={person.card or '-'}{marker}"
                if aliases:
                    detail += f" 曾用={'、'.join(aliases)}"
                lines.append("- " + detail)
            text = "\n".join(lines)

        await self.ctx.send.text(text, session_id)
        return True, text, 2


def create_plugin() -> SpeakerMapPlugin:
    """宿主按这个名字找工厂函数。"""

    return SpeakerMapPlugin()
