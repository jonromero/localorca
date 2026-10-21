#!/usr/bin/env python3
"""LocalOrca design stage: Claude builds the prototype, you shape it, then Qwen builds the rest.

    python3 orca.py new ~/proj/app            (interview, then the design stage opens in the dashboard)
    python3 orca.py design ~/proj/app         (build the prototype, or revise it with your open notes)
    python3 orca.py note ~/proj/app "text"    (add a design note)
    python3 orca.py handoff ~/proj/app --go   (break the rest into small tasks and start Qwen)

Why: Qwen's failures cluster at the START of a project (wiring a whole app together, the first end-to-end test).
Claude is good at exactly that. So Claude researches, builds the real UI and a walking skeleton with green tests,
and you look at it in the live preview BEFORE anything is planned for Qwen. Notes you leave are either applied by
Claude right away (a revision) or become Qwen's first tasks at hand-off. Qwen then starts on a running, tested app
whose small files and patterns it can copy.

Files (in orca/): STAGE (design | build), RESEARCH.md, DESIGN_NOTES.md (- [ ] N01: ...), PROTOTYPE.json.
A project without STAGE is a build-stage project (everything made before this existed).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import orca

NOTE_RE = re.compile(r"^- \[( |x|>)\] (N\d+): (.*)$")
AGENT_TOOLS = ["Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"]
CONFIG_KEYS = ("test_cmd", "setup_cmd", "preview_cmd", "preview_url", "smoke_cmd")

RESEARCH_PROMPT = """You are the architect for a build that happens in two parts. First YOU build a prototype: the real UI and a walking skeleton. Then a small local coding model (Qwen, 32k context, no memory between tasks, works from files only, tests are its only judge) builds the rest one small task at a time on top of your code.
Now: RESEARCH, then the SPEC. Use WebSearch and WebFetch for what matters here: the simplest current stack and libraries (versions that exist today), how similar products solve the core problem, the UI patterns users expect, known pitfalls. Be practical, 5 to 10 searches. Do not write code.

