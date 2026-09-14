# Technical Contract

`status`: active
`activation_reason`: production
`opt_out_reason`: none
`manifest`: TECHNICAL_CONTRACT.md (this file)

## Authority

This contract is normative for the codebase once `status` is `active`. It records accepted technical decisions and invariants. Product Spec and Notion are planning input, not part of this contract. Conflicts between this contract, the plan, and code are planning decisions: resolve through the plan, never by silently rewriting this file to match new code.

## Applicable modules

| Module | Path | Applicable | Reason if not applicable |
|---|---|---|---|
| Architecture | `docs/contracts/architecture.md` | yes | |
| Data/Research | `docs/contracts/data-research.md` | no | Stateless local CLI; no database, training data, or reproducibility surface. |
| Verification | `docs/contracts/verification.md` | yes | |
| Behavior Specification | `docs/contracts/behavior-spec.md` | no | No downstream API consumers; observable behavior is the CLI surface documented in the README. |

## Risk controls

| ID | Risk | Accepted control / decision | Required evidence | Decision ref |
|---|---|---|---|---|
| R-01 | File content sent to an external LLM leaks sensitive user data. | Local-first extraction; provider changes stay config-only; external use is explicit and disclosed. | README privacy disclosure plus config schema review. | DEC-0001 |
| R-02 | A rename run destroys user filenames or file content. | Only the filename changes, never file bytes; dry-run preview; undo covers the most recent successful run. | Behavioral tests for rename and undo paths. | DEC-0001 |
| R-03 | A published release ships a breaking change. | Full `uv run nox` gate plus protected-branch CI before merge. | Green CI on the PR head. | DEC-0001 |

## Open items

None. No blocking open items remain.

## Activation and change

- Activation: user approval of this draft; no blocking open items.
- Change: only via a plan-named, user-approved amendment; code follows in the same approved package.
- Decision records: `docs/decisions/` (supersede, never silently rewrite).

Do not copy Product Spec prose, explain source code, or list dependency versions here.
