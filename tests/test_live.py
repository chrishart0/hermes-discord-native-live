from __future__ import annotations

import asyncio
import base64
import types

import pytest

from discord_native_live.live import Live, append_chunks


class Socket:
    def __init__(self):
        self.events = []
        self.closed = False

    async def send_json(self, event):
        self.events.append(event)


async def test_delegation_uses_transcript_not_missing_event_text():
    calls = []
    live = Live(lambda *args: calls.append(args), lambda _: None)
    live.ws = Socket()
    for text in ["Review ", "the repo"]:
        await live.event({"type": "session.input_transcript.delta", "delta": text})
    event = {"type": "session.delegation.created", "delegation": {"id": "a", "target": "client"}}
    await live.event(event)
    await live.event(event)
    assert calls == [("a", "Review the repo", "User: Review the repo")]


async def test_results_remain_bound_when_b_finishes_first():
    live = Live(lambda *_: None, lambda _: None)
    live.ws = Socket()
    await live.result("b", "B done")
    await live.result("a", "A done")
    await live.result("a", "A duplicate")
    assert [(e["delegation_id"], e["content"]) for e in live.ws.events] == [("b", "B done"), ("a", "A done")]
    assert all(e["type"] == "session.commentary.append" for e in live.ws.events)


async def test_only_native_live_protocol_events_and_raw_pcm():
    pcm = b"\x01\0" * 480
    received = []
    live = Live(lambda *_: None, received.append)
    live.ws = Socket()
    await live.audio(pcm)
    await live.event({"type": "session.output_audio.delta", "delta": base64.b64encode(pcm).decode()})
    assert received == [pcm]
    assert live.ws.events == [{"type": "session.input_audio.append", "audio": base64.b64encode(pcm).decode()}]


async def test_quiet_progress_uses_thinking_channel():
    live = Live(lambda *_: None, lambda _: None)
    live.ws = Socket()
    await live.append("Task 12 running", "d12")
    assert live.ws.events[0]["type"] == "session.thinking.append"
    assert live.ws.events[0]["delegation_id"] == "d12"


@pytest.mark.parametrize("text", ["x" * 800, "漢字🙂" * 300, "é" * 250, "", "a b. c"])
def test_append_size_cap_includes_non_ascii(text):
    chunks = list(append_chunks(text))
    assert "".join(chunks) == text
    assert all(len(c.encode()) <= 400 for c in chunks)


async def test_vendor_errors_do_not_reflect_secrets():
    live = Live(lambda *_: None, lambda _: None)
    await live.event({"type": "error", "error": {"code": "bad_auth", "message": "SECRET"}})
    assert "SECRET" not in live.error
    assert live.finalized.is_set() and live.started.is_set()


async def test_closed_voice_drops_late_results():
    live = Live(lambda *_: None, lambda _: None)
    live.ws = Socket()
    live.closed = True
    await live.result("a", "late")
    assert not live.ws.events


async def test_two_delegations_do_not_block_receiver_on_work():
    barrier = asyncio.Event()
    entered = []
    tasks = []
    async def work(key):
        entered.append(key)
        if key == "a":
            await barrier.wait()
    live = Live(lambda key, *_: tasks.append(asyncio.create_task(work(key))), lambda _: None)
    live.ws = Socket()
    for key in ["a", "b"]:
        await live.event({"type": "session.input_transcript.delta", "delta": key})
        await live.event({"type": "session.delegation.created", "delegation": {"id": key, "target": "client"}})
    await asyncio.wait_for(tasks[1], 1)
    assert entered == ["a", "b"] and not tasks[0].done()
    barrier.set()
    await tasks[0]


async def test_new_delegation_does_not_resubmit_previous_user_request():
    calls = []
    live = Live(lambda *args: calls.append(args), lambda _: None)
    live.ws = Socket()
    for key, text in [('a', 'Audit the repository.'), ('b', 'What is the date?')]:
        await live.event({'type': 'session.input_transcript.delta', 'delta': text})
        await live.event({'type': 'session.delegation.created', 'delegation': {'id': key, 'target': 'client'}})
    assert calls[0][1] == 'Audit the repository.'
    assert calls[1][1] == 'What is the date?'
    assert 'Audit the repository.' in calls[1][2]  # history stays available as context
