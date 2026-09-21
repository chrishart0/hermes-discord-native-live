# Contributing

Keep this a small adapter around native Hermes, not a second voice-agent platform. Use supported plugin APIs when available; keep unavoidable private instance hooks documented and guarded. Do not copy Discord cryptography, create a replacement scheduler, or change core/desktop code in this repository.

Install `.[test]` and run `python -m pytest -q`. Add a focused regression for behavior changes. The A/B concurrency test must prove B returns while A remains active through native admission, not by skipping the busy/session path. Preserve task ownership, approvals and the distinction between speech interruption and job cancellation.

CI checks the real host loader separately; its pinned source is in `.github/workflows/tests.yml`. Bump that pin deliberately with compatibility evidence. Keep `plugin.yaml` and `pyproject.toml` versions aligned. No live keys or paid service calls in CI.

Before a pull request, explain the user-visible fix, test evidence and any untested live behavior. The manual Discord checklist is in `docs/VALIDATION.md`. Do not label an offline fake as a real voice test.
