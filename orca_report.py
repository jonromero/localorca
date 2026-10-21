"""Post-build report: where did the time go, what was wasteful, what is wrong with the code.

    python3 orca.py report DIR        prints it and writes orca/REPORT.md (also written automatically when a build ends)

Everything here is computed from files the build already leaves behind (tasks.jsonl, orca/logs/*.json transcripts,
claude_calls.jsonl, events.log, orca/failed/, git). No model is involved, so the numbers can be trusted.
"""

import json
import re
from collections import Counter, defaultdict

LOOK_CMD = re.compile(r"^\s*(cat|sed -n|head|tail|grep|rg|ls|find|wc|node -e|git (diff|log|show|status))\b")
WRITE_TOOLS = ("write_file", "replace_in_file")
# anything that can fail a test: assert.x, expect, named imports like strictEqual/ok, throws, python self.assert...
ASSERTION = re.compile(r"assert|expect|[eE]qual\(|\bok\(|throws|rejects|raises|fail\(|\.match\(|\.test\(|\bis_(true|false)")
EVENT_FLAGS = [("NO FILE WAS CHANGED", "attempts rejected as fake passes (tests green, no code written)"),
               ("context budget hit", "attempts cut short by the context budget"),
               ("step limit", "attempts that ran out of steps"),
               ("escalating to Claude", "times Claude was asked for a hint"),
               ("memory pressure", "times the RAM guard made the build wait"),
               ("LM Studio server not answering", "times LM Studio did not answer"),
               ("BLOCKED", "tasks blocked and rolled back")]


def fmt(n):
    n = n or 0
    return "%.1fM" % (n / 1e6) if n >= 1e6 else "%.0fk" % (n / 1e3) if n >= 1e4 else "%.1fk" % (n / 1e3) if n >= 1e3 else str(int(n))


