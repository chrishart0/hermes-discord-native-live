"""Only Hermes integration seams. No agent, scheduler, database or approval fork.

Each request uses a real Discord thread, hence a distinct native gateway session.
Two instance-local observers capture final results; all execution and text delivery
still run through the real adapter. Compatibility assumptions are checked at join.
"""
from __future__ import annotations

import asyncio
import inspect
from functools import wraps
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

current_job: ContextVar[Any] = ContextVar("discord_live_job", default=None)


def owner(source) -> tuple:
    return (source.profile or "default", str(source.scope_id or source.guild_id),
            str(source.parent_chat_id or source.chat_id), str(source.user_id))


@dataclass
class Job:
    id: str
    delegation: str
    prompt: str
    source: Any
    origin: tuple
    voice: Any
    status: str = "submitted"
    result: str = ""
    announced: bool = False
    stop_requested: bool = False


class Native:
    def __init__(self, gateway, adapter, notify):
        self.gateway, self.adapter, self.notify = gateway, adapter, notify
        self.jobs: dict[str, Job] = {}
        self.patches = []
        self.enabled = True
        self.pending = 0
        self.check()
        self.observe()

    def check(self):
        required = {
            self.gateway: ("_run_agent", "_profile_scope_for_source", "_is_user_authorized_for_source",
                           "_resolve_session_agent_runtime"),
            self.adapter: ("handle_message", "on_processing_complete", "_is_allowed_user",
                           "join_voice_channel", "leave_voice_channel", "_reset_voice_timeout",
                           "_voice_timeout_limit", "play_in_voice_channel", "get_user_voice_channel"),
        }
        for target, names in required.items():
            for name in names:
                if not callable(getattr(target, name, None)):
                    raise RuntimeError(f"Unsupported Hermes host: missing {name}; see docs/COMPATIBILITY.md")
        if "source" not in inspect.signature(self.gateway._run_agent).parameters:
            raise RuntimeError("Unsupported Hermes _run_agent signature")

    def authorized(self, source, *, voice=False) -> bool:
        if source.is_bot or not source.user_id:
            return False
        with self.gateway._profile_scope_for_source(source):
            if not self.gateway._is_user_authorized_for_source(source, allow_adapter_delegation=False):
                return False
            if voice:
                guild = self.adapter._client.get_guild(int(source.scope_id or source.guild_id))
                return bool(guild and self.adapter._is_allowed_user(str(source.user_id), guild=guild, is_dm=False))
            return True

    def patch(self, target, name, replacement):
        original = getattr(target, name)
        self.patches.append((target, name, original, replacement))
        setattr(target, name, replacement)

    def observe(self):
        run = self.gateway._run_agent
        signature = inspect.signature(run)

        @wraps(run)
        async def observed_run(*args, **kwargs):
            source = signature.bind(*args, **kwargs).arguments.get("source")
            job = self.job_for(source) if self.enabled else None
            token = current_job.set(job) if job is not None else None
            try:
                if job:
                    job.status = "running"
                result = await run(*args, **kwargs)
                if job and isinstance(result, dict):
                    job.result = str(result.get("final_response") or "")
                    job.status = "failed" if result.get("failed") or result.get("error") else "finished"
                    if result.get("interrupted"):
                        job.status = "interrupted"
                return result  # never transform Hermes's result
            finally:
                if token is not None:
                    current_job.reset(token)

        complete = self.adapter.on_processing_complete

        @wraps(complete)
        async def observed_complete(event, outcome):
            try:
                return await complete(event, outcome)
            finally:
                job = self.job_for(event.source) if self.enabled else None
                if job and not job.announced:
                    value = getattr(outcome, "value", outcome)
                    if value in {"failure", "cancelled"}:
                        job.status = str(value)
                    elif job.status not in {"failed", "interrupted", "finished"}:
                        job.status = "finished"
                    job.announced = True
                    # Schedules a voice-only delivery; never awaits a network send
                    # inside the native completion/approval/session lifecycle.
                    try:
                        self.notify(job)
                    except Exception:
                        # Voice observation must not break native text finalization.
                        import logging
                        logging.getLogger(__name__).error("Live result observer failed; text result is unchanged")

        self.patch(self.gateway, "_run_agent", observed_run)
        self.patch(self.adapter, "on_processing_complete", observed_complete)

    def job_for(self, source):
        if source is None:
            return None
        job = self.jobs.get(str(source.thread_id or ""))
        if job and owner(source) == job.origin:
            return job
        return None

    def inventory(self, origin):
        return [{"thread_id": j.id, "request": j.prompt[:100], "status": j.status,
                 "stop_requested": j.stop_requested}
                for j in self.jobs.values() if j.origin == origin][-24:]

    def _prune(self):
        for key in list(self.jobs):
            if len(self.jobs) < 128:
                break
            if self.jobs[key].announced:
                del self.jobs[key]

    async def submit(self, voice, delegation: str, prompt: str, context: str) -> Job:
        from gateway.platforms.event import MessageEvent, MessageType
        from gateway.session_identity import replace_source
        from tools.voice_live import voice_live_turn_note
        import discord

        self._prune()
        active = sum(not j.announced for j in self.jobs.values()) + self.pending
        if active >= voice.max_jobs or len(self.jobs) >= 128:
            raise RuntimeError("Live work capacity is full; existing jobs continue. Try again after one finishes.")
        if not self.enabled or voice.closed:
            raise RuntimeError("Live connection ended before the task was submitted")
        if not self.authorized(voice.source, voice=True):
            raise PermissionError("Voice operator is no longer authorized")
        self.pending += 1
        try:
            # A real thread lets native routing, history, limits, /stop and approvals
            # work unmodified. Synthetic thread IDs would break Discord delivery.
            thread = await voice.channel.create_thread(
                name=("Voice: " + " ".join(prompt.split()))[:95],
                type=discord.ChannelType.public_thread, auto_archive_duration=1440,
                reason="Authorized Hermes live-voice request",
            )
            source = replace_source(
                voice.source, chat_id=str(thread.id), thread_id=str(thread.id),
                parent_chat_id=str(voice.channel.id), chat_type="thread",
                chat_name=thread.name, message_id=None, prospective_thread_id=None,
                auto_thread_created=False, auto_thread_initial_name=None,
            )
            if not self.authorized(source):
                raise PermissionError("Native Hermes authorization refused the task thread")
            job = Job(str(thread.id), delegation, prompt, source, owner(source), voice)
            self.jobs[job.id] = job
            try:
                with self.gateway._profile_scope_for_source(source):
                    # Pin the parent's selected native model without copying keys.
                    model, runtime = await asyncio.to_thread(
                        self.gateway._resolve_session_agent_runtime, source=voice.source)
                    entry = await self.gateway.async_session_store.get_or_create_session(source)
                    override = {"model": model}
                    override.update({k: runtime[k] for k in ("provider", "base_url") if runtime.get(k)})
                    await self.gateway.async_session_store.set_model_override(entry.session_key, override)
                    seed = await thread.send(
                        "Voice request:\n" + prompt[:1750], allowed_mentions=discord.AllowedMentions.none())
                    source.message_id = str(seed.id)
                    task_context = (
                        context + "\n[Existing voice work, authoritative thread IDs: "
                        + str(self.inventory(job.origin)) + "]\n"
                        "For status, corrections, or cancellation, use discord_live_work on the existing task; "
                        "do not restart it. Never cancel a task just because another question was asked. "
                        "Ask which task when cancellation is ambiguous. Detailed output belongs in this thread."
                    )
                    event = MessageEvent(
                        text=prompt, message_type=MessageType.TEXT, source=source,
                        message_id=str(seed.id), user_id=source.user_id, user_name=source.user_name,
                        channel_prompt=voice_live_turn_note(task_context[-9000:]),
                        internal=False, allow_gateway_control=False,
                    )
                    await self.adapter.handle_message(event)
                    if not event._gateway_accepted:
                        raise RuntimeError("Native gateway did not accept this task; its text thread is available for retry")
                return job
            except BaseException:
                job.status, job.announced = "dispatch failed", True
                raise
        finally:
            self.pending -= 1

    async def control(self, origin, action: str, task_id: str = "", message: str = ""):
        from gateway.platforms.event import MessageEvent, MessageType
        from gateway.session_identity import replace_source

        if action == "list":
            return self.inventory(origin)
        job = self.jobs.get(str(task_id))
        if not job or job.origin != origin:
            raise PermissionError("No task with that ID belongs to this voice operator and channel")
        if not self.authorized(job.source):
            raise PermissionError("Native Hermes authorization refused the task control")
        if action not in {"cancel", "steer"}:
            raise ValueError("Use list, cancel or steer")
        if job.announced:
            return {"thread_id": job.id, "status": job.status, "changed": False}
        if action == "steer" and not message.strip():
            raise ValueError("Steer needs a message")
        if action == "cancel":
            job.stop_requested = True
        event = MessageEvent(
            text="/stop" if action == "cancel" else "/steer " + message[:4000],
            message_type=MessageType.COMMAND, source=replace_source(job.source, message_id=None),
            internal=False, allow_gateway_control=True,
        )
        with self.gateway._profile_scope_for_source(job.source):
            await self.adapter.handle_message(event)
        return {"thread_id": job.id, "status": f"{action} requested"}

    def unload(self):
        self.enabled = False
        for target, name, original, replacement in reversed(self.patches):
            # Do not overwrite a newer plugin's observer. A buried wrapper is
            # inert once disabled; gateway-owned jobs are deliberately untouched.
            if getattr(target, name) is replacement:
                setattr(target, name, original)
