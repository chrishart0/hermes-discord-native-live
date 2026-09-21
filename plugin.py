"""Register commands and route authenticated Discord requests to voice calls."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass

from .native import HermesTaskBridge, TaskOwner, authorized, current_work
from .session import DiscordVoiceSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingCommand:
    created_at: float
    event: object
    gateway: object
    args: str


class DiscordLivePlugin:
    def __init__(self, ctx):
        self.ctx = ctx
        self.tickets: OrderedDict[str, PendingCommand] = OrderedDict()
        self.ticket_lock = threading.Lock()
        self.sessions: dict[tuple[int, int], DiscordVoiceSession] = {}
        self.bridges: dict[tuple[int, int], HermesTaskBridge] = {}
        self.tasks: set[asyncio.Task] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.disabled = False

    def spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name="discord-native-live:" + name)
        self.tasks.add(task)

        def done(completed):
            self.tasks.discard(completed)
            if not completed.cancelled() and completed.exception():
                logger.error("Discord Live %s failed (%s)", name, type(completed.exception()).__name__)

        task.add_done_callback(done)
        return task

    def capture_command(self, event=None, gateway=None, **_):
        """Carry per-request context through the host's raw-args-only command API.

        This hook runs before command authorization. Record context only; the
        registered handler runs after native authorization and consumes it once.
        """
        if (self.disabled or event is None or gateway is None
                or getattr(event.source.platform, "value", None) != "discord"
                or event.internal or not event.allow_gateway_control
                or event.get_command() != "live-discord"):
            return None
        from gateway.session_identity import replace_source

        ticket = uuid.uuid4().hex
        now = time.monotonic()
        stored = dataclasses.replace(event, source=replace_source(event.source))
        with self.ticket_lock:
            while self.tickets and (
                len(self.tickets) >= 128 or next(iter(self.tickets.values())).created_at < now - 60
            ):
                self.tickets.popitem(last=False)
            self.tickets[ticket] = PendingCommand(now, stored, gateway, event.get_command_args())
        return {"action": "rewrite", "text": "/live-discord " + ticket}

    async def command(self, ticket: str) -> str:
        with self.ticket_lock:
            request = self.tickets.pop(ticket.strip(), None)
        if request is None or time.monotonic() - request.created_at > 60:
            return "Run /live-discord in a Hermes Discord text channel; no authenticated command context was found."
        if self.disabled:
            return "Discord Live is unloading."
        self.loop = asyncio.get_running_loop()
        try:
            source = request.event.source
            adapter = request.gateway._delivery_adapter_for(source)
            if adapter is None or not hasattr(adapter, "_voice_clients"):
                raise RuntimeError("This command needs the native Discord adapter on the gateway host")
            if not authorized(request.gateway, adapter, source, voice=True):
                raise PermissionError("This Discord user is not authorized for voice")
            action, _, args = request.args.strip().partition(" ")
            handlers = {"join": self.join, "leave": self.leave, "status": self.status,
                        "cancel": self.control, "steer": self.control}
            handler = handlers.get(action.lower() or "status")
            if handler is None:
                return "Use /live-discord join, leave, status, cancel <thread-id>, or steer <thread-id> <text>."
            return await handler(request, adapter, action.lower(), args)
        except (PermissionError, ValueError, RuntimeError) as exc:
            return str(exc)
        except Exception as exc:
            logger.error("Discord Live command failed (%s)", type(exc).__name__)
            return "Live command failed. Check host compatibility and voice dependencies; text chat is unchanged."

    def _session(self, request, adapter) -> DiscordVoiceSession | None:
        owner = TaskOwner.from_source(request.event.source)
        session = self.sessions.get((id(adapter), int(owner.guild_id)))
        if session and TaskOwner.from_source(session.source) != owner:
            raise PermissionError("This live call belongs to another operator or text channel")
        return session

    def _bridge(self, request, adapter) -> HermesTaskBridge | None:
        return self.bridges.get((id(request.gateway), id(adapter)))

    async def status(self, request, adapter, action, args) -> str:
        session = self._session(request, adapter)
        bridge = self._bridge(request, adapter)
        return json.dumps({
            "connected": bool(session and not session.closing),
            "model": session.live.model if session else None,
            "tasks": bridge.inventory(TaskOwner.from_source(request.event.source)) if bridge else [],
        })

    async def leave(self, request, adapter, action, args) -> str:
        session = self._session(request, adapter)
        if session is not None:
            await session.close()
        return "Live voice ended. Existing Hermes tasks continue in their text threads."

    async def control(self, request, adapter, action, args) -> str:
        bridge = self._bridge(request, adapter)
        if bridge is None:
            return "There are no live-voice tasks from this gateway session."
        task_id, _, message = args.strip().partition(" ")
        return json.dumps(await bridge.control(
            TaskOwner.from_source(request.event.source), action, task_id, message
        ))

    async def join(self, request, adapter, action, args) -> str:
        import discord
        from hermes_cli.config import load_config

        source = request.event.source
        if self._session(request, adapter) is not None:
            return "A live call is already active or starting in this server."
        with request.gateway._profile_scope_for_source(source):
            if not self.ctx._gateway_injection_allowed():
                raise PermissionError("Set plugins.entries.discord-native-live.allow_gateway_injection: true to enable voice work")
            limit = (load_config().get("gateway") or {}).get("max_concurrent_sessions")
            if limit is not None and int(limit) < 2:
                raise ValueError("Native gateway.max_concurrent_sessions must allow at least two sessions")
        channel = adapter._client.get_channel(int(source.chat_id))
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Start in a normal server text channel, not a DM, forum or nested thread")
        voice_channel = await adapter.get_user_voice_channel(
            int(source.scope_id or source.guild_id), source.user_id
        )
        if voice_channel is None:
            raise ValueError("Join a Discord voice channel first")
        # Another join can complete while Discord resolves the user's channel.
        if self._session(request, adapter) is not None:
            return "A live call is already active or starting in this server."
        key = (id(request.gateway), id(adapter))
        bridge = self.bridges.get(key)
        if bridge is None:
            bridge = self.bridges[key] = HermesTaskBridge(request.gateway, adapter)
        bridge.attach(lambda guild_id: (id(adapter), guild_id) in self.sessions)
        session = DiscordVoiceSession(self, bridge, request.event, channel, voice_channel)
        self.sessions[session.key] = session
        try:
            await session.start()
        except BaseException:
            if self.sessions.get(session.key) is session:
                self.sessions.pop(session.key)
            raise
        return (
            "GPT-Live is connected. Your voice is sent to the native configured OpenAI endpoint "
            "and billed separately. Speak after this message; use headphones. "
            "Each backend request gets a task thread. /live-discord leave ends audio, not work."
        )

    @staticmethod
    def work_schema() -> dict:
        return {
            "name": "discord_live_work",
            "description": "List, steer or cancel existing work for this voice operator. Never restart a task to get its status. Cancellation needs an explicit user request and exact thread_id.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "steer", "cancel"]},
                "thread_id": {"type": "string"}, "message": {"type": "string"},
            }, "required": ["action"], "additionalProperties": False},
        }

    def work_tool(self, args, **_) -> str:
        context = current_work.get()
        if self.disabled or context is None or self.loop is None:
            return json.dumps({"error": "Not executing in a task owned by this voice plugin"})
        try:
            if asyncio.get_running_loop() is self.loop:
                return json.dumps({"error": "This control must run in the native agent worker thread"})
        except RuntimeError:
            pass
        bridge, owner = context
        future = asyncio.run_coroutine_threadsafe(
            bridge.control(owner, args.get("action", ""), args.get("thread_id", ""), args.get("message", "")),
            self.loop,
        )
        try:
            return json.dumps({"result": future.result(timeout=10)})
        except Exception as exc:
            # Timeout is an unknown outcome, not permission to retry a mutation.
            return json.dumps({"error": f"Control outcome unconfirmed ({type(exc).__name__}); check task status before retrying"})

    async def _shutdown(self) -> None:
        await asyncio.gather(*(session.close() for session in list(self.sessions.values())), return_exceptions=True)
        for bridge in self.bridges.values():
            bridge.unload()
        pending = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def unload(self) -> None:
        self.disabled = True
        with self.ticket_lock:
            self.tickets.clear()
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(lambda: self.spawn(self._shutdown(), "shutdown"))
        else:
            for bridge in self.bridges.values():
                bridge.unload()
