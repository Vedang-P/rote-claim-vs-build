# claim-vs-build

Agents hallucinate. This closes the triangle between **what was ASKED**, **what the agent CLAIMED it built**, and **what the project actually contains** — checked deterministically, no model in the loop.

A [Rote Play](https://www.modiqo.ai) for the [Rote Playoffs Hackathon](https://www.modiqo.ai/blog/the-playoffs) by [@vedang](https://play.modiqo.ai/vedang/claim-vs-build).

## The pain it kills

An agent says: *"Done. Added retry logic to src/sync.ts and all tests pass."*

Was it asked for retry logic? Does `src/sync.ts` exist? Do the tests actually pass? This play answers all three from evidence — and it's the second half that most tooling fakes: a changed test file is not proof tests pass, so command claims are **executed for real**, using only commands the project's own manifests declare.

```
rote play run vedang/claim-vs-build root=/absolute/path/to/project \
  ask_text="add a retry budget to the sync loop and make sure tests pass" \
  claim_text="I implemented the retry budget in src/sync.ts and all tests pass"
```

## What it checks (deterministically)

| Claim type | Check | Verdicts |
|---|---|---|
| **file** ("created src/auth.ts") | exists in tree? in diff? | HELD / **CONTRADICTED** (hard) |
| **action** ("implemented the metric extractor") | object tokens searched in diff paths + added lines | HELD (with `file:line` evidence) / NO-EVIDENCE |
| **command** ("all tests pass") | mapped ONLY to repo-declared commands (npm scripts, Makefile targets, pytest), executed, 180s cap, max 4 | HELD (exit 0) / **CONTRADICTED** (exit ≠ 0) / UNVERIFIABLE (nothing declared — never invents commands) |
| **count** ("added 12 tests") | honest UNVERIFIABLE in v1 with observed numbers | UNVERIFIABLE |

**ASK coverage**: the original request is tokenized per sentence; `must/should/needs to` sentences with weak build evidence are flagged **ASK GAPS** — even when every claim held. That's the direction hallucination actually hurts: asked-but-not-built.

## Verdicts

`SHIPPED AS CLAIMED` · `CLAIMS CONTRADICTED` · `ASK GAPS` · `SHIPPED, WITH UNPROVEN CLAIMS` · `CLAIMS UNPROVABLE` — with the fail-open rule built into the algebra: **an unprovable "all tests pass" can never ride under a clean verdict**.

## Differentiation

- vs `sakshamsai26/claim-vs-reality-auditor`: that play explicitly never runs tests ("a changed test file is not proof tests pass") and takes no ask input — "all tests pass" stays UNPROVEN there and asked-but-not-built is invisible. Here command claims are provable and the ask closes the triangle.
- vs `psohi-labs/polygraph`: that checks agent claims against external APIs (OSV/World Bank/USGS). This checks build claims against the local repo and diff.

## v0.1.1 — the committed-work fix

Agents commit their work. The old default (diff vs HEAD) went blind the moment they did: committed files showed an empty diff and honest-but-useless evidence. Now the diff base auto-resolves to the default branch when HEAD is a feature branch, so committed agent work is verified for real. Plus: ask coverage searches file contents (a "login" ask is evidenced by src/auth.ts's content, not its filename), count claims report observed test definitions added in the diff, and every verdict ships a concrete next action.

## Tested on real agent sessions

- **paper-brief** (this hackathon): ask = the author's original request, claim = the shipped description, evidence = whole tree → `SHIPPED, WITH UNPROVEN CLAIMS` (no declared checks existed to prove "all checks pass" — honest), ask coverage 86% EVIDENCED
- **env-setup-brief** (this hackathon): → `SHIPPED AS CLAIMED`, ask 71% EVIDENCED
- **contradiction demo**: real failing `npm test` (exit 1) + phantom `src/auth.ts` → `CLAIMS CONTRADICTED`, both caught with evidence
- 16/16 bundled self-check (parsing, command mapping, verdict algebra) on every invocation; a failed self-check strips the verdict of authority

## Parameters

| Name | Required | Default | Description |
|---|---|---|---|
| `root` | yes | — | **Absolute** path to the project |
| `ask_text` / `ask_file` | one required | — | The original request |
| `claim_text` / `claim_file` | one required | — | The agent's completion summary |
| `base` | no | `HEAD` | Diff base (`HEAD` = uncommitted work; `empty` = whole tree is the build) |
| `run_checks` | no | `true` | Execute repo-declared test/build/lint commands (disclosed, bounded) |

## Discipline

- Runs only commands the project's own manifests declare — never agent-invented
- Read-only except bounded, disclosed execution of those declared checks
- Python 3.8+ grammar-verified; needs python3 + git
- `rote play lint` clean · quality score 1.00

## License

MIT
