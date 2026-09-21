# Validation

## Automated checks

The default suite checks independent task admission, same-session queuing as a negative control, immutable per-turn updates, later continuations, shared-gateway context, capacity reservation, authorization, controls, audio framing and voice teardown.

The pinned Hermes CI job also:

- Runs the real installer and PluginManager in a temporary profile; loading must not open audio.
- Exercises the actual receiver's buffer and flush contract.
- Imports actual session-key and admission functions rather than reconstructing their AST.
- Starts a real background-delegation registry worker behind a barrier, returns the initial acknowledgement, answers B while A remains live, then routes A's completion through native admission and checks its later voice update.

That last test substitutes the model, network and completion-drain router. It is not a full gateway end-to-end test. Without `HERMES_SOURCE`, it is explicitly skipped. Historical v0.1.1 receipts remain in [VERIFICATION.md](VERIFICATION.md); they are not evidence for a newer commit. Use the exact commit's Actions run for current hosted results.

No automated check validates real Discord/GPT-Live audio, paid provider access, acoustic echo, conversational timing, or real tool approval/cancellation propagation.

## Manual acceptance checklist

Use headphones, a restricted test channel and harmless read-only tasks. Record plugin commit, Hermes commit, Python version and provider model. Do not record credentials or private customer audio in public reports.

1. Confirm ordinary native text, approvals and voice work without Live. Install, enable and restart, then check `/live-discord status`.
2. Join voice and run `/live-discord join`. Speak after confirmation. Have a multi-turn conversation. Verify another participant cannot trigger voice work.
3. Start long task A. Ask independent backend question B. **B must answer while the same A is still running**, not after it finishes or by cancelling/restarting it.
4. Interrupt B's speech. Confirm A continues. Ask A's status and give a correction; verify the existing task is targeted.
5. Exercise a task that itself delegates background work. Hear both its acknowledgement and its later result. Check each result is associated with the correct task and is not repeated by a duplicate completion callback.
6. End voice while a harmless task runs. Confirm text completion still arrives and the provider connection closes. Rejoin; old results must not suddenly play.
7. Exercise a benign native approval in its text thread. Explicitly cancel a separate harmless task and verify native cancellation, not just the “requested” message.
8. Test missing credentials, missing thread permission, operator departure/revoked authorization, transport failure and repeated join/leave—including leaving while connecting. Check for leaked voice connections, and confirm ordinary voice works afterward.

Keep the release experimental until this check succeeds. Passing unit or loader tests is not a substitute for that receipt.