Product brief:
{brief}
{rules}
Output EXACTLY these three sections and nothing else. Each starts with its marker line.
=====RESEARCH.md=====
(UNDER 60 lines: findings with source links, and the decision each one leads to.)
=====SPEC.md=====
(UNDER 70 lines. The builder reads ALL of it at the start of EVERY task with a tiny context, so every line must earn its place. Short `## Section` headings: Goal, Stack, ## Screens (every screen and its main controls, one line each), ## Files (the folder layout, one line per file, small files: source at most about 150 lines), ## Conventions (module pattern, naming, how tests are written, so the small model copies them), Data (shapes, at most 3 lines per example). No prose, no marketing, no rationale.)
=====CONFIG=====
(JSON only: {{"test_cmd": "...", "setup_cmd": "...", "preview_cmd": "...", "preview_url": "...", "smoke_cmd": "..."}}. test_cmd runs the whole suite fast and exits nonzero on failure, with no browser (LocalOrca runs its own headless real-app check). setup_cmd installs dependencies, or "". Screen apps (web, games): preview_cmd starts the real app, which listens on the PORT env variable, preview_url is a CONCRETE url with the port it uses when PORT is not set (for example http://localhost:8000), and smoke_cmd is left out. Everything else: smoke_cmd runs scripts/smoke.sh (the real product, real input, no mocks), preview_cmd and preview_url are left out.)
"""

PROTOTYPE_PROMPT = """You are building the PROTOTYPE of this product in the current folder. Read orca/SPEC.md and orca/RESEARCH.md first (orca/BRIEF.md has the owner's words).
A small local model (Qwen, 32k context, no memory between tasks) will build everything else ON TOP of your code, so your code is the pattern it copies. The owner will look at your prototype in a browser and leave notes before anything is planned for Qwen.

Build:
1/ The REAL UI. Every screen in the spec exists, is reachable, and looks like the finished product: layout, styling, navigation, controls, empty states. Where the logic is not built yet, show realistic sample data and put a `TODO(qwen): <what to build here>` comment at that spot.
2/ A walking skeleton. The app starts with preview_cmd ({preview_cmd}) and listens on the PORT env variable (non-screen products: scripts/smoke.sh runs the real product). ONE core user path works end to end with real code, no fakes.
3/ Tests. `{test_cmd}` passes. Include pure logic tests, and for a web app a test that every file the page and its scripts load exists. Keep test_cmd FAST and headless-free: no browser tests, no Playwright or Puppeteer, do not install browsers. LocalOrca checks the real app in a browser itself: {smoke_note}
4/ Qwen-friendly code. SMALL files: source at most about 150 lines, tests at most about 250. One file per function group, the folder layout from the spec, one consistent pattern everywhere, short comments only where they help. No dead code.

Rules: install dependencies with the project's own tool and record them (package.json, requirements.txt; Python uses ./venv). Run `{test_cmd}` and fix until it passes. Never touch .git and do not commit, LocalOrca commits for you. In orca/ edit ONLY SPEC.md: make ## Files match the real layout and add a short ## Prototype section (under 12 lines: what is real, what is sample data, where the TODO(qwen) marks are). Never use em dashes.
{look}
When done, reply with at most 8 lines: what the owner will see, what is real, what is placeholder.
Then end with 3 to 5 lines that each start exactly `TRY: `: one thing the owner should open, click or look at in the browser to judge this prototype (the look, a screen, a control). Never a thing that is still a placeholder.
"""

REVISE_PROMPT = """The owner looked at the prototype in this folder and left these notes. Apply ALL of them to the code now.
{look}
Read orca/SPEC.md first. Same rules as the prototype: no browser tests or browser installs (LocalOrca checks the real app itself: {smoke_note}), real UI, small files (source at most about 150 lines), one consistent pattern, TODO(qwen) marks where logic is still missing, `{test_cmd}` must pass, never touch .git, do not commit, in orca/ edit only SPEC.md (keep ## Files and ## Prototype true). Never use em dashes.
NOTES TO APPLY NOW:
{notes}

EARLIER NOTES (already applied in earlier revisions, for context only. A new note may say an earlier fix did NOT work: then the earlier change was wrong, look at the screenshots and find the real cause):
{history}
Reply with at most 8 lines: what changed, one line per note id.
Then end with 3 to 5 lines that each start exactly `TRY: `: one thing the owner should open, click or look at in the browser to judge this prototype (the look, a screen, a control). Never a thing that is still a placeholder.
"""

FIX_PROMPT = """The checks failed after your work. Fix the REAL code (change a test only if the test itself is wrong), then run `{test_cmd}` again until it passes. Do not commit.
{fail}
"""

BREAKDOWN_PROMPT = """The owner approved the prototype in this folder. Plan the REST of the build for a small local coding model (Qwen, 32k context, no memory between tasks). At the start of every task it reads orca/SPEC.md and a code map, nothing else. Tests are its only judge.
You may Read, Glob and Grep the code. Do not edit anything.

Output EXACTLY one marker line `=====TASKS.md=====` and then task lines only, each ONE line: `- [ ] T01: title | done when: a test shows ...`
Rules:
1/ 20 to 60 tasks. OPEN NOTES first: each becomes one task (or a few, if it is big), in note order, its title starting with the note id in brackets, for example `T01: [N02] bigger Add button`.
2/ Then the rest of the product, ordered so each task builds on the last, and every task adds one visible or usable thing to the RUNNING app (a vertical slice, never invisible plumbing). Replace the TODO(qwen) marks with real code.
3/ Every task names the real files it changes (paths from the code) or the new small files it creates. SMALL: one function, or two or three tiny related ones, roughly 50 to 100 new lines, at most 3 behaviours in its done-when. A file stays under about 150 lines; a group that grows becomes a folder of small files.
4/ "done when" is always checked automatically by the test command: write "done when: a test shows ...". Never "in the browser", "visually", "looks right" or "manually".
5/ Do not plan the prototype again, it exists and works.

OPEN NOTES FROM THE OWNER (not yet in the code):
{notes}

TODO(qwen) MARKS IN THE CODE:
{todos}

CODE MAP:
{codemap}
"""


# ---------------------------------------------------------------- stage and notes
def stage(P):
    s = P.rd("STAGE").strip()
    return s if s in ("design", "build") else "build"


def set_stage(P, s):
    P.wr("STAGE", s + "\n")


def proto(P):
    try:
        return json.loads(P.rd("PROTOTYPE.json")) or {}
    except ValueError:
        return {}


def notes(P):
    out = []
    for line in P.rd("DESIGN_NOTES.md").splitlines():
        m = NOTE_RE.match(line)
        if m:
            out.append({"mark": m.group(1), "id": m.group(2), "text": m.group(3)})
    return out


def open_notes(P):
    return [n for n in notes(P) if n["mark"] == " "]


def split_note(text):
    """'1. fix A 2. add B' becomes two notes, so each can be applied and checked on its own."""
    parts = [p.strip(" ;,") for p in re.split(r"(?:^|\s)\d+[.)]\s+", text)]
    parts = [p for p in parts if len(p) >= 3]
    return parts if len(parts) >= 2 else [text]


def add_note(P, text):
    """Adds one note per numbered request. Returns the new ids. Committed alone, so nothing can erase it."""
    text = " ".join(str(text or "").split())[:400]
    if len(text) < 3:
        return []
    lines = P.rd("DESIGN_NOTES.md").splitlines() or ["# Design notes. [ ] open, [x] applied by Claude, [>] handed to Qwen as a task", ""]
    ids = []
    for part in split_note(text):
        nid = "N%02d" % (len(notes(P)) + len(ids) + 1)
        lines.append("- [ ] %s: %s" % (nid, part))
        ids.append(nid)
    P.wr("DESIGN_NOTES.md", "\n".join(lines) + "\n")
    rel = "%s/DESIGN_NOTES.md" % P.o.name
    P.git("add -- %s" % rel)
    P.git("commit -qm %s -- %s" % (json.dumps("design note: %s" % ", ".join(ids)), rel))
    return ids


def mark_notes(P, ids, mark, suffix=""):
    lines = P.rd("DESIGN_NOTES.md").splitlines()
    for i, line in enumerate(lines):
        m = NOTE_RE.match(line)
        if m and m.group(2) in ids:
            lines[i] = "- [%s] %s: %s%s" % (mark, m.group(2), m.group(3), suffix)
    P.wr("DESIGN_NOTES.md", "\n".join(lines) + "\n")


def notes_block(ns):
    return "\n".join("%s: %s" % (n["id"], n["text"]) for n in ns) or "(none)"


def summary_lines(P):
    """For the dashboard and the preview badge."""
    pr, ns = proto(P), notes(P)
    o = sum(1 for n in ns if n["mark"] == " ")
    return pr, o, sum(1 for n in ns if n["mark"] == "x")


# ---------------------------------------------------------------- Claude with live progress
def usage_of(result, msg_usage=None):
    """Exact token counts of one claude run. modelUsage (from Claude Code itself) covers EVERY model the run used,
    including the small model Claude Code calls on the side, so it wins over result.usage. A run that was killed has
    no result: then the per-call usage of each assistant message is summed, so the tokens still count."""
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "cost_usd": 0.0, "models": []}
    mu = (result or {}).get("modelUsage") or {}
    if isinstance(mu, dict) and any(isinstance(v, dict) for v in mu.values()):
        for name, m in mu.items():
            if isinstance(m, dict):
                tot["input"] += m.get("inputTokens", 0) or 0
                tot["output"] += m.get("outputTokens", 0) or 0
                tot["cache_read"] += m.get("cacheReadInputTokens", 0) or 0
                tot["cache_create"] += m.get("cacheCreationInputTokens", 0) or 0
                tot["cost_usd"] += m.get("costUSD", 0) or 0
                tot["models"].append(name)
        tot["source"] = "modelUsage"
    else:
        uses = [(result or {}).get("usage")] if (result or {}).get("usage") else list((msg_usage or {}).values())
        for u in uses:
            tot["input"] += u.get("input_tokens", 0) or 0
            tot["output"] += u.get("output_tokens", 0) or 0
            tot["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
            tot["cache_create"] += u.get("cache_creation_input_tokens", 0) or 0
        tot["cost_usd"] = (result or {}).get("total_cost_usd") or 0
        tot["source"] = "result.usage" if (result or {}).get("usage") else "per-message (run did not finish)"
    tot["cost_usd"] = round(tot["cost_usd"], 6)
    return tot


def log_usage(P, purpose, tot, result, secs):
    """One line in the Messages panel and one JSON line in orca/design_usage.jsonl (kept in git, for comparing builds)."""
    billed = tot["input"] + tot["output"] + tot["cache_create"]
    orca.log("CLAUDE TOKENS %s: %s in, %s out, %s cache write, %s cache read (%s counted), %d turns, %s%s" % (
        purpose, "{:,}".format(tot["input"]), "{:,}".format(tot["output"]), "{:,}".format(tot["cache_create"]),
        "{:,}".format(tot["cache_read"]), "{:,}".format(billed), (result or {}).get("num_turns") or 0, orca.fmt_dur(secs),
        ", " + ", ".join(tot["models"]) if tot["models"] else ""))
    row = dict(tot, t=time.time(), purpose=purpose, counted=billed, turns=(result or {}).get("num_turns"), secs=round(secs, 1))
    try:
        P.o.mkdir(exist_ok=True)
        with open(str(P.o / "design_usage.jsonl"), "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def design_totals(P):
    """All Claude tokens spent in the design stage so far (from design_usage.jsonl)."""
    tot = {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "counted": 0, "cost_usd": 0.0, "secs": 0.0}
    for l in P.rd("design_usage.jsonl").splitlines():
        try:
            r = json.loads(l)
        except ValueError:
            continue
        tot["calls"] += 1
        for k in ("input", "output", "cache_read", "cache_create", "counted", "cost_usd", "secs"):
            tot[k] += r.get(k) or 0
    return tot


def totals_line(P):
    t = design_totals(P)
    f = "{:,}".format
    return "Claude design so far: %s tokens counted (%s in, %s out, %s cache write), %s cache read, %d calls, %s" % (
        f(t["counted"]), f(t["input"]), f(t["output"]), f(t["cache_create"]), f(t["cache_read"]), t["calls"], orca.fmt_dur(t["secs"]))

def describe_tool(name, inp, root):
    inp = inp or {}
    path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
    if path:
        try:
            path = os.path.relpath(path, str(root)) if os.path.isabs(path) else path
        except ValueError:
            pass
    if name in ("Write", "Edit", "MultiEdit", "Read"):
        return {"Write": "write", "Edit": "edit", "MultiEdit": "edit", "Read": "read"}[name] + " " + path, path
    if name == "Bash":
        return "run " + " ".join(str(inp.get("command", "")).split())[:90], ""
    if name == "WebSearch":
        return "search: " + str(inp.get("query", ""))[:90], ""
    if name == "WebFetch":
        return "read web: " + str(inp.get("url", ""))[:90], ""
    if name in ("Glob", "Grep"):
        return "%s %s" % (name.lower(), str(inp.get("pattern", ""))[:80]), ""
    return name, ""


def handle_event(P, ev, run):
    """One line of claude's stream-json output. Updates the dashboard state. Returns nothing."""
    t = ev.get("type")
    if t == "system" and ev.get("session_id"):
        run["sid"] = ev["session_id"]
    elif t == "assistant":
        msg = ev.get("message") or {}
        if msg.get("usage"):
            run["msg_usage"][msg.get("id") or len(run["msg_usage"])] = msg["usage"]   # one entry per API call (streamed chunks share the id)
        for c in msg.get("content") or []:
            if c.get("type") == "text" and c.get("text", "").strip():
                run["text"] = c["text"].strip()
                orca.set_state(ai_text=run["text"][-1500:])
            elif c.get("type") == "tool_use":
                desc, path = describe_tool(c.get("name", ""), c.get("input"), P.root)
                run["acts"].append(desc)
                kw = {"actions": run["acts"][-6:], "step": len(run["acts"])}
                if path and not path.startswith(".."):
                    kw.update(cur_file=path, cur_action="write_file" if desc.startswith(("write", "edit")) else "read_file")
                orca.set_state(**kw)
                if not desc.startswith(("read ", "glob", "grep")):
                    orca.log("  claude: " + desc)
    elif t == "result":
        run["result"] = ev
        if ev.get("session_id"):
            run["sid"] = ev["session_id"]


def claude_agent(P, prompt, purpose, tools, timeout, resume=None):
    """Runs `claude -p` IN the project with file tools, streaming what it does to the dashboard.
    Returns (final text or None, session id). K in the dashboard ends it with the design process."""
    cfg = P.cfg
    model = orca.claude_model_now(cfg)
    cmd = (list(cfg["claude_cmd"]) + (["--model", model] if model else []) + (["--resume", resume] if resume else [])
           + [x for d in (cfg.get("extra_dirs") or []) for x in ("--add-dir", str(d))]   # reference folders (orca.json extra_dirs)
           + ["--allowedTools", ",".join(tools), "--permission-mode", "acceptEdits",
              "--output-format", "stream-json", "--verbose", prompt])
    run = {"acts": [], "text": "", "result": None, "sid": resume, "msg_usage": {}}
    t0 = time.time()
    orca.set_state(claude_since=t0)
    try:
        p = subprocess.Popen(cmd, cwd=str(P.root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
    except FileNotFoundError:
        orca.log("cannot start '%s' (is Claude Code installed? set claude_cmd in orca.json)" % cmd[0])
        return None, resume
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    other = []
    try:
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                other.append(line)
                continue
            if isinstance(ev, dict):
                handle_event(P, ev, run)
    finally:
        timer.cancel()
        p.wait()
    r = run["result"]
    tot = usage_of(r, run["msg_usage"])
    if r or run["msg_usage"]:
        orca.record("claude", purpose if r else purpose + "-unfinished", input=tot["input"], output=tot["output"],
                    cache_read=tot["cache_read"], cache_create=tot["cache_create"], cost_usd=tot["cost_usd"])
        log_usage(P, purpose if r else purpose + "-unfinished", tot, r, time.time() - t0)
    if not r:
        orca.log("claude ended without a result (rc=%s)%s: %s" % (p.returncode, " TIMEOUT" if p.returncode in (-9, 137) else "",
                                                                  "".join(other)[-300:].strip()))
        return None, run["sid"]
    orca.note_claude_model(r)
    if r.get("is_error"):
        orca.log("claude returned an error: %s" % str(r.get("result"))[:300])
        return None, run["sid"]
    return (r.get("result") or run["text"] or "").strip(), run["sid"]


# ---------------------------------------------------------------- checks
def check(P):
    """The hand-off gate: tests green AND the real app starts clean. Returns (ok, output)."""
    P.cfg.update(json.loads((P.root / "orca.json").read_text()))
    c = P.cfg
    if c.get("setup_cmd"):
        orca.set_state(phase="design: installing dependencies")
        rc, out = orca.sh(c["setup_cmd"], cwd=P.root, timeout=900)
        if rc != 0:
            return False, "setup_cmd failed:\n" + orca.clip(out, 3000)
    if not c.get("test_cmd"):
        return False, "orca.json has no test_cmd"
    orca.set_state(phase="design: running tests")
    rc, out = orca.sh(c["test_cmd"], cwd=P.root, timeout=c["cmd_timeout"] * 3)
    if rc != 0:
        return False, "TESTS FAILED (%s):\n%s" % (c["test_cmd"], orca.clip(out, 5000))
    if orca.smoke_command(P):
        orca.set_state(phase="design: real-app check")
        src, sout = orca.run_smoke(P)
        if src not in (0, 3):
            return False, "The tests pass, but the REAL APP check failed (started with preview_cmd, opened headless):\n" + orca.clip(sout, 5000)
    return True, orca.clip(out, 600)


def look_note(P):
    """How Claude SEES the page. Claude used to change CSS blind and call a note done."""
    cmd = orca.smoke_command(P)
    if not cmd:
        return ""
    shots = P.o / "shots"
    return ("SEE THE PAGE, do not guess: run `ORCA_SHOTS=%s %s` and open every PNG it lists with your Read tool "
            "(screenshots of the real app at 2 s, 6 s and 12 s, then 1680 px and 1024 px wide: a layout that moves or "
            "resizes between them is a bug). Do it BEFORE you change anything, to see what the owner sees, and AFTER. "
            "A note is done only when the screenshots show it is done. Say what the screenshots showed." % (shots, cmd))


def smoke_note(P):
    """Tells Claude how LocalOrca checks the real app, so it runs that instead of building its own browser test.
    (Real failure: Island 2's prototype hung for 10 minutes on `npx playwright install chromium`.)"""
    cmd = orca.smoke_command(P)
    if not cmd:
        return "(no real-app check is configured for this project)."
    return ("run `%s` to see what it sees (it starts preview_cmd on a free PORT, opens the page headless and reports "
            "console errors and failed requests). It must pass." % cmd)


def build_round(P, prompt, purpose):
    """Claude works, then the checks run. Failures go back to the SAME Claude session (it remembers what it did)."""
    c = P.cfg
    text, sid = claude_agent(P, prompt, purpose, AGENT_TOOLS, c["design_timeout"])
    if text is None and not sid:
        return False, "", "Claude did not run"
    ok, out = check(P)
    for i in range(c["design_fix_rounds"]):
        if ok:
            break
        orca.log("checks failed, Claude fixes them (round %d of %d):\n%s" % (i + 1, c["design_fix_rounds"], orca.clip(out, 400)))
        orca.set_state(phase="design: Claude fixes the checks")
        t2, sid = claude_agent(P, FIX_PROMPT.format(test_cmd=c["test_cmd"], fail=out), purpose + "-fix", AGENT_TOOLS,
                               c["design_timeout"], resume=sid)
        text = t2 or text
        ok, out = check(P)
    return ok, text or "", out


# ---------------------------------------------------------------- memory
def free_local_model(P):
    """The design stage never uses Qwen. But LM Studio keeps the last model in RAM (about 20 GB for a 27B to 35B), and
    Claude's work here needs memory too: npm, test runs, a headless Chromium. So unload it, unless another LocalOrca
    build is using it right now. The hand-off loads Qwen again before the build starts. Returns a short message."""
    if not P.cfg.get("design_unload_model", True):
        return "kept (design_unload_model is off)"
    rc, out = orca.sh("ps -axo command=", timeout=10)
    if rc == 0 and re.search(r"orca\.py\s+run\s", out):
        orca.log("another LocalOrca build is running, so LM Studio's model stays loaded")
        return "kept (another build uses it)"
    loaded = [m.get("id", "?") for m in (orca.all_models(P.cfg) or []) if m.get("state") == "loaded"]
    if not loaded:
        return "nothing loaded"
    if not shutil.which("lms"):
        orca.log("LM Studio still holds %s in memory. Eject it in LM Studio to give Claude's work more RAM "
                 "(the `lms` command was not found)" % ", ".join(loaded))
        return "lms not found"
    orca.sh("lms unload --all", timeout=120)
    orca.log("unloaded %s from LM Studio: the design stage does not use Qwen, so Claude's builds and browser get the RAM. "
             "The hand-off loads it again." % ", ".join(loaded))
    return "unloaded " + ", ".join(loaded)


# ---------------------------------------------------------------- steps
def research(P):
    brief = P.rd("BRIEF.md").strip()
    if not brief:
        sys.exit("no orca/BRIEF.md, start with: python3 orca.py new %s" % P.root)
    orca.set_state(phase="design: Claude researches and writes the spec", task="design", actions=[], ai_text="", model="Claude")
    orca.log("Claude researches the product and writes the spec (web search on, usually 2 to 6 min)")
    prompt = RESEARCH_PROMPT.format(brief=brief, rules=orca.house_rules_block(P))
    out, _ = claude_agent(P, prompt, "research", ["WebSearch", "WebFetch", "Read"], P.cfg["claude_timeout"] * 2)
    if not out:
        sys.exit("research failed (is `claude` installed and logged in?)")
    parts = re.split(r"^=====(RESEARCH\.md|SPEC\.md|CONFIG)=====\s*$", out, flags=re.M)
    sec = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if "SPEC.md" not in sec:
        P.wr("research_raw.txt", out)
        sys.exit("could not read the spec from Claude's reply, saved to %s/research_raw.txt" % P.o.name)
    P.wr("RESEARCH.md", sec.get("RESEARCH.md", "") + "\n")
    P.wr("SPEC.md", sec["SPEC.md"] + "\n")
    conf = {}
    try:
        conf = json.loads(re.sub(r"^```\w*|```$", "", sec.get("CONFIG", "{}").strip(), flags=re.M))
    except ValueError:
        orca.log("CONFIG block not valid JSON, set test_cmd by hand in orca.json")
    cp = P.root / "orca.json"
    base = json.loads(cp.read_text()) if cp.exists() else {}
    base.update({k: v for k, v in conf.items() if k in CONFIG_KEYS})
    base["smoke_from"] = "T00"              # the prototype is the skeleton: the real-app check is on from Qwen's first task
    cp.write_text(json.dumps(base, indent=2) + "\n")
    P.cfg.update(base)
    P.commit("orca: research and spec")
    stack = re.search(r"^#+\s*Stack.*?(?=^#|\Z)", P.rd("SPEC.md"), flags=re.S | re.M)
    if stack:
        orca.log("STACK:\n" + stack.group(0).strip()[:800])


def split_try(text):
    """Claude ends with `TRY: ...` lines: what the owner should check in the browser. Returns (summary without them, tries)."""
    keep, tries = [], []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(?:[-*]\s*)?\**TRY:?\**:?\s*(.+)$", line)
        (tries.append(m.group(1).strip()) if m else keep.append(line))
    return "\n".join(keep).strip(), tries[:5]


def finish_round(P, ok, text, out, rev, applied=()):
    pr = proto(P)
    text, tries = split_try(text)
    if tries or not pr.get("try"):
        pr["try"] = tries
    pr.update({"revision": rev, "at": time.time(), "ok": ok, "summary": (text or "")[:1500],
               "fail": "" if ok else orca.clip(out, 1500)})
    P.wr("PROTOTYPE.json", json.dumps(pr, indent=1) + "\n")
    pr["applied"] = list(applied)
    if applied:
        mark_notes(P, applied, "x", " (rev %d)" % rev)
    P.commit("orca: prototype %s" % ("built" if rev == 1 else "revision %d" % rev))
    if text:
        orca.log("CLAUDE: " + " ".join(text.split())[:600])
    if ok:
        orca.log("PROTOTYPE %s READY. Tests pass and the app starts. Look at it with (P), add notes with (n), "
                 "(b) to have Claude apply them, (h) to hand off to Qwen." % ("v%d" % rev))
    else:
        orca.log("PROTOTYPE v%d IS NOT GREEN YET:\n%s\n(b) gives Claude another round. Hand-off waits for green." % (rev, orca.clip(out, 500)))
    orca.log(totals_line(P))
    url = orca.start_preview_bg(P, open_browser=ok)
    if url:
        orca.log("preview: " + url)
    orca.set_state(phase="design review: look, add notes, then hand off" if ok else "design: prototype checks FAIL", step=0)


def cmd_design(a):
    P = orca.Project(a.dir)
    pid = orca.running_pid(P)
    if pid:
        sys.exit(orca.already_running_help(P, pid))
    if stage(P) == "build" and P.tasks() and not getattr(a, "force", False):
        sys.exit("%s is already in the build stage (Qwen has tasks). The design stage is for new projects.\n"
                 "Add --force to go back to design (open tasks stay, the hand-off writes a fresh list)." % P.root)
    set_stage(P, "design")
    P.wr("run.pid", str(os.getpid()))
    import atexit
    atexit.register(lambda: (P.o / "run.pid").exists() and (P.o / "run.pid").unlink())
    if not (P.root / ".git").exists():
        orca.sh("git init -q", cwd=P.root)
    gi = P.root / ".gitignore"
    have = gi.read_text() if gi.exists() else ""
    add = [l for l in orca.runtime_ignores() + ["__pycache__/", "venv/", ".venv/", "node_modules/"] if l not in have]
    if add:
        gi.write_text(have.rstrip("\n") + ("\n" if have.strip() else "") + "\n".join(add) + "\n")
    try:
        free_local_model(P)
    except Exception as e:                   # a memory helper must never stop the design
        orca.log("(could not check LM Studio: %s)" % e)
    if not P.rd("SPEC.md").strip() or not P.rd("RESEARCH.md").strip():
        research(P)
    orca.ensure_venv(P)
    rev = int(proto(P).get("revision") or 0)
    ns = open_notes(P)
    if rev and not ns and proto(P).get("ok"):
        orca.log("prototype v%d is green and there are no open notes. Add notes with (n), or hand off with (h)." % rev)
        orca.set_state(phase="design review: look, add notes, then hand off")
        return
    orca.set_state(task="design", model="Claude", actions=[], ai_text="", step=0,
                   phase="design: Claude builds the prototype" if not rev else "design: Claude applies %d notes" % len(ns))
    if not rev:
        orca.log("Claude builds the prototype: real UI, walking skeleton, tests (usually 10 to 30 min, watch it here)")
        prompt = PROTOTYPE_PROMPT.format(test_cmd=P.cfg["test_cmd"], preview_cmd=P.cfg.get("preview_cmd") or "(none)",
                                         smoke_note=smoke_note(P), look=look_note(P))
        ok, text, out = build_round(P, prompt, "prototype")
        finish_round(P, ok, text, out, 1)
    elif ns:
        orca.log("Claude applies %d notes: %s" % (len(ns), ", ".join(n["id"] for n in ns)))
        history = notes_block([n for n in notes(P) if n["mark"] == "x"])
        ok, text, out = build_round(P, REVISE_PROMPT.format(test_cmd=P.cfg["test_cmd"], notes=notes_block(ns), smoke_note=smoke_note(P),
                                                           look=look_note(P), history=history), "revise")
        finish_round(P, ok, text, out, rev + 1, applied=[n["id"] for n in ns])
    else:
        orca.log("prototype v%d is not green, Claude gets another round" % rev)
        ok, out = check(P)
        if not ok:
            ok, text, out = build_round(P, FIX_PROMPT.format(test_cmd=P.cfg["test_cmd"], fail=out), "prototype-fix")
        else:
            text = "the checks pass now"
        finish_round(P, ok, text, out, rev)


def todo_marks(P):
    rc, out = P.git("grep -n 'TODO(qwen)' -- . ':!orca' ':!.orca'")
    return orca.clip(out.strip(), 4000) if rc == 0 and out.strip() else "(none)"


def parse_tasks(out):
    body = out.split("=====TASKS.md=====", 1)[-1]
    lines = [l.strip() for l in body.splitlines() if re.match(r"^- \[ \] T\d+: .+\|\s*done when:", l.strip())]
    seen, keep = set(), []
    for l in lines:
        tid = re.match(r"^- \[ \] (T\d+):", l).group(1)
        if tid != "T00" and tid not in seen:
            seen.add(tid)
            keep.append(l)
    return keep


def cmd_handoff(a):
    P = orca.Project(a.dir)
    pid = orca.running_pid(P)
    if pid:
        sys.exit(orca.already_running_help(P, pid))
    if stage(P) != "design":
        sys.exit("%s is not in the design stage, nothing to hand off" % P.root)
    if not proto(P).get("revision"):
        sys.exit("no prototype yet. Build it first: python3 orca.py design %s" % P.root)
    P.wr("run.pid", str(os.getpid()))
    import atexit
    atexit.register(lambda: (P.o / "run.pid").exists() and (P.o / "run.pid").unlink())
    orca.set_state(task="hand-off", model="Claude", actions=[], ai_text="", step=0, phase="hand-off: checking the prototype")
    orca.ensure_venv(P)        # the same environment the prototype was checked in (real failure: "mypy: command not found")
    ok, out = check(P)
    if not ok:
        orca.log("HAND-OFF STOPPED: the prototype is not green, so Qwen would start on a broken base.\n%s\n"
                 "Press (b) so Claude fixes it, then (h) again." % orca.clip(out, 600))
        orca.set_state(phase="design: prototype checks FAIL")
        sys.exit(1)
    ns = open_notes(P)
    orca.set_state(phase="hand-off: Claude breaks the rest into small tasks")
    orca.log("Claude breaks the rest into small tasks for Qwen (%d open notes become the first ones, 2 to 5 min)" % len(ns))
    cmap, _ = orca.code_map(P, cap=12000)
    prompt = BREAKDOWN_PROMPT.format(notes=notes_block(ns), todos=todo_marks(P), codemap=cmap)
    text, _ = claude_agent(P, prompt, "breakdown", ["Read", "Glob", "Grep"], P.cfg["claude_timeout"] * 2)
    tasks = parse_tasks(text or "")
    if len(tasks) < 3:
        P.wr("breakdown_raw.txt", text or "")
        orca.log("could not read a task list from Claude's reply (saved to %s/breakdown_raw.txt). Try (h) again." % P.o.name)
        orca.set_state(phase="design review: hand-off failed, try again")
        sys.exit(1)
    head = "- [x] T00: prototype by Claude (real UI and walking skeleton, revision %d) | done when: the tests and the real-app check pass" % proto(P)["revision"]
    P.wr("TASKS.md", "\n".join([head] + tasks) + "\n")
    for name in ("LESSONS.md", "PROGRESS.md"):
        if not (P.o / name).exists():
            P.wr(name, "")
    P.add_progress("T00 done: Claude built the prototype. Copy its file layout and patterns. TODO(qwen) marks show where logic is missing.")
    if ns:
        mark_notes(P, [n["id"] for n in ns], ">", " (Qwen task)")
    cp = P.root / "orca.json"
    conf = json.loads(cp.read_text())
    conf["smoke_from"] = "T00"
    cp.write_text(json.dumps(conf, indent=2) + "\n")
    set_stage(P, "build")
    P.commit("orca: hand-off to Qwen (%d tasks)" % len(tasks))
    orca.log("DESIGN STAGE TOTAL. " + totals_line(P))
    P.add_progress("design stage cost: " + totals_line(P))
    orca.log("HANDED OFF: %d tasks for Qwen%s." % (len(tasks), ", the build starts now" if a.go else ". Press (r) to start the build"))
    orca.set_state(phase="handed off to Qwen", task="", actions=[], ai_text="")
    if a.go:
        (P.o / "run.pid").unlink()
        orca.cmd_run(type("A", (), {"dir": str(P.root), "force": False})())


def cmd_note(a):
    P = orca.Project(a.dir)
    ids = add_note(P, a.text)
    print("added %s" % ", ".join(ids) if ids else "empty note, nothing added")


def start_bg(P, what, *extra):
    """Runs `orca.py design|handoff DIR` as its own process (like the build), so the dashboard stays live and K ends it."""
    P.o.mkdir(exist_ok=True)
    out = open(str(P.o / "run.out"), "a")
    return subprocess.Popen([sys.executable, str(Path(orca.__file__).resolve()), what, str(P.root)] + list(extra),
                            stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
