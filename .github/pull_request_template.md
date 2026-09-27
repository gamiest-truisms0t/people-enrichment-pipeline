## What and why

<!-- One paragraph: the change, the reason, and how it was verified. -->

## Checklist

- [ ] Reviewed the whole diff before opening this pull request; findings fixed
- [ ] `make check` passes locally; new behaviour has tests; coverage stays at or above 90 %
- [ ] Infrastructure: read the `terraform plan` comment; ran `make asl-validate` if the state machine changed
- [ ] README "Brief coverage and additions" and the `Unreleased` section of `CHANGELOG.md` updated
- [ ] No secrets or personal data in the diff; gitleaks passes
- [ ] Stays within the always-free allowances or the free credits; otherwise the description says what changes
