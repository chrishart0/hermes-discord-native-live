# Validation and release checklist

## Executed locally

`python -m pytest -q`: **52 passed**, Python 3.13, September 21, 2026. A wheel build also passed.

The critical test submits A through the production plugin and upstream-derived native session admission, holds its lower runner behind a barrier, then submits B through the same path. B finishes while A remains running. A is released and its separately correlated result follows. A negative-control test intentionally reuses the same session key and proves that the harness queues B.

Additional tests cover request/delegation deduplication, model override without secret copying, task ownership, explicit native controls, revoked authorization, capacity rejection, plugin/voice teardown without cancelling native jobs, raw audio framing/resampling, speaker rejection, local playback interruption, bounded queues, command context tickets, provider shutdown after Discord failure, final usage receipt distinction, observer failure isolation, packaging metadata and nested adapter context.

**Limits:** network, LLM, persistence, native lower execution, full authorization and approval flows are mocked. Native admission/key excerpts are used, not a full imported Hermes gateway. `HERMES_SOURCE` makes the harness extract the relevant methods from an actual checkout instead of the retained excerpts; it does not remove those lower mocks. Offline passing tests do not prove production audio, complete Hermes compatibility, or live approval behavior.

## Native installer/loader CI

The `hermes-host` job installs a pinned real Hermes checkout (`4b8a8134009a8727a289bcabeb0019fedd353128`), runs the native installer into a disposable profile, and loads this plugin with the actual `PluginManager`. It checks registration, native voice helpers and required Discord/runtime signatures, then re-runs the regression suite with the checkout's admission/key methods. Check the linked GitHub Actions run for the executed result; a workflow file alone is not a pass.

## Still requires manual testing

- A real running gateway connection and voice session. The installer/loader check is a separate CI test, not a live gateway.
- Paid GPT-Live session, real Discord connection, DAVE/Opus receive or microphone playback.
- Full native approval prompt and cancellation propagation with real tools.
- Cloud latency, turn quality, jitter and long-call soak.

## Manual acceptance run before relying on it

Use a restricted test server/channel, headphones and non-destructive tasks. Record exact plugin commit, Hermes commit, Python version and provider/model. Never put API keys, raw private transcripts or customer data in the test receipt.

1. Start with no plugin: confirm native text, approvals and ordinary voice work. Install plugin with the native voice config and explicit grant; restart; verify `/live-discord status`.
2. Join voice and `/live-discord join`. Speak after the confirmation. Confirm a short multi-turn conversation, not just a socket handshake. Try a second speaker: no second-speaker audio should reach the provider or trigger work.
3. Ask for read-only long task A, such as a substantial repository analysis. Confirm its text thread starts and remains active. Ask an unrelated question B requiring backend tools. **B must answer while the same A continues**, not after it ends and not by restarting A.
4. Interrupt B's spoken answer. Confirm A is still running. Ask for A's status and then an explicit correction; verify status/control targets A's existing thread, not a duplicate job.
5. Let A finish. Verify full output in its own text thread and one correlated spoken result in the original call. Check duplicate delivery and late events cannot repeat it.
6. Start another harmless job. Leave voice. Confirm the native job continues, results arrive as text, cloud audio stops and a `session.closed` usage receipt is received where possible. Rejoin: old results must not suddenly play.
7. Exercise one benign native approval in a task thread; respond there. Confirm no plugin bypass. Explicitly cancel another harmless task and verify native cancellation, not merely a 'requested' acknowledgement.
8. Test missing credentials, missing thread permission, removed operator authorization, user leaving channel, transport failure and repeated join/leave. Confirm bounded cleanup, no leaked billed connection and ordinary voice works after leaving Live.

A successful test should record 'B returned while A was still running', exact thread/session identities, interruption/cancellation distinctions and final connection cleanup. If a check fails, retain the report and keep the release experimental; do not reinterpret a mocked test as that missing receipt.
