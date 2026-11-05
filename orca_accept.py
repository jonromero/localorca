"""Acceptance tests: black-box tests Claude writes from the BRIEF at hand-off, which Qwen can read but never change.

Why: in the benchmarks the plan drifted from the brief (a 7-bag instead of one draw per piece, no JSON API, a rate limit
per hour instead of per minute) and Qwen's own tests only tested the plan, so 126 to 230 green tests shipped apps that
failed the brief. The acceptance tests are the brief's judge:
  - every test name carries an id, A01, A02, ... (orca/ACCEPTANCE.md maps each id to the brief line it checks)
  - each task line may end `| accept: A03 A07`: those must pass before the task is done
  - an acceptance test that passed once must keep passing (a ratchet), so a later task cannot break it quietly
  - the course checks see which ids still fail, so they plan against the brief, not only against the task list
Results are read from the runner's own output: pytest -rA (PASSED/FAILED lines), unittest -v (... ok / ... FAIL), node --test TAP (ok / not ok) or its
spec reporter (check and cross marks). A runner we cannot read per test still counts as a whole (exit code).
"""
import json
import re

import orca

ID_RE = re.compile(r"(?<![A-Za-z0-9])A(\d{2,3})(?!\d)")     # test_A07_name, "A07 left moves", A07:
FAIL_WORDS = re.compile(r"^\s*(not ok\b|FAILED\b|ERROR\b|✖|✗|FAIL\b)", re.I)
PASS_WORDS = re.compile(r"^\s*(ok\b|PASSED\b|✔|✓|PASS\b)", re.I)
# pytest -rA summary lines look like "PASSED acceptance/acc_api.py::test_A07_name" (the word comes first);
# its progress lines "acceptance/acc_api.py::test_A07_name PASSED" put it last.
TAIL_FAIL = re.compile(r"\s(FAILED|ERROR)(\s|$)|\.\.\. (FAIL|ERROR)$")     # ...and unittest -v: "test_A07_x (m.T) ... FAIL"
TAIL_PASS = re.compile(r"\sPASSED(\s|$)|\.\.\. ok$")


def parse(out):
    """Per-id results from a test runner's output: {"A07": True, ...}. A failure anywhere for an id wins."""
    res = {}
    for line in (out or "").splitlines():
        ids = ["A" + m for m in ID_RE.findall(line)]
        if not ids:
            continue
        if FAIL_WORDS.search(line) or TAIL_FAIL.search(line):
            ok = False
        elif PASS_WORDS.search(line) or TAIL_PASS.search(line):
            ok = True
        else:
            continue
        for i in ids[:1]:                      # the first id on the line is the test's own
            res[i] = res.get(i, True) and ok
    return res


def names(P):
    """{id: short description} from orca/ACCEPTANCE.md lines like `A07: Left moves the piece one column | brief: ...`."""
    out = {}
    for line in P.rd("ACCEPTANCE.md").splitlines():
        m = re.match(r"^\s*[-*]?\s*(A\d{2,3})\s*[:.)-]\s*(.+)$", line)
        if m:
            out[m.group(1)] = m.group(2).split("|")[0].strip()[:120]
    return out


def active(P):
    return bool(P.cfg.get("acceptance_cmd"))


def state(P):
    try:
        return json.loads(P.rd("ACCEPTANCE.json") or "{}")
    except ValueError:
        return {}


def save_state(P, st):
    P.wr("ACCEPTANCE.json", json.dumps(st, indent=1) + "\n")


def run(P):
    """Run the acceptance tests. Returns (results {id: ok}, rc, output)."""
    rc, out = orca.sh(P.cfg["acceptance_cmd"], cwd=P.root, timeout=P.cfg["cmd_timeout"] * 3)
    return parse(out), rc, out


def task_ids(task_text):
    """The ids a task line claims: `... | accept: A03 A07`."""
    m = re.search(r"\|\s*accept:\s*([A0-9 ,]+)", task_text or "")
    return ["A" + x for x in ID_RE.findall(m.group(1))] if m else []


