#!/usr/bin/env python3
"""LocalOrca: Claude designs, Qwen builds, tests judge.

Stdlib only. Python 3.9+. Talks to LM Studio (OpenAI-compatible server) and
the `claude` CLI (claude -p).

  python3 orca.py new  ~/proj/myapp "describe the product here"   # Claude builds the prototype, you review it in the dashboard
  python3 orca.py handoff ~/proj/myapp --go                          # (or (h) in the dashboard) Claude plans the rest, Qwen builds
  python3 orca.py run  ~/proj/myapp
  python3 orca.py status ~/proj/myapp
  python3 orca.py review ~/proj/myapp      # run a course check now (Claude looks at the running app and steers the task list)

Design rules:
  * Every task starts with a FRESH context (spec + lessons + task). State lives in files.
  * Tests are the judge. A task is done only when test_cmd exits 0.
  * Commit per passing task. Blocked tasks are rolled back (diff saved).
  * Claude builds the prototype and plans; after the hand-off only when Qwen is stuck and at course checks.
  * RAM guard: waits (and optionally reloads the model) when the Mac is under pressure.
"""
import argparse
import atexit
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULTS = {
    "base_url": "http://localhost:1234/v1",
    "model": "",                 # blank = first model LM Studio reports
    "model_prefer": ["27b", "30b|32b"],   # regexes, best first. Used when model is blank or "auto"
    "min_model_b": 20,           # ignore models smaller than this (billions of params)
    "max_model_b": 36,           # ignore models bigger than this (will not fit your RAM)
    "model_key": "",             # for `lms load` (used only when reloading)
    "context_tokens": 32768,     # what the model is loaded with (LocalOrca reloads it if LM Studio has less)
    "prompt_budget_tokens": 28000,   # max conversation size before we abort an attempt. Our estimate is cautious (about 15% high),
                                     # so 28000 fits a 32768 window with the 4096 reply. Change it live with (C) in the dashboard
    "max_output_tokens": 4096,
    "draft_model": "",           # optional small model of the same family for speculative decoding (same output, faster)
    "thinking": "off",           # off | low | medium | xhigh | default. Applies to attempts before thinking_retry_from
    "thinking_retry": "medium",  # used from attempt thinking_retry_from on (stubborn failures get more thought)
    "thinking_retry_from": 3,
    "temperature": 0.6,
    "top_p": 0.95,
    "test_cmd": "",
    "setup_cmd": "",
    "venv": "auto",              # auto | true | false. Python projects get <project>/venv
    "max_attempts": 5,           # attempts per task
    "same_fail_escalate": 3,     # same failure this many times -> ask Claude
    "escalate_after": 3,         # ...or after this many failed attempts, even if the failures differ (close-but-not-quite loops)
    "max_escalations": 2,
    "max_steps": 30,             # tool calls per attempt
    "halt_after_blocked": 2,     # stop the run after N blocked tasks in a row
    "checkpoint_every": 8,       # course check: Claude looks at the RUNNING app and fixes the open task list every N passing tasks (0 = off)
    "checkpoint_minutes": 90,    # ...or after this many minutes since the last check (0 = no time trigger)
    "checkpoint_max_add": 5,     # caps per check, so a check steers instead of rewriting the plan
    "checkpoint_max_remove": 5,
    "preview_cmd": "",           # starts your app for the live preview, e.g. "node server.js". (P) in the dashboard
    "preview_url": "",           # where your app answers, e.g. "http://localhost:8080"
    "smoke_cmd": "auto",         # real-app check run after test_cmd on every task once the skeleton exists. auto = built-in orca_smoke.py, "" = off
    "smoke_from": "",            # id of the walking-skeleton task. The smoke test is enforced once it is done (the planner sets this)
    "preview_port": 7777,        # open http://localhost:7777 for the app with live reload + Feedback button
    "extra_dirs": [],            # folders outside the project Qwen may READ (not write). (D) in the dashboard
    "claude_cmd": ["claude", "-p"],
    "claude_model": "",          # "" = your Claude Code default. Or opus | sonnet | haiku | a full model name. L in the dashboard
    "claude_timeout": 600,
    "design_timeout": 2700,      # seconds for one Claude prototype build or revision in the design stage (it writes many files)
    "design_unload_model": True, # design stage: unload Qwen from LM Studio (it is not used) so Claude's builds get the RAM
    "design_fix_rounds": 2,      # design stage: if tests or the real-app check fail after Claude's work, it gets this many fix rounds
    "claude_quota_tokens": 0,    # tokens you consider one 5h window budget (calibrate with Claude /usage), shows a %
    "quota_window_hours": 5,     # subscription windows reset on a rolling 5h cycle
    "quota_check": True,         # read your real subscription % from Anthropic (needs a Claude Code login)
    "show_cost": False,          # API-equivalent dollars. Meaningless on a subscription, so off
    "min_free_pct": 15,          # macOS memory_pressure "free percentage" floor
    "max_swap_mb": 3000,         # swap used above this counts as pressure
    "reload_every": 0,           # reload model every N passing tasks (0 = never)
    "lessons_max_chars": 5000,   # compact LESSONS.md above this
    "auto_split": True,          # after a passing task, queue a fix task to split any source file over file_warn_lines (one at a time)
    "escalate_lessons": True,   # Claude's escalation may add ONE general rule to LESSONS.md when it spots a repeating mistake pattern
    "file_warn_lines": 200,     # a source file longer than this is marked TOO BIG in the code map and the report
    "test_warn_lines": 300,      # same for test files
    "map_max_chars": 6000,      # size of the CODE MAP in every task prompt (about 1.5k tokens)
    "progress_keep": 12,        # lines kept in PROGRESS.md
    "spec_max_chars": 8000,
    "cmd_timeout": 180,
}

SYSTEM = """You are a careful coding agent working inside a git repo.
You have NO memory of earlier tasks. Everything you need is in the prompt and the files.
Rules:
- Do ONLY the current task. Do not refactor unrelated code or start other tasks.
- The CODE MAP in the prompt already lists every function earlier tasks built. Use it instead of re-reading files. Read a file only to see code you are about to change.
- Keep the tests small: at most 6 tests per task, one idea each, on tiny hand-built inputs (for example a 3x3 world you fill yourself), never on large random or generated data whose values you cannot predict. If a test keeps failing, simplify or delete that test instead of re-reading the file again and again.
- Fake only the OUTSIDE world (network, clock, the model, the DOM). Never inject a stand-in for a function or object this project wrote itself, the real one must run. (A page once stayed blank because a test injected a fake game state and the real wiring was never run.)
- Never edit a test file, or change an exported function, that an earlier task wrote, unless your task says so. Put your tests in a NEW test file. (A task once deleted an import from an old test file and broke every old test.) If an old test fails because of your change, fix your code, not the old test.
- Read before you edit. Use replace_in_file for small changes, write_file for new files.
- After editing, run the tests with the run tool and fix failures.
- Keep outputs small: read line ranges, use grep or head through run.
- When the task is finished and tests pass, call done with a one line summary.
  If you are stuck, call done and say exactly what blocked you.
- Never touch .git or the orca/ folder (that is the build's own notes).
- Commands already run inside the project root. Do not cd anywhere, use relative paths.
- Python: pip and python already point at the project's venv. Install packages only with pip and record them in requirements.txt."""

PLAN_PROMPT = """You are the architect for a build run executed by a small local coding model (Qwen 27B, 32k context, no memory between tasks).

Product brief:
{brief}

Output EXACTLY these three sections and nothing else. Each starts with its marker line.
=====SPEC.md=====
(Concise spec, UNDER 70 lines: goal, stack, file layout, key decisions, conventions, data shapes. Pick the simplest stack with the fewest dependencies. The builder reads this ENTIRE spec at the start of EVERY task and has a tiny context, so every line must earn its place: no prose, no marketing, no rationale, no feature lists that tasks already say, no examples longer than 3 lines. Use short `## Section` headings (one per module or topic) so it stays skimmable.)
=====TASKS.md=====
(30 to 60 tasks max.Each is ONE line: `- [ ] T01: title | done when: check`. Each task is SMALL: one function (or two or three tiny related ones), one test file, roughly 50 to 100 new lines, at most 3 behaviours in its "done when". If a task would list more than 3 actions or functions, split it into several tasks (a, b, c) that each leave the test command passing. SMALL FILES TOO: one file per function group, at most about 150 lines of source and 250 of tests. Never plan "part 1 / part 2" of the same file. A group that outgrows one file becomes a folder of small files (for example actions/move.js, actions/blocks.js) plus a thin index file that re-exports them. A bundle of six things in one task is the number one reason the small builder gets stuck. Ordered so each builds on the last. WALKING SKELETON FIRST: T01 to about T05 must produce an app that really RUNS end to end (server, page or entry point, and one visible or usable result, however crude: for a game a flat ground with one coloured block). The task that completes it is the skeleton task. From then on EVERY task is a vertical slice that adds one visible or usable thing to the running app (a tree, a button, a number on screen), never a layer of invisible plumbing, and never a module that nothing on screen needs yet. After every task from the skeleton on, Orca runs a REAL PRODUCT CHECK, so every project needs one, whatever the stack. Screen apps (web, games): Orca starts the app with preview_cmd and opens it in a headless browser, so the app must read the PORT env variable and log no console errors. Everything else (CLI, API server, library, bot, data pipeline, desktop or mobile logic): the skeleton task must also create a small script `scripts/smoke.sh` (or smoke.py / smoke.js) that runs the REAL product the way a user would, with real input and no mocks (a CLI: run it on a sample file and compare its real output; an API: start the server on the PORT env variable and curl real endpoints; a library: a short program that imports and uses it), prints what went wrong and exits nonzero on any problem. That script is the smoke_cmd. Every project gets exactly one of the two, never neither.
The builder can ONLY verify a task by running test_cmd. Nobody looks at a screen. So every "done when" must be something test_cmd checks automatically, written as "done when: a test shows ...". Never write "in the browser", "visually", "looks right" or "manually".
For anything with a UI: keep logic in pure modules with tests, give UI code small testable functions (event handlers, state updates, formatters), and add an early task with a test that every file the HTML page and its scripts load really exists (catches missing files and wrong paths). If the stack allows it (Python: Playwright; Node: only if dependencies are allowed), add one task for a headless-browser smoke test: the page loads with no JS errors and the main controls work.)
=====CONFIG=====
(JSON only: {"test_cmd": "...", "setup_cmd": "...", "preview_cmd": "...", "preview_url": "...", "smoke_cmd": "...", "smoke_from": "T05"}. test_cmd runs the whole test suite and exits nonzero on failure. setup_cmd installs dependencies, or "". preview_cmd starts the real app (it must listen on the PORT env variable), preview_url is a CONCRETE url with the port the app uses when PORT is not set (for example http://localhost:8000, never the word PORT), smoke_from is the id of the skeleton task. Screen apps set preview_cmd and preview_url and leave smoke_cmd out. Everything else sets smoke_cmd (for example "sh scripts/smoke.sh") and leaves preview_cmd and preview_url out. smoke_from is ALWAYS set.)
"""

ESCALATE_PROMPT = """You are the senior engineer. A small local model failed the same task {n} times with the same error.
Diagnose the ROOT CAUSE and give precise instructions it can follow (max 20 lines, include exact code if it is short). Do not edit files. Do not apologize or preamble.
Then, ONLY if this failure is a repeatable PATTERN of mistakes (not specific to this one task) that the LESSONS below do not already cover, end with ONE final line that starts exactly `LESSON: ` followed by a general rule for the builder (imperative, max 25 words, no task ids, no file names). It will be shown to the builder in every later task of this project, so it must be short and broadly true. If there is nothing new to teach, write no LESSON line.

LESSONS ALREADY KNOWN:
{lessons}

SPEC (head):
{spec}

TASK: {task}

TEST OUTPUT:
{fail}

WORKING TREE STATUS AND DIFF:
"""

CHECKPOINT_PROMPT = """You are the product lead for a build run by a small local coding model. Your ONE job: get to a working app someone could use or demo, as fast as possible, then grow it.
You are given the spec, the task list (x done, ! blocked, open ones have a space), recent progress, build numbers, and the result of starting the REAL app{shot_note}.
Answer in this exact format and nothing else:
STATUS: <one line: what works for a user right now, honestly>
DEMO: <one line: the shortest path from here to a usable demo>
Then zero or more change lines, at most {max_add} ADD and {max_remove} REMOVE:
REMOVE: <open task id> | <why it does not serve the demo now>
ADD: <open task id to insert before, or END> | <task text> | done when: <what a test checks>
MOVE: <open task id> | <open task id to move before, or END>
Rules: only touch OPEN tasks. Each new task is small (one function, at most 3 behaviours, one new test file), a vertical slice that adds something visible or usable to the running app, and its done-when is checked by the test command with REAL code (fakes only for the outside world). Prefer fixing what blocks the running app over new features. If the plan is on track, write no change lines.

SPEC:
{spec}

TASKS:
{tasks}

PROGRESS:
{progress}

BUILD NUMBERS:
{numbers}

REAL APP CHECK:
{smoke}
"""

COMPACT_PROMPT = """Compact these project lessons into at most 20 short bullet lines, each starting with "- ". Merge duplicates, drop anything obsolete, keep every concrete rule (exact names, commands and numbers stay exact). Output only the bullets, no heading, no other text.

"""

TOOLS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "List files in a directory (non-recursive). Paths are relative to the project, or absolute inside an EXTRA FOLDER.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a text file. Optional 1-based line range. Default: first 300 lines. Paths are relative to the project, or absolute inside an EXTRA FOLDER (read-only).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Create or overwrite a file with full content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "replace_in_file", "description": "Replace one exact occurrence of old with new in a file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
            "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "run", "description": "Run a shell command in the project root (tests, grep, python). 180s timeout.",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {
        "name": "done", "description": "Finish the task. Summary is one line.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
]

BLOCKED_CMD = re.compile(r"\b(sudo|git\s+push|git\s+reset|git\s+checkout|git\s+clean|rm\s+-rf\s+(/|~))|\|\s*(sh|bash)\b")
CD_PREFIX = re.compile(r"^\s*cd\s+(\S+)\s*(&&|;)\s*(.+)$", re.S)
# Searching the whole Mac is slow (minutes), can time out, and looks at the user's private files.
OUTSIDE_SEARCH = re.compile(r"\bfind\s+(/|~|\$HOME)(\s|$)|\b(mdfind|locate)\b|\bls\s+-\w*R\w*\s+(/|~)(\s|$)|\bgrep\s+-\w*r\w*\b[^|;&]*\s(/|~)(\s|$)")


