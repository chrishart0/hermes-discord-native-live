# Compatibility and integration boundaries

CI targets Hermes commit `4b8a8134009a8727a289bcabeb0019fedd353128`. Missing methods are checked at join, but semantic changes in later Hermes versions may require an update. Real Discord microphone testing is separate.

## Host dependencies

| Area | Integration point |
| --- | --- |
| Registration | `register(ctx)`, hooks, commands, tools, `on_unload`, `get_config` |
| Command context | `pre_gateway_dispatch` rewrite followed by native command authorization |
| Consent | Existing `allow_gateway_injection` grant |
| Live settings | `tools.voice_live` configuration, credential resolver and per-turn note |
| Execution | `adapter.handle_message`, its admission receipt, gateway `_run_agent`, `on_processing_complete` |
| Background status | `tools.async_delegation.has_live_for_session(session_key=...)` |
| Model and routing | Native session store, model resolution, `replace_source` and profile scope |
| Audio | Native Discord join/leave, receiver, playback and inactivity timeout |

The registered-command API passes raw arguments rather than an event. A bounded, single-use ticket carries request context from the pre-dispatch hook to the authorized handler. Recording a ticket does not execute work or open audio.

## Scoped private hooks

`HermesTaskBridge.attach()` installs instance observers when joining, not when requesting status. An observer that does not own a turn forwards it without changing task context. Unload restores only hooks still owned by that bridge.

`ReceiverTap` contains the private audio adaptation. Hermes's receiver normally calls `_buffers[ssrc].extend(decoded_pcm)`. The replacement mapping forwards that call to a bounded queue but exposes no completed utterances to batch STT. It wraps `map_ssrc` to admit only genuine subsequent SPEAKING events for the operator; inferred old mappings are not trusted. Closing the tap restores its buffers and callback only if still owned. Packet decryption and Opus decoding remain native.

These are version-sensitive hooks, not public extension APIs. A supported raw-PCM callback and a task-update observer would remove most of this compatibility cost. This plugin does not add a generic framework to anticipate those APIs.

## Task and update lifetime

Each voice request creates a real Discord thread and distinct native session key. Hermes performs execution, approvals, persistence and text delivery. The selected parent model is pinned without copying credentials.

A `VoiceTask` retains request identity. Each runner result produces an immutable `TaskUpdate`; the completion callback drains those updates once. Later turns produce new updates for the same request. The Live connection accepts multiple spoken updates for one delegation ID. There is no permanent “already announced” flag that discards later results.

Capacity counts a pending admission or an admitted active task, never both. A native live background delegation keeps a task active even when its parent turn is idle. This is a projection of Hermes state, not a scheduler. Future scheduled jobs and arbitrary background processes are not an exhaustive part of that status projection. An idle turn is never presented as proof all future work finished.

Correlation is retained for the call, up to 128 requests. Ending voice detaches delivery; existing work continues as text. A new call does not replay old results. No durable voice-result inbox is added.

## Startup and shutdown

Both the provider connection and Discord call own their startup task. Close first cancels and awaits startup, then releases resources; late startup cannot allocate a provider client after cleanup. Discord cleanup failure does not skip provider close. Transport EOF is not a final billing receipt.

Speech interruption clears local playback, not native work. The energy gate is not an echo canceller or a full turn detector. Capture is bounded to about one second and playback to five seconds; overload stops voice instead of replaying stale audio. Actual latency, jitter, interruptions and reconnect behavior need a microphone test.

Only one initiating operator is supported. Public task threads inherit parent-channel visibility. Native text authorization still applies to other channel participants. Independent task sessions do not isolate filesystems; simultaneous edits require ordinary worktree practices.

## Protocol

Primary GPT-Live WebSocket events are used, not the older Realtime `response.*` protocol. Provider audio is PCM16 mono at 24 kHz; Discord is 48 kHz stereo, resampled with streaming soxr.

- https://developers.openai.com/api/docs/guides/voice-websockets
- https://developers.openai.com/api/docs/guides/live-delegation
- https://github.com/NousResearch/hermes-agent/blob/4b8a8134009a8727a289bcabeb0019fedd353128/tools/voice_live.py
- https://github.com/NousResearch/hermes-agent/blob/4b8a8134009a8727a289bcabeb0019fedd353128/plugins/platforms/discord/adapter.py