def failure_excerpt(out, ids, cap=3000):
    """The part of the runner's output about these ids (their failure messages), so Qwen sees what the brief expects."""
    lines, keep, take = (out or "").splitlines(), [], 0
    for line in lines:
        if any(re.search(r"(?<![A-Za-z0-9])%s(?!\d)" % i, line) for i in ids):
            take = 25
        if take > 0:
            keep.append(line)
            take -= 1
    return orca.clip("\n".join(keep) or out, cap)


def restore(P):
    """Undo any change to the acceptance folder (Qwen's write tools refuse it; this also covers shell commands)."""
    d = P.cfg.get("acceptance_dir", "acceptance")
    if (P.root / d).exists():
        P.git("checkout HEAD -- %s" % d)
        P.git("clean -fdq -- %s" % d)


def suspended(P):
    return set((state(P).get("suspended") or {}).keys())


def suspend(P, ids, why):
    """An acceptance test Claude judged WRONG (it contradicts the brief, or crashes in its own code before it checks the
    product) stops judging tasks. It is kept and listed for the course check and the owner. (Real failure: A42 called a
    helper with the wrong object and died with a TypeError, so no code could make it pass and two tasks blocked.)"""
    st = state(P)
    sus = st.get("suspended") or {}
    for i in ids:
        sus[i] = why[:200]
    st["suspended"] = sus
    save_state(P, st)


def test_source(P, ids, cap=4000):
    """The code of these acceptance tests (from the line that names the id, up to the next test), for Claude to judge."""
    d = P.root / P.cfg.get("acceptance_dir", "acceptance")
    out = []
    for f in sorted(d.rglob("*")) if d.exists() else []:
        if not f.is_file() or f.suffix not in (".py", ".js", ".mjs", ".ts", ".cjs"):
            continue
        lines = f.read_text(errors="replace").splitlines()
        for n, line in enumerate(lines):
            if any(re.search(r"(?<![A-Za-z0-9])%s(?!\d)" % i, line) for i in ids) and re.search(r"\b(def|test|it)\b", line):
                end = n + 1
                while end < len(lines) and end - n < 40 and not re.match(r"^(def |async def |test\(|it\(|\S)", lines[end]):
                    end += 1
                out.append("%s:%d\n%s" % (f.relative_to(P.root), n + 1, "\n".join(lines[n:end])))
    helpers = [f for f in sorted(d.glob("*")) if f.is_file() and re.search(r"support|helper|conftest", f.name)] if d.exists() else []
    for f in helpers:
        out.append("%s (helpers)\n%s" % (f.relative_to(P.root), orca.clip(f.read_text(errors="replace"), 1500)))
    return orca.clip("\n\n".join(out), cap)


def gate(P, task, last_attempt):
    """Check one task against the acceptance tests after its own tests pass.
    Returns (ok, message for Qwen, note for the log). Regressions always fail. A task's own ids fail it too, except on
    its last attempt, where good work is kept and the ids stay open for the course check (rolling back would lose it)."""
    if not active(P):
        return True, "", ""
    res, rc, out = run(P)
    st = state(P)
    if not res:
        # a runner we cannot read per test: only "it got worse" is visible
        was_ok = st.get("whole_ok", False)
        st["whole_ok"] = rc == 0
        if was_ok and rc != 0:
            return False, "The ACCEPTANCE tests (read-only, written from the brief) passed before this task and fail now:\n" + orca.clip(out, 3000), "acceptance regressed"
        save_state(P, st)
        return True, "", ""
    sus = set((st.get("suspended") or {}).keys())
    passing = set(st.get("passing", []))
    broke = sorted(i for i in passing - sus if res.get(i) is False)
    mine = [i for i in task_ids(task["text"]) if not res.get(i) and i not in sus]
    nm = names(P)
    desc = lambda ids: "\n".join("  %s: %s" % (i, nm.get(i, "")) for i in ids)
    folder = P.cfg.get("acceptance_dir", "acceptance")
    if broke:
        return False, ("This change BREAKS acceptance tests that passed before (read-only tests in %s/, written from the brief; "
                       "fix the real code, never the tests):\n%s\n%s" % (folder, desc(broke), failure_excerpt(out, broke))), \
            "breaks " + " ".join(broke)
    if mine and not last_attempt:
        return False, ("ACCEPTANCE-ONLY. " +"Your own tests pass, but this task must also make these ACCEPTANCE tests pass (read-only, in %s/, "
                       "written from the brief: read them to see exactly what is expected, then fix the real code):\n%s\n%s"
                       % (folder, desc(mine), failure_excerpt(out, mine))), "needs " + " ".join(mine)
    st["passing"] = sorted(set(passing) | {i for i, ok in res.items() if ok})
    st["last"] = {i: ok for i, ok in sorted(res.items())}
    st["total"] = len(res)
    if mine:
        st["debt"] = sorted(set(st.get("debt", [])) | set(mine))
    save_state(P, st)
    note = "acceptance %d/%d" % (sum(res.values()), len(res)) + (" (kept, but %s still fail)" % " ".join(mine) if mine else "")
    return True, "", note


