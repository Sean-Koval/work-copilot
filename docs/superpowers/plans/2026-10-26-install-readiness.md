# Existing MVP local-install readiness pass

User explicitly requested parallel continuation of the existing published project, with readable setup/daily-use documentation and practical laptop validation. No version bump, release tag, new architecture, live account grant, or installation on the user's computer is implied.

- Docs worker: README → getting started → daily UI workflow; explicit local-1.0 acceptance checklist separating verified Linux proof from open laptop/client/workplace gates.
- Smoke worker: temporary synthetic fresh/restart test, source/wiki/hooks persistence and actual SDK tool checks; no user data or credentials; clear exit/result.
- Setup worker: read-only doctor and precise configuration/credential capability diagnostics; regression tests for identified defects.
- Lead: supported client config documentation, CLI wiring, package/startup error handling, fresh non-editable installation/live HTTP repeat checks, integration and independent review before parent publication.

Interfaces: `run_smoke() -> {success, checks, synthetic_only}`; `diagnose_path(data_dir, vault=None, config=None, timezone_name='UTC') -> dict`. CLI smoke/doctor branch before initializing or changing user's state.

Acceptance: existing tests remain green; new smoke works twice and from a fresh installed package outside checkout; missing/unwritable state fails clearly without doctor writing files; syntax/static contracts pass. Actual model/client/browser/company OAuth remain explicitly unverified where environment prevents them.
