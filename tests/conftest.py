from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import sys
import types
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from hermes_snapshot import Admission, Platform, build_session_key

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("discord_native_live", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)


@dataclass
class Source:
    platform: object = Platform.DISCORD
    chat_id: str = "100"
    user_id: str = "42"
    user_id_alt: str | None = None
    user_name: str = "Operator"
    profile: str = "alpha"
    scope_id: str = "1"
    guild_id: str = "1"
    parent_chat_id: str | None = None
    thread_id: str | None = None
    chat_type: str = "channel"
    chat_name: str = "control"
    is_bot: bool = False
    message_id: str | None = None
    prospective_thread_id: str | None = None
    auto_thread_created: bool = False
    auto_thread_initial_name: str | None = None

    def to_dict(self):
        return dataclasses.asdict(self)


@dataclass
class Event:
    text: str
    source: Source = field(default_factory=Source)
    message_type: object = None
    message_id: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    channel_prompt: str | None = None
    internal: bool = False
    allow_gateway_control: bool = True
    metadata: dict = field(default_factory=dict)
    _gateway_accepted: bool = False

    def get_command(self):
        return self.text.split()[0].lstrip("/")

    def get_command_args(self):
        return self.text.partition(" ")[2]


class Channel:
    def __init__(self):
        self.id = 100
        self.next_id = 200
        self.threads = []

    async def create_thread(self, **kwargs):
        self.next_id += 1
        thread = Thread(self.next_id, kwargs["name"])
        self.threads.append(thread)
        return thread


class Thread:
    def __init__(self, id, name):
        self.id, self.name = id, name
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append(content)
        return types.SimpleNamespace(id=self.id * 10)


class Store:
    def __init__(self):
        self.overrides = {}

    async def get_or_create_session(self, source):
        return types.SimpleNamespace(session_key=build_session_key(source, profile=source.profile))

    async def set_model_override(self, key, override):
        assert "api_key" not in override
        self.overrides[key] = dict(override)


class Gateway:
    def __init__(self):
        self.async_session_store = Store()
        self.entered = {}
        self.barriers = {}
        self.finished = []
        self.authorized = True
        self.calls = []

    def _profile_scope_for_source(self, source):
        return nullcontext()

    def _is_user_authorized_for_source(self, source, **kwargs):
        return self.authorized

    def _resolve_session_agent_runtime(self, source):
        return "selected-parent-model", {"provider": "custom", "api_key": "SECRET"}

    async def _run_agent(self, message, context_prompt, history, source, session_id, **kwargs):
        assert not history
        self.calls.append((message, source.thread_id, session_id))
        self.entered.setdefault(message, asyncio.Event()).set()
        if message in self.barriers:
            await self.barriers[message].wait()
        self.finished.append(message)
        return {"final_response": "answer:" + message, "messages": []}


class Adapter(Admission):
    """Actual native admission/guards; fake network and lower agent execution."""
    def __init__(self, gateway):
        self.gateway = gateway
        self._message_handler = self.process
        self._active_sessions = {}
        self._session_tasks = {}
        self._background_tasks = set()
        self._expected_cancelled_tasks = set()
        self.controls = []
        self.text_results = []
        self.queued = []
        self.name = "Discord"
        self._client = types.SimpleNamespace(get_guild=lambda _: object())

    def _event_session_key(self, event):
        return build_session_key(event.source, profile=event.source.profile)

    def _drop_unresolved(self, event):
        return event.source.profile == "unresolved"

    def _heal_stale_session_lock(self, key):
        task = self._session_tasks.get(key)
        if task and task.done():
            self._active_sessions.pop(key, None)

    async def _handle_message_while_active(self, event, key):
        if event.allow_gateway_control:
            self.controls.append((event.text, event.source.thread_id))
        else:
            self.queued.append(event)

    async def process(self, event):
        return await self.gateway._run_agent(event.text, event.channel_prompt, [], event.source, self._event_session_key(event))

    async def _process_message_background(self, event, key):
        outcome = "success"
        try:
            result = await self._message_handler(event)
            self.text_results.append((event.source.thread_id, result["final_response"]))
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            await self.on_processing_complete(event, outcome)
            self._active_sessions.pop(key, None)

    async def on_processing_complete(self, event, outcome):
        pass

    def _is_allowed_user(self, *args, **kwargs):
        return True

    async def join_voice_channel(self, *args, **kwargs):
        return True

    async def leave_voice_channel(self, *args, **kwargs):
        pass

    def _reset_voice_timeout(self, *args):
        pass

    def _voice_timeout_limit(self):
        return 300

    async def play_in_voice_channel(self, *args):
        return True

    async def get_user_voice_channel(self, *args):
        return None


@pytest.fixture
def host(monkeypatch):
    def replace_source(source, **changes):
        copied = dataclasses.replace(source, **changes)
        for name in ("_transport_adapter_ref", "_authorization_profile_home", "_identity"):
            if hasattr(source, name):
                setattr(copied, name, getattr(source, name))
        return copied

    modules = {
        "gateway.platforms.event": types.SimpleNamespace(MessageEvent=Event, MessageType=types.SimpleNamespace(TEXT="text", COMMAND="command")),
        "gateway.session_identity": types.SimpleNamespace(replace_source=replace_source),
        "tools.async_delegation": types.SimpleNamespace(has_live_for_session=lambda **_: False),
        "tools.voice_live": types.SimpleNamespace(voice_live_turn_note=lambda text: "native note:" + text),
        "discord": types.SimpleNamespace(ChannelType=types.SimpleNamespace(public_thread=11), AllowedMentions=types.SimpleNamespace(none=lambda: None), TextChannel=Channel),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    gateway = Gateway()
    adapter = Adapter(gateway)
    gateway._delivery_adapter_for = lambda source: adapter
    from discord_native_live.native import HermesTaskBridge
    notices = []
    native = HermesTaskBridge(gateway, adapter)
    native.attach(lambda _: False)
    channel = Channel()
    source = Source()
    source._identity = object()
    voice = types.SimpleNamespace(source=source, channel=channel, max_jobs=4, bridge=native, closing=False, closed=False, publish=notices.append)
    return types.SimpleNamespace(gateway=gateway, adapter=adapter, native=native, voice=voice, notices=notices)
