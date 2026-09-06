#!/usr/bin/env -S rote play run
/**
 * @rote-frontmatter
 * ---
 * name: claim-vs-build
 * description: 'Agents hallucinate. This play closes the triangle between what was ASKED, what the agent CLAIMED it built, and what the project actually contains - checked deterministically, no model in the loop. Feed it the original request (ask_text or ask_file) and the agent''s completion summary (claim_text or claim_file); it reads the project tree and git diff vs a base revision and scores every checkable claim: file claims (held/contradicted - a claimed file that does not exist is a contradiction), action claims ("implemented X" - object tokens searched in diff paths and added lines, NO-EVIDENCE when absent because absence of a fuzzy object is not proof), command claims ("all tests pass" - mapped ONLY to commands the project''s own manifests declare (npm scripts, Makefile targets, pytest) and executed with a timeout, never agent-invented commands; exit 0 = held, nonzero = contradicted), count claims (honest UNVERIFIABLE in v1). ASK coverage: per-sentence token evidence in the build; MUST sentences (must/should/needs to) with weak evidence are flagged as ASK GAPS even when every claim held - the direction hallucination actually hurts. Verdicts: SHIPPED AS CLAIMED / CLAIMS CONTRADICTED / ASK GAPS / SHIPPED WITH UNPROVEN CLAIMS / CLAIMS UNPROVABLE, each with per-claim evidence pointers. Read-only except bounded execution of repo-declared checks (run_checks=true, disclosed, 180s timeout each, max 4). Tested on real agent sessions from this hackathon. Needs only python3 and git.'
 * source: https://github.com/Vedang-P/rote-claim-vs-build
 * tags:
 * - domain-agent-operations
 * - job-claim-verification
 * - audience-developers
 * - tool-git
 * - effect-read-only
 * discoverability:
 *   tags:
 *   - domain-agent-operations
 *   - job-claim-verification
 *   - audience-developers
 *   - tool-git
 *   - effect-read-only
 * provenance:
 *   author: vedang
 *   tier: local
 *   workspace: claim-vs-build
 *   created_at: 2026-09-06T00:00:00Z
 *   rote_version: 0.79.0
 * parameters:
 * - name: root
 *   type: string
 *   required: true
 *   description: ABSOLUTE path to the project directory to verify against
 *   example: '/Users/you/src/my-project'
 * - name: ask_file
 *   type: string
 *   required: false
 *   default: ''
 *   description: Path to a text file holding the ORIGINAL request (spec, issue text, prompt). Combine with ask_text if needed. Exactly one of ask_file/ask_text must be non-empty.
 *   example: '/Users/you/src/my-project/TASK.md'
 * - name: ask_text
 *   type: string
 *   required: false
 *   default: ''
 *   description: The original request inline (quote it)
 *   example: 'add a retry budget to the sync loop and make sure tests pass'
 * - name: claim_file
 *   type: string
 *   required: false
 *   default: ''
 *   description: Path to a text file holding the agent''s completion summary
 *   example: '/tmp/agent-summary.txt'
 * - name: claim_text
 *   type: string
 *   required: false
 *   default: ''
 *   description: The agent''s completion summary inline
 *   example: 'I implemented the retry budget in src/sync.ts and all tests pass'
 * - name: base
 *   type: string
 *   required: false
 *   default: ''
 *   description: 'Git revision to diff against. Leave empty for auto: the default branch when HEAD is a feature branch (so committed agent work is included), HEAD for uncommitted work, or pass a rev explicitly.'
 *   example: 'HEAD'
 * - name: run_checks
 *   type: string
 *   required: false
 *   default: 'true'
 *   description: 'true executes the project''s OWN declared test/build/lint commands (max 4, 180s each) to prove command claims; false keeps the play read-only'
 *   example: 'true'
 * metadata:
 *   version: 0.1.1
 *   rote_version: 0.79.0
 *   status: draft
 *   format: typescript
 *   requires_endpoints: []
 *   kind: atomic
 *   flow_type: parallel
 *   execution_model: steps_with_presentation
 *   requires_sessions: false
 *   discoverability:
 *     tags:
 *     - agent
 *     - verification
 *     - claims
 *     - hallucination
 *     - diff
 * contract:
 *   atomic: true
 *   input:
 *     type: none
 *   output:
 *     format: json
 *     destination: stdout
 *   composable: true
 * steps:
 *   selfcheck:
 *     type: process.exec
 *     argv:
 *     - python3
 *     - '@resource{analyze.py}'
 *     - --selfcheck
 *     timeout_ms: 60000
 *   validate:
 *     type: process.exec
 *     argv:
 *     - python3
 *     - '@resource{analyze.py}'
 *     - --validate
 *     - $root
 *     timeout_ms: 30000
 *   brief:
 *     type: process.exec
 *     argv:
 *     - python3
 *     - '@resource{analyze.py}'
 *     - $root
 *     - --base
 *     - $base
 *     - --ask-file
 *     - $ask_file
 *     - --ask-text
 *     - $ask_text
 *     - --claim-file
 *     - $claim_file
 *     - --claim-text
 *     - $claim_text
 *     - --run-checks
 *     - $run_checks
 *     depends_on:
 *     - validate
 *     timeout_ms: 240000
 * presentation_fixtures:
 *   selfcheck: resources/presentation-fixtures/selfcheck/fixture.yaml
 *   validate: resources/presentation-fixtures/validate/fixture.yaml
 *   brief: resources/presentation-fixtures/brief/fixture.yaml
 * ---
 */

