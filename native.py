"""Adapt native Hermes task threads to voice requests and per-turn updates."""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gateway.session import SessionSource

logger = logging.getLogger(__name__)
MAX_TRACKED_TASKS = 128


@dataclass(frozen=True)
class TaskOwner:
    profile: str
    guild_id: str
    channel_id: str
    user_id: str

    @classmethod
    def from_source(cls, source: SessionSource) -> TaskOwner:
        return cls(
            source.profile or "default",
            str(source.scope_id or source.guild_id),
            str(source.parent_chat_id or source.chat_id),
            str(source.user_id),
        )


@dataclass(frozen=True)
class TaskUpdate:
    """Copy at turn completion; queued delivery never reads changing task state."""

    update_id: str
    delegation_id: str
    thread_id: str
    request: str
    text: str
    turn_status: str


@dataclass
class VoiceTask:
    thread_id: str
    delegation_id: str
    request: str
    source: SessionSource
    owner: TaskOwner
    publish: Callable[[TaskUpdate], None] | None
    session_key: str = ""
    turn_status: str = "submitted"
    stop_requested: bool = False
    revision: int = 0
    updates: deque[TaskUpdate] = field(default_factory=deque)


# Propagates into Hermes's worker thread, not into unrelated adapter turns.
current_work: ContextVar[tuple[HermesTaskBridge, TaskOwner] | None] = ContextVar(
    "discord_live_work", default=None
)


def authorized(gateway, adapter, source: SessionSource, *, voice: bool = False) -> bool:
    if source.is_bot or not source.user_id:
        return False
    with gateway._profile_scope_for_source(source):
        if not gateway._is_user_authorized_for_source(source, allow_adapter_delegation=False):
            return False
        if voice:
            guild = adapter._client.get_guild(int(source.scope_id or source.guild_id))
            return bool(guild and adapter._is_allowed_user(
                str(source.user_id), guild=guild, is_dm=False
            ))
        return True


