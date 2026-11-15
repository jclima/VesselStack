# Contributing to VesselStack

Contributions should keep self-hosted boat monitoring clear, reviewable, and
operationally bounded.

## Before submitting a change

1. Do not commit credentials, tokens, vessel positions, MMSIs, call signs,
   device identifiers, telemetry databases, logs, or backup archives.
2. Keep hardware-specific behavior opt-in and document its rollback path.
3. Add a dry-run mode before introducing privileged or destructive behavior.
4. Run `tests/test-distribution.sh`, the Boat Chat unit tests,
   `python3 scripts/check-public-tree.py` and `git diff --check`.
5. Explain operational impact, verification, and rollback in the pull request.
6. Update `README.md` in every pull request. CI rejects changes without it.

Follow the [writing rules](brand/README.md#writing-for-vesselstack) in documentation.
Use short imperative commit subjects. Keep pull requests focused, and use a
draft pull request while installation or safety validation remains incomplete.