const presentationSdk = await import("__ROTE_PRESENTATION_SDK__").catch((cause) => {
  throw new Error(
    "This is a rote steps presentation program. Run it with `rote play run <name>`.",
    { cause },
  );
});
const { FlowOutput, loadPresentationContext, stepName } = presentationSdk;

const out = new FlowOutput();
const ctx = await loadPresentationContext();

function body(stepId: "selfcheck" | "validate" | "brief"): Record<string, unknown> | null {
  const step = stepId === "selfcheck"
    ? ctx.step(stepName("selfcheck"))
    : stepId === "validate"
    ? ctx.step(stepName("validate"))
    : ctx.step(stepName("brief"));
  if (step.outcome.status !== "completed" && step.outcome.status !== "restored") {
    return null;
  }
  const b = step.outcome.output.body as Record<string, unknown>;
  const stdout = b?.stdout as { text?: unknown } | undefined;
  const text = typeof stdout?.text === "string" ? stdout.text : "";
  try {
    return JSON.parse(text || "{}") as Record<string, unknown>;
  } catch {
    return null;
  }
}

function selfCheckLine(): { trusted: boolean; line: string } {
  const b = body("selfcheck");
  if (!b) return { trusted: false, line: "Self-check: UNAVAILABLE - the self-check step did not complete" };
  const total = Number(b.total ?? 0);
  const passed = Number(b.passed ?? 0);
  if (b.state === "passed" && total > 0) {
    return { trusted: true, line: `Self-check: PASSED (${passed}/${total} bundled cases: parsing, command mapping, verdict algebra)` };
  }
  const fails = (b.failures as Array<{ name?: string; detail?: string }>) ?? [];
  return {
    trusted: false,
    line: `Self-check: FAILED (${passed}/${total}) - ${fails.slice(0, 3).map((f) => `${f.name}: ${f.detail}`).join("; ")}`,
  };
}

type ClaimRow = { kind: string; claim: string; verdict: string; why: string };
type AskRow = { sentence: string; must: boolean; evidence_ratio: number; verdict: string };
type CmdRow = { name: string; kind: string; exit: number; output_tail: string };

const sc = selfCheckLine();
const v = body("validate");
const valid = v?.ok === true;
const brief = body("brief");

const report: string[] = [];
report.push(sc.line);
report.push("");

