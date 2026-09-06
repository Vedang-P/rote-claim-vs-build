#!/usr/bin/env python3
"""claim-vs-build analyzer: what was ASKED vs what the agent CLAIMED vs what
was actually BUILT, checked deterministically.

Three inputs close the triangle:
- ASK: the original request text (ask_text or ask_file)
- CLAIM: the agent's completion summary (claim_text or claim_file)
- EVIDENCE: the project tree + git diff vs a base revision

Deterministic checks, no model in the loop:
- File claims: does the claimed file exist / appear in the diff (hard -
  contradicted when absent)
- Action claims ("added X", "implemented Y"): object tokens searched in diff
  paths, added lines, and the tree (soft - NO-EVIDENCE when absent, because
  absence of a fuzzy object is not proof of contradiction)
- Command claims ("all tests pass"): mapped ONLY to commands the project's
  own manifests declare (package.json scripts, Makefile targets, pytest
  presence) and executed with a timeout - never agent-invented commands.
  Exit 0 = HELD, nonzero = CONTRADICTED, nothing declared = UNVERIFIABLE.
- ASK coverage: per-sentence token evidence in the build; MUST sentences
  (must/should/has to) with weak evidence are flagged.

Never guesses: every claim ends HELD / CONTRADICTED / NO-EVIDENCE /
UNVERIFIABLE with its evidence pointer. Exit 0 report, 2 unusable input,
1 self-check failed.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

NAME_STOP = set("""the a an and or of for to in on with without into from by as is are was were be been this that these those it its all any some new more very
i we you they he she them us our your their my me
quickly important relevant properly correctly easily simple fast ready also just really much many lot lots nicely good better best sure makes need using used within across want wants able""".split())

TOKEN_STOP = set("""the a an and or of for to in on with without into from by as is are was were be been this that these those it its all any some new more very
must should needs has have do does done make made build built use using used add added support supports implement implemented create created fix fixed update updated change changed remove removed
code file files project repo system feature features functionality work working works test tests tested please need wants want able can will would like also based""".split())

PATH_MENTION = re.compile(
    r"\b[\w.-]+(?:/[\w.-]+)*\.(?:ts|tsx|js|jsx|mjs|cjs|py|go|rs|rb|java|kt|php|md|json|ya?ml|toml|sh|env|txt)\b")

# Object window: runs past intra-path dots ("main.ts") but stops at a
# sentence-ending period (dot + whitespace), a semicolon, a newline, or a
# coordinating "and <verb>", so each verb yields its own claim.
ACTION_VERB = re.compile(
    r"\b(?:created|added|implemented|built|wrote|fixed|updated|removed|deleted|refactored|extended)\b"
    r"(?P<object>(?:(?!\.[\s])(?!\band\s+(?:added|created|implemented|built|wrote|fixed|updated|removed|deleted|refactored|extended)\b)[^;\n]){3,120})",
    re.IGNORECASE)

COMMAND_CLAIM = re.compile(
    r"\b(?:all\s+)?(?:tests?|test\s+suite|build|lint|linting|checks?|ci)\b"
    r"[^.;\n]{0,30}?\b(?:pass(?:es|ed)?|green|succeed(?:s|ed)?|clean|ok|okay|fine)\b"
    r"|all\s+checks?\s+pass", re.IGNORECASE)

COUNT_CLAIM = re.compile(r"\b(\d+)\s+(tests?|test cases|files?|endpoints?|routes?|components?)\b",
                         re.IGNORECASE)

MUST_SENTENCE = re.compile(r"\b(?:must|should|needs?\s+to|has\s+to|require[ds]?)\b", re.IGNORECASE)

COMMAND_CAP = 4
COMMAND_TIMEOUT = 180
DIFF_BYTES_CAP = 400000


def read_text_file(path, cap=200000):
    try:
        with open(path, "rb") as fh:
            data = fh.read(cap)
        return data.decode("utf-8", "replace")
    except OSError:
        return None


def stem(w):
    return w[:-1] if w.endswith("s") and len(w) > 4 else w


def tokens(text, min_len=3):
    out = []
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{%d,}" % (min_len - 1), text):
        wl = w.lower().strip("_-")
        if wl not in TOKEN_STOP and wl not in NAME_STOP:
            out.append(wl)
    return out


# --------------------------------------------------------------------------
# Evidence gathering
# --------------------------------------------------------------------------

def git(root, args, timeout=60):
    try:
        p = subprocess.run(["git", "-C", str(root)] + args,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


def resolve_base(root):
    """Pick the diff base the way a reviewer thinks: what did the agent build
    relative to the default branch? Falls back to HEAD (uncommitted work)
    when the repo has no default branch or HEAD is it."""
    rc, out, _ = git(root, ["symbolic-ref", "refs/remotes/origin/HEAD"],
                     timeout=15)
    default = None
    if rc == 0 and out.strip():
        default = out.strip().split("/")[-1]
    if not default:
        for cand in ("main", "master"):
            rc2, out2, _ = git(root, ["rev-parse", "--verify", cand],
                               timeout=15)
            if rc2 == 0:
                default = cand
                break
    rc, cur, _ = git(root, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
    cur = cur.strip() if rc == 0 else ""
    if default and cur and cur != default:
        return default, ("diff vs default branch %s (HEAD is %s), so committed "
                         "agent work is included" % (default, cur))
    return "HEAD", "diff vs HEAD (uncommitted work on the default branch)"


def evidence(root, base):
    """Diff vs base. A non-git root falls back to tree mode:
    every file IS the build (fresh project)."""
    ev = {"mode": "diff", "base": base, "changed": [], "added_lines": [],
          "tree_files": [], "git_ok": True, "truncated": False}
    rc, out, err = git(root, ["rev-parse", "--is-inside-work-tree"])
    if rc != 0 or out.strip() != "true":
        ev["mode"] = "tree"
        ev["git_ok"] = False
    else:
        if base == "empty":
            diff_args = ["diff", "-U0", "--no-color",
                         "4b825dc642cb6eb9a060e54bf8d69288fbee4904"]
            name_args = ["ls-files"]
        else:
            diff_args = ["diff", "-U0", "--no-color", base]
            name_args = ["diff", "--name-status", base]
        rc, out, err = git(root, diff_args)
        if rc != 0:
            ev["git_ok"] = False
        else:
            lines = out.splitlines()
            if len(out) > DIFF_BYTES_CAP:
                lines = lines[:int(DIFF_BYTES_CAP / 60)]
                ev["truncated"] = True
            ev["added_lines"] = [l[1:] for l in lines
                                 if l.startswith("+") and not l.startswith("+++")][:20000]
        rc, out, err = git(root, name_args)
        if rc == 0:
            rows = out.splitlines()
            ev["changed"] = rows[:4000]
            if len(rows) > 4000:
                ev["truncated"] = True
    # tree inventory (bounded)
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in {"node_modules", ".git", ".venv", "venv",
                                    "__pycache__", "dist", "build", "target"}
                       and not d.startswith(".")]
        for fn in filenames:
            count += 1
            if len(ev["tree_files"]) < 6000:
                ev["tree_files"].append(
                    str(Path(dirpath) / fn).replace(str(root) + "/", ""))
    ev["tree_count"] = count
    if count > 6000:
        ev["truncated"] = True
    return ev


def diff_paths(ev):
    out = set()
    for row in ev["changed"]:
        parts = row.split("\t")
        out.add(parts[-1].strip())
    return {p for p in out if p and ".." not in p}


def declared_commands(root):
    """Commands the project ITSELF declares. Never invented here."""
    cmds = []
    pj = Path(root) / "package.json"
    if pj.is_file():
        text = read_text_file(pj)
        if text:
            try:
                scripts = json.loads(text).get("scripts", {}) or {}
            except json.JSONDecodeError:
                scripts = {}
            for key in ("test", "build", "lint", "check", "typecheck"):
                if key in scripts:
                    runner = None
                    for candidate in (Path(root) / "node_modules" / ".bin",
                                      None):
                        pass
                    if Path(root).joinpath("node_modules/.bin").is_dir():
                        runner = ["npm", "run", "--silent", key]
                    else:
                        runner = ["npm", "run", "--silent", key]
                    cmds.append({"name": "npm run %s" % key,
                                 "argv": runner,
                                 "kind": key})
    pyproject = Path(root) / "pyproject.toml"
    pytest_cfg = (pyproject.is_file()
                  and "pytest" in (read_text_file(pyproject) or "")) \
        or (Path(root) / "pytest.ini").is_file() \
        or (Path(root) / "setup.cfg").is_file()
    if pytest_cfg or any((Path(root) / f).exists()
                         for f in ("tests", "test")):
        if (Path(root) / "tests").is_dir() or (Path(root) / "test").is_dir():
            cmds.append({"name": "python3 -m pytest -q",
                         "argv": ["python3", "-m", "pytest", "-q"],
                         "kind": "test"})
    mk = Path(root) / "Makefile"
    if mk.is_file():
        mtext = read_text_file(mk) or ""
        targets = set(re.findall(r"^([a-zA-Z][\w-]*):", mtext, re.M))
        for t in ("test", "check", "lint"):
            if t in targets:
                cmds.append({"name": "make %s" % t,
                             "argv": ["make", t], "kind": t})
    return cmds[:COMMAND_CAP]


# --------------------------------------------------------------------------
# Claim + ask parsing
# --------------------------------------------------------------------------

TEST_DEF_PAT = re.compile(
    r"^\s*(?:async\s+)?def\s+test_|^\s*(?:it|test)\s*\(|^\s*func\s+Test")


def count_test_defs(added_lines):
    return sum(1 for l in added_lines
               if TEST_DEF_PAT.search(l) and not l.strip().startswith("#"))


def parse_claims(text):
    claims = []
    for m in PATH_MENTION.finditer(text):
        claims.append({"kind": "file", "text": m.group(0),
                       "path": m.group(0)})
    for m in ACTION_VERB.finditer(text):
        obj = m.group("object").strip()
        # an object that runs into a command claim ("... and all tests pass")
        # belongs to the command claim - cut it there
        cm = COMMAND_CLAIM.search(obj)
        if cm:
            obj = obj[:cm.start()].strip()
        # the object may continue past a coordinating verb ("... and added
        # X") - cut at the next action verb so each verb gets its own claim
        obj = re.split(r"\s+and\s+(?:the\s+)?(?=added|created|implemented|built|wrote|fixed|updated|removed|deleted|refactored|extended)\b",
                       obj, maxsplit=1, flags=re.I)[0]
        # trim leading filler
        obj = re.sub(r"^(?:the|a|an|to|and|also)\s+", "", obj, flags=re.I)
        # a path object is already covered by the file claim, and a numeric
        # object belongs to the count claim - never double-report
        if PATH_MENTION.search(obj) or re.match(r"\s*\d", obj):
            continue
        tk = tokens(obj)[:6]
        if tk:
            claims.append({"kind": "action", "text": obj[:120],
                           "tokens": tk})
    for m in COMMAND_CLAIM.finditer(text):
        claims.append({"kind": "command", "text": m.group(0)[:120]})
    for m in COUNT_CLAIM.finditer(text):
        claims.append({"kind": "count", "text": m.group(0),
                       "n": int(m.group(1)),
                       "unit": m.group(2).lower()})
    # dedupe by (kind, text)
    seen = set()
    out = []
    for c in claims:
        k = (c["kind"], c["text"].lower())
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def parse_ask(text):
    sentences = [s.strip() for s in re.split(r"[.\n;]+", text) if s.strip()]
    items = []
    for s in sentences:
        tk = tokens(s)
        if len(tk) < 2:
            continue
        items.append({
            "sentence": s[:200],
            "tokens": tk[:10],
            "must": bool(MUST_SENTENCE.search(s)),
            "paths": [m.group(0) for m in PATH_MENTION.finditer(s)],
        })
    return items


# --------------------------------------------------------------------------
# Checking
# --------------------------------------------------------------------------

def check_file(claim, root, ev, dpaths):
    p = claim["path"]
    exists = (Path(root) / p).is_file()
    in_diff = p in dpaths
    if exists and in_diff:
        return "HELD", "exists and changed in this build (in diff vs %s)" % ev["base"]
    if exists:
        return "HELD", "exists in the tree (not in diff vs %s)" % ev["base"]
    if in_diff:
        return "CONTRADICTED", "appears in diff but missing from the tree now"
    return "CONTRADICTED", "no such file in the tree"


def check_action(claim, ev, dpaths):
    tk = claim["tokens"]
    corpus_paths = " ".join(dpaths).lower() + " " + " ".join(
        ev["tree_files"]).lower()
    added = ev["added_lines"]
    evidence = []
    for i, line in enumerate(added):
        ll = line.lower()
        hits = [t for t in tk if t in ll]
        if len(hits) >= 2 or (len(hits) == 1 and len(tk) == 1):
            evidence.append("diff+%d: %s" % (i, line.strip()[:100]))
            if len(evidence) >= 3:
                break
    if not evidence:
        for p in dpaths:
            pl = p.lower()
            if sum(1 for t in tk if t in pl) >= 2:
                evidence.append("changed file: %s" % p)
                if len(evidence) >= 3:
                    break
    if evidence:
        return "HELD", "; ".join(evidence)
    # single weak token in whole tree?
    weak = [t for t in tk if t in corpus_paths]
    if weak:
        return "NO-EVIDENCE", "token(s) %s appear in paths only, none in the diff" % ", ".join(weak[:3])
    return "NO-EVIDENCE", "no diff or tree evidence for tokens: %s" % ", ".join(tk[:4])


def run_commands(root, cmds):
    results = []
    for c in cmds:
        try:
            p = subprocess.run(c["argv"], cwd=str(root),
                               capture_output=True, text=True,
                               timeout=COMMAND_TIMEOUT)
            code, tail = p.returncode, (p.stdout or p.stderr)[-400:]
        except subprocess.TimeoutExpired:
            code, tail = 124, "timed out after %ss" % COMMAND_TIMEOUT
        except FileNotFoundError as e:
            code, tail = 127, "missing tool: %s" % e.filename
        results.append({"name": c["name"], "kind": c["kind"],
                        "exit": code, "output_tail": tail})
    return results


def check_command_claims(command_claims, results):
    if not command_claims:
        return []
    if not results:
        return [{"claim": command_claims[0]["text"][:100],
                 "verdict": "UNVERIFIABLE", "kind": "command",
                 "why": "no declared test/build/lint command found in the "
                        "project manifests; the play never invents commands"}]
    out = []
    failed = [r for r in results if r["exit"] != 0]
    for c in command_claims:
        if failed:
            f = failed[0]
            if f["exit"] == 5 and "pytest" in f["name"]:
                out.append({"claim": c["text"][:100], "kind": "command",
                            "verdict": "UNVERIFIABLE",
                            "why": "pytest collected no tests (exit 5); "
                                   "'tests pass' is not provable in a repo "
                                   "with no tests"})
            else:
                out.append({"claim": c["text"][:100], "kind": "command", "verdict": "CONTRADICTED",
                            "why": "`%s` exited %d" % (f["name"], f["exit"])})
        else:
            names = ", ".join("`%s`" % r["name"] for r in results)
            out.append({"claim": c["text"][:100], "kind": "command", "verdict": "HELD",
                        "why": "all declared commands exited 0: %s" % names})
    return out


def check_ask(items, ev, dpaths, readme_text):
    out = []
    added_blob = "\n".join(ev["added_lines"]).lower()
    blob = ev.get("content_blob", "")
    for it in items:
        tk = it["tokens"]
        if not tk:
            continue
        hits = 0
        for t in tk:
            forms = (t, stem(t))
            if (any(f in added_blob for f in forms)
                    or any(f in p.lower() for p in dpaths for f in forms)
                    or any(f in fl.lower() for fl in ev["tree_files"]
                           for f in forms)
                    or (readme_text and any(f in readme_text.lower()
                                            for f in forms))
                    or (blob and any(f in blob for f in forms))):
                hits += 1
        ratio = hits / len(tk)
        out.append({
            "sentence": it["sentence"], "must": it["must"],
            "evidence_ratio": round(ratio, 2),
            "verdict": "EVIDENCED" if ratio >= 0.5 else "NOT-EVIDENT",
        })
    return out


def cmd_brief(args):
    root = args.root
    p = Path(root)
    if not os.path.isabs(root):
        print(json.dumps({"ok": False,
                          "why": "root must be an ABSOLUTE path"}))
        return 2
    if not p.is_dir():
        print(json.dumps({"ok": False, "why": "not a directory: %s" % root}))
        return 2

    ask_text = args.ask_text or ""
    if args.ask_file:
        t = read_text_file(args.ask_file, 50000)
        if t is None:
            print(json.dumps({"ok": False,
                              "why": "cannot read ask_file: %s" % args.ask_file}))
            return 2
        ask_text += "\n" + t
    claim_text = args.claim_text or ""
    if args.claim_file:
        t = read_text_file(args.claim_file, 50000)
        if t is None:
            print(json.dumps({"ok": False,
                              "why": "cannot read claim_file: %s" % args.claim_file}))
            return 2
        claim_text += "\n" + t
    if not ask_text.strip():
        print(json.dumps({"ok": False,
                          "why": "no ask supplied: pass ask_text or ask_file"}))
        return 2
    if not claim_text.strip():
        print(json.dumps({"ok": False,
                          "why": "no claim supplied: pass claim_text or claim_file"}))
        return 2

    if args.base:
        base, base_note = args.base, ""
    else:
        base, base_note = resolve_base(p)
    ev = evidence(p, base)
    dpaths = diff_paths(ev)
    empty_diff = (ev["mode"] == "diff" and len(ev["added_lines"]) == 0
                  and len(dpaths) == 0)
    readme_text = ""
    for cand in ("README.md", "readme.md", "README.rst", "README"):
        c = p / cand
        if c.is_file():
            readme_text = read_text_file(c, 50000) or ""
            break

    claims = parse_claims(claim_text)
    ask_items = parse_ask(ask_text)

    # per-claim checks
    claim_results = []
    for c in claims:
        if c["kind"] == "file":
            v, why = check_file(c, p, ev, dpaths)
            claim_results.append({"kind": "file", "claim": c["text"],
                                  "verdict": v, "why": why})
        elif c["kind"] == "action":
            v, why = check_action(c, ev, dpaths)
            claim_results.append({"kind": "action", "claim": c["text"],
                                  "verdict": v, "why": why})
        elif c["kind"] == "command":
            continue  # handled jointly below
        elif c["kind"] == "count":
            if "tests" in c["unit"] or "test cases" in c["unit"]:
                observed = count_test_defs(ev["added_lines"])
                if observed >= c["n"] > 0:
                    claim_results.append({
                        "kind": "count", "claim": c["text"], "verdict": "HELD",
                        "why": "%d test definition(s) added in the diff "
                               "(claimed %d)" % (observed, c["n"])})
                else:
                    claim_results.append({
                        "kind": "count", "claim": c["text"],
                        "verdict": "NO-EVIDENCE",
                        "why": "claimed %d, observed %d test definition(s) "
                               "added in the diff (static count; parametrized "
                               "or renamed tests remain unknown)"
                               % (c["n"], observed)})
            else:
                claim_results.append({
                    "kind": "count", "claim": c["text"],
                    "verdict": "UNVERIFIABLE",
                    "why": "claimed %d %s; static counting for this unit is "
                           "out of scope" % (c["n"], c["unit"])})
    command_claims = [c for c in claims if c["kind"] == "command"]
    cmds = declared_commands(p)
    cmd_results = run_commands(p, cmds) if (command_claims and args.run_checks) else []
    claim_results.extend(check_command_claims(command_claims, cmd_results))

    ask_results = check_ask(ask_items, ev, dpaths, readme_text)

    # verdict algebra
    verdicts = [r["verdict"] for r in claim_results]
    n_contra = verdicts.count("CONTRADICTED")
    n_held = verdicts.count("HELD")
    n_noev = verdicts.count("NO-EVIDENCE")
    n_unver = verdicts.count("UNVERIFIABLE")
    must_gaps = [a for a in ask_results if a["must"]
                 and a["verdict"] == "NOT-EVIDENT"]

    if not claim_results:
        verdict = "NO CHECKABLE CLAIMS"
        why = "the claim text contained no file, action, or command claim this play can check"
    elif n_contra > 0:
        verdict = "CLAIMS CONTRADICTED"
        why = "%d of %d claims contradicted by the build" % (n_contra, len(claim_results))
    elif must_gaps:
        verdict = "ASK GAPS"
        why = "claims held, but %d MUST item(s) from the ask lack build evidence" % len(must_gaps)
    elif n_held > 0 and n_noev == 0 and n_unver == 0:
        # every claim provable AND proven - an unprovable "all tests pass"
        # must never ride under a clean verdict (fail-open rule)
        verdict = "SHIPPED AS CLAIMED"
        why = "all %d checkable claims held with no unproven remainder" % n_held
    elif n_held > 0:
        verdict = "SHIPPED, WITH UNPROVEN CLAIMS"
        why = "%d held, %d without evidence, %d unverifiable" % (n_held, n_noev, n_unver)
    else:
        verdict = "CLAIMS UNPROVABLE"
        why = "no claim found hard evidence in this build"

    if verdict == "CLAIMS CONTRADICTED":
        nxt = ("fix the contradicted items above, or correct the summary "
               "before shipping it")
    elif verdict == "ASK GAPS":
        nxt = ("ask items above have no build evidence: implement them, or "
               "say plainly they were skipped")
    elif verdict == "SHIPPED, WITH UNPROVEN CLAIMS":
        nxt = ("add the missing evidence (declared test/build commands, or a "
               "diff that contains the work), or soften the claim wording")
    elif verdict == "CLAIMS UNPROVABLE" and empty_diff:
        nxt = ("diff vs %s is empty; pass base=<branch or rev> if the agent "
               "committed its work, or make the changes the claims describe"
               % base)
    elif verdict == "CLAIMS UNPROVABLE":
        nxt = ("declare test/build commands in package.json or a Makefile so "
               "command claims can be proven")
    else:
        nxt = "nothing to fix; the build matches the claims within this play's evidence boundary"

    print(json.dumps({
        "ok": True,
        "root": str(p.resolve()),
        "evidence": {"mode": ev["mode"], "base": ev["base"],
                     "base_note": base_note,
                     "empty_diff": empty_diff,
                     "git_ok": ev["git_ok"], "tree_count": ev["tree_count"],
                     "changed_paths": len(dpaths),
                     "added_lines": len(ev["added_lines"]),
                     "truncated": ev["truncated"]},
        "counts": {"claims": len(claim_results), "held": n_held,
                   "contradicted": n_contra, "no_evidence": n_noev,
                   "unverifiable": n_unver,
                   "ask_items": len(ask_results),
                   "ask_evidenced": sum(1 for a in ask_results
                                        if a["verdict"] == "EVIDENCED"),
                   "must_gaps": len(must_gaps)},
        "verdict": verdict, "why": why, "next": nxt,
        "claims": claim_results,
        "ask": ask_results[:40],
        "commands_run": cmd_results,
        "declared_commands": [c["name"] for c in cmds],
        "run_checks": args.run_checks,
    }))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("root", nargs="?")
    ap.add_argument("--base", default="HEAD")
    ap.add_argument("--ask-text", default="")
    ap.add_argument("--ask-file", default="")
    ap.add_argument("--claim-text", default="")
    ap.add_argument("--claim-file", default="")
    ap.add_argument("--run-checks", default="true")
    args = ap.parse_args()
    if args.selfcheck:
        return cmd_selfcheck()
    if args.validate:
        if not args.root or not os.path.isabs(args.root) \
                or not Path(args.root).is_dir():
            print(json.dumps({"ok": False,
                              "why": "root must be an ABSOLUTE path to an "
                                     "existing directory"}))
            return 2
        print(json.dumps({"ok": True, "root": str(Path(args.root).resolve())}))
        return 0
    if not args.root:
        ap.error("pass a root, --selfcheck, or --validate")
    return cmd_brief(args)


def cmd_selfcheck():
    base = Path(__file__).resolve().parent
    cases = json.loads((base / "selfcheck" / "cases.json").read_text())
    total, passed, failures = 0, 0, []

    def record(name, ok, detail=""):
        nonlocal total, passed
        total += 1
        if ok:
            passed += 1
        else:
            failures.append({"name": name, "passed": False, "detail": detail})

    for c in cases.get("claim_cases", []):
        got = parse_claims(c["text"])
        kinds = sorted(x["kind"] for x in got)
        exp_kinds = sorted(c["expect_kinds"])
        ok = kinds == exp_kinds
        for want in c.get("expect_contains", []):
            ok = ok and any(want["kind"] == g["kind"]
                            and want["text"].lower() in g["text"].lower()
                            for g in got)
        record("claims:" + c["name"], ok, "got %s" % kinds)

    for c in cases.get("ask_cases", []):
        got = parse_ask(c["text"])
        must_count = sum(1 for g in got if g["must"])
        record("ask:" + c["name"],
               len(got) == c["expect_items"] and must_count == c["expect_must"],
               "got %d items / %d must" % (len(got), must_count))

    for c in cases.get("command_map_cases", []):
        # pure mapping logic exercised via a fake manifest on disk
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            for fn, content in c["manifests"].items():
                fp = Path(td) / fn
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(content)
            got = [x["name"] for x in declared_commands(td)]
        ok = got == c["expect"]
        record("cmdmap:" + c["name"], ok, "got %s" % got)

    for c in cases.get("verdict_cases", []):
        # algebra: recompute the verdict branch from counts - this copy MUST
        # mirror the brief algebra, and the self-check exists to prove it
        n_contra = c["counts"]["contradicted"]
        held = c["counts"]["held"]
        gaps = c["counts"]["must_gaps"]
        unver = c["counts"].get("unverifiable", 0)
        if n_contra > 0:
            v = "CLAIMS CONTRADICTED"
        elif gaps:
            v = "ASK GAPS"
        elif held and c["counts"]["no_evidence"] == 0 and unver == 0:
            v = "SHIPPED AS CLAIMED"
        elif held:
            v = "SHIPPED, WITH UNPROVEN CLAIMS"
        else:
            v = "CLAIMS UNPROVABLE"
        record("verdict:" + c["name"], v == c["expect"], "got %s" % v)

    print(json.dumps({"total": total, "passed": passed, "failures": failures,
                      "state": "passed" if not failures else "FAILED"}))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
