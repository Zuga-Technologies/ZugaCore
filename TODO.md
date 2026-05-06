# Justin's TODO — ZugaCore

Cybersecurity lane, secondary repo. Boundary-test scope only — no symlink-layout changes.

## Active

- [ ] **Auth-boundary test: cross-studio user_id isolation** (issue: [Justin] Auth-boundary test: cross-studio user_id isolation)
  - Output: pytest suite under `tests/auth/` (create dir if missing)
  - Must fail on a deliberate `user_id="default"` reintroduction (verify by reverting one fix locally).

## Workflow

- Branch off this branch (`Justins-Edits`) for each PR.
- PR target = `main`.
- Buga reviews.

## References

- Spec: `docs/superpowers/specs/2026-05-06-justin-cybersecurity-deliverables-design.md` (parent monorepo)
- Lane file: `memory/projects/active/project_justin_cybersecurity_lane.md` (parent monorepo)
- Regression history: `feedback_user_id_default_bucket_regression.md` (parent monorepo memory)
