"""Plugin registration, command admission and bounded connection-local routing."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict

from .native import Native, current_job, owner
from .session import Session

log = logging.getLogger(__name__)


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx
        self.tickets = OrderedDict()
        self.ticket_lock = threading.Lock()
        self.sessions = {}
        self.natives = {}
        self.tasks = set()
        self.loop = None
        self.disabled = False

    def spawn(self, coro, name):
        task = asyncio.create_task(coro, name="discord-native-live:" + name)
        self.tasks.add(task)

        def done(t):
            self.tasks.discard(t)
            if not t.cancelled() and t.exception():
                # Exception bodies can contain credentials or user text.
                log.error("Discord native live %s failed (%s)", name, type(t.exception()).__name__)

        task.add_done_callback(done)
        return task

    def capture_command(self, event=None, gateway=None, **_):
        """Runs BEFORE native auth. Store only; never join, send audio, or run work.

        A one-use opaque ticket carries the native event into the registered
        slash handler, which the gateway invokes only AFTER its own auth gates.
        This avoids ambient 'last sender' state and cross-user command races.
        """
        if (self.disabled or event is None or gateway is None
                or getattr(event.source.platform, "value", None) != "discord"
                or event.internal or not event.allow_gateway_control
                or event.get_command() != "live-discord"):
            return None
        from gateway.session_identity import replace_source
        import dataclasses

        ticket = uuid.uuid4().hex
        now = time.monotonic()
        # Copy provenance, not only the dataclass's serialized fields.
        stored = dataclasses.replace(event, source=replace_source(event.source))
        with self.ticket_lock:
            while self.tickets and (len(self.tickets) >= 128 or next(iter(self.tickets.values()))[0] < now - 60):
                self.tickets.popitem(last=False)
            self.tickets[ticket] = (now, stored, gateway, event.get_command_args())
        return {"action": "rewrite", "text": "/live-discord " + ticket}

    async def command(self, ticket: str):
        with self.ticket_lock:
            admitted = self.tickets.pop(ticket.strip(), None)
        if not admitted or time.monotonic() - admitted[0] > 60:
            return "Run /live-discord from a Hermes Discord text channel; no authenticated command context was found."
        _, event, gateway, raw = admitted
        if self.disabled:
            return "Discord native live is unloading."
        self.loop = asyncio.get_running_loop()
        try:
            adapter = gateway._delivery_adapter_for(event.source)
            # Support the actual local Discord transport, never an unrelated
            # profile adapter or a relay with no local voice connection.
            if adapter is None or not hasattr(adapter, "_voice_clients"):
                raise RuntimeError("This command needs the native Discord adapter on the gateway host")
            key = (id(gateway), id(adapter))
            native = self.natives.get(key)
            if native is None:
                native = self.natives[key] = Native(gateway, adapter, self.completed)
                play = adapter.play_in_voice_channel

                async def play_unless_live(guild_id, audio_path):
                    if (id(adapter), guild_id) in self.sessions:
                        return False  # native callers can use text/file fallback
                    return await play(guild_id, audio_path)

                native.patch(adapter, "play_in_voice_channel", play_unless_live)
            if not native.authorized(event.source, voice=True):
                raise PermissionError("This Discord user is not authorized for voice")
            command, _, args = raw.strip().partition(" ")
            command = command.lower() or "status"
            if command in {"cancel", "steer"}:
                task_id, _, message = args.strip().partition(" ")
                return json.dumps(await native.control(owner(event.source), command, task_id, message))
            guild_id = int(event.source.scope_id or event.source.guild_id)
            session_key = (id(adapter), guild_id)
            existing = self.sessions.get(session_key)
            if command == "status":
                own_session = existing and owner(existing.source) == owner(event.source)
                return json.dumps({"connected": bool(own_session and not existing.closed),
                                   "model": existing.live.model if own_session else None,
                                   "tasks": native.inventory(owner(event.source))})
            if command == "leave":
                if existing:
                    if owner(existing.source) != owner(event.source):
                        raise PermissionError("Only the initiating operator/channel may end this live call")
                    await existing.close()
                return "Live voice ended. Existing Hermes tasks continue in their text threads."
            if command != "join":
                return "Use /live-discord join, leave, status, cancel <thread-id>, or steer <thread-id> <text>."
            if existing:
                return "A live call is already active or starting in this server."
            with gateway._profile_scope_for_source(event.source):
                # Match the host's explicit grant for plugins that start gateway
                # turns; installing a plugin alone does not grant voice execution.
                if not self.ctx._gateway_injection_allowed():
                    raise PermissionError("Set plugins.entries.discord-native-live.allow_gateway_injection: true to enable voice work")
                from hermes_cli.config import load_config
                config = load_config()
                limit = (config.get("gateway") or {}).get("max_concurrent_sessions")
                if limit is not None and int(limit) < 2:
                    raise ValueError("Native gateway.max_concurrent_sessions must allow at least two sessions")
            import discord
            channel = adapter._client.get_channel(int(event.source.chat_id))
            if not isinstance(channel, discord.TextChannel):
                raise ValueError("Start in a normal server text channel, not a DM, forum or nested thread")
            voice_channel = await adapter.get_user_voice_channel(guild_id, event.source.user_id)
            if voice_channel is None:
                raise ValueError("Join a Discord voice channel first")
            session = Session(self, native, event, channel, voice_channel)
            self.sessions[session_key] = session  # reserve before any awaits
            try:
                await session.start()
            except BaseException:
                if self.sessions.get(session_key) is session:
                    self.sessions.pop(session_key, None)
                raise
            return (
                "GPT-Live is connected. Your voice is sent to the native configured OpenAI endpoint and billed separately. "
                "Speak after this message; use headphones. Each backend request gets a task thread. "
                "Use /live-discord leave to end audio without stopping work."
            )
        except (PermissionError, ValueError, RuntimeError) as exc:
            return str(exc)
        except Exception as exc:
            log.error("Live command failed (%s)", type(exc).__name__)
            return "Live command failed. Check host compatibility and voice dependencies; native text chat is unchanged."

    def completed(self, job):
        if not self.disabled and not job.voice.closed:
            self.spawn(self.deliver(job), "result")

    async def deliver(self, job):
        voice = job.voice
        if voice.closed:
            return
        text = job.result or f"Task {job.id} ended ({job.status}); check its text thread for details."
        if job.status in {"failed", "failure", "cancelled", "interrupted"}:
            text = f"Task {job.id} ended ({job.status}). " + text
        try:
            await voice.live.result(job.delegation, f"For your request {job.prompt[:100]!r}: {text}")
        except Exception:
            voice.live.error = "Voice result delivery failed; the native text thread retains the result."
            await voice.close()

    @staticmethod
    def work_schema():
        return {
            "name": "discord_live_work",
            "description": "List, steer or cancel existing work for THIS live-voice operator. Use for progress requests; never restart a task to get its status. Cancellation needs an explicit user request and exact thread_id.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "steer", "cancel"]},
                "thread_id": {"type": "string"}, "message": {"type": "string"},
            }, "required": ["action"], "additionalProperties": False},
        }

    def work_tool(self, args, **_):
        job = current_job.get()
        if self.disabled or job is None or self.loop is None:
            return json.dumps({"error": "Not executing in a task owned by this live-voice plugin"})
        # Native synchronous tools run on its worker thread. Refuse the loop
        # thread rather than deadlock run_coroutine_threadsafe().result().
        try:
            if asyncio.get_running_loop() is self.loop:
                return json.dumps({"error": "This control must run in the native agent worker thread"})
        except RuntimeError:
            pass
        native = job.voice.native
        future = asyncio.run_coroutine_threadsafe(
            native.control(job.origin, args.get("action", ""), args.get("thread_id", ""), args.get("message", "")),
            self.loop,
        )
        try:
            return json.dumps({"result": future.result(timeout=10)})
        except Exception as exc:
            # A timeout is an UNKNOWN control outcome, not permission to retry a
            # cancel/steer automatically. Never cancel the backend worker here.
            return json.dumps({"error": f"Control outcome unconfirmed ({type(exc).__name__}); check native task status before retrying"})

    def unload(self):
        self.disabled = True
        with self.ticket_lock:
            self.tickets.clear()
        for native in self.natives.values():
            native.unload()
        if self.loop is not None and not self.loop.is_closed():
            async def shutdown():
                await asyncio.gather(*(s.close() for s in list(self.sessions.values())), return_exceptions=True)
                pending = [t for t in self.tasks if t is not asyncio.current_task()]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            self.loop.call_soon_threadsafe(lambda: self.spawn(shutdown(), "shutdown"))