def mins(s):
    s = s or 0
    return "%dm%02ds" % (s // 60, s % 60) if s >= 60 else "%ds" % s


def attempt_stats(P):
    """One record per Qwen attempt, from the saved transcripts: steps, looks before the first write, repeated looks."""
    out, logs = [], P.o / "logs"
    if not logs.is_dir():
        return out
    for f in sorted(logs.glob("*.json"), key=lambda x: x.stat().st_mtime):
        m = re.match(r"^([A-Za-z]\d+)-a(\d+)$", f.stem)
        if not m:
            continue
        try:
            msgs = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        steps = looks = looks_before_write = repeats = 0
        wrote, seen = False, Counter()
        chars = 0
        for msg in msgs:
            chars += len(msg.get("content") or "")
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                steps += 1
                if name in WRITE_TOOLS:
                    wrote = True
                    continue
                key = None
                if name in ("read_file", "list_files"):
                    key = (name, args.get("path"), args.get("start_line"), args.get("end_line"))
                elif name == "run" and LOOK_CMD.match(args.get("cmd") or ""):
                    key = ("run", args.get("cmd"))
                if key:
                    looks += 1
                    looks_before_write += 0 if wrote else 1
                    seen[key] += 1
                    if seen[key] > 1:
                        repeats += 1
        out.append({"id": m.group(1), "attempt": int(m.group(2)), "steps": steps, "looks": looks,
                    "before": looks_before_write, "repeats": repeats, "wrote": wrote, "ctx": int(chars / 3.5)})
    return out


def code_facts(P):
    import orca
    src, tests, big, names, weak = [], [], [], defaultdict(list), []
    for path in orca.repo_files(P):
        if not path.endswith(orca.SRC_EXT) or orca.MAP_SKIP.search(path):
            continue
        try:
            text = (P.root / path).read_text(errors="replace")
        except OSError:
            continue
        n = text.count("\n") + 1
        if orca.is_test_file(path):
            tests.append((path, n))
            blocks = re.split(r"^(?=test\(|it\(|\s*def test_)", text, flags=re.M)[1:]
            weak += ["%s (%s)" % (path, b.splitlines()[0].strip()[:60]) for b in blocks
                     if not ASSERTION.search(b)]
        else:
            src.append((path, n))
            for a in orca.file_api(path, text):
                name = a.split("(")[0].split(" ", 1)[-1]
                names[name].append(path)
        limit = P.cfg.get("test_warn_lines", 300) if orca.is_test_file(path) else P.cfg.get("file_warn_lines", 200)
        if n > limit:
            big.append((path, n))
    dups = {k: v for k, v in names.items() if len(set(v)) > 1 and len(k) > 3}
    return src, tests, big, dups, weak


def build_report(P):
    import orca
    rows = orca.jread(P, "tasks.jsonl")
    last = {}
    for r in rows:
        last[r["id"]] = r                         # a task that ran twice counts by its latest run
    final = {t["id"]: t["mark"] for t in P.tasks()}
    done = [i for i, m in final.items() if m == "x"]
    blocked = [i for i, m in final.items() if m == "!"]
    todo = [i for i, m in final.items() if m == " "]
    L = ["# Build report: %s" % P.root.name, ""]
    secs = sum(r.get("secs", 0) for r in rows)
    qin, qout = sum(r.get("qwen_in", 0) for r in rows), sum(r.get("qwen_out", 0) for r in rows)
    calls = orca.jread(P, "claude_calls.jsonl")
    by_purpose = Counter()
    for c in calls:
        by_purpose[c.get("purpose", "?")] += c.get("tokens", 0)
    L += ["## Summary", "",
          "- Tasks: %d done, %d blocked, %d still open (%d total)" % (len(done), len(blocked), len(todo), len(final)),
          "- Build time inside tasks: %s (%s of it on tasks that ended blocked)" % (
              mins(secs), mins(sum(r.get("secs", 0) for r in rows if not r.get("ok")))),
          "- Qwen tokens: %s in, %s out (%.0f to 1)" % (fmt(qin), fmt(qout), qin / float(max(qout, 1))),
          "- Claude tokens: %s (%s)" % (fmt(sum(by_purpose.values())),
                                      ", ".join("%s %s" % (k, fmt(v)) for k, v in by_purpose.most_common()) or "none"), ""]

    # attempts
    hist = Counter(min(r.get("attempts", 1), 4) for r in last.values() if r["id"][:1] in "TF")
    feat = [r for r in last.values() if r["id"][:1] in "TF"]
    if feat:
        first = sum(1 for r in feat if r.get("ok") and r.get("attempts", 1) == 1)
        L += ["## Attempts", "",
              "- Feature tasks passing on the FIRST attempt: %d of %d (%.0f%%)" % (first, len(feat), 100.0 * first / len(feat)),
              "- Attempts per task: 1 x%d, 2 x%d, 3 x%d, 4+ x%d" % (hist[1], hist[2], hist[3], hist[4]), ""]
    slow = sorted(last.values(), key=lambda r: -r.get("secs", 0))[:8]
    L += ["## Slowest tasks", ""] + ["- %s: %s, %d attempt(s)%s%s" % (
        r["id"], mins(r.get("secs")), r.get("attempts", 1), "" if r.get("ok") else ", BLOCKED",
        ", Claude %s" % fmt(r["claude"]) if r.get("claude") else "") for r in slow] + [""]

    # blocked
    if blocked:
        L += ["## Blocked tasks", ""]
        for t in blocked:
            L.append("- %s: %s  (diff: orca/failed/%s.diff)" % (t, __import__('orca').why_blocked(P.rd("failed/%s.txt" % t)), t))
        L.append("")

    # waste
    at = attempt_stats(P)
    if at:
        L += ["## Wasted steps (from the Qwen transcripts)", ""]
        tot_steps = sum(a["steps"] for a in at)
        L += ["- %d attempts, %d tool calls; %d%% were reads or searches, %d repeated an earlier one" % (
            len(at), tot_steps, 100.0 * sum(a["looks"] for a in at) / max(tot_steps, 1), sum(a["repeats"] for a in at))]
        chunk = max(len(at) // 4, 10)
        starts = list(range(0, len(at), chunk))
        if len(starts) > 1 and len(at) - starts[-1] < chunk // 2:          # fold a tiny tail into the row before it
            starts.pop()
        L += ["", "Trend (attempts in order, about %d per row). Falling numbers mean the builder wastes less:" % chunk, "",
              "| attempts | avg steps | reads before first write | repeated reads |", "|---|---|---|---|"]
        for k, i in enumerate(starts):
            g = at[i:(starts[k + 1] if k + 1 < len(starts) else len(at))]
            L.append("| %d to %d | %.1f | %.1f | %.1f |" % (i + 1, i + len(g), sum(a["steps"] for a in g) / float(len(g)),
                                                          sum(a["before"] for a in g) / float(len(g)),
                                                          sum(a["repeats"] for a in g) / float(len(g))))
        bad = sorted([a for a in at if a["before"] >= 6 or not a["wrote"]], key=lambda a: -a["before"])[:6]
        if bad:
            L += ["", "Worst attempts: " + ", ".join("%s-a%d (%d reads before writing%s)" % (
                a["id"], a["attempt"], a["before"], "" if a["wrote"] else ", never wrote") for a in bad)]
        L.append("")

    # escalation
    esc = [r for r in last.values() if r.get("claude")]
    esc = [r for r in esc if r["id"][:1] in "TF"]
    if esc:
        ok = sum(1 for r in esc if r.get("ok"))
        L += ["## Did Claude's help work?", "",
              "- %d feature tasks needed Claude's hint; %d of them passed afterwards (%d did not)" % (len(esc), ok, len(esc) - ok), ""]

    # events
    ev = P.rd("events.log")
    hits = [(ev.count(k), d) for k, d in EVENT_FLAGS if ev.count(k)]
    if hits:
        L += ["## Build health (from events.log)", ""] + ["- %d %s" % h for h in hits] + [""]

    # code
    try:
        src, tests, big, dups, weak = code_facts(P)
    except Exception as e:                       # the report must never fail a build
        src = tests = big = []
        dups, weak = {}, []
        L += ["(code facts unavailable: %s)" % e, ""]
    if src or tests:
        L += ["## The code", "",
              "- %d source files, %d lines; %d test files, %d lines" % (len(src), sum(n for _, n in src), len(tests), sum(n for _, n in tests))]
        if big:
            L.append("- Files over the size limit (source %d lines, tests %d): " % (
                P.cfg.get("file_warn_lines", 200), P.cfg.get("test_warn_lines", 300)) + ", ".join("%s (%d)" % b for b in sorted(big, key=lambda b: -b[1])))
        if dups:
            L.append("- Same name exported from more than one file: " + ", ".join("%s (%s)" % (k, ", ".join(sorted(set(v)))) for k, v in list(dups.items())[:8]))
        if weak:
            L.append("- Tests with no assertion (heuristic): %d, e.g. %s" % (len(weak), "; ".join(weak[:3])))
        L.append("")

    # advice
    tips = []
    slow_blocks = [r for r in last.values() if not r.get("ok") and r.get("secs", 0) > 600]
    if slow_blocks:
        tips.append("Blocked tasks cost %s. Split those tasks smaller (one function, at most 3 behaviours) and read orca/failed/*.txt." %
                    mins(sum(r["secs"] for r in slow_blocks)))
    if at and sum(a["repeats"] for a in at) > len(at):
        tips.append("The builder re-reads the same things a lot. Check that the CODE MAP is on (orca/MAP.md exists) and lower prompt_budget_tokens if contexts run full.")
    if big:
        tips.append("Split the files over the size limit (a folder of small files plus a thin index). Small files are what the builder handles well.")
    if weak:
        tips.append("Add real assertions to the tests listed above, a test that asserts nothing proves nothing.")
    if esc and sum(1 for r in esc if r.get("ok")) < len(esc) / 2:
        tips.append("Claude's hints rescued fewer than half of the stuck tasks. The tasks are probably too big or the done-when too vague.")
    if tips:
        L += ["## Suggestions", ""] + ["- " + t for t in tips] + [""]
    L.append("_Generated by `python3 orca.py report`. Read it, then tell Claude what you want changed._")
    return "\n".join(L) + "\n"


def write_report(P):
    text = build_report(P)
    (P.o / "REPORT.md").write_text(text)
    return text