# ---------------------------------------------------------------- helpers
def log(msg):
    line = time.strftime("%H:%M:%S") + " " + msg
    print(line, flush=True)
    if CUR is not None and CUR.o.exists():     # feeds `orca.py watch`
        try:
            with open(str(CUR.o / "events.log"), "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _kill_group(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        p.communicate(timeout=5)
    except Exception:
        pass


def sh(cmd, cwd=None, timeout=120, inp=None):
    """Runs a command in its OWN process group, so a timeout or Ctrl-C ends everything it started.
    (Real failure: timed-out `node --test` runs left their test children alive for days.)"""
    try:
        p = subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=cwd, text=True, start_new_session=True,
                             stdin=subprocess.PIPE if inp is not None else None,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as e:
        return 127, str(e)
    try:
        out, err = p.communicate(inp, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(p)
        return 124, "TIMEOUT after %ss" % timeout
    except BaseException:                    # Ctrl-C or K from the dashboard: take the children down too
        _kill_group(p)
        raise
    return p.returncode, (out or "") + (err or "")


def clip(s, n):
    if len(s) <= n:
        return s
    h = n // 2
    return s[:h] + "\n...[clipped %d chars]...\n" % (len(s) - n) + s[-h:]


def live_budget(P):
    """prompt_budget_tokens read fresh from orca.json, so a change in the dashboard counts from the next step."""
    try:
        v = int(json.loads((P.root / "orca.json").read_text()).get("prompt_budget_tokens", 0))
        if v >= 4000:
            return v
    except (OSError, ValueError, TypeError):
        pass
    return P.cfg["prompt_budget_tokens"]


def est_tokens(msgs):
    return sum(len(json.dumps(m)) for m in msgs) // 3   # conservative for code


def strip_think(s):
    return re.sub(r"<think>.*?</think>", "", s or "", flags=re.S).strip()


CUR = None   # current project, used by record()
ACTIONS = []  # last tool calls, shown by the UI


def record(kind, purpose, **n):
    """Accumulate token counts in orca/usage.json (kind = qwen | claude)."""
    if CUR is None:
        return
    p = CUR.o / "usage.json"
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        d = {}
    e = d.setdefault(kind, {}).setdefault(purpose, {"calls": 0})
    e["calls"] += 1
    for k, v in n.items():
        e[k] = round(e.get(k, 0) + (v or 0), 6)
    CUR.o.mkdir(exist_ok=True)
    p.write_text(json.dumps(d, indent=1))
    if kind == "claude":
        tok = (n.get("input") or 0) + (n.get("output") or 0) + (n.get("cache_create") or 0)
        try:
            with open(str(CUR.o / "claude_calls.jsonl"), "a") as f:
                f.write(json.dumps({"t": time.time(), "purpose": purpose, "tokens": tok}) + "\n")
        except OSError:
            pass


def set_state(**kw):
    """Tiny state file so `watch` can show what the run is doing right now."""
    if CUR is None or not CUR.o.exists():
        return
    p = CUR.o / "state.json"
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        d = {}
    d.update(kw)
    d["updated"] = time.time()
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(str(tmp), str(p))      # atomic, so the UI never reads half a file
    except OSError:
        pass


def window_tokens(P, hours=None):
    """Claude tokens (in + out + cache write) used by this tool in the last N hours."""
    cutoff = time.time() - 3600 * (hours or P.cfg["quota_window_hours"])
    tot, calls = 0, 0
    for l in P.rd("claude_calls.jsonl").splitlines():
        try:
            j = json.loads(l)
        except ValueError:
            continue
        if j.get("t", 0) >= cutoff:
            tot += j.get("tokens", 0)
            calls += 1
    return tot, calls


# ---------------------------------------------------------------- real subscription usage
QUOTA_FILE = Path.home() / ".localorca" / "quota.jsonl"   # shared by all projects, so "today" adds up


def claude_token():
    """Claude Code's OAuth access token (file first, then macOS Keychain). Never logged, never stored by us."""
    cfgdir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
    for src in ("file", "keychain"):
        try:
            if src == "file":
                raw = (cfgdir / ".credentials.json").read_text()
            else:
                rc, raw = sh(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"], timeout=20)
                if rc != 0:
                    continue
            j = json.loads(raw)
            o = j.get("claudeAiOauth") or j
            if o.get("accessToken"):
                return o["accessToken"], o.get("expiresAt")
        except (OSError, ValueError, AttributeError):
            continue
    return None, None


def parse_quota(j):
    out = {"raw_keys": sorted(j.keys()) if isinstance(j, dict) else []}

    def grab(name, kinds):
        v = j.get(name)
        if isinstance(v, dict) and v.get("utilization") is not None:
            return float(v["utilization"]), v.get("resets_at")
        for l in j.get("limits") or []:
            if isinstance(l, dict) and l.get("kind") in kinds and l.get("percent") is not None and not l.get("scope"):
                return float(l["percent"]), l.get("resets_at")
        return None, None
    out["five"], out["five_reset"] = grab("five_hour", ("five_hour", "session"))
    out["week"], out["week_reset"] = grab("seven_day", ("seven_day", "weekly"))
    return out


def fetch_quota():
    """Ask Anthropic for the plan usage Claude Code's /usage screen shows. Undocumented endpoint, best effort.
    Does NOT refresh the token (that could log Claude Code out)."""
    tok, exp = claude_token()
    if not tok:
        return {"error": "no Claude Code login found, run `claude` once"}
    if exp and exp / 1000.0 < time.time():
        return {"error": "login token expired, open Claude Code once"}
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage",
                                 headers={"Authorization": "Bearer " + tok, "anthropic-beta": "oauth-2025-04-20",
                                          "User-Agent": "LocalOrca"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return parse_quota(json.load(r))
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After") if e.headers else None
        return {"error": "HTTP %d" % e.code, "retry_after": int(ra) if ra and ra.isdigit() else 0}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"error": str(e)[:60]}


QUOTA_CACHE = Path.home() / ".localorca" / "quota_cache.json"   # shared by all processes, so they poll together, not each on their own
QUOTA_EVERY = 300      # seconds between real requests. The endpoint is undocumented and rate limits (HTTP 429)
QUOTA_BACKOFF = 900    # wait at least this long after a 429


def _cache_read():
    try:
        return json.loads(QUOTA_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def fetch_and_record(force=False):
    """Usage with a shared cache: at most one real request per QUOTA_EVERY seconds across all orca processes,
    a longer pause after HTTP 429, and the last good numbers (marked stale) while we wait."""
    now = time.time()
    c = _cache_read()
    good = c.get("q") if c.get("q") and not c["q"].get("error") else None

    def cached():
        if good:
            return dict(good, stale=int(now - c.get("t", now)))
        return {"error": c.get("err") or "waiting"}
    if not force and now < c.get("next_ok", 0):
        return cached()
    q = fetch_quota()
    if not q.get("error") and (q.get("five") is not None or q.get("week") is not None):
        try:
            QUOTA_FILE.parent.mkdir(exist_ok=True)
            with open(str(QUOTA_FILE), "a") as f:
                f.write(json.dumps({"t": now, "five": q.get("five"), "week": q.get("week")}) + "\n")
            QUOTA_CACHE.write_text(json.dumps({"t": now, "q": q, "next_ok": now + QUOTA_EVERY}))
        except OSError:
            pass
        return q
    wait = max(q.get("retry_after", 0), QUOTA_BACKOFF) if q.get("error") == "HTTP 429" else 120
    try:
        QUOTA_FILE.parent.mkdir(exist_ok=True)
        QUOTA_CACHE.write_text(json.dumps({"t": c.get("t", now), "q": c.get("q"), "next_ok": now + wait, "err": q.get("error")}))
    except OSError:
        pass
    if good:
        return dict(good, stale=int(now - c.get("t", now)))
    return q


def today_delta():
    """Weekly-limit % used since local midnight, from our own samples."""
    midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = []
    try:
        for l in QUOTA_FILE.read_text().splitlines()[-5000:]:
            try:
                r = json.loads(l)
            except ValueError:
                continue
            if r.get("week") is not None:
                rows.append(r)
    except OSError:
        return None
    if not rows:
        return None
    before = [r for r in rows if midnight - 36 * 3600 <= r["t"] < midnight]
    base = before[-1]["week"] if before else next((r["week"] for r in rows if r["t"] >= midnight), None)
    if base is None:
        return None
    now = rows[-1]["week"]
    return now - base if now >= base else now      # a weekly reset in between: count from zero


def fmt_reset(iso):
    try:
        d = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return "?"
    far = (d - datetime.datetime.now(d.tzinfo)).total_seconds() > 86400
    return d.strftime("%a %H:%M") if far else d.strftime("%H:%M")


def quota_line(q):
    if not q:
        return ""
    if q.get("error"):
        return "Subscription: unavailable (%s)" % q["error"]
    if q.get("five") is None and q.get("week") is None:
        return "Subscription: unknown response format, run `python3 orca.py quota --raw`"
    parts = []
    if q.get("five") is not None:
        parts.append("5h window %.0f%% (resets %s)" % (q["five"], fmt_reset(q.get("five_reset"))))
    if q.get("week") is not None:
        parts.append("week %.0f%%" % q["week"])
    d = today_delta()
    if d is not None:
        parts.append("today +%.1f%% of week" % d)
    return "Subscription: " + " | ".join(parts)


def quota_short(q):
    if not q or q.get("error"):
        return "plan %%: n/a%s" % (" (%s)" % q["error"] if q and q.get("error") else "")
    parts = []
    if q.get("five") is not None:
        parts.append("5h %.0f%%" % q["five"])
    if q.get("week") is not None:
        parts.append("wk %.0f%%" % q["week"])
    d = today_delta()
    if d is not None:
        parts.append("day +%.1f%%" % d)
    if q.get("stale", 0) > 60:
        parts.append("(%dm old)" % (q["stale"] // 60))
    return "  ".join(parts) or "plan %: n/a"


def usage_report(P):
    try:
        d = json.loads(P.rd("usage.json"))
    except ValueError:
        d = {}
    cost_on = P.cfg["show_cost"]
    lines = []
    q = d.get("qwen", {}).get("build")
    if q:
        lines.append("Qwen (local, free): %d calls, %d prompt tok, %d completion tok"
                     % (q["calls"], q.get("prompt", 0), q.get("completion", 0)))
    tot, cost = 0, 0.0
    for purpose, e in d.get("claude", {}).items():
        tot += e.get("input", 0) + e.get("output", 0) + e.get("cache_create", 0)
        cost += e.get("cost_usd", 0)
        lines.append("Claude %-9s %d calls, in %d, out %d, cache write %d, cache read %d%s"
                     % (purpose, e["calls"], e.get("input", 0), e.get("output", 0), e.get("cache_create", 0),
                        e.get("cache_read", 0), (", $%.3f" % e.get("cost_usd", 0)) if cost_on else ""))
    if d.get("claude"):
        lines.append("Claude total: %d tokens (in + out + cache write)%s" % (tot, (", $%.3f API-equivalent" % cost) if cost_on else ""))
        wt, wc = window_tokens(P)
        s = "Claude last %dh: %d tokens in %d calls" % (P.cfg["quota_window_hours"], wt, wc)
        if P.cfg["claude_quota_tokens"]:
            s += ", %.1f%% of your %d token window budget" % (100.0 * wt / P.cfg["claude_quota_tokens"], P.cfg["claude_quota_tokens"])
        lines.append(s)
    if P.cfg["quota_check"]:
        lines.append(quota_line(fetch_and_record()))
    return "\n".join(lines) or "no usage recorded yet"


# ---------------------------------------------------------------- statistics (whole project)
def fmt_dur(s):
    s = int(max(0, s))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm %02ds" % (s // 60, s % 60)
    return "%dh %02dm" % (s // 3600, (s % 3600) // 60)


def fmt_tok(n):
    n = int(n or 0)
    if n >= 1000000:
        return "%.2fM" % (n / 1e6)
    if n >= 1000:
        return "%.1fk" % (n / 1e3)
    return str(n)


def jlog(P, name, obj):
    try:
        P.o.mkdir(exist_ok=True)
        with open(str(P.o / name), "a") as f:
            f.write(json.dumps(obj) + "\n")
    except OSError:
        pass


def jread(P, name):
    out = []
    for l in P.rd(name).splitlines():
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def git_history(P):
    """(unix time, subject) for every commit, oldest first. Covers work done before sessions were recorded."""
    rc, out = P.git("log --reverse '--format=%ct|%s'")
    rows = []
    if rc == 0:
        for l in out.splitlines():
            a, _, b = l.partition("|")
            if a.isdigit():
                rows.append((int(a), b))
    return rows


def session_spans(P, st):
    """(start, end) of every recorded run of this project. A run that was killed hard has no end line."""
    starts, ends = {}, {}
    for r in jread(P, "sessions.jsonl"):
        if "end" in r and "start" in r:
            ends[r["start"]] = r["end"]
        elif "start" in r:
            starts[r["start"]] = r.get("pid")
    spans, order = [], sorted(starts)
    for i, s0 in enumerate(order):
        e = ends.get(s0)
        if e is None:
            if starts[s0] and running_pid(P) == starts[s0]:
                e = time.time()
            elif i == len(order) - 1:
                e = max(s0, min(st.get("updated", s0), time.time()))
            else:
                e = s0
        spans.append((s0, e))
    return spans


def legacy_active(rows, before):
    """Estimate for work done before runs were recorded: gaps between commits, each capped at 30 minutes."""
    tot, prev = 0, None
    for t, _ in rows:
        if before and t >= before:
            break
        if prev is not None:
            tot += min(t - prev, 1800)
        prev = t
    return tot


def task_times(P, rows):
    """{task id: {"secs", "tries", "qwen", "claude", "est"}}. Recorded values win, older tasks are estimated from commit gaps."""
    out = {}
    for r in jread(P, "tasks.jsonl"):
        e = out.setdefault(r["id"], {"secs": 0, "tries": 0, "qwen": 0, "claude": 0, "est": False})
        e["secs"] += r.get("secs", 0)
        e["tries"] += r.get("attempts", 1)
        e["qwen"] += r.get("qwen_in", 0) + r.get("qwen_out", 0)
        e["claude"] += r.get("claude", 0)
    prev = None
    for t, subj in rows:
        m = re.match(r"^(?:orca: block )?(T\d+)\b", subj)
        if m and m.group(1) not in out and prev is not None:
            out[m.group(1)] = {"secs": min(t - prev, 1800), "tries": 0, "qwen": 0, "claude": 0, "est": True}
        prev = t
    return out


def timed_task(P, task):
    """do_task plus a record of how long it took and what it cost, for the statistics screen."""
    t0 = time.time()
    try:
        u = json.loads(P.rd("usage.json")).get("qwen", {})
    except ValueError:
        u = {}
    qi0 = sum(e.get("prompt", 0) for e in u.values())
    qo0 = sum(e.get("completion", 0) for e in u.values())
    cl0 = window_tokens(P, 24 * 365)[0]
    ok = do_task(P, task)
    try:
        u = json.loads(P.rd("usage.json")).get("qwen", {})
        st = json.loads(P.rd("state.json"))
    except ValueError:
        u, st = {}, {}
    jlog(P, "tasks.jsonl", {"id": task["id"], "t": t0, "secs": round(time.time() - t0, 1), "ok": ok,
                            "attempts": st.get("attempt", 1),
                            "qwen_in": sum(e.get("prompt", 0) for e in u.values()) - qi0,
                            "qwen_out": sum(e.get("completion", 0) for e in u.values()) - qo0,
                            "claude": window_tokens(P, 24 * 365)[0] - cl0})
    return ok


def stats_lines(P, quota=None, fetch=True):
    """The whole project from its first commit on: time, tokens of every AI, and a row per task. [(text, kind)]"""
    L = []

    def add(t="", k="n"):
        L.append((t, k))
    now = time.time()
    tasks = P.tasks()
    done = sum(1 for t in tasks if t["mark"] == "x")
    blk = sum(1 for t in tasks if t["mark"] == "!")
    todo = len(tasks) - done - blk
    try:
        st = json.loads(P.rd("state.json"))
    except ValueError:
        st = {}
    rows = git_history(P)
    spans = session_spans(P, st)
    add("PROJECT  %s" % P.root.name, "h")
    add("  tasks   %s %d done, %d blocked, %d to do (of %d)" % (bar((done + blk) / float(max(len(tasks), 1)), 20), done, blk, todo, len(tasks)),
        "ok" if not blk else "warn")
    add("  state   %s" % (st.get("phase") or "not started"), "dim")
    add()
    add("TIME (whole project)", "h")
    first = rows[0][0] if rows else (spans[0][0] if spans else None)
    last = max([r[0] for r in rows[-1:]] + [e for _, e in spans]) if (rows or spans) else None
    rec_active = sum(e - s for s, e in spans)
    old_active = legacy_active(rows, spans[0][0] if spans else None)
    if first:
        add("  started          %s" % time.strftime("%a %d %b %H:%M", time.localtime(first)))
        add("  since start     %s   (wall clock, includes pauses)" % fmt_dur((now if running_pid(P) else (last or now)) - first))
    add("  active build    %s   (%d run%s%s)" % (fmt_dur(rec_active + old_active), len(spans), "" if len(spans) == 1 else "s",
                                                  ", earlier work estimated from git" if old_active else ""))
    tt = task_times(P, rows)
    fin = [(tid, tt[tid]["secs"]) for t in tasks for tid in (t["id"],) if t["mark"] == "x" and tid in tt and tt[tid]["secs"] > 0]
    if fin:
        avg = sum(s for _, s in fin) / float(len(fin))
        slow = max(fin, key=lambda x: x[1])
        fast = min(fin, key=lambda x: x[1])
        add("  per task         avg %s, slowest %s (%s), fastest %s (%s)" % (fmt_dur(avg), slow[0], fmt_dur(slow[1]), fast[0], fmt_dur(fast[1])))
        if todo:
            add("  still to go     ~%s at this pace (a guess: %d tasks x %s)" % (fmt_dur(avg * todo), todo, fmt_dur(avg)), "info")
    add()
    try:
        d = json.loads(P.rd("usage.json"))
    except ValueError:
        d = {}
    add("TOKENS (every AI on this project)", "h")
    qw = d.get("qwen", {})
    qin, qout = sum(e.get("prompt", 0) for e in qw.values()), sum(e.get("completion", 0) for e in qw.values())
    qcalls = sum(e.get("calls", 0) for e in qw.values())
    qtot = qin + qout
    add("  %-22s %6s calls   in %-8s out %-8s total %s" % ("Qwen (local, free)", qcalls, fmt_tok(qin), fmt_tok(qout), fmt_tok(qtot)), "ai")
    ctot = 0
    for purpose, e in sorted(d.get("claude", {}).items()):
        t = e.get("input", 0) + e.get("output", 0) + e.get("cache_create", 0)
        ctot += t
        add("  %-22s %6s calls   in %-8s out %-8s total %s   (cache write %s, read %s)" % (
            "Claude " + purpose, e.get("calls", 0), fmt_tok(e.get("input", 0)), fmt_tok(e.get("output", 0)), fmt_tok(t),
            fmt_tok(e.get("cache_create", 0)), fmt_tok(e.get("cache_read", 0))), "info")
    if d.get("claude_model_used") or P.cfg.get("claude_model"):
        add("  Claude model: %s (last call used %s)" % (claude_model_now(P.cfg) or "Claude Code default", d.get("claude_model_used", "?")), "dim")
    add("  %-22s %s tokens   = Qwen %s + Claude %s" % ("ALL AIs", fmt_tok(qtot + ctot), fmt_tok(qtot), fmt_tok(ctot)), "bold")
    if qtot + ctot:
        add("  Qwen did %.0f%% of the tokens %s" % (100.0 * qtot / (qtot + ctot), bar(qtot / float(qtot + ctot), 20)), "dim")
    if done and qtot:
        add("  per finished task: Qwen %s, Claude %s" % (fmt_tok(qtot / done), fmt_tok(ctot / done)), "dim")
    add("  Claude tokens = in + out + cache write. Cache reads are listed but not counted (they are cheap).", "dim")
    add()
    add("SUBSCRIPTION", "h")
    if P.cfg["quota_check"]:
        q = quota if quota is not None else (fetch_and_record() if fetch else None)
        add("  " + (quota_line(q) or "not checked yet"))
    wt, wc = window_tokens(P)
    add("  Claude in the last %dh: %s tokens in %d calls (this tool only)" % (P.cfg["quota_window_hours"], fmt_tok(wt), wc))
    add()
    add("TASKS", "h")
    add("  %-5s %-8s %5s %9s %9s %9s   %s" % ("id", "result", "tries", "time", "Qwen tok", "Claude", "title"), "dim")
    for t in tasks:
        e = tt.get(t["id"])
        res = {"x": "done", "!": "BLOCKED", " ": "todo"}.get(t["mark"], "?")
        if t["mark"] == " " and st.get("task") == t["id"]:
            res = "running"
        title = t["text"].split("|")[0].strip()[:44]
        if e:
            tm = ("~" if e["est"] else "") + fmt_dur(e["secs"])
            add("  %-5s %-8s %5s %9s %9s %9s   %s" % (t["id"], res, e["tries"] or "-", tm, fmt_tok(e["qwen"]) if e["qwen"] else "-",
                                                      fmt_tok(e["claude"]) if e["claude"] else "-", title),
                {"done": "ok", "BLOCKED": "bad", "running": "warn"}.get(res, "n"))
        else:
            add("  %-5s %-8s %5s %9s %9s %9s   %s" % (t["id"], res, "-", "-", "-", "-", title), {"BLOCKED": "bad", "running": "warn"}.get(res, "dim"))
    add()
    add("  ~ = estimated from git commit times (that task ran before runs were recorded).", "dim")
    return L


def kill_run(P, wait=8, with_preview=False):
    """Really end the build process: Ctrl-C first (clean: state saved, pid file removed), then SIGKILL if it hangs.
    A build started by the dashboard has its own process group, so its leftovers (pytest, claude) are cleaned up too."""
    pid = running_pid(P)
    if not pid:
        (P.o / "run.pid").unlink() if (P.o / "run.pid").exists() else None
        extra = stop_preview(P) if with_preview else ""
        return "no running build found (a leftover run.pid was removed)" + ("; " + extra if extra else "")
    try:
        own_group = os.getpgid(pid) == pid
    except OSError:
        own_group = False
    try:
        os.kill(pid, signal.SIGINT)
    except OSError:
        pass
    for _ in range(wait * 5):
        if not running_pid(P):
            break
        time.sleep(0.2)
    how = "stopped with Ctrl-C"
    if running_pid(P):
        how = "did not react to Ctrl-C, force killed"
        try:
            os.killpg(pid, signal.SIGKILL) if own_group else os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        ended = {r["start"] for r in jread(P, "sessions.jsonl") if "end" in r}
        for r in jread(P, "sessions.jsonl"):
            if r.get("pid") == pid and "start" in r and "end" not in r and r["start"] not in ended:
                jlog(P, "sessions.jsonl", {"start": r["start"], "end": time.time()})
        time.sleep(0.3)
    elif own_group:
        try:
            os.killpg(pid, signal.SIGTERM)      # children the build left behind
        except OSError:
            pass
    if (P.o / "run.pid").exists():
        (P.o / "run.pid").unlink()
    set_state(phase="killed by you")
    extra = stop_preview(P) if with_preview else ""
    return "build (pid %d) %s%s. Finished tasks are committed, run again to resume." % (pid, how, " and the " + extra if extra else "")


def cmd_kill(a):
    print(kill_run(Project(a.dir), with_preview=True))


def set_model(P, mid):
    """Pin the model in orca.json ("auto" lets LocalOrca pick). Keeps every other setting."""
    cp = P.root / "orca.json"
    try:
        c = json.loads(cp.read_text()) if cp.exists() else {}
    except ValueError:
        c = {}
    c["model"] = mid
    if c.get("model_key"):
        c["model_key"] = "" if mid == "auto" else mid
    cp.write_text(json.dumps(c, indent=2) + "\n")
    P.cfg.update(c)


CONTEXT_PRESETS = [          # (prompt budget, model context): the budget is our cautious estimate, the context is what LM Studio loads
    (16000, 24576), (22000, 32768), (28000, 32768), (32000, 40960), (40000, 49152),
]


def set_context(P, budget, ctx):
    """Save the prompt budget and the model context in orca.json. The budget counts from the next step; the context
    needs a model reload (done at the next start, see loaded_context)."""
    cp = P.root / "orca.json"
    try:
        c = json.loads(cp.read_text()) if cp.exists() else {}
    except ValueError:
        c = {}
    c["prompt_budget_tokens"] = int(budget)
    c["context_tokens"] = int(ctx)
    cp.write_text(json.dumps(c, indent=2) + "\n")
    P.cfg.update(prompt_budget_tokens=int(budget), context_tokens=int(ctx))


def loaded_context(cfg, mid):
    """The context length LM Studio really loaded the model with, or None if it cannot tell."""
    for m in all_models(cfg) or []:
        if m.get("id") == mid and m.get("state") == "loaded":
            return m.get("loaded_context_length")
    return None


def set_claude_model(P, name):
    cp = P.root / "orca.json"
    try:
        c = json.loads(cp.read_text()) if cp.exists() else {}
    except ValueError:
        c = {}
    c["claude_model"] = name
    cp.write_text(json.dumps(c, indent=2) + "\n")
    P.cfg["claude_model"] = name


def preview_pid(P):
    try:
        pid = int(P.rd("preview.pid").split()[0])
        os.kill(pid, 0)
    except (ValueError, IndexError, ProcessLookupError):
        return None
    except PermissionError:
        pass
    rc, out = sh("ps -p %d -o command=" % pid, timeout=10)
    return pid if rc != 0 or "orca" in out else None


def preview_port_running(P):
    """The port a running preview recorded in preview.pid ("pid port"). An older preview recorded none: it used 7777."""
    if not preview_pid(P):
        return None
    parts = P.rd("preview.pid").split()
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 7777


def choose_preview_port(P):
    """Each project gets its own preview port. orca.json "preview_port" wins when you set it. Otherwise the port comes from
    the project path (stable between runs) and moves up when it is taken, so two apps never share a page.
    (Real failure: ReadAlong's preview could not start, port 7777 was IslandSurvivor's, and the browser showed Island.)"""
    try:
        explicit = json.loads((P.root / "orca.json").read_text()).get("preview_port")
    except (OSError, ValueError):
        explicit = None
    if explicit:
        return int(explicit)
    import socket
    import zlib
    start = 7777 + zlib.crc32(str(P.root.resolve()).encode()) % 20
    for i in range(60):
        port = 7777 + (start - 7777 + i) % 60
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    return start


def stop_preview(P, wait=6):
    """End the preview server (it stops your app too). Returns a short message, or "" when none was running."""
    pid = preview_pid(P)
    if not pid:
        return ""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(wait * 5):
        if not preview_pid(P):
            break
        time.sleep(0.2)
    if preview_pid(P):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        (P.o / "preview.pid").unlink()
    except OSError:
        pass
    return "preview (pid %d) stopped" % pid


def start_preview_bg(P, open_browser=True):
    """Starts the live preview as its own process (if it is not running) and returns its URL, "" without preview_url."""
    try:
        cfg = json.loads((P.root / "orca.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    if not cfg.get("preview_url"):
        return ""
    P.cfg.update(cfg)
    if not preview_pid(P):
        P.o.mkdir(exist_ok=True)
        out = open(str(P.o / "preview.out"), "a")
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "preview", str(P.root)],
                         stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(15):
            time.sleep(0.2)
            if preview_port_running(P):
                break
    url = "http://localhost:%s" % (preview_port_running(P) or choose_preview_port(P))
    if open_browser and sys.platform == "darwin":
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return url


def cmd_preview(a):
    P = Project(a.dir)
    if preview_pid(P):
        sys.exit("a preview is already running for this project: http://localhost:%s" % preview_port_running(P))
    import orca_preview
    orca_preview.run(P)


def cmd_stats(a):
    P = Project(a.dir)
    print("\n".join(t for t, _ in stats_lines(P)))


def cmd_report(a):
    import orca_report
    P = Project(a.dir)
    print(orca_report.write_report(P))
    print("(saved to %s/REPORT.md)" % P.o.name)


# The build's notes folder. It used to be the hidden ".orca"; now it is a visible "orca" folder.
ORCA_DIR, LEGACY_DIR = "orca", ".orca"
NOTES_DIRS = (ORCA_DIR, LEGACY_DIR)
RUNTIME_NAMES = ["logs/", "failed/", "STOP", "state.json", "events.log", "usage.json", "claude_calls.jsonl", "run.pid",
                 "protected.json", "run.out", "sessions.jsonl", "tasks.jsonl", "preview.out", "preview.pid", "REPORT.md", "checkpoint.png",
                 "popups_seen.json", "shots/"]


def runtime_ignores(d=ORCA_DIR):
    return ["%s/%s" % (d, n) for n in RUNTIME_NAMES]


def migrate_notes_dir(P):
    """Move an old hidden .orca folder to the visible orca folder (keeps git history). Only called by `run`."""
    old, new = P.root / LEGACY_DIR, P.root / ORCA_DIR
    if not old.is_dir() or new.exists():
        return
    rc, out = P.git("mv %s %s" % (LEGACY_DIR, ORCA_DIR))
    if rc != 0:
        old.rename(new)
    P.o = new
    gi = P.root / ".gitignore"
    if gi.exists():
        gi.write_text(gi.read_text().replace(LEGACY_DIR + "/", ORCA_DIR + "/"))
    P.git("add -A -- %s .gitignore" % ORCA_DIR)
    P.git("commit -qm %s" % json.dumps("orca: move .orca to the visible orca folder"))
    log("moved the hidden .orca folder to orca/ (easier to see and edit)")

# ---------------------------------------------------------------- project
class Project:
    def __init__(self, root):
        global CUR
        CUR = self
        self.root = Path(root).expanduser().resolve()
        self.o = self.root / (LEGACY_DIR if (self.root / LEGACY_DIR).is_dir() and not (self.root / ORCA_DIR).exists() else ORCA_DIR)
        self.cfg = dict(DEFAULTS)
        cp = self.root / "orca.json"
        if cp.exists():
            self.cfg.update(json.loads(cp.read_text()))

    def rd(self, name):
        p = self.o / name
        return p.read_text() if p.exists() else ""

    def wr(self, name, text):
        self.o.mkdir(exist_ok=True)
        (self.o / name).write_text(text)

    def git(self, args, timeout=60):
        return sh("git -c user.name=LocalOrca -c user.email=orca@localhost " + args, cwd=self.root, timeout=timeout)

    def commit(self, msg):
        self.git("add -A")
        rc, out = self.git("commit -qm %s" % json.dumps(msg))
        return rc == 0

    def head(self):
        rc, out = self.git("rev-parse HEAD")
        return out.strip() if rc == 0 else ""

    def protected(self):
        """Files the user protected from the dashboard while a build runs (cleared when each task starts)."""
        try:
            return set(json.loads(self.rd("protected.json")))
        except ValueError:
            return set()

    # ---- tasks
    TASK_RE = re.compile(r"^- \[( |x|!)\] ([A-Za-z]\d+): (.*)$")

    def tasks(self):
        out = []
        for line in self.rd("TASKS.md").splitlines():
            m = self.TASK_RE.match(line)
            if m:
                out.append({"mark": m.group(1), "id": m.group(2), "text": m.group(3)})
        return out

    def next_task(self):
        for t in self.tasks():
            if t["mark"] == " ":
                return t
        return None

    def mark(self, tid, mark):
        lines = self.rd("TASKS.md").splitlines()
        for i, line in enumerate(lines):
            m = self.TASK_RE.match(line)
            if m and m.group(2) == tid:
                lines[i] = "- [%s] %s: %s" % (mark, tid, m.group(3))
        self.wr("TASKS.md", "\n".join(lines) + "\n")

    def add_tasks_front(self, items):
        lines = self.rd("TASKS.md").splitlines()
        n = len([1 for t in self.tasks() if t["id"].startswith("R")])
        new = ["- [ ] R%02d: %s" % (n + i + 1, it) for i, it in enumerate(items)]
        idx = next((i for i, l in enumerate(lines) if self.TASK_RE.match(l) and l.startswith("- [ ]")), len(lines))
        self.wr("TASKS.md", "\n".join(lines[:idx] + new + lines[idx:]) + "\n")

    def remove_task(self, tid):
        lines = self.rd("TASKS.md").splitlines()
        keep = [l for l in lines if not (self.TASK_RE.match(l) and self.TASK_RE.match(l).group(2) == tid)]
        self.wr("TASKS.md", "\n".join(keep) + "\n")

    def insert_task(self, line, before=None):
        """Insert a full task line before task `before` (or at the end of the task list)."""
        lines = self.rd("TASKS.md").splitlines()
        idx = len(lines)
        for i, l in enumerate(lines):
            m = self.TASK_RE.match(l)
            if m and m.group(2) == before:
                idx = i
                break
        else:
            idx = max([i + 1 for i, l in enumerate(lines) if self.TASK_RE.match(l)] or [len(lines)])
        self.wr("TASKS.md", "\n".join(lines[:idx] + [line] + lines[idx:]) + "\n")

    # ---- memory (agent side): lessons are durable, progress is a rolling tail
    def add_lesson(self, line):
        cur = self.rd("LESSONS.md")
        if line.strip() and line.strip() not in cur:
            self.wr("LESSONS.md", cur + "- " + line.strip().lstrip("- ") + "\n")
        self.compact_lessons()

    def add_progress(self, line):
        lines = [l for l in self.rd("PROGRESS.md").splitlines() if l.strip()]
        lines.append("- " + line.strip())
        self.wr("PROGRESS.md", "\n".join(lines[-self.cfg["progress_keep"]:]) + "\n")

    def compact_lessons(self):
        txt = self.rd("LESSONS.md")
        if len(txt) <= self.cfg["lessons_max_chars"]:
            return
        cap = self.cfg["lessons_max_chars"]
        log("compacting LESSONS.md (%d chars) with the local model" % len(txt))
        out, why = "", ""
        try:
            r = lm_chat(self.cfg, [{"role": "user", "content": COMPACT_PROMPT + txt}], "off", use_tools=False, max_tokens=2000)
            out = strip_think(r["choices"][0]["message"].get("content") or "").strip()
        except Exception as e:               # LM Studio busy or down: fall back, never stop the build
            why = "local model error: %s" % e
        lines = [l for l in out.splitlines() if l.strip()]
        if not why:
            if not lines or any(not l.lstrip().startswith("-") for l in lines):
                why = "reply is not a bullet list"
            elif len(out) > cap * 0.6:
                why = "reply too long (%d chars)" % len(out)
            elif len(out) < min(len(txt), cap) * 0.15:
                why = "reply suspiciously short (%d chars)" % len(out)
        if why:
            keep, size = [], 0                # deterministic fallback: newest lessons that fit in half the cap
            for l in reversed(txt.splitlines()):
                if size + len(l) + 1 > cap // 2:
                    break
                keep.insert(0, l)
                size += len(l) + 1
            self.wr("LESSONS.md", "\n".join(keep) + "\n")
            log("  local compaction rejected (%s), kept the newest %d lessons instead" % (why, len(keep)))
        else:
            self.wr("LESSONS.md", "\n".join(lines) + "\n")
            log("  LESSONS.md compacted by the local model: %d -> %d chars, %d lines (old version in git)" % (len(txt), len(out), len(lines)))


def claude_model_now(cfg):
    """Read fresh from orca.json, so a change made with L in the dashboard counts from the next Claude call."""
    try:
        if CUR is not None:
            return json.loads((CUR.root / "orca.json").read_text()).get("claude_model", cfg.get("claude_model", "")) or ""
    except (OSError, ValueError):
        pass
    return cfg.get("claude_model", "") or ""


def note_claude_model(j):
    """Remember which Claude model really answered (from modelUsage in claude's JSON output)."""
    mu = j.get("modelUsage") or {}
    if not mu or CUR is None:
        return
    name = max(mu, key=lambda k: (mu[k] or {}).get("outputTokens", 0) if isinstance(mu[k], dict) else 0)
    p = CUR.o / "usage.json"
    try:
        d = json.loads(p.read_text())
        d["claude_model_used"] = name
        p.write_text(json.dumps(d, indent=1))
    except (OSError, ValueError):
        pass


class Heartbeat:
    """Shows that a long silent step is alive: a spinner with elapsed time on a terminal, a line every `every` seconds otherwise.
    `orca.py new` used to sit there for minutes with no sign of life."""
    def __init__(self, label, hint="", every=15, tty=None):
        self.label, self.hint, self.every = label, hint, every
        self.tty = sys.stdout.isatty() if tty is None else tty
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self.t0 = time.time()

    def _elapsed(self):
        s = int(time.time() - self.t0)
        return "%d:%02d" % (s // 60, s % 60)

    def _run(self):
        i, last = 0, 0
        while not self._stop.wait(0.25 if self.tty else min(self.every, 0.25)):
            if self.tty:
                sys.stdout.write("\r  %s %s  %s %s\033[K" % (orca_bar(None, 22), self.label, self._elapsed(), self.hint))
                sys.stdout.flush()
                i += 1
            elif time.time() - last >= self.every:
                last = time.time()
                print("  ... %s, %s so far %s" % (self.label, self._elapsed(), self.hint), flush=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(1)
        if self.tty:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        print("  done: %s (%s)" % (self.label, self._elapsed()), flush=True)


def ask_claude(cfg, prompt, stdin_text="", purpose="misc", progress=None, hint="", cwd=None, extra_args=()):
    model = claude_model_now(cfg)
    cmd = list(cfg["claude_cmd"]) + (["--model", model] if model else []) + list(extra_args) + ["--output-format", "json", prompt]
    if progress:
        with Heartbeat(progress, hint, every=cfg.get("progress_every", 15)):
            rc, out = sh(cmd, cwd=cwd, timeout=cfg["claude_timeout"], inp=stdin_text or "")
    else:
        rc, out = sh(cmd, cwd=cwd, timeout=cfg["claude_timeout"], inp=stdin_text or "")  # "" closes stdin so claude never waits on it
    if rc != 0:
        log("claude call failed (rc=%s): %s" % (rc, out[:300]))
        return None
    text = out.strip()
    try:
        j = json.loads(text)
        if isinstance(j, dict) and "result" in j:
            if j.get("is_error"):
                log("claude returned an error: %s" % str(j.get("result"))[:300])
                return None
            u = j.get("usage") or {}
            record("claude", purpose, input=u.get("input_tokens"), output=u.get("output_tokens"),
                   cache_read=u.get("cache_read_input_tokens"), cache_create=u.get("cache_creation_input_tokens"),
                   cost_usd=j.get("total_cost_usd"))
            note_claude_model(j)
            text = (j.get("result") or "").strip()
    except ValueError:
        record("claude", purpose)   # plain text reply, usage unknown
    return text


# ---------------------------------------------------------------- new
INTERVIEW_PROMPT = """You are a product interviewer and architect preparing a build brief for a small local coding model that works from files only.
Below is the conversation so far.
If important things are still unclear, ask the 3 to 6 most important open questions, numbered and short. For each, offer a sensible default in brackets so the user can answer "defaults". Cover, as needed: users, must-have vs later features, stack constraints, data and storage, auth, look and feel, how to test, definition of done.
When you have enough, output a first line that is exactly READY, then the complete brief (under 80 lines, plain text, no code) for the user to approve.
If the user asked for changes to a draft brief, apply them and output READY plus the revised brief.
Never write code. No preamble.

CONVERSATION:
"""


def read_block(prompt):
    """Multi-line input. Finish with an empty line. Returns None on EOF."""
    print(prompt)
    lines = []
    while True:
        try:
            l = input()
        except EOFError:
            return "\n".join(lines) if lines else None
        if not l.strip() and lines:
            return "\n".join(lines)
        if l.strip() or lines:
            lines.append(l)


def house_rules_block(P):
    """Your standing rules (house_rules.md next to orca.py, plus one in the project if present) for interview and plan."""
    parts = []
    for p in (Path(__file__).resolve().parent / "house_rules.md", P.root / "house_rules.md"):
        if p.exists():
            parts.append(p.read_text())
    if not parts:
        return ""
    return ("\n\nHOUSE RULES from the user. Follow them when choosing the stack, test_cmd and tasks. "
            "If the brief clearly needs something else, say why in the spec:\n" + clip("\n\n".join(parts), 6000))


def interview(P, seed):
    """Back and forth with Claude until the user accepts a brief. /go accepts, /quit aborts."""
    if not seed:
        seed = read_block("Describe the product (empty line to send):")
        if not seed:
            sys.exit("no description given")
    convo = "USER: " + seed.strip()
    draft = ""
    while True:
        out = ask_claude(P.cfg, INTERVIEW_PROMPT + house_rules_block(P), convo, purpose="interview",
                          progress="Claude is thinking about your answers", hint="(usually under a minute)")
        if not out:
            sys.exit("interview failed (is `claude` installed and logged in?)")
        first, _, rest = out.partition("\n")
        if first.strip() == "READY":
            draft = rest.strip()
            print("\n----- DRAFT BRIEF -----\n%s\n-----------------------" % draft)
            convo += "\n\nCLAUDE (draft brief):\n" + draft
            msg = read_block("Type changes, or /go to accept, /quit to abort (empty line to send):")
        else:
            print("\n" + out + "\n")
            convo += "\n\nCLAUDE:\n" + out
            msg = read_block("Your answers (or /go to stop asking and draft the brief, /quit):")
        if msg is None or msg.strip() == "/quit":
            sys.exit("aborted")
        if msg.strip() == "/go":
            if draft:
                P.wr("interview.md", convo + "\n")
                return draft
            msg = "That is enough detail. Use sensible defaults for anything open and output READY plus the brief now."
        convo += "\n\nUSER: " + msg.strip()
        draft = "" if first.strip() != "READY" else draft


def cmd_new(a):
    root = Path(a.dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    P = Project(root)
    if (P.o / "SPEC.md").exists() or (P.o / "TASKS.md").exists():
        if not getattr(a, "force", False):
            sys.exit("\n%s already has a LocalOrca project (%s/SPEC.md, TASKS.md).\n"
                     "`new` would REPLACE its plan with a fresh one that ignores the code you already have,\n"
                     "and empty LESSONS.md and PROGRESS.md. Your code stays, but Qwen would start rebuilding it.\n\n"
                     "To keep improving this app instead:\n"
                     "  1/ add tasks at the bottom of %s/TASKS.md, like\n"
                     "     - [ ] T40: what to do | done when: what the test checks\n"
                     "  2/ for a new feature also add a line to %s/SPEC.md\n"
                     "  3/ python3 orca.py tui %s --run\n\n"
                     "Really start over? Add --force (the old plan stays in git history)." % (root, P.o, P.o, P.o, root))
        log("--force: replacing the existing plan (the old one stays in git history)")
    brief = a.brief
    if a.brief_file:
        brief = Path(a.brief_file).expanduser().read_text()
    if a.interview or not brief:
        brief = interview(P, brief)
    P.wr("BRIEF.md", brief + "\n")
    if not getattr(a, "qwen_only", False):
        import orca_design
        for name in ("SPEC.md", "RESEARCH.md", "PROTOTYPE.json", "TASKS.md"):
            if (P.o / name).exists():
                P.wr(name, "")              # --force: start the design over (the old files stay in git history)
        orca_design.set_stage(P, "design")
        if sys.stdout.isatty() and not getattr(a, "no_tui", False):
            orca_design.start_bg(P, "design")
            log("design stage: Claude researches and builds the prototype, the dashboard shows it live")
            time.sleep(1.5)
            cmd_tui(argparse.Namespace(dir=str(root), run=False))
        else:
            orca_design.cmd_design(argparse.Namespace(dir=str(root), force=True))
        return
    log("asking Claude to plan the build (SPEC, 30 to 60 tasks, config). This is ONE long call, expect 1 to 4 minutes.")
    out = ask_claude(P.cfg, PLAN_PROMPT.replace("{brief}", brief) + house_rules_block(P), purpose="plan",
                     progress="Claude is writing the plan", hint="(usually 1 to 4 min)")
    if not out:
        sys.exit("planning failed (is `claude` installed and logged in?)")
    parts = re.split(r"^=====(SPEC\.md|TASKS\.md|CONFIG)=====\s*$", out, flags=re.M)
    sec = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if "SPEC.md" not in sec or "TASKS.md" not in sec:
        (root / "plan_raw.txt").write_text(out)
        sys.exit("could not parse plan, raw output saved to plan_raw.txt")
    P.o.mkdir(exist_ok=True)
    P.wr("SPEC.md", sec["SPEC.md"] + "\n")
    P.wr("TASKS.md", sec["TASKS.md"] + "\n")
    P.wr("LESSONS.md", "")
    P.wr("PROGRESS.md", "")
    conf = {}
    try:
        conf = json.loads(re.sub(r"^```\w*|```$", "", sec.get("CONFIG", "{}").strip(), flags=re.M))
    except ValueError:
        log("CONFIG block not valid JSON, set test_cmd by hand in orca.json")
    cp = root / "orca.json"
    base = json.loads(cp.read_text()) if cp.exists() else {}
    base.update({k: v for k, v in conf.items() if k in ("test_cmd", "setup_cmd", "preview_cmd", "preview_url", "smoke_cmd", "smoke_from")})
    cp.write_text(json.dumps(base, indent=2) + "\n")
    (root / ".gitignore").write_text("\n".join(runtime_ignores() + ["__pycache__/", "venv/", ".venv/", "node_modules/"]) + "\n")
    if not (root / ".git").exists():
        sh("git init -q", cwd=root)
    P = Project(root)
    P.commit("orca: plan")
    n = len(P.tasks())
    log("plan received: SPEC %d lines, %d tasks, config keys: %s" % (len(P.rd("SPEC.md").splitlines()), n, ", ".join(sorted(conf)) or "none"))
    stack = re.search(r"^#+\s*Stack.*?(?=^#|\Z)", P.rd("SPEC.md"), flags=re.S | re.M)
    if stack:
        log("STACK (check it now, changing it later is expensive):\n" + stack.group(0).strip()[:1500])
    log("test_cmd: %s" % (json.loads((root / "orca.json").read_text()).get("test_cmd") or "(none!)"))
    log("planned %d tasks. REVIEW %s/TASKS.md and orca.json, then run:" % (n, P.o))
    log("  caffeinate -i python3 %s run %s" % (Path(__file__).name, root))
    if a.go:
        cmd_run(argparse.Namespace(dir=str(root)))


# ---------------------------------------------------------------- LM Studio
def lm_models(cfg):
    with urllib.request.urlopen(cfg["base_url"] + "/models", timeout=15) as r:
        return [m["id"] for m in json.load(r)["data"]]


def think_params(mode):
    """Template switches from the Qwen chat template: enable_thinking, reasoning_effort (xhigh | medium | low)."""
    kw = {}
    if mode == "off":
        kw["chat_template_kwargs"] = {"enable_thinking": False}
    elif mode in ("low", "medium", "xhigh"):
        kw["chat_template_kwargs"] = {"enable_thinking": True, "reasoning_effort": mode}
        if mode in ("low", "medium"):
            kw["reasoning_effort"] = mode
    return kw


def lm_chat(cfg, messages, mode="default", use_tools=True, max_tokens=None):
    body = {"model": cfg["model"], "messages": messages, "stream": False,
            "temperature": cfg["temperature"], "top_p": cfg["top_p"], "max_tokens": max_tokens or cfg["max_output_tokens"]}
    if use_tools:
        body["tools"] = TOOLS
    extras = {} if cfg.get("_no_extras") else think_params(mode)
    if cfg.get("draft_model") and not cfg.get("_no_extras"):
        extras["draft_model"] = cfg["draft_model"]     # speculative decoding: same output, often faster
    for tries in (0, 1):
        req = urllib.request.Request(cfg["base_url"] + "/chat/completions", json.dumps(dict(body, **extras)).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 400 and extras and tries == 0:
                log("  LM Studio rejected the extra options (thinking / draft_model, HTTP 400), continuing without them. "
                    "If you set draft_model, check its name matches LM Studio exactly and that it is compatible.")
                cfg["_no_extras"] = True
                extras = {}
                continue
            raise


def thinking_is_on(P):
    """True/False, or None when the probe request failed."""
    try:
        r = lm_chat(P.cfg, [{"role": "user", "content": "Reply with the single word OK."}], "off", use_tools=False, max_tokens=600)
        m = r["choices"][0]["message"]
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
        log("could not probe thinking mode: %s" % e)
        return None
    raw = m.get("content") or ""
    tm = re.search(r"<think>(.*?)</think>", raw, flags=re.S)
    think = (m.get("reasoning_content") or (tm.group(1) if tm else "")).strip()
    out_tok = (r.get("usage") or {}).get("completion_tokens") or 0
    # When the template pre-fills an empty <think></think>, LM Studio's parser can still file the plain answer
    # ("OK") under reasoning. Real thinking is long, so judge by size, not by which field the text landed in.
    on = len(think.split()) > 15 or out_tok > 60
    log("thinking probe: %d reasoning words, %d output tokens, answer %r" % (len(think.split()), out_tok, clip(strip_think(raw) or think, 40)))
    return on


def probe_thinking(P):
    """When thinking is set to off, check that LM Studio really turned it off. If not, reload the model once:
    a changed prompt template only counts after a reload."""
    if P.cfg["thinking"] != "off":
        return
    on = thinking_is_on(P)
    if on:
        log("thinking is still ON. Reloading the model once, in case its prompt template was just changed.")
        reload_model(P)
        on = thinking_is_on(P)
    if on:
        log("WARNING: thinking is still ON after a reload (LM Studio ignored enable_thinking). Edit the model's Prompt Template, see README, Thinking.")
    elif on is False:
        log("thinking off confirmed")


def all_models(cfg):
    """Every downloaded model with its state, from LM Studio's native API. None if unsupported."""
    root = cfg["base_url"].rsplit("/v1", 1)[0]
    try:
        with urllib.request.urlopen(root + "/api/v0/models", timeout=15) as r:
            return json.load(r)["data"]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None


def param_b(mid):
    m = re.search(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)b\b", mid.lower())
    return float(m.group(1)) if m else 0.0


def suitable_models(cfg, models):
    c = [m for m in models if m.get("type") in ("llm", "vlm") and "embed" not in m["id"].lower()]
    c = [m for m in c if param_b(m["id"]) <= cfg["max_model_b"]]
    big = [m for m in c if param_b(m["id"]) == 0 or param_b(m["id"]) >= cfg["min_model_b"]]
    return big


def pick_model(cfg, cands):
    """Text models beat vision models, then the model_prefer order, then bigger."""
    def key(m):
        mid = m["id"].lower()
        rank = next((i for i, p in enumerate(cfg["model_prefer"]) if re.search(p, mid)), len(cfg["model_prefer"]))
        return (m.get("type") == "vlm", rank, -param_b(mid))
    return min(cands, key=key)["id"] if cands else None


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def lms_stream(cmd, timeout, on_pct):
    """Run a command in a pseudo-terminal so lms prints its progress bar, and report the percentage."""
    import pty
    import select
    master, slave = pty.openpty()
    p = subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    start, tail = time.time(), ""
    try:
        return _lms_loop(p, master, start, tail, timeout, on_pct, select)
    except BaseException:
        p.kill()
        raise


def _lms_loop(p, master, start, tail, timeout, on_pct, select):
    while True:
        ready = select.select([master], [], [], 1.0)[0]
        if ready:
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            text = ANSI.sub("", data.decode("utf-8", "replace"))
            tail = (tail + text)[-2000:]
            found = re.findall(r"(\d{1,3}(?:\.\d+)?)\s*%", text)
            on_pct(min(100.0, float(found[-1])) if found else None, time.time() - start)
        else:
            on_pct(None, time.time() - start)
            if p.poll() is not None:
                break
        if time.time() - start > timeout:
            p.kill()
            os.close(master)
            return 124, tail
    os.close(master)
    return p.wait(), tail


def load_model(P, key):
    """Unload others, load `key`, show a progress bar. Returns (rc, output)."""
    log("loading %s with context %d (this unloads other models)" % (key, P.cfg["context_tokens"]))
    sh("lms unload --all", timeout=120)
    st = {"pct": 0.0, "t": 0.0, "step": -1}

    def on_pct(pct, elapsed):
        if pct is not None:
            st["pct"] = pct
        if time.time() - st["t"] < 0.5 and pct != 100.0:
            return
        st["t"] = time.time()
        set_state(phase="loading model", load_pct=st["pct"], load_name=key, load_secs=int(elapsed))
        line = "loading model %s %3d%%  %ds" % (orca_bar(st["pct"] / 100.0, 28), st["pct"], elapsed)
        if sys.stdout.isatty():
            sys.stdout.write("\r" + line + "  ")
            sys.stdout.flush()
        elif int(st["pct"]) // 10 > st["step"]:
            st["step"] = int(st["pct"]) // 10
            log(line)

    set_state(phase="loading model", load_pct=0, load_name=key, load_secs=0)
    rc, out = lms_stream("lms load %s --context-length %d --gpu max -y" % (json.dumps(key), P.cfg["context_tokens"]),
                         900, on_pct)
    if sys.stdout.isatty():
        sys.stdout.write("\n")
    set_state(phase="model loaded" if rc == 0 else "model load FAILED", load_pct=None)
    log("model %s" % ("loaded" if rc == 0 else "load failed (rc=%s)" % rc))
    return rc, out


def ensure_model(P):
    """Pick the best suitable model and make sure it is the one loaded."""
    c = P.cfg
    models = all_models(c)
    loaded = [m["id"] for m in (models or []) if m.get("state") == "loaded"]
    if c["model"] and c["model"] != "auto":
        want = c["model"]
        log("model pinned in orca.json: " + want)
    elif models is None:
        ids = [m for m in lm_models(c) if "embed" not in m.lower()]
        if not ids:
            sys.exit("LM Studio has no models")
        want = ids[0]
        log("WARNING: cannot tell which model is loaded, using the first listed (%s). Pin \"model\" in orca.json." % want)
    else:
        cands = suitable_models(c, models)
        ok_loaded = [m["id"] for m in cands if m["id"] in loaded]
        if len(ok_loaded) == 1:
            want = ok_loaded[0]
            log("a suitable model is already loaded, keeping it: " + want)
        else:
            want = pick_model(c, cands)
            if not want:
                sys.exit("No suitable model downloaded (need %s-%sB). Download one in LM Studio or pin \"model\"." % (c["min_model_b"], c["max_model_b"]))
            log("picked model: %s (from %d candidates)" % (want, len(cands)))
    have = loaded_context(c, want) if (models is not None and want in loaded) else None
    too_small = bool(have) and have < c["context_tokens"]
    if too_small:
        log("%s is loaded with context %d but orca.json wants %d: reloading" % (want, have, c["context_tokens"]))
    if models is not None and (want not in loaded or too_small):
        rc, out = load_model(P, want)
        if rc != 0:
            sys.exit("lms load failed. Is the lms CLI installed (~/.lmstudio/bin)?\n" + out[-600:])
    c["model"] = want
    c["model_key"] = c["model_key"] or want
    set_state(model=want)
    b = param_b(want)
    if 0 < b < 15:
        log("WARNING: %sB is a small model. Long builds need 27B-class." % b)


# ---------------------------------------------------------------- tools
def extra_dirs_now(P):
    """Read fresh from orca.json, so folders added with (D) in the dashboard count from the next step."""
    try:
        ds = json.loads((P.root / "orca.json").read_text()).get("extra_dirs", P.cfg.get("extra_dirs", []))
    except (OSError, ValueError):
        ds = P.cfg.get("extra_dirs", [])
    out = []
    for d in ds or []:
        p = Path(os.path.expanduser(str(d))).resolve()
        if p.is_dir() and p != Path("/") and p != Path.home().resolve():
            out.append(p)
    return out


def dir_summary(d, cap=2000):
    n = 0
    for _, dn, fn in os.walk(str(d)):
        dn[:] = [x for x in dn if not x.startswith(".")]
        n += len(fn)
        if n >= cap:
            return "%d+ files" % cap
    return "%d files" % n


def read_path(P, rel, allow_notes=False):
    """Like safe_path, but absolute paths inside an extra folder are allowed too (for reading only)."""
    r = str(rel or ".")
    if r.startswith(("/", "~")):
        p = Path(os.path.expanduser(r)).resolve()
        for d in extra_dirs_now(P):
            if p == d or d in p.parents:
                return p
        if p == P.root or P.root in p.parents:
            return safe_path(P.root, str(p.relative_to(P.root)), allow_notes)
        raise ValueError("path outside the project and not in an extra folder (the user can add folders with (D))")
    return safe_path(P.root, r, allow_notes)


def set_extra_dirs(P, dirs):
    cp = P.root / "orca.json"
    try:
        c = json.loads(cp.read_text()) if cp.exists() else {}
    except ValueError:
        c = {}
    c["extra_dirs"] = dirs
    cp.write_text(json.dumps(c, indent=2) + "\n")
    P.cfg["extra_dirs"] = dirs


def safe_path(root, rel, allow_notes=False):
    """Paths for Qwen's tools stay inside the project and out of .git and orca/. The dashboard (you) may open orca/."""
    p = (root / (rel or ".")).resolve()
    if p != root and root not in p.parents:
        raise ValueError("path outside project (extra folders are read-only)")
    parts = p.relative_to(root).parts
    if parts and (parts[0] == ".git" or (parts[0] in NOTES_DIRS and not allow_notes)):
        raise ValueError("no access to .git or the orca/ notes folder")
    return p


def run_tool(P, name, args):
    root = P.root
    try:
        if name == "list_files":
            p = read_path(P, args.get("path", "."))
            return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir()
                                    if x.name not in (".git",) + NOTES_DIRS))[:3000] or "(empty)"
        if name == "read_file":
            p = read_path(P, args["path"])
            lines = p.read_text(errors="replace").splitlines()
            s = max(int(args.get("start_line") or 1), 1)
            e = int(args.get("end_line") or (s + 299))
            chunk = lines[s - 1:e]
            body = "\n".join("%d: %s" % (s + i, l) for i, l in enumerate(chunk))
            tail = "" if e >= len(lines) else "\n[file has %d lines, use start_line/end_line]" % len(lines)
            return clip(body, 8000) + tail
        if name in ("write_file", "replace_in_file"):
            rel = str(safe_path(root, args["path"]).relative_to(root))
            # Old tool calls in the history are shortened to a marker. Models sometimes copy the marker back
            # as if it were code, which writes garbage into the file. Refuse that.
            if any(OMIT_RE.search(str(args.get(k, ""))) for k in ("content", "old", "new")):
                return ("ERROR: you sent the history placeholder ('ORCA-OMITTED ... chars') instead of real text. "
                        "Nothing was written. read_file to see the current text, then send the full real content.")
            if rel in P.protected():
                return ("ERROR: %s is PROTECTED. The user edited it by hand and asked that it is not changed during this task. "
                        "Do not write to it. Work around it, or call done and say what it blocks." % rel)
        if name == "write_file":
            p = safe_path(root, args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"])
            return "wrote %s (%d chars)" % (args["path"], len(args["content"]))
        if name == "replace_in_file":
            p = safe_path(root, args["path"])
            txt = p.read_text()
            c = txt.count(args["old"])
            if c != 1:
                return "ERROR: old text found %d times, must be exactly 1. Read the file and copy it exactly." % c
            p.write_text(txt.replace(args["old"], args["new"]))
            return "replaced in " + args["path"]
        if name == "run":
            cmd = args["cmd"]
            if BLOCKED_CMD.search(cmd):
                return "ERROR: command not allowed"
            if OUTSIDE_SEARCH.search(cmd):
                return ("ERROR: do not search the whole disk (slow, and outside the project). Work inside the project. "
                        "If you need a resource like a font, use what the library ships (Pillow: ImageFont.load_default()) "
                        "or add the file to the project.")
            note = ""
            m = CD_PREFIX.match(cmd)
            if m:
                target = Path(os.path.expanduser(m.group(1).strip("'\""))).resolve() if m.group(1).startswith(("/", "~")) \
                    else (root / m.group(1).strip("'\"")).resolve()
                if target != root and root not in target.parents:
                    # Models love to guess a home folder like /home/user. Run the rest in the project instead of failing.
                    cmd = m.group(3)
                    note = "(note: ignored 'cd %s', commands always run in the project root, no cd needed)\n" % m.group(1)
            rc, out = sh(cmd, cwd=root, timeout=P.cfg["cmd_timeout"])
            return note + "exit %s\n%s" % (rc, clip(out, 5000))
        return "ERROR: unknown tool " + name
    except Exception as e:  # tool errors go back to the model, not up the stack
        return "ERROR: %s" % e


OMIT_MARK = "[ORCA-OMITTED %d chars: shortened in this chat history only, to save space. The file on disk is complete and unchanged. Never copy this marker into a file]"
OMIT_RE = re.compile(r"\[ORCA-OMITTED \d+ chars|^<\d+ chars>$")


def shrink(msgs):
    """Keep the last 6 messages intact; squash older tool output and big file writes."""
    for i, m in enumerate(msgs[:-6]):
        if i < 2:
            continue
        if m["role"] == "tool" and len(m["content"]) > 200:
            m["content"] = m["content"][:120] + " ...[trimmed]"
        if m["role"] == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                try:
                    a = json.loads(tc["function"]["arguments"])
                except ValueError:
                    continue
                for k in ("content", "old", "new"):
                    if k in a and len(a[k]) > 300:
                        a[k] = OMIT_MARK % len(a[k])
                tc["function"]["arguments"] = json.dumps(a)


def smoke_command(P):
    """The real-app check command, or "" when this project has none."""
    c = P.cfg
    cmd = c.get("smoke_cmd") or ("auto" if "smoke_cmd" not in c else "")
    if cmd == "auto":
        if not (c.get("preview_url") and c.get("smoke_from")):
            return ""
        here = Path(__file__).resolve().parent                    # ONE shared tool venv next to orca.py (not one per app, Chromium is big)
        found = [v / "bin" / "python" for v in (here / ".smoke-venv", here / "venv") if (v / "bin" / "python").exists()]
        py = str(found[0]) if found else sys.executable
        cmd = "%s %s %s" % (shlex.quote(py), shlex.quote(str(Path(__file__).with_name("orca_smoke.py"))), shlex.quote(str(P.root)))
    return cmd


def smoke_active(P, tid):
    """On for the skeleton task itself and for every task after it."""
    frm = P.cfg.get("smoke_from") or ""
    if not frm or not smoke_command(P):
        return False
    return tid == frm or any(t["id"] == frm and t["mark"] == "x" for t in P.tasks())


def run_smoke(P):
    rc, out = sh(smoke_command(P), cwd=P.root, timeout=P.cfg["cmd_timeout"])
    return rc, out


def repo_files(P):
    rc, out = P.git("ls-files -co --exclude-standard")
    return [l for l in out.splitlines() if l.strip() and not l.startswith(tuple(d + "/" for d in NOTES_DIRS))]


JS_EXPORT = re.compile(r"^export\s+(?:default\s+)?(?:async\s+)?(function\*?|class|const|let|var)\s+([A-Za-z_$][\w$]*)(\([^)]*\))?", re.M)
JS_EXPORT_LIST = re.compile(r"^export\s*\{([^}]*)\}", re.M)
JS_CJS = re.compile(r"^module\.exports\s*=\s*\{([^}]*)\}", re.M)
PY_DEF = re.compile(r"^(?:async\s+)?(def|class)\s+([A-Za-z]\w*)\s*(\([^)]*\))?", re.M)
SRC_EXT = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py")
MAP_SKIP = re.compile(r"(^|/)(vendor|node_modules|dist|build|venv|\.venv|__pycache__)/|\.min\.")


def file_api(path, text):
    """What a file offers to the rest of the project: exported names and signatures. Plain regex, no model."""
    if path.endswith(".py"):
        return ["%s %s%s" % (k, n, (a or "")[:80]) for k, n, a in PY_DEF.findall(text) if not n.startswith("_")]
    out = ["%s %s%s" % (k, n, (a or "")[:80]) for k, n, a in JS_EXPORT.findall(text)]
    for rx in (JS_EXPORT_LIST, JS_CJS):
        for body in rx.findall(text):
            out += ["export " + x.strip().split(" as ")[-1].split(":")[0].strip() for x in body.split(",") if x.strip()]
    return out


def is_test_file(path):
    base = path.rsplit("/", 1)[-1]
    return (".test." in base or ".spec." in base or base.startswith("test_") or base.endswith("_test.py")
            or path.startswith(("test/", "tests/")))


def code_map(P, task=None, cap=None):
    """A map of what the project already contains, built from the files themselves (always current, never hallucinated):
    one entry per source file with its exported functions. The builder starts every task with no memory, so without
    this it spends its first steps reading files only to learn what already exists. Returns (text, set of covered paths).
    Files the task mentions by name are shown in full, the rest shrink to bare names if space runs out."""
    cap = cap or P.cfg.get("map_max_chars", 6000)
    entries = []
    for path in repo_files(P):
        if not path.endswith(SRC_EXT) or MAP_SKIP.search(path):
            continue
        try:
            f = P.root / path
            if f.stat().st_size > 200_000:
                continue
            text = f.read_text(errors="replace")
        except OSError:
            continue
        lines = text.count("\n") + 1
        if is_test_file(path):
            n = len(re.findall(r"^\s*(?:test|it)\(|^\s*(?:async\s+)?def test_", text, re.M))
            big = lines > P.cfg.get("test_warn_lines", 300)
            entries.append((path, [], "%d tests, %d lines%s" % (n, lines, ", TOO BIG" if big else "")))
        else:
            big = lines > P.cfg.get("file_warn_lines", 200)
            entries.append((path, file_api(path, text), "%d lines%s" % (lines, ", TOO BIG: do not read it whole, use line ranges, "
                                                                         "and put new code in a new file when the task allows" if big else "")))
    if not entries:
        return "", set()
    wanted = (task["text"] if task else "").lower()

    def related(path):
        stem = path.rsplit("/", 1)[-1].split(".")[0].lower()
        return len(stem) > 2 and stem in wanted

    def render(e, level):                # 0 = signatures, 1 = bare names, 2 = path only
        path, api, note = e
        if not api or level == 2:
            return "%s  (%s)" % (path, note)
        if level == 0:
            return "%s  (%s)\n%s" % (path, note, "\n".join("  " + a for a in api))
        return "%s: %s" % (path, ", ".join(a.split("(")[0].split(" ", 1)[-1] for a in api))

    entries.sort(key=lambda e: (not related(e[0]), is_test_file(e[0]), e[0]))
    covered = {e[0] for e in entries}
    for level in (0, 1, 2):              # shrink the files the task does not mention until everything fits
        text = "\n".join(render(e, 0 if related(e[0]) else level) for e in entries)
        if len(text) <= cap:
            return text, covered
    return text[:max(cap - 20, 0)] + "\n(map truncated)", covered


def repo_map(P, skip=()):
    files = [l for l in repo_files(P) if l not in skip][:120]
    return "\n".join(files) or "(none)"


def build_prompt(P, task, hint, last_fail):
    c = P.cfg
    parts = ["# SPEC\n" + clip(P.rd("SPEC.md"), c["spec_max_chars"])]
    if P.rd("LESSONS.md").strip():
        parts.append("# LESSONS (follow these)\n" + P.rd("LESSONS.md"))
    if P.rd("PROGRESS.md").strip():
        parts.append("# RECENT PROGRESS\n" + P.rd("PROGRESS.md"))
    cmap, covered = code_map(P, task)
    if cmap:
        parts.append("# CODE MAP (what earlier tasks already built: every source file with its exported functions. "
                     "Trust it. Do NOT read_file a file just to learn its names or signatures. Read a file only to see "
                     "the body of code you must change.)\n" + cmap)
    parts.append("# OTHER FILES\n" + repo_map(P, skip=covered))
    ex = extra_dirs_now(P)
    if ex:
        parts.append("# EXTRA FOLDERS (read-only, use these absolute paths with list_files / read_file)\n" +
                     "\n".join("%s  (%s)" % (d, dir_summary(d)) for d in ex))
    parts.append("# CURRENT TASK\n%s: %s\nTest command: %s" % (task["id"], task["text"], c["test_cmd"]))
    if hint:
        parts.append("# SENIOR ENGINEER HINT (follow it)\n" + hint)
    if last_fail:
        parts.append("# PREVIOUS ATTEMPT LEFT THE TREE IN ITS CURRENT STATE. TESTS FAIL WITH:\n" + last_fail)
    return "\n\n".join(parts)


def agent_attempt(P, task, hint, last_fail, tag, mode="default"):
    c = P.cfg
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": build_prompt(P, task, hint, last_fail)}]
    summary, nudges = "", 0
    seen, streak = set(), 0          # files read since the last write, calls since the last write
    set_state(think=mode)
    for step in range(c["max_steps"]):
        # Only shorten history when it is getting big. Rewriting old messages (1) shows the model markers it
        # tends to copy, and (2) breaks LM Studio's prompt cache, so every step re-reads the whole chat.
        budget = live_budget(P)                      # (C) in the dashboard can change it while the build runs
        if est_tokens(msgs) > budget * 0.6:
            shrink(msgs)
        if est_tokens(msgs) > budget:
            summary = "context budget exhausted"
            log("  context budget hit, ending attempt")
            break
        set_state(phase="Qwen generating", step=step + 1, ctx_tokens=est_tokens(msgs), since=time.time())
        try:
            resp = lm_chat(c, msgs, mode)
        except (urllib.error.URLError, OSError, ValueError) as e:
            summary = "LM Studio error: %s" % e
            log("  " + summary)
            time.sleep(10)
            break
        if resp["choices"][0].get("finish_reason") == "length":
            log("  reply was cut off at max_output_tokens (thinking too long? lower thinking in orca.json)")
        u = resp.get("usage") or {}
        record("qwen", "build", prompt=u.get("prompt_tokens"), completion=u.get("completion_tokens"))
        m = resp["choices"][0]["message"]
        calls = m.get("tool_calls") or []
        raw = m.get("content") or ""
        tm = re.search(r"<think>(.*?)</think>", raw, flags=re.S)
        think = m.get("reasoning_content") or (tm.group(1) if tm else "")
        am = {"role": "assistant", "content": strip_think(raw)}
        set_state(ai_text=clip(am["content"], 1500), ai_think=clip(think, 1500))
        if calls:
            am["tool_calls"] = calls
        msgs.append(am)
        if not calls:
            nudges += 1
            if nudges > 2:
                summary = am["content"][:200] or "model stopped without calling done"
                break
            msgs.append({"role": "user", "content": "Use the tools. Call done when the task is finished."})
            continue
        finished = False
        for tc in calls:
            fn = tc["function"]
            try:
                args = json.loads(fn["arguments"] or "{}")
            except ValueError:
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": "ERROR: arguments were not valid JSON"})
                continue
            if fn["name"] == "done":
                summary = args.get("summary", "")
                finished = True
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": "ok"})
                continue
            log("  %s %s" % (fn["name"], json.dumps(args)[:90]))
            set_state(last_tool="%s %s" % (fn["name"], json.dumps(args)[:60]))
            out = run_tool(P, fn["name"], args)
            if fn["name"] in ("write_file", "replace_in_file") and not out.startswith("ERROR"):
                seen, streak = set(), 0
            else:
                streak += 1
                if fn["name"] == "read_file":
                    key = (str(args.get("path")), args.get("start_line"), args.get("end_line"))
                    if key in seen:
                        out = "[You already read this exact part in this attempt. It has not changed. Stop re-reading and write the code.]\n" + out
                    seen.add(key)
            ACTIONS.append("%s %s" % (fn["name"], str(args.get("path") or args.get("cmd") or "")[:50]))
            set_state(actions=ACTIONS[-6:])
            if fn["name"] in ("read_file", "write_file", "replace_in_file") and args.get("path"):
                set_state(cur_file=args["path"], cur_action=fn["name"])
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": clip(out, 6000)})
        if finished:
            break
        if streak >= 10 and streak % 5 == 0:
            msgs.append({"role": "user", "content": "You have made %d tool calls without writing anything. You have enough "
                         "information. Write the code for the task NOW (write_file creates files that do not exist yet), add "
                         "its tests, run the tests, then call done." % streak})
    else:
        summary = "step limit reached"
    (P.o / "logs").mkdir(parents=True, exist_ok=True)
    (P.o / "logs" / (tag + ".json")).write_text(json.dumps(msgs, indent=1))
    return summary


# ---------------------------------------------------------------- memory guard (RAM)
def free_pct():
    rc, out = sh("memory_pressure", timeout=20)
    m = re.search(r"free percentage:\s*(\d+)%", out)
    return int(m.group(1)) if m else None


def swap_used_mb():
    rc, out = sh("sysctl -n vm.swapusage", timeout=10)
    m = re.search(r"used = ([\d.]+)M", out)
    return float(m.group(1)) if m else None


def reload_model(P):
    key = P.cfg["model_key"]
    if not key:
        log("  no model_key in orca.json, skipping reload")
        return
    load_model(P, key)


SWAP_SEEN = {}


def pressure_ok(P):
    """Too little free memory, or the Mac is actively swapping right now. A big but steady swap number is old
    (macOS keeps swapped pages for hours, Chrome tabs mostly), so only GROWING swap counts."""
    fp, sw = free_pct(), swap_used_mb()
    now = time.time()
    grew = 0
    if sw is not None:
        last = SWAP_SEEN.get("v")
        if last is not None and now - SWAP_SEEN.get("t", 0) < 600:
            grew = sw - last
        SWAP_SEEN.update(v=sw, t=now)
    bad = (fp is not None and fp < P.cfg["min_free_pct"]) or \
          (sw is not None and sw > P.cfg["max_swap_mb"] and grew > 512 and (fp is None or fp < 30))
    return (not bad), fp, sw


def ram_guard(P):
    """Wait for memory pressure to ease. After 5 minutes, reload the model once to free cache."""
    reloaded = False
    for i in range(40):
        ok, fp, sw = pressure_ok(P)
        if ok:
            try:
                lm_models(P.cfg)
                return
            except (urllib.error.URLError, OSError):
                log("LM Studio server not answering, waiting")
        else:
            log("memory pressure: free=%s%% swap=%sMB, waiting" % (fp, sw))
            set_state(phase="waiting for RAM (free %s%%, swap %sMB)" % (fp, sw))
        if i == 10 and not reloaded:
            reload_model(P)
            reloaded = True
        time.sleep(30)
    sys.exit("memory pressure or server down for 20 minutes, stopping")


# ---------------------------------------------------------------- task loop
def signature(out):
    t = re.sub(r"0x[0-9a-f]+|\d+(\.\d+)?", "N", out[-800:])
    return hashlib.md5(t.encode()).hexdigest()[:10]


def escalate(P, task, fail, n):
    log("  escalating to Claude")
    set_state(phase="Claude escalation")
    rc1, status = P.git("status --short")
    diff = work_diff(P)              # Claude must see the files Qwen created too, not only the edits
    stdin = clip(status + "\n" + diff, 40000)
    prompt = ESCALATE_PROMPT.replace("{n}", str(n)).replace("{spec}", clip(P.rd("SPEC.md"), 3000)) \
        .replace("{task}", "%s: %s" % (task["id"], task["text"])).replace("{fail}", clip(fail, 3000)) \
        .replace("{lessons}", clip(P.rd("LESSONS.md").strip() or "(none yet)", 2500))
    reply = ask_claude(P.cfg, prompt, stdin, purpose="escalate") or ""
    hint, lesson = split_lesson(reply)
    if lesson and P.cfg.get("escalate_lessons", True) and lesson_is_new(P.rd("LESSONS.md"), lesson):
        log("  lesson from Claude: " + lesson)
        P.add_lesson(lesson)
    return hint


def split_lesson(reply):
    """(hint without the LESSON line, the lesson or ""). The lesson is for LESSONS.md, the hint is for Qwen."""
    lines = (reply or "").rstrip().splitlines()
    lesson, keep = "", []
    for l in lines:
        m = re.match(r"^\s*\**LESSON:\**\s*(.+)$", l)
        if m:
            lesson = m.group(1).strip().strip("*").strip()
        else:
            keep.append(l)
    words = lesson.split()
    if len(words) > 35 or re.search(r"\b[TRF]\d{2,}\b", lesson):         # too long, or tied to one task: not a general rule
        lesson = ""
    return "\n".join(keep).strip(), lesson


def lesson_is_new(existing, lesson):
    """False when the lesson mostly repeats a line that is already there (word overlap), so LESSONS.md does not bloat."""
    def words(s):
        return {w for w in re.findall(r"[a-z0-9_]+", s.lower()) if len(w) > 3}
    new = words(lesson)
    if len(new) < 3:
        return False
    for line in existing.splitlines():
        old = words(line)
        if old and len(new & old) / float(len(new | old)) >= 0.5:
            return False
    return True


JUNK = re.compile(r"(__pycache__|\.pyc$|\.pytest_cache|\.mypy_cache|node_modules/|\.DS_Store|\.coverage|(^|/)logs/|\.log$)")  # logs the app writes while tests run are not work


def work_diff(P):
    """Diff of the uncommitted work INCLUDING new files (plain `git diff HEAD` silently leaves those out)."""
    P.git("add --intent-to-add -- . ':(exclude)orca' ':(exclude).orca'")
    rc, out = P.git("diff HEAD -- . ':(exclude)orca' ':(exclude).orca'")
    return out


def tree_snapshot(P):
    """Fingerprint of every uncommitted file (path + content hash). Comparing two snapshots tells whether an attempt
    really changed the project, even if the tree was already dirty before it started. Test caches do not count."""
    rc, out = P.git("status --porcelain --untracked-files=all -- . ':(exclude)orca' ':(exclude).orca'")
    if rc != 0:
        return None
    snap = {}
    for l in out.splitlines():
        path = l[3:].split(" -> ")[-1].strip().strip('"')
        if not path or JUNK.search(path):
            continue
        f = P.root / path
        try:
            snap[path] = hashlib.md5(f.read_bytes()).hexdigest() if f.is_file() else "gone"
        except OSError:
            snap[path] = "unreadable"
    return snap


SPLIT_TASK = ("Split %s (%d lines) into several small files of about %d lines each, grouped by function (for example a folder "
              "of small modules), and keep %s as a thin module that re-exports everything so every existing import keeps working. "
              "Move code only: do not change behaviour, do not rename anything, do not touch the tests except an import path if it truly must change "
              "| done when: all existing tests still pass and no source file you created is over %d lines")


def queue_splits(P):
    """Keep source files small while the build runs. After a passing task, if a source file is over `file_warn_lines`,
    queue ONE fix task (R) to split it, before the next task. One split task per file, ever (a blocked split is not retried),
    biggest file first, one at a time. Test files are left alone (splitting tests risks losing cases)."""
    if not P.cfg.get("auto_split", True):
        return None
    limit = P.cfg.get("file_warn_lines", 200)
    existing = P.rd("TASKS.md")
    big = []
    for path in repo_files(P):
        if not path.endswith(SRC_EXT) or MAP_SKIP.search(path) or is_test_file(path):
            continue
        try:
            n = (P.root / path).read_text(errors="replace").count("\n") + 1
        except OSError:
            continue
        if n > limit and ("Split %s (" % path) not in existing:
            big.append((n, path))
    if not big:
        return None
    n, path = max(big)
    target = max(limit * 3 // 4, 80)
    P.add_tasks_front([SPLIT_TASK % (path, n, target, path, limit)])
    log("  %s has %d lines (limit %d): queued a split task" % (path, n, limit))
    return path


def rollback(P):
    """Undo an attempt's work in the PROJECT files only. The notes folder (orca/) is never touched: a plain
    `git reset --hard` also threw away what you edited in TASKS.md, SPEC.md or LESSONS.md while the task ran,
    and any lesson Claude had just added."""
    ex = "':(exclude)%s' ':(exclude)%s'" % (ORCA_DIR, LEGACY_DIR)
    P.git("reset -q -- . " + ex)             # un-stage the intent-to-add entries work_diff made for new files
    P.git("checkout -q -- . " + ex)          # tracked files back to the last commit
    P.git("clean -fdq -- . " + ex)           # files the attempt created


def why_blocked(text):
    """One useful line from a saved test failure: the first failing test and its message, not a trailing '}'.
    Used for the lesson written when a task is blocked (and by the report)."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for i, l in enumerate(lines):
        if l.startswith(("✖", "not ok", "FAIL", "Error", "AssertionError", "E  ")) or "Error:" in l:
            nxt = next((m for m in lines[i + 1:i + 4] if not m.startswith(("✔", "✖", "ℹ", "▶", "ok "))), "")
            return (l + (" | " + nxt if nxt else ""))[:160]
    return lines[-1][:160] if lines else "no error saved"


def do_task(P, task):
    c, tid = P.cfg, task["id"]
    log("== %s: %s" % (tid, task["text"][:100]))
    P.wr("protected.json", "[]")
    set_state(task=tid, title=task["text"].split("|")[0].strip(), attempt=0, max_attempts=c["max_attempts"], phase="starting", last_tool="", actions=[], ai_text="", ai_think="", cur_file="")
    del ACTIONS[:]
    hint, last_fail, sigs, escal = "", "", [], 0
    task_start = tree_snapshot(P)
    for attempt in range(1, c["max_attempts"] + 1):
        log(" attempt %d/%d" % (attempt, c["max_attempts"]))
        set_state(attempt=attempt, phase="Qwen working")
        mode = c["thinking_retry"] if attempt >= c["thinking_retry_from"] else c["thinking"]
        before = tree_snapshot(P)
        summary = agent_attempt(P, task, hint, last_fail, "%s-a%d" % (tid, attempt), mode)
        after = tree_snapshot(P)         # taken BEFORE our test run, so its cache files cannot count as work
        # Work left by an earlier attempt of the SAME task counts too (it was not committed yet).
        changed = before is None or after != before or (attempt > 1 and bool(after) and after != task_start)
        set_state(phase="running tests")
        rc, out = sh(c["test_cmd"], cwd=P.root, timeout=c["cmd_timeout"] * 3)
        lazy = summary in ("step limit reached", "context budget exhausted") or summary.startswith("LM Studio error")
        if rc == 0 and not changed and (tid[:1] != "R" or lazy):
            # Tests that were already green prove nothing: a feature task with no file changes is not done.
            log(" tests pass but NO FILE WAS CHANGED, so the task is not done")
            rc, out = 1, ("NO CHANGES. The tests pass, but no file in the project was changed during this attempt, "
                          "so the task is NOT done. Files the task names that do not exist yet must be CREATED with write_file, "
                          "together with tests for them. Do not just read files.")
        if rc == 0 and smoke_active(P, tid):
            set_state(phase="smoke test (real app)")
            src, sout = run_smoke(P)
            if src == 3:
                log(" (smoke test skipped: %s)" % clip(sout, 200).strip())
            elif src != 0:
                log(" REAL APP SMOKE TEST FAILED")
                rc, out = 1, ("The unit tests pass, but the REAL APP is broken. It was started with preview_cmd and opened in a "
                              "headless browser. Fix the real code (not the tests). Do not fake anything.\n" + sout)
        if rc == 0:
            P.mark(tid, "x")
            P.add_progress("%s done: %s" % (tid, summary[:160]))
            try:
                P.wr("MAP.md", "# Code map (rebuilt after every passing task, fed to the builder)\n\n" + code_map(P)[0] + "\n")
            except Exception as e:           # the map is a helper, never a reason to fail a task
                log("  (map not written: %s)" % e)
            P.commit("%s: %s" % (tid, task["text"].split("|")[0].strip()))
            log(" PASS")
            return True
        last_fail = clip(out, 3500)
        sig = signature(out)
        sigs.append(sig)
        log(" tests failed (sig %s)" % sig)
        stuck = sigs.count(sig) >= c["same_fail_escalate"] or (attempt >= c.get("escalate_after", 3) and escal == 0)
        if stuck and escal < c["max_escalations"] and attempt < c["max_attempts"]:
            hint = escalate(P, task, last_fail, sigs.count(sig))
            escal += 1
            sigs = []
    (P.o / "failed").mkdir(parents=True, exist_ok=True)
    diff = work_diff(P)              # includes NEW files, so `git apply orca/failed/T17.diff` brings everything back
    (P.o / "failed" / (tid + ".diff")).write_text(diff)
    (P.o / "failed" / (tid + ".txt")).write_text(last_fail)
    rollback(P)
    P.mark(tid, "!")
    P.add_lesson("%s was blocked (see orca/failed). First failure: %s" % (tid, why_blocked(last_fail)))
    P.commit("orca: block %s" % tid)
    log(" BLOCKED, rolled back, diff saved to %s/failed/%s.diff" % (P.o.name, tid))
    return False


def apply_checkpoint(P, out):
    """Apply a course check reply: only open tasks are touched, within the caps. Returns (applied, rejected) lines."""
    tasks = P.tasks()
    open_ids = {t["id"] for t in tasks if t["mark"] == " "}
    applied, rejected = [], []
    n_add = n_rem = 0
    c_next = 1 + max([int(t["id"][1:]) for t in tasks if t["id"][:1] == "C"] or [0])
    for raw in out.splitlines():
        line = raw.strip()
        kind, _, rest = line.partition(":")
        kind = kind.strip().upper()
        if kind not in ("REMOVE", "ADD", "MOVE"):
            continue
        parts = [p.strip() for p in rest.split("|")]
        if kind == "REMOVE":
            tid = parts[0]
            if tid not in open_ids:
                rejected.append(line + "   (not an open task)")
            elif n_rem >= P.cfg.get("checkpoint_max_remove", 5):
                rejected.append(line + "   (over the remove cap)")
            else:
                P.remove_task(tid)
                open_ids.discard(tid)
                n_rem += 1
                applied.append(line)
        elif kind == "ADD":
            before, text = parts[0], " | ".join(parts[1:]).strip()
            if not text or "done when" not in text.lower():
                rejected.append(line + "   (no done when)")
            elif n_add >= P.cfg.get("checkpoint_max_add", 5):
                rejected.append(line + "   (over the add cap)")
            else:
                tid = "C%02d" % c_next
                c_next += 1
                P.insert_task("- [ ] %s: %s" % (tid, text), before if before in open_ids else None)
                open_ids.add(tid)
                n_add += 1
                applied.append("ADD %s before %s: %s" % (tid, before if before in open_ids else "END", text[:140]))
        else:
            tid, before = (parts + [""])[:2]
            if tid not in open_ids or (before not in open_ids and before.upper() != "END") or tid == before:
                rejected.append(line + "   (not open tasks)")
            else:
                t = next(t for t in P.tasks() if t["id"] == tid)
                P.remove_task(tid)
                P.insert_task("- [ ] %s: %s" % (tid, t["text"]), None if before.upper() == "END" else before)
                applied.append(line)
    return applied, rejected


def checkpoint(P, why):
    """Course check: Claude sees the REAL running app and steers the open task list toward a working demo."""
    log("-- course check (%s)" % why)
    set_state(phase="course check (Claude)")
    shot = P.o / "checkpoint.png"
    if shot.exists():
        shot.unlink()
    smoke = "(no real-app check configured for this project)"
    cmd = smoke_command(P)
    if cmd:
        os.environ["ORCA_SHOT"] = str(shot)
        try:
            rc, smoke = run_smoke(P)
        finally:
            os.environ.pop("ORCA_SHOT", None)
        smoke = "exit %d\n%s" % (rc, clip(smoke, 2500))
    tasks = P.tasks()
    done = [t for t in tasks if t["mark"] == "x"]
    rows = ["(%d earlier done tasks not shown)" % (len(done) - 15)] if len(done) > 15 else []
    rows += ["[x] %s: %s" % (t["id"], t["text"][:120]) for t in done[-15:]]
    rows += ["[%s] %s: %s" % (t["mark"], t["id"], t["text"][:240]) for t in tasks if t["mark"] != "x"]
    try:
        import orca_report
        numbers = "\n".join(orca_report.build_report(P).splitlines()[2:9])
    except Exception:
        numbers = "(not available)"
    has_shot = shot.exists()
    prompt = (CHECKPOINT_PROMPT
              .replace("{shot_note}", " and a SCREENSHOT of it at orca/checkpoint.png (open it with your Read tool and look at it)" if has_shot else "")
              .replace("{max_add}", str(P.cfg.get("checkpoint_max_add", 5)))
              .replace("{max_remove}", str(P.cfg.get("checkpoint_max_remove", 5)))
              .replace("{spec}", clip(P.rd("SPEC.md"), 4000))
              .replace("{tasks}", "\n".join(rows))
              .replace("{progress}", clip(P.rd("PROGRESS.md"), 1500))
              .replace("{numbers}", numbers)
              .replace("{smoke}", smoke))
    out = ask_claude(P.cfg, prompt, purpose="checkpoint", cwd=str(P.root),
                     extra_args=["--allowedTools", "Read"] if has_shot else [])
    if not out:
        log("   course check: no answer from Claude, carrying on")
        return
    applied, rejected = apply_checkpoint(P, out)
    status = next((l[7:].strip() for l in out.splitlines() if l.upper().startswith("STATUS:")), "")
    demo = next((l[5:].strip() for l in out.splitlines() if l.upper().startswith("DEMO:")), "")
    entry = ["## %s, after %d done tasks (%s)" % (time.strftime("%Y-%m-%d %H:%M"), len(done), why),
             "STATUS: " + status, "DEMO: " + demo, ""] + ["- " + a for a in applied] +             ["- REJECTED " + r for r in rejected] + (["- no changes"] if not applied and not rejected else []) + [""]
    P.wr("CHECKPOINT.md", P.rd("CHECKPOINT.md") + "\n".join(entry) + "\n")
    log("   status: %s" % status[:150])
    log("   course check: %d change(s) applied, %d rejected (orca/CHECKPOINT.md)" % (len(applied), len(rejected)))
    P.commit("orca: course check")


def ensure_venv(P):
    """Python projects run inside <project>/venv. PATH is patched for every subprocess we start."""
    mode = P.cfg["venv"]
    if mode is False or str(mode).lower() == "false":
        return
    hint = any((P.root / f).exists() for f in ("requirements.txt", "pyproject.toml", "setup.py")) or \
        re.search(r"\b(pip3?|pytest|python3?|unittest)\b", P.cfg["setup_cmd"] + " " + P.cfg["test_cmd"])
    if str(mode).lower() == "auto" and not hint:
        return
    v = P.root / "venv"
    if not (v / "bin" / "python").exists():
        log("creating venv")
        rc, out = sh([sys.executable, "-m", "venv", str(v)], timeout=300)
        if rc != 0:
            sys.exit("venv creation failed:\n" + out[-800:])
    os.environ["VIRTUAL_ENV"] = str(v)
    os.environ["PATH"] = str(v / "bin") + os.pathsep + os.environ.get("PATH", "")
    os.environ.pop("PYTHONHOME", None)
    log("using venv " + str(v))
    if re.match(r"\s*(python3?\s+-m\s+)?pytest\b", P.cfg["test_cmd"]) and not (v / "bin" / "pytest").exists():
        log("installing pytest into the venv")
        sh("python -m pip install -q pytest", cwd=P.root, timeout=600)


def try_setup(P):
    if not P.cfg["setup_cmd"] or (P.o / "setup.done").exists():
        return
    log("setup: " + P.cfg["setup_cmd"])
    rc, out = sh(P.cfg["setup_cmd"], cwd=P.root, timeout=900)
    if rc == 0:
        P.wr("setup.done", "1")
    else:
        log("setup failed, will retry after the next task (T01 may create the files it needs):\n" + out[-400:])


RUNTIME_FILES = [p.rstrip("/") for d in NOTES_DIRS for p in runtime_ignores(d)]


def untrack_runtime(P):
    """Older versions committed runtime files. Stop tracking them so rollbacks and diffs ignore them."""
    rc, tracked = P.git("ls-files -- " + " ".join(RUNTIME_FILES))
    if rc == 0 and tracked.strip():
        P.git("rm -r --cached -q --ignore-unmatch -- " + " ".join(RUNTIME_FILES))
        P.git("commit -qm %s" % json.dumps("orca: stop tracking runtime files"))
        log("stopped tracking runtime files that an older version had committed")


def running_pid(P):
    """PID of a live orca run on this project, else None. A leftover run.pid of a dead process is ignored."""
    try:
        pid = int(P.rd("run.pid").strip())
    except ValueError:
        return None
    if pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    rc, out = sh("ps -p %d -o command=" % pid, timeout=10)  # guards against the pid being reused by another program
    if rc == 0 and "orca" not in out:
        return None
    return pid


def already_running_help(P, pid):
    pidfile = P.o / "run.pid"
    return ("\nA build is already running on this project (pid %d). Starting a second one would fight over LM Studio and git.\n"
            "  1/ stop it nicely:   python3 orca.py stop %s            (halts after the current task)\n"
            "  2/ stop it now:      python3 orca.py stop %s --now      (like Ctrl-C, finished tasks stay committed)\n"
            "  3/ if that fails:    kill %d      (then, if it is stuck, kill -9 %d)\n"
            "  4/ then remove the pid file only if it is left over and nothing is running:\n"
            "                       rm %s\n"
            "  5/ really sure you want two? add --force to run.\n") % (pid, P.root, P.root, pid, pid, pidfile)


def cmd_run(a):
    P = Project(a.dir)
    pid = running_pid(P)
    if pid and not getattr(a, "force", False):
        sys.exit(already_running_help(P, pid))
    if not pid and (P.o / "run.pid").exists():
        (P.o / "run.pid").unlink()
        log("removed a stale run.pid (that process is gone)")
    if not pid:
        migrate_notes_dir(P)
    if P.rd("STAGE").strip() == "design" and not getattr(a, "force", False):
        sys.exit("%s is in the DESIGN stage: Claude's prototype is waiting for your review.\n"
                 "Look at it ((P) in the dashboard), add notes ((n)), then hand off to Qwen:\n"
                 "  python3 orca.py handoff %s --go       (or (h) in the dashboard)" % (P.root, P.root))
    if not P.cfg["test_cmd"]:
        sys.exit("orca.json has no test_cmd. Tests are the judge, set one.")
    ensure_model(P)
    probe_thinking(P)
    gi = P.root / ".gitignore"
    have = gi.read_text() if gi.exists() else ""
    add = [l for l in runtime_ignores(P.o.name) + ["venv/"] if l not in have]
    if add:
        gi.write_text(have.rstrip("\n") + "\n" + "\n".join(add) + "\n")
        P.git("add -- .gitignore")
        P.git("commit -qm %s -- .gitignore" % json.dumps("orca: update .gitignore"))
    untrack_runtime(P)
    if (P.o / "STOP").exists():
        (P.o / "STOP").unlink()
        log("removed the old STOP file from the last stop")
    P.wr("run.pid", str(os.getpid()))
    atexit.register(lambda: (P.o / "run.pid").exists() and (P.o / "run.pid").unlink())
    t_start = time.time()
    jlog(P, "sessions.jsonl", {"start": t_start, "pid": os.getpid()})
    atexit.register(lambda: jlog(P, "sessions.jsonl", {"start": t_start, "end": time.time()}))
    ensure_venv(P)
    rc, dirty = P.git("status --porcelain")
    if rc == 0 and dirty.strip():
        log("resuming with uncommitted changes from an interrupted task (kept, Qwen will see them):\n" + clip(dirty, 300))
    set_state(halt_reason="", halted_at=0)
    passed, blocked_row, final_done = 0, 0, False
    since_cp, last_cp = 0, time.time()
    while True:
        if (P.o / "STOP").exists():
            log("STOP file found, halting")
            break
        task = P.next_task()
        if not task:
            if not final_done and P.cfg.get("checkpoint_every", 8):
                final_done = True            # one final course check per run; the tasks it adds are built, not checked again
                try:
                    checkpoint(P, "all tasks done")
                except Exception as e:
                    log("  (course check failed: %s)" % e)
                if P.next_task():
                    log("the final course check added tasks, building them now")
                    continue
            log("ALL TASKS RESOLVED. Blocked ones are marked [!].")
            break
        ram_guard(P)
        if P.cfg["quota_check"]:
            q = fetch_and_record()
            set_state(quota=quota_short(q))
            log(quota_line(q))
        try_setup(P)
        if timed_task(P, task):
            blocked_row, passed = 0, passed + 1
            try:
                queue_splits(P)
            except Exception as e:           # a helper must never stop the build
                log("  (split check failed: %s)" % e)
            if P.cfg["reload_every"] and passed % P.cfg["reload_every"] == 0:
                reload_model(P)
            since_cp += 1
            every, mins = P.cfg.get("checkpoint_every", 8), P.cfg.get("checkpoint_minutes", 90)
            why = ("the skeleton runs" if every and task["id"] == P.cfg.get("smoke_from") else
                   "%d tasks" % since_cp if every and since_cp >= every else
                   "%d minutes" % mins if every and mins and time.time() - last_cp >= mins * 60 else "")
            if why and P.next_task():
                try:
                    checkpoint(P, why)
                except Exception as e:       # a steering helper must never stop the build
                    log("  (course check failed: %s)" % e)
                since_cp, last_cp = 0, time.time()
        else:
            blocked_row += 1
            if blocked_row >= P.cfg["halt_after_blocked"]:
                log("%d blocked in a row, halting. Fix the spec or tasks, then run again." % blocked_row)
                set_state(halt_reason="%d tasks blocked in a row" % blocked_row, halted_at=time.time())
                break
    set_state(phase="finished or halted")
    log("project statistics (python3 orca.py stats DIR shows this any time):\n" + "\n".join(t for t, _ in stats_lines(P)))
    try:
        import orca_report
        orca_report.write_report(P)
        log("report written to %s/REPORT.md (python3 orca.py report DIR rebuilds it)" % P.o.name)
    except Exception as e:                   # a report problem must never turn a finished build into a failure
        log("(report not written: %s)" % e)


ORCA_WAVES = "~~-~^~~-~~^-"
ORCA_FINS = ["_/\\_", "_/\\_", "'_/\\_", ":_/\\_", "'_/\\_", "_/\\_", "_/\\_", "<o`>", "_/\\_"]


def orca_bar(frac=None, w=30, t=None):
    """The LocalOrca progress bar: a little orca swims along the water as the work gets done, with a calm wake
    behind it and waves ahead, spouting now and then and jumping once in a while. frac None = it swims back and
    forth (busy, no percentage known). Animated by the clock, so redrawing it often makes it move."""
    t = time.time() if t is None else t
    k = int(t * 3)
    n = max(8, w - 2)
    fin = ORCA_FINS[k % len(ORCA_FINS)]
    if frac is None:
        f = abs(((t / 6.0) % 2) - 1)                      # back and forth every 12 s
    else:
        f = max(0.0, min(1.0, frac))
    if frac is not None and f >= 1.0:
        body = (" SPLOOSH! " + "-" * n)[:n] if k % 4 < 2 else ("-" * (n - 10) + " ~done!~ ")[:n]
        return "[" + body.ljust(n)[:n] + "]"
    pos = int(f * (n - len(fin)))
    cells = [("-" if frac is not None else ORCA_WAVES[(c + k) % len(ORCA_WAVES)]) if c < pos
             else ORCA_WAVES[(c + k) % len(ORCA_WAVES)] for c in range(n)]
    s = "".join(cells)
    return "[" + (s[:pos] + fin + s[pos + len(fin):])[:n] + "]"


def bar(frac, w=30):
    n = max(0, min(w, int(frac * w)))
    return "[" + "#" * n + "-" * (w - n) + "]"


def render(P):
    ts = P.tasks()
    done = sum(1 for t in ts if t["mark"] == "x")
    blk = sum(1 for t in ts if t["mark"] == "!")
    total = max(len(ts), 1)
    try:
        st = json.loads(P.rd("state.json"))
    except ValueError:
        st = {}
    L = ["LocalOrca  " + str(P.root), ""]
    L.append("tasks   %s %d/%d done, %d blocked" % (orca_bar((done + blk) / float(total)), done, len(ts), blk))
    if st.get("task"):
        age = int(time.time() - st.get("updated", time.time()))
        L.append("now     %s: %s" % (st["task"], st.get("title", "")[:70]))
        L.append("        attempt %s/%s, step %s, %s (state %ds old)" % (st.get("attempt"), st.get("max_attempts"), st.get("step", "-"), st.get("phase"), age))
        if st.get("last_tool"):
            L.append("        last tool: " + st["last_tool"])
        if st.get("ctx_tokens"):
            L.append("context %s ~%d / %d tokens" % (bar(st["ctx_tokens"] / float(live_budget(P)), 20), st["ctx_tokens"], live_budget(P)))
        if st.get("phase") == "Qwen generating" and st.get("since"):
            L.append("        Qwen has been generating for %ds" % int(time.time() - st["since"]))
    else:
        L.append("now     nothing running yet")
    L.append("memory  free %s%%, swap used %s MB" % (free_pct(), swap_used_mb()))
    L += ["", "--- usage"] + usage_report(P).splitlines()
    ev = P.rd("events.log").splitlines()[-12:]
    L += ["", "--- recent activity"] + (ev or ["(none)"])
    rc, out = P.git("log --oneline -5")
    L += ["", "--- recent commits"] + (out.strip().splitlines() if rc == 0 else ["(none)"])
    return L


def cmd_watch(a):
    P = Project(a.dir)
    cols = lambda: shutil.get_terminal_size((100, 30)).columns
    if a.once:
        print("\n".join(l[:cols()] for l in render(P)))
        return
    try:
        while True:
            frame = "\n".join(l[:cols() - 1] for l in render(P))
            sys.stdout.write("\033[H\033[J" + frame + "\n\n(Ctrl-C to close, the run keeps going)\n")
            sys.stdout.flush()
            time.sleep(2)
    except KeyboardInterrupt:
        print()


def cmd_tui(a):
    import orca_tui
    orca_tui.run(Project(a.dir), start_run=a.run)


def cmd_stop(a):
    P = Project(a.dir)
    if a.now:
        try:
            pid = int(P.rd("run.pid").strip())
            os.kill(pid, signal.SIGINT)
            print("sent Ctrl-C to run (pid %d). Finished tasks are committed, rerun to resume." % pid)
        except (ValueError, OSError):
            print("no running build found")
        return
    P.o.mkdir(exist_ok=True)
    (P.o / "STOP").write_text("stop")
    print("STOP set. The run halts after the current task. (use --now to interrupt immediately)")


def cmd_quota(a):
    q = fetch_and_record(force=True)
    print(quota_line(q))
    if a.raw:
        print("fields seen:", ", ".join(q.get("raw_keys", [])) or "(none)")
        print({k: v for k, v in q.items() if k != "raw_keys"})


def cmd_usage(a):
    print(usage_report(Project(a.dir)))


def cmd_status(a):
    P = Project(a.dir)
    ts = P.tasks()
    done = sum(1 for t in ts if t["mark"] == "x")
    blk = [t["id"] for t in ts if t["mark"] == "!"]
    nxt = P.next_task()
    print("%d/%d done, blocked: %s" % (done, len(ts), ", ".join(blk) or "none"))
    print("next: %s" % (("%s %s" % (nxt["id"], nxt["text"][:80])) if nxt else "nothing"))
    print("memory free: %s%%  swap used: %s MB" % (free_pct(), swap_used_mb()))
    print(usage_report(P))


def cmd_review(a):
    P = Project(a.dir)
    checkpoint(P, "asked by hand")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("dir")
    n.add_argument("brief", nargs="?", default="")
    n.add_argument("--brief-file")
    n.add_argument("--interview", action="store_true", help="refine the brief with Claude first (default when no brief is given)")
    n.add_argument("--go", action="store_true", help="start building right after planning")
    n.add_argument("--force", action="store_true", help="replace the plan of an existing project (code stays, plan starts over)")
    n.add_argument("--qwen-only", action="store_true", help="old flow: Claude plans every task, Qwen builds from an empty folder (no prototype stage)")
    n.add_argument("--no-tui", action="store_true", help="design stage in this terminal instead of the dashboard")
    dg = sub.add_parser("design", help="Claude builds the prototype, or revises it with your open design notes")
    dg.add_argument("dir")
    dg.add_argument("--force", action="store_true", help="go back to the design stage on a project that already has tasks")
    ho = sub.add_parser("handoff", help="prototype approved: Claude breaks the rest into small tasks for Qwen")
    ho.add_argument("dir")
    ho.add_argument("--go", action="store_true", help="start the build right after")
    nt = sub.add_parser("note", help="add a design note for the prototype")
    nt.add_argument("dir")
    nt.add_argument("text")
    w = sub.add_parser("watch")
    w.add_argument("dir")
    w.add_argument("--once", action="store_true", help="print one frame and exit")
    t = sub.add_parser("tui")
    t.add_argument("dir")
    t.add_argument("--run", action="store_true", help="also start the build in the background")
    qp = sub.add_parser("quota")
    qp.add_argument("dir", nargs="?", default=".")
    qp.add_argument("--raw", action="store_true", help="show which fields the API returned")
    sp = sub.add_parser("stop")
    sp.add_argument("dir")
    sp.add_argument("--now", action="store_true", help="interrupt right away (like Ctrl-C)")
    for name in ("run", "status", "review", "usage", "stats", "report", "kill", "preview"):
        s = sub.add_parser(name)
        s.add_argument("dir")
        if name == "run":
            s.add_argument("--force", action="store_true", help="start even if a build seems to be running (not recommended)")
    lmbin = os.path.expanduser("~/.lmstudio/bin")
    if os.path.isdir(lmbin) and lmbin not in os.environ.get("PATH", ""):
        os.environ["PATH"] += os.pathsep + lmbin
    a = ap.parse_args()
    design = lambda name: (lambda a: getattr(__import__("orca_design"), name)(a))   # the design stage lives in orca_design.py
    try:
        {"design": design("cmd_design"), "handoff": design("cmd_handoff"), "note": design("cmd_note"),
         "new": cmd_new, "run": cmd_run, "status": cmd_status, "review": cmd_review, "usage": cmd_usage, "watch": cmd_watch, "tui": cmd_tui, "stop": cmd_stop, "quota": cmd_quota, "stats": cmd_stats, "report": cmd_report, "kill": cmd_kill, "preview": cmd_preview}[a.cmd](a)
    except KeyboardInterrupt:
        set_state(phase="stopped by Ctrl-C")
        log("stopped. Finished tasks are committed. Rerun the same command to resume.")
        sys.exit(130)


if __name__ == "__main__":
    main()