if (!valid) {
  report.push("VERDICT  UNUSABLE INPUT");
  report.push(`  ${(v?.why as string) ?? "validation failed"}`);
} else if (!brief || brief.ok !== true) {
  report.push("VERDICT  UNKNOWN");
  report.push(`  the brief step did not complete: ${(brief as { why?: string } | null)?.why ?? "no output"}`);
} else {
  const counts = brief.counts as Record<string, number>;
  const claims = (brief.claims as ClaimRow[]) ?? [];
  const ask = (brief.ask as AskRow[]) ?? [];
  const cmdsRun = (brief.commands_run as CmdRow[]) ?? [];
  const ev = brief.evidence as Record<string, unknown>;

  const verdict = !sc.trusted
    ? "ANALYZER FAILED ITS OWN SELF-CHECK"
    : String(brief.verdict);

  report.push(`CLAIM VS BUILD  ${brief.root}`);
  report.push(`  evidence: ${ev.mode === "tree" ? "whole tree (non-git root)" : `git diff vs ${ev.base}`} · ${ev.changed_paths} changed path(s) · ${ev.added_lines} added line(s) · ${ev.tree_count} files in tree${ev.truncated ? " · TRUNCATED at caps" : ""}`);
  if (ev.base_note) report.push(`  base: ${ev.base_note}`);
  if (ev.empty_diff) report.push("  WARNING  the diff vs the base is empty: file claims were verified against the tree, but action claims have no diff to search. Pass base=<branch or rev> if the agent committed its work.");
  report.push(`  checkable claims: ${counts.claims} · ask items: ${counts.ask_items} · run_checks: ${brief.run_checks}`);
  report.push("");
  report.push(`VERDICT  ${verdict}`);
  report.push(`  ${brief.why}`);
  if (brief.next) report.push(`  next: ${brief.next}`);
  report.push("");

  if (claims.length > 0) {
    report.push(`CLAIM SCORECARD`);
    for (const c of claims) {
      report.push(`  [${c.verdict}] (${c.kind}) ${c.claim}`);
      report.push(`        ${c.why}`);
    }
    report.push(`  held ${counts.held} · contradicted ${counts.contradicted} · no-evidence ${counts.no_evidence} · unverifiable ${counts.unverifiable}`);
    report.push("");
  }
  if (cmdsRun.length > 0) {
    report.push("COMMANDS EXECUTED  (declared by the project's own manifests)");
    for (const r of cmdsRun) {
      report.push(`  exit ${r.exit}  ${r.name}`);
    }
    report.push("");
  }
  if (ask.length > 0) {
    const must = ask.filter((a) => a.must);
    report.push("ASK COVERAGE  (does the build contain evidence for what was asked)");
    for (const a of (must.length > 0 ? must : ask).slice(0, 10)) {
      report.push(`  [${a.verdict}]${a.must ? " MUST" : ""} (${Math.round(a.evidence_ratio * 100)}% token evidence) ${a.sentence}`);
    }
    if (counts.must_gaps > 0) {
      report.push(`  ${counts.must_gaps} MUST item(s) lack build evidence - the ask is not fully reflected in the build`);
    }
    report.push("");
  }
  report.push("DISCLOSURES");
  report.push("  - command claims run ONLY commands the project's manifests declare; nothing agent-invented is ever executed");
  report.push("  - NO-EVIDENCE means nothing was found in the diff/tree, not proof the work is missing; CONTRADICTED means hard evidence of absence");
  report.push("  - a non-git root is treated as whole-tree build evidence (fresh project mode)");
}

out.human(report.join("\n"));
const c = (brief?.counts as Record<string, number> | undefined) ?? {};
out.summary(
  `${brief?.verdict ?? "UNKNOWN"}: ${c.held ?? 0} held, ${c.contradicted ?? 0} contradicted, ` +
  `${c.must_gaps ?? 0} must-gap(s). Self-check ${sc.trusted ? "passed" : "FAILED"}.`,
);
out.result({
  self_check: sc.trusted,
  brief: brief ?? null,
});