class HermesTaskBridge:
    def __init__(self, gateway, adapter):
        self.gateway = gateway
        self.adapter = adapter
        self.tasks: dict[str, VoiceTask] = {}
        self.pending_admissions = 0
        self.attached = False
        self.patches = []

    def check(self) -> None:
        from tools.async_delegation import has_live_for_session

        required = (
            (self.gateway, ("_run_agent", "_profile_scope_for_source",
                            "_is_user_authorized_for_source", "_resolve_session_agent_runtime")),
            (self.adapter, ("handle_message", "on_processing_complete", "_is_allowed_user",
                            "join_voice_channel", "leave_voice_channel", "_reset_voice_timeout",
                            "_voice_timeout_limit", "play_in_voice_channel", "get_user_voice_channel")),
        )
        for target, names in required:
            for name in names:
                if not callable(getattr(target, name, None)):
                    raise RuntimeError(f"Unsupported Hermes host: missing {name}")
        if not callable(has_live_for_session):
            raise RuntimeError("Hermes background-work status is unavailable")
        if "source" not in inspect.signature(self.gateway._run_agent).parameters:
            raise RuntimeError("Unsupported Hermes _run_agent signature")

    def authorized(self, source, *, voice=False) -> bool:
        return authorized(self.gateway, self.adapter, source, voice=voice)

    def _patch(self, target, name, replacement) -> None:
        self.patches.append((target, name, getattr(target, name), replacement))
        setattr(target, name, replacement)

    def attach(self, voice_active: Callable[[int], bool]) -> None:
        """Install instance observers once, when a voice call is started."""
        if self.attached:
            return
        self.check()
        run = self.gateway._run_agent
        signature = inspect.signature(run)

        @wraps(run)
        async def observed_run(*args, **kwargs):
            source = signature.bind(*args, **kwargs).arguments.get("source")
            task = self.task_for(source) if self.attached else None
            if task is None:
                return await run(*args, **kwargs)
            token = current_work.set((self, task.owner))
            task.turn_status = "running"
            try:
                result = await run(*args, **kwargs)
                if isinstance(result, dict):
                    status = "failed" if result.get("failed") or result.get("error") else "idle"
                    if result.get("interrupted"):
                        status = "interrupted"
                    self._record_turn(task, str(result.get("final_response") or ""), status)
                else:
                    self._record_turn(task, "", "idle")
                return result
            except asyncio.CancelledError:
                self._record_turn(task, "The Hermes turn was interrupted.", "interrupted")
                raise
            except Exception:
                self._record_turn(task, "The Hermes turn failed; check its text thread.", "failed")
                raise
            finally:
                current_work.reset(token)

        complete = self.adapter.on_processing_complete

        @wraps(complete)
        async def observed_complete(event, outcome):
            try:
                return await complete(event, outcome)
            finally:
                task = self.task_for(event.source) if self.attached else None
                if task is not None:
                    # A rejected turn may never reach _run_agent. Do not leave its slot reserved.
                    if task.turn_status == "submitted":
                        self._record_turn(task, "Hermes did not run this request; check its text thread.", "failed")
                    while task.updates:
                        update = task.updates.popleft()
                        task.turn_status = update.turn_status
                        if task.publish is not None:
                            try:
                                task.publish(update)
                            except Exception:
                                logger.error("Voice update callback failed for thread %s", task.thread_id)

        play = self.adapter.play_in_voice_channel

        @wraps(play)
        async def play_unless_live(guild_id, audio_path):
            if self.attached and voice_active(guild_id):
                return False
            return await play(guild_id, audio_path)

        self._patch(self.gateway, "_run_agent", observed_run)
        self._patch(self.adapter, "on_processing_complete", observed_complete)
        self._patch(self.adapter, "play_in_voice_channel", play_unless_live)
        self.attached = True

    @staticmethod
    def _record_turn(task: VoiceTask, text: str, status: str) -> None:
        task.revision += 1
        task.updates.append(TaskUpdate(
            f"{task.thread_id}:{task.revision}", task.delegation_id, task.thread_id,
            task.request, text, status,
        ))

    def task_for(self, source) -> VoiceTask | None:
        if source is None:
            return None
        task = self.tasks.get(str(source.thread_id or ""))
        return task if task and TaskOwner.from_source(source) == task.owner else None

    @staticmethod
    def background_active(task: VoiceTask) -> bool:
        from tools.async_delegation import has_live_for_session

        return bool(task.session_key and has_live_for_session(session_key=task.session_key))

    def active(self, task: VoiceTask) -> bool:
        return task.turn_status in {"submitted", "running"} or self.background_active(task)

    def inventory(self, owner: TaskOwner) -> list[dict[str, Any]]:
        return [
            {"thread_id": task.thread_id, "request": task.request[:100],
             "last_turn": task.turn_status, "background_work": self.background_active(task),
             "stop_requested": task.stop_requested}
            for task in self.tasks.values() if task.owner == owner
        ][-24:]

    def detach_voice(self, publish: Callable[[TaskUpdate], None]) -> None:
        """Stop voice delivery without ending any Hermes work."""
        for task in self.tasks.values():
            if task.publish == publish:
                task.publish = None

    def _prune(self) -> None:
        # Retain correlations for the entire call: an idle turn can resume later.
        for key, task in list(self.tasks.items()):
            if task.publish is None and not self.active(task):
                del self.tasks[key]

    async def submit(self, voice, delegation_id: str, prompt: str, context: str) -> VoiceTask:
        self._prune()
        occupied = self.pending_admissions + sum(self.active(task) for task in self.tasks.values())
        if occupied >= voice.max_jobs:
            raise RuntimeError("Live work capacity is full; existing jobs continue.")
        if len(self.tasks) + self.pending_admissions >= MAX_TRACKED_TASKS:
            raise RuntimeError("This call has reached its task-history limit; leave and rejoin to continue.")
        if not self.attached or voice.closing:
            raise RuntimeError("Live connection ended before task submission")
        if not self.authorized(voice.source, voice=True):
            raise PermissionError("Voice operator is no longer authorized")

        self.pending_admissions += 1
        try:
            task, thread = await self._create_task(voice, delegation_id, prompt)
            self.tasks[task.thread_id] = task
        finally:
            # Transfer the reservation into the submitted task before the next await.
            self.pending_admissions -= 1
        try:
            await self._dispatch(task, voice.source, context, thread)
        except BaseException:
            task.turn_status = "dispatch failed"
            raise
        return task

    async def _create_task(self, voice, delegation_id: str, prompt: str):
        import discord
        from gateway.session_identity import replace_source

        thread = await voice.channel.create_thread(
            name=("Voice: " + " ".join(prompt.split()))[:95],
            type=discord.ChannelType.public_thread, auto_archive_duration=1440,
            reason="Authorized Hermes live-voice request",
        )
        source = replace_source(
            voice.source, chat_id=str(thread.id), thread_id=str(thread.id),
            parent_chat_id=str(voice.channel.id), chat_type="thread", chat_name=thread.name,
            message_id=None, prospective_thread_id=None, auto_thread_created=False,
            auto_thread_initial_name=None,
        )
        if voice.closing or not self.authorized(source):
            raise RuntimeError("Voice ended or authorization changed before task admission")
        return VoiceTask(str(thread.id), delegation_id, prompt, source,
                         TaskOwner.from_source(source), voice.publish), thread

    async def _dispatch(self, task: VoiceTask, parent_source, context: str, thread) -> None:
        import discord
        from gateway.platforms.event import MessageEvent, MessageType
        from tools.voice_live import voice_live_turn_note

        with self.gateway._profile_scope_for_source(task.source):
            model, runtime = await asyncio.to_thread(
                self.gateway._resolve_session_agent_runtime, source=parent_source
            )
            entry = await self.gateway.async_session_store.get_or_create_session(task.source)
            task.session_key = entry.session_key
            override = {"model": model}
            override.update({key: runtime[key] for key in ("provider", "base_url") if runtime.get(key)})
            await self.gateway.async_session_store.set_model_override(entry.session_key, override)
            seed = await thread.send(
                "Voice request:\n" + task.request[:1750], allowed_mentions=discord.AllowedMentions.none()
            )
            task.source.message_id = str(seed.id)
            task_context = (
                context + "\n[Existing voice work: " + str(self.inventory(task.owner)) + "]\n"
                "Use discord_live_work for status, corrections, or explicit cancellation of an existing task. "
                "Do not restart it or cancel it merely because another question was asked. "
                "Ask which task when cancellation is ambiguous. Detailed output belongs in this thread."
            )
            event = MessageEvent(
                text=task.request, message_type=MessageType.TEXT, source=task.source,
                message_id=str(seed.id), user_id=task.source.user_id, user_name=task.source.user_name,
                channel_prompt=voice_live_turn_note(task_context[-9000:]),
                internal=False, allow_gateway_control=False,
            )
            if task.publish is None:
                raise RuntimeError("Voice ended before task admission")
            await self.adapter.handle_message(event)
            if not event._gateway_accepted:
                raise RuntimeError("Native gateway did not accept this task; check its text thread.")

    async def control(self, owner: TaskOwner, action: str, task_id: str = "", message: str = ""):
        from gateway.platforms.event import MessageEvent, MessageType
        from gateway.session_identity import replace_source

        if action == "list":
            return self.inventory(owner)
        if action not in {"cancel", "steer"}:
            raise ValueError("Use list, cancel or steer")
        task = self.tasks.get(task_id)
        if task is None or task.owner != owner:
            raise PermissionError("No task with that ID belongs to this operator and channel")
        if not self.authorized(task.source):
            raise PermissionError("Native Hermes authorization refused the task control")
        if action == "steer" and not message.strip():
            raise ValueError("Steer needs a message")
        event = MessageEvent(
            text="/stop" if action == "cancel" else "/steer " + message[:4000],
            message_type=MessageType.COMMAND,
            source=replace_source(task.source, message_id=None),
            internal=False, allow_gateway_control=True,
        )
        with self.gateway._profile_scope_for_source(task.source):
            await self.adapter.handle_message(event)
        if action == "cancel":
            task.stop_requested = True
        return {"thread_id": task.thread_id, "status": f"{action} requested"}

    def unload(self) -> None:
        self.attached = False
        for target, name, original, replacement in reversed(self.patches):
            if getattr(target, name) is replacement:
                setattr(target, name, original)
        self.patches.clear()