def only_needs(msg):
    """True if a failed attempt failed ONLY on its own acceptance ids (its own tests pass, nothing regressed)."""
    return (msg or "").startswith("ACCEPTANCE-ONLY.")


def keep_with_debt(P, task):
    """The task would block, but its own tests pass and only its acceptance ids fail: keep the work (rolling it back
    would also block every task built on it), and leave the ids open for the course check."""
    st = state(P)
    res, rc, out = run(P)
    sus = set((st.get("suspended") or {}).keys())
    passing = set(st.get("passing", []))
    if any(res.get(i) is False for i in passing - sus):
        return False
    st["passing"] = sorted(passing | {i for i, ok in res.items() if ok})
    st["last"] = {i: ok for i, ok in sorted(res.items())}
    st["total"] = len(res)
    st["debt"] = sorted(set(st.get("debt", [])) | {i for i in task_ids(task["text"]) if not res.get(i)})
    save_state(P, st)
    return True


def baseline(P):
    """At hand-off: run once, remember what already passes. Returns the results."""
    res, rc, out = run(P)
    save_state(P, {"passing": sorted(i for i, ok in res.items() if ok), "last": res, "total": len(res),
                   "whole_ok": rc == 0})
    return res, out


def summary(P, cap=2500):
    """For the course check: which acceptance ids fail now, with what they check."""
    if not active(P):
        return "(no acceptance tests in this project)"
    st = state(P)
    last = st.get("last") or {}
    if not last:
        return "(acceptance tests not run yet)"
    nm = names(P)
    bad = [i for i, ok in sorted(last.items()) if not ok]
    head = "%d of %d acceptance tests pass." % (len(last) - len(bad), len(last))
    sus = st.get("suspended") or {}
    if sus:
        head += "\nSUSPENDED (judged wrong, not judging tasks; fix or rewrite them): " + "; ".join("%s %s" % kv for kv in sorted(sus.items()))
    claimed = {}
    for t in P.tasks():
        for i in task_ids(t["text"]):
            claimed.setdefault(i, []).append("%s[%s]" % (t["id"], t["mark"]))
    rows = ["  %s: %s   (tasks: %s)" % (i, nm.get(i, ""), ", ".join(claimed.get(i, [])) or "NONE") for i in bad]
    return orca.clip(head + ("\nFAILING:\n" + "\n".join(rows) if rows else ""), cap)


def uncovered(ids, task_lines):
    """Acceptance ids that no task line claims."""
    got = set()
    for l in task_lines:
        got.update(task_ids(l))
    return [i for i in ids if i not in got]


def cover_tasks(missing, nm, next_num, per=3):
    """Task lines for acceptance ids the plan left out, so every brief requirement has a task."""
    out = []
    for k in range(0, len(missing), per):
        grp = missing[k:k + per]
        what = "; ".join("%s %s" % (i, nm.get(i, "")) for i in grp)
        out.append("- [ ] T%02d: Make acceptance tests pass: %s | done when: a test shows the behaviour, and acceptance tests %s pass | accept: %s"
                   % (next_num, orca.clip(what, 220).replace("|", "/"), " ".join(grp), " ".join(grp)))
        next_num += 1
    return out
