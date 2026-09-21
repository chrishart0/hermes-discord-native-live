"""Executable upstream admission/key excerpts, NOT a full Hermes installation.

Copyright (c) 2025 Nous Research. MIT; see THIRD_PARTY_NOTICES.md.
Source: NousResearch/hermes-agent, retrieved 2026-09-21.
base.py Git blob: 2a24934662f512b98c4748011c030c118475717d
session.py Git blob: a2be18cbbcddc0806c015cecaed3e22fde48fcbf
Selected: BasePlatformAdapter.handle_message, _start_session_processing,
_track_session_task; build_session_key plus its two key helpers.
Only annotations/comments/docstrings are reduced. Platform identity resolution,
LLM execution, Discord I/O and persistence are separately faked by the harness.
With HERMES_SOURCE set, tests load these methods directly from that checkout's
AST instead, so a changed upstream admission path fails instead of being hidden.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class Platform(Enum):
    DISCORD = "discord"
    TELEGRAM = "telegram"
    SLACK = "slack"
    WHATSAPP = "whatsapp"


def coerce_plaintext_gateway_command(event):
    # Tests submit already canonical slash commands; never aliases/plain words.
    assert event.text.startswith("/")


def _session_key_namespace(profile):
    if not profile or profile == "default":
        return "agent:main"
    return "agent:main~" if profile == "main" else f"agent:{profile}"


def _canonical_participant(source):
    participant_id = source.user_id_alt or source.user_id
    if participant_id and source.platform == Platform.WHATSAPP:
        raise AssertionError("WhatsApp is outside this fixture")
    return participant_id


def build_session_key(source, group_sessions_per_user=True, thread_sessions_per_user=False, profile=None):
    is_dm = source.chat_type == "dm"
    chat_id = source.chat_id
    if is_dm and source.platform == Platform.WHATSAPP:
        raise AssertionError("WhatsApp is outside this fixture")
    thread_id = source.thread_id or (None if is_dm else source.prospective_thread_id)
    chat_type_slot = "thread" if thread_id and not source.thread_id else source.chat_type
    if is_dm:
        isolate_user = not chat_id
    else:
        isolate_user = group_sessions_per_user and not (thread_id and not thread_sessions_per_user)
    participant_id = _canonical_participant(source) if (isolate_user or not is_dm) else None
    parts = [_session_key_namespace(profile), source.platform.value, chat_type_slot]
    if source.platform == Platform.SLACK and source.scope_id:
        parts.append(str(source.scope_id))
    if chat_id:
        parts.append(chat_id)
    user_part = [str(participant_id)] if isolate_user and participant_id else []
    thread_part = [thread_id] if thread_id else []
    parts += user_part + thread_part if is_dm else thread_part + user_part
    return ":".join(str(part) for part in parts)


class Admission:
    def _start_session_processing(self, event, session_key, *, interrupt_event=None):
        guard = interrupt_event or asyncio.Event()
        self._active_sessions[session_key] = guard
        task = asyncio.create_task(self._process_message_background(event, session_key))
        if not self._track_session_task(session_key, task):
            self._session_tasks.pop(session_key, None)
            self._release_session_guard(session_key, guard=guard)
            return False
        return True

    def _track_session_task(self, session_key, task):
        self._session_tasks[session_key] = task
        try:
            self._background_tasks.add(task)
        except TypeError:
            return False
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)
            task.add_done_callback(self._expected_cancelled_tasks.discard)
        return True

    async def handle_message(self, event):
        event._gateway_accepted = False
        if not self._message_handler:
            if not getattr(self, "_no_message_handler_logged", False):
                self._no_message_handler_logged = True
                logger.error("[%s] No gateway message handler", self.name)
            return
        if event.allow_gateway_control:
            coerce_plaintext_gateway_command(event)
        if self._drop_unresolved(event):
            return
        expected_session_key = str((event.metadata or {}).get("gateway_session_key") or "").strip()
        if (not expected_session_key and getattr(self, "_topic_recovery_fn", None) is not None
                and event.source.platform == Platform.TELEGRAM and event.source.chat_type == "dm"):
            await asyncio.to_thread(self._apply_topic_recovery, event)
        session_key = self._event_session_key(event)
        if expected_session_key and session_key != expected_session_key:
            logger.warning("Dropping internally routed event: expected session=%s derived=%s", expected_session_key, session_key)
            return
        if session_key in self._active_sessions:
            self._heal_stale_session_lock(session_key)
        if session_key in self._active_sessions:
            await self._handle_message_while_active(event, session_key)
            return
        event._gateway_accepted = self._start_session_processing(event, session_key)


def load_checkout():
    """Use real upstream method bodies when an actual checkout is supplied."""
    root = os.environ.get("HERMES_SOURCE")
    if not root:
        return
    root = Path(root)
    specs = [
        ("gateway/session.py", None, ["_session_key_namespace", "_canonical_participant", "build_session_key"]),
        ("gateway/platforms/base.py", "BasePlatformAdapter", ["handle_message", "_start_session_processing", "_track_session_task"]),
    ]
    for filename, class_name, names in specs:
        tree = ast.parse((root / filename).read_text())
        parent = tree if class_name is None else next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
        selected = [n for n in parent.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        assert {n.name for n in selected} == set(names), "Hermes admission layout changed"
        namespace = dict(globals())
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, str(root / filename), "exec"), namespace)
        for name in names:
            if class_name:
                setattr(Admission, name, namespace[name])
            else:
                globals()[name] = namespace[name]

load_checkout()
