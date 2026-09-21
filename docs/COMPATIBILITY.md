# Compatibility and design boundaries

Inspected against public NousResearch/hermes-agent commit `4b8a8134009a8727a289bcabeb0019fedd353128` on September 21, 2026. Runtime checks reject missing methods, but cannot prove semantic compatibility with every new release. A pinned-host installer/loader check is included in CI. No real microphone/Discord acceptance result is claimed.

## Native contracts used

| Area | Required surface |
| --- | --- |
| Plugin loader | `register(ctx)`, `register_hook`, `register_command`, `register_tool`, `on_unload`, `get_config` |
| Command context | `pre_gateway_dispatch(event, gateway)` rewrite, followed by native registered-command authorization |
| Consent | `PluginContext._gateway_injection_allowed()`; existing per-plugin explicit grant |
| Live configuration | `tools.voice_live.build_session_config`, `_live_section`, `_resolve_credentials`, `voice_live_turn_note` |
| Native execution | `adapter.handle_message`, `MessageEvent._gateway_accepted`, gateway `_run_agent`, native completion callback |
| Model choice | `_resolve_session_agent_runtime`, `async_session_store.get_or_create_session` / `set_model_override` |
| Identity | native `replace_source`, profile scope, native authorization plus Discord voice-user/role check |
| Voice transport | existing adapter voice client/receiver/listener/mixer maps and join/leave/timeout functions |

Native Live helpers inspected at Git blob `b9ab6f78639aa7adc7b08f8b416894d68d21e0c8`; plugin API at `15fa39d715479b4a71144e843a107ab3b3b2bdf2`. Test admission/key excerpt identities are recorded in `tests/hermes_snapshot.py`. Blob IDs identify inspected file versions, not an asserted released Hermes version.

## Instance hooks, explicitly

The plugin temporarily replaces one receiver's `_buffers` mapping with a decoded-PCM sink and wraps `map_ssrc`. It does **not** copy packet decryption, DAVE, Opus decoding or Discord socket code. The batch listener is stopped during Live. Previously inferred speaker mappings are not trusted; only genuine subsequent SPEAKING callbacks admit the initiating user.

It wraps a gateway instance's `_run_agent` to observe results/context and an adapter instance's `on_processing_complete` to announce them after native completion. It also suppresses that adapter's ordinary voice playback in a Live-owned guild. Wrappers return original results unchanged. Owned hooks are restored during teardown; a newer replacement is never overwritten. These are private integration points and can break on updates.

A dedicated supported raw-PCM callback and a task-completion observer would be preferable future upstream seams. That is not a reason to introduce a new provider framework or refactor Desktop in this plugin.

## Independent tasks, not fake asynchronous execution

Each delegation creates a real task thread before entering `BasePlatformAdapter.handle_message`. Native session keys therefore differ. Hermes owns execution, concurrency guards, approvals, limits, persistence and text delivery. There is no `AIAgent` construction or subprocess runner in the plugin. The in-memory job map holds only correlation/status for the current gateway process; it is not the execution authority.

Distinct sessions do not guarantee distinct filesystem workspaces. Concurrent edits to the same repository need the user's normal worktree/isolation practices. There is no added workspace manager. User/session-specific settings beyond the selected model are not silently copied.

## Boundaries and limitations

Only one initiating speaker per call. Task threads are public **within their parent channel's visibility**. Ordinary native users with channel access can still interact through native text authorization; the plugin does not create a new access-control regime.

Speech interruption drops queued local PCM, not backend jobs. A small energy gate is not AEC or a sophisticated turn detector. The Live service owns conversational turn-taking. Timing, natural interruptions, packet jitter, DAVE reconnect behavior and speech quality still need real-device testing.

Capture is bounded to roughly one second, playback to five seconds. Overruns stop voice instead of replaying stale audio. Provider shutdown is attempted even if Discord cleanup fails. A socket EOF is not treated as a confirmed `session.closed` usage receipt. No transcript/audio or provider error body is logged by plugin code; native platform/provider logging policies still apply.

No automatic speech replay on reconnect, crash-resilient voice-result inbox, rich progress tracing, voice approvals, local-speech fallback, other provider, browser, or separate dashboard is included.

## Protocol references

- https://developers.openai.com/api/docs/guides/voice-websockets
- https://developers.openai.com/api/docs/guides/live-delegation
- https://github.com/NousResearch/hermes-agent/blob/main/tools/voice_live.py
- https://github.com/NousResearch/hermes-agent/blob/main/plugins/platforms/discord/adapter.py

Primary Live WebSocket is used—not an older Realtime `response.*` protocol. Audio is PCM16 mono at 24 kHz; Discord frames are 48 kHz stereo, resampled with streaming soxr.
