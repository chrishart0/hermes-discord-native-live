from __future__ import annotations

import asyncio
import dataclasses
import types

import pytest

from conftest import Event, Source
from discord_native_live.native import current_job, owner


async def finished(host):
    if host.adapter._background_tasks:
        await asyncio.wait_for(asyncio.gather(*list(host.adapter._background_tasks)), 2)


async def test_b_finishes_while_a_is_held_in_native_admission(host):
    """Not two free-standing mock tasks: submit -> actual native admission/guards
    -> native-owned processing -> runner observer -> completion observer."""
    gate = host.gateway.barriers["A"] = asyncio.Event()
    a = await host.native.submit(host.voice, "delegation-A", "A", "context-A")
    await asyncio.sleep(0)
    assert a.status == "running"
    a_key = host.adapter._event_session_key(Event("A", a.source))
    a_task = host.adapter._session_tasks[a_key]
    b = await host.native.submit(host.voice, "delegation-B", "B", "context-B")
    b_key = host.adapter._event_session_key(Event("B", b.source))
    await asyncio.wait_for(host.adapter._session_tasks[b_key], 1)
    assert a_key != b_key and a.id != b.id
    assert a.status == "running" and not a_task.done() and not a_task.cancelled()
    assert host.gateway.finished == ["B"]
    assert [(j.delegation, j.result) for j in host.notices] == [("delegation-B", "answer:B")]
    assert host.adapter.queued == []
    gate.set()
    await finished(host)
    assert [(j.delegation, j.result) for j in host.notices] == [("delegation-B", "answer:B"), ("delegation-A", "answer:A")]
    assert len(host.gateway.calls) == 2


async def test_same_native_session_really_queues(host):
    """Negative control: this harness would detect accidentally reusing A's key."""
    gate = host.gateway.barriers["A"] = asyncio.Event()
    a = await host.native.submit(host.voice, "a", "A", "")
    event = Event("B", a.source, allow_gateway_control=False)
    await host.adapter.handle_message(event)
    assert not event._gateway_accepted
    assert host.adapter.queued == [event]
    gate.set()
    await finished(host)


async def test_model_is_pinned_without_credentials_and_provenance_survives(host):
    job = await host.native.submit(host.voice, "a", "test", "")
    await finished(host)
    assert job.source._identity is host.voice.source._identity
    assert job.source.profile == "alpha"
    assert list(host.gateway.async_session_store.overrides.values()) == [{"model": "selected-parent-model", "provider": "custom"}]
    assert not job.source.is_bot
    assert host.adapter.text_results == [(job.id, "answer:test")]


async def test_later_or_duplicate_completion_not_announced_again(host):
    job = await host.native.submit(host.voice, "a", "test", "")
    await finished(host)
    await host.adapter.on_processing_complete(Event("test", job.source), "success")
    assert len(host.notices) == 1


async def test_foreign_user_profile_and_channel_cannot_control(host):
    gate = host.gateway.barriers["A"] = asyncio.Event()
    job = await host.native.submit(host.voice, "a", "A", "")
    for changed in [dict(user_id="99"), dict(profile="beta"), dict(chat_id="999")]:
        foreign = owner(dataclasses.replace(host.voice.source, **changed))
        with pytest.raises(PermissionError):
            await host.native.control(foreign, "cancel", job.id)
    assert host.adapter.controls == []
    gate.set()
    await finished(host)


async def test_explicit_control_uses_native_command_on_exact_thread(host):
    gate = host.gateway.barriers["A"] = asyncio.Event()
    job = await host.native.submit(host.voice, "a", "A", "")
    await host.native.control(job.origin, "steer", job.id, "Focus on tests")
    await host.native.control(job.origin, "cancel", job.id)
    assert host.adapter.controls == [("/steer Focus on tests", job.id), ("/stop", job.id)]
    assert job.stop_requested
    gate.set()
    await finished(host)


async def test_no_native_admission_is_not_success(host):
    host.adapter._message_handler = None
    with pytest.raises(RuntimeError, match="did not accept"):
        await host.native.submit(host.voice, "a", "test", "")
    assert next(iter(host.native.jobs.values())).status == "dispatch failed"


async def test_unload_does_not_cancel_native_work(host):
    gate = host.gateway.barriers["A"] = asyncio.Event()
    await host.native.submit(host.voice, "a", "A", "")
    await asyncio.sleep(0)
    host.native.unload()
    assert all(not t.cancelled() and not t.done() for t in host.adapter._background_tasks)
    gate.set()
    await finished(host)
    assert host.adapter.text_results[0][1] == "answer:A"


async def test_runner_context_is_task_scoped(host):
    seen = []
    original = host.native.gateway._run_agent
    # Validate the observer-bound ContextVar in the native worker boundary.
    async def see_context(message, context_prompt, history, source, session_id, **kwargs):
        seen.append(current_job.get().id)
        return {"final_response": "ok"}
    # A second native fixture hooks the actual callable to test thread propagation.
    host.native.unload()
    host.gateway._run_agent = see_context
    from discord_native_live.native import Native
    host.native = Native(host.gateway, host.adapter, host.notices.append)
    host.voice.native = host.native
    a = await host.native.submit(host.voice, "a", "A", "")
    b = await host.native.submit(host.voice, "b", "B", "")
    await finished(host)
    assert seen == [a.id, b.id]
    assert current_job.get() is None


async def test_native_authorization_revocation_prevents_new_work(host):
    host.gateway.authorized = False
    with pytest.raises(PermissionError):
        await host.native.submit(host.voice, "a", "test", "")
    assert not host.voice.channel.threads


async def test_capacity_never_runs_inline(host):
    host.voice.max_jobs = 2
    for word in ["A", "B"]:
        host.gateway.barriers[word] = asyncio.Event()
        await host.native.submit(host.voice, word, word, "")
    with pytest.raises(RuntimeError, match="capacity"):
        await host.native.submit(host.voice, "C", "C", "")
    assert len(host.voice.channel.threads) == 2
    for gate in host.gateway.barriers.values():
        gate.set()
    await finished(host)


async def test_second_adapter_observer_can_wrap_same_gateway(host):
    from conftest import Adapter
    from discord_native_live.native import Native
    other = Native(host.gateway, Adapter(host.gateway), lambda _: None)
    job = await host.native.submit(host.voice, 'a', 'A', '')
    await finished(host)
    assert job.result == 'answer:A'
    other.unload()


async def test_notification_error_cannot_break_native_text_completion(host):
    def fail(_):
        raise RuntimeError('observer failed')
    host.native.notify = fail
    await host.native.submit(host.voice, 'a', 'A', '')
    await finished(host)
    assert host.adapter.text_results[0][1] == 'answer:A'


async def test_unrelated_adapter_observer_preserves_owned_worker_context(host):
    from conftest import Adapter
    from discord_native_live.native import Native
    seen = []
    host.native.unload()
    async def real_worker(message, context_prompt, history, source, session_id, **kwargs):
        seen.append(current_job.get().id)
        return {"final_response": "ok"}
    host.gateway._run_agent = real_worker
    unrelated = Native(host.gateway, Adapter(host.gateway), lambda _: None)
    host.native = Native(host.gateway, host.adapter, host.notices.append)
    host.voice.native = host.native
    a = await host.native.submit(host.voice, "a", "A", "")
    await finished(host)
    assert seen == [a.id]
    assert current_job.get() is None
    host.native.unload()
    unrelated.unload()
