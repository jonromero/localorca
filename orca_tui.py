#!/usr/bin/env python3
"""LocalOrca terminal dashboard. Python stdlib (curses) only.

    python3 orca.py tui ~/proj/app            # watch a run that is already going
    python3 orca.py tui ~/proj/app --run      # start the build in the background and watch it

Browsing is read-only. The only thing that writes is the `e` key, which opens a file in your $EDITOR
(for VS Code set EDITOR="code -w" so the dashboard waits for you).
Keys: (Up/Down) or (j/k) browse files, (Enter) open, (PgUp/PgDn/Home/End) scroll, mouse click, double click and wheel,
      (e) edit in $EDITOR, (f) follow Qwen again, (r) run the build when none is running, (s) stop after this task,
      (D) folders outside the project that Qwen may read, (P) live preview of the app in your browser,
      (q) quit (a running build keeps going)
      t statistics for the whole project (time, tokens of every AI, per task), K kill the build process (asks first),
      L pick the model that builds (lists what LM Studio has downloaded),
      a ask Claude: opens a Claude Code chat in the project (like a normal terminal session), /exit comes back
      o settings menu: models, context, folders
      C context: how much chat Qwen may hold per attempt (28k default, counts from the next step)
DESIGN STAGE (new projects): Claude builds the prototype first and the dashboard shows it working.
      (P) look at the prototype, (n) add a design note (or use Feedback in the preview),
      (b) Claude builds the prototype or applies the open notes, (h) hand off: Claude breaks the rest into tasks, Qwen builds
"""
import curses
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import orca
import orca_design

A = {}
MIN_ROWS, MIN_COLS = 24, 90


def init_colors():
    try:
        curses.start_color()
        curses.use_default_colors()
        for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW,
                               curses.COLOR_CYAN, curses.COLOR_MAGENTA), 1):
            curses.init_pair(i, c, -1)
        pair = curses.color_pair
    except curses.error:
        pair = lambda n: 0
    A.update(ok=pair(1) | curses.A_BOLD, bad=pair(2) | curses.A_BOLD, warn=pair(3), info=pair(4),
             ai=pair(5), dim=curses.A_DIM, bold=curses.A_BOLD, title=pair(4) | curses.A_BOLD,
             cur=pair(3) | curses.A_BOLD, plain=0, new=pair(1))


def put(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w - 1:
        return
    text = str(text).replace("\x00", "")              # curses refuses NUL bytes (a binary file once crashed the dashboard)
    try:
        win.addnstr(y, x, text, max(0, w - x - 1), attr)
    except (curses.error, ValueError):
        pass


def panel(title, y, x, h, w, lines):
    """lines: list of (text, attr)."""
    win = curses.newwin(h, w, y, x)
    try:
        win.border()
    except curses.error:
        pass
    put(win, 0, 2, " %s " % title, A["title"])
    for i, (text, attr) in enumerate(lines[:h - 2]):
        put(win, 1 + i, 1, text, attr)
    win.noutrefresh()


def window_start(n, idx, height):
    if n <= height:
        return 0
    return max(0, min(idx - height // 2, n - height))


def window_around(items, idx, height):
    """Slice of items that keeps idx visible."""
    s = window_start(len(items), idx, height)
    return items[s:s + height]


def fmt_k(n):
    return "%.1fk" % (n / 1000.0) if n >= 1000 else str(int(n))


# ------------------------------------------------------------------ data
def notes_ok(p):
    """From the orca/ notes folder show only the files you steer the build with (SPEC, TASKS, LESSONS, ...), not logs."""
    for d in orca.NOTES_DIRS:
        if p.startswith(d + "/"):
            rest = p[len(d) + 1:]
            return rest.endswith(".md") and (("/" not in rest) or rest.startswith("reviews/"))
    return True


def project_files(P):
    rc, out = P.git("ls-files -co --exclude-standard")
    if rc == 0:
        paths = [p for p in out.splitlines() if p and not p.startswith(("venv/", ".venv/")) and notes_ok(p)]
    else:
        paths = []
        for dp, dn, fn in os.walk(str(P.root)):
            dn[:] = [d for d in dn if d not in (".git", "venv", ".venv", "node_modules", "__pycache__")]
            paths += [p for p in (os.path.relpath(os.path.join(dp, f), str(P.root)) for f in fn) if notes_ok(p)]
    return sorted(paths)


def tree_lines(P, paths, cur, sel, pinned):
    """Returns ([(text, attr, path_or_None)], index of the selected row)."""
    now = time.time()
    lines, seen, sel_idx = [], set(), 0
    for p in paths:
        parts = p.split("/")
        for d in range(1, len(parts)):
            dp = "/".join(parts[:d])
            if dp not in seen:
                seen.add(dp)
                lines.append(("  " * (d - 1) + parts[d - 1] + "/", A["info"], None))
        try:
            age = now - (P.root / p).stat().st_mtime
        except OSError:
            age = 1e9
        is_sel, is_cur = (p == sel), (p == cur)
        attr = A["cur"] if is_cur else (A["ok"] if age < 90 else A["plain"])
        if is_sel and pinned:
            attr |= curses.A_REVERSE
        if is_sel:
            sel_idx = len(lines)
        mark = ">" if is_sel else ("*" if is_cur else " ")
        lines.append(("  " * (len(parts) - 1) + mark + " " + parts[-1], attr, p))
    return lines, sel_idx


def read_view(P, rel, action, height, scroll):
    """Lines of a file for the viewer. scroll=None means follow Qwen (tail after writes, head after reads)."""
    try:
        p = orca.read_path(P, rel, allow_notes=True)
        if p.stat().st_size > 2000000:
            return [("(file is larger than 2MB, press e to open it in your editor)", A["dim"])], 0, 0
        raw = p.read_bytes()
        if b"\x00" in raw[:8192]:
            return [("(binary file, %d bytes: not shown)" % len(raw), A["dim"])], 0, 0
        text = raw.decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return [("(cannot read %s)" % rel, A["dim"])], 0, 0
    rows = text.splitlines() or ["(empty file)"]
    n = len(rows)
    maxs = max(0, n - height)
    if scroll is None:
        start = 0 if action == "read_file" else maxs
    else:
        start = max(0, min(scroll, maxs))
    out = [("%4d  %s" % (i + 1, rows[i].replace("\t", "    ")), A["plain"]) for i in range(start, min(n, start + height))]
    return out, n, start


def usage_short(P):
    try:
        d = json.loads(P.rd("usage.json"))
    except ValueError:
        d = {}
    q = d.get("qwen", {}).get("build", {})
    cl = d.get("claude", {})
    ct = sum(e.get("input", 0) + e.get("output", 0) + e.get("cache_create", 0) for e in cl.values())
    cm = d.get("claude_model_used", "")
    cm = (" " + cm.replace("claude-", "")[:18]) if cm else ""
    if P.cfg["quota_check"]:
        s = "Claude%s %s tok" % (cm, fmt_k(ct))
    else:
        wt, wc = orca.window_tokens(P)
        s = "Claude %s tok, %dh: %s" % (fmt_k(ct), P.cfg["quota_window_hours"], fmt_k(wt))
        if P.cfg["claude_quota_tokens"]:
            s += " (%.0f%%)" % (100.0 * wt / P.cfg["claude_quota_tokens"])
    if P.cfg["show_cost"]:
        s += " $%.2f" % sum(e.get("cost_usd", 0) for e in cl.values())
    return "Qwen %s in / %s out" % (fmt_k(q.get("prompt", 0)), fmt_k(q.get("completion", 0))), s


def colorize_event(line):
    if line[9:11] == "==":              # task header: never colored by words inside the task title
        return A["bold"]
    u = line.upper()
    if "NO ERRORS" in u or "SMOKE OK" in u:     # good news that happens to contain the word "error"
        return A["ok"]
    if "PASS" in u or "ALL TASKS" in u:
        return A["ok"]
    if "BLOCKED" in u or "FAILED" in u or "ERROR" in u:
        return A["bad"]
    if "WARNING" in u or "WAIT" in u:
        return A["warn"]
    if "CLAUDE" in u or "REVIEW" in u or "ESCALAT" in u:
        return A["info"]
    if line[9:11] == "==":
        return A["bold"]
    return A["plain"]


# ------------------------------------------------------------------ drawing
QUOTA = {"q": None}


def poll_quota(P):
    while True:
        try:
            QUOTA["q"] = orca.fetch_and_record()
        except Exception as e:      # never let the poller kill the UI
            QUOTA["q"] = {"error": str(e)[:40]}
        time.sleep(60)


def draw(stdscr, P, st, ram, note, ui):
    rows, cols = stdscr.getmaxyx()
    stdscr.erase()
    if rows < MIN_ROWS or cols < MIN_COLS:
        put(stdscr, 0, 0, "Terminal too small: need %dx%d, have %dx%d. Resize or zoom out." % (MIN_COLS, MIN_ROWS, cols, rows), A["warn"])
        stdscr.noutrefresh()
        return
    if ui.get("screen") == "stats":
        draw_stats(stdscr, P, st, ui, note)
        return
    tasks = P.tasks()
    done = sum(1 for t in tasks if t["mark"] == "x")
    blk = sum(1 for t in tasks if t["mark"] == "!")
    total = max(len(tasks), 1)
    cur_task = st.get("task")
    age = int(time.time() - st.get("updated", time.time())) if st else 0

    # top bar
    phase = st.get("phase", "idle")
    if phase == "loading model":
        pct = st.get("load_pct") or 0
        phase = "LOADING MODEL %s %d%% (%ds)" % (orca.orca_bar(pct / 100.0, 14), pct, st.get("load_secs", 0))
    if phase == "Qwen generating" and st.get("since"):
        phase += " %ds" % int(time.time() - st["since"])
    design = orca_design.stage(P) == "design"
    if design:
        pr, n_open, _ = orca_design.summary_lines(P)
        top = " LocalOrca  %s   DESIGN STAGE  prototype %s  notes open %d   %s   RAM free %s%%   %s" % (
            P.root.name, "v%d" % pr["revision"] if pr.get("revision") else "not built", n_open, phase, ram[0],
            orca.quota_short(QUOTA["q"]) if P.cfg["quota_check"] else "")
    else:
        top = " LocalOrca  %s   tasks %s %d/%d  blocked %d   %s   RAM free %s%%   %s" % (
            P.root.name, orca.orca_bar((done + blk) / float(total), 18), done, len(tasks), blk, phase, ram[0],
            orca.quota_short(QUOTA["q"]) if P.cfg["quota_check"] else "")
    put(stdscr, 0, 0, top.ljust(cols - 1), curses.A_REVERSE)
    stdscr.noutrefresh()

    H = rows - 2
    lw = max(28, cols * 27 // 100)
    rw = cols - lw
    h_files = H * 52 // 100
    h_tasks = H - h_files
    h_file = H * 48 // 100
    h_low = H - h_file
    w_ai = rw * 52 // 100
    w_msg = rw - w_ai

    # files
    paths = project_files(P)
    ui["paths"] = paths
    cur_file = st.get("cur_file", "")
    if ui["pin"] and ui["pin"] not in paths:
        ui["pin"] = None                     # the file vanished (a blocked task was rolled back)
    pinned = ui["pin"]
    sel = pinned or cur_file
    ui["sel"] = sel
    entries, si = tree_lines(P, paths, cur_file, sel, bool(pinned))
    fstart = window_start(len(entries), si, h_files - 2)
    shown = entries[fstart:fstart + h_files - 2]
    panel("Files (%d)" % len(paths), 1, 0, h_files, lw, [(a, b) for a, b, _ in shown])
    ui["layout"]["files"] = (1, 0, h_files, lw, [c for _, _, c in shown])

    # tasks + usage
    tl, ti = [], 0
    for t in tasks:
        is_cur = (t["id"] == cur_task and t["mark"] == " ")
        mark = {"x": "[x]", "!": "[!]", " ": "[ ]"}[t["mark"]]
        attr = A["ok"] if t["mark"] == "x" else A["bad"] if t["mark"] == "!" else A["cur"] if is_cur else A["dim"]
        if is_cur:
            ti = len(tl)
        title = t["text"].split("|")[0].strip()
        tl.append(("%s %s %s" % (mark, t["id"], title), attr))
    body = h_tasks - 2 - 4
    lines = window_around(tl, ti, max(body, 1))
    lines = lines + [("", 0)] * (max(body, 0) - len(lines))
    q, c = usage_short(P)
    qs = orca.quota_short(QUOTA["q"]) if P.cfg["quota_check"] else ""
    if design:
        lines = design_lines(P, max(body, 1), lw - 3)
    lines += [(q, A["ai"]), (c, A["info"]), (qs, A["ok"] if QUOTA["q"] and not QUOTA["q"].get("error") else A["dim"]),
              (note or "tests judge, git remembers", A["dim"])]
    panel("Design review" if design else "Progress", 1 + h_files, 0, h_tasks, lw, lines)

    # current file
    action = st.get("cur_action", "")
    ui["layout"]["file"] = (1, lw, h_file, rw)
    ui["file_h"] = h_file - 2
    if sel:
        fl, n, vstart = read_view(P, sel, action, h_file - 2, ui["scroll"] if pinned else None)
        ui["shown_start"] = vstart
        if pinned:
            ui["scroll"] = vstart
        who = "Claude" if design else "Qwen"
        how = "pinned, f to follow %s" % who if pinned else "following %s: " % who + (action or "").replace("_file", "").replace("_in", " ")
        ftitle = "%s  [%s]  lines %d-%d of %d" % (sel, how, min(vstart + 1, n), min(vstart + h_file - 2, n), n)
    else:
        fl, ftitle = [("(no file touched yet, pick one on the left)", A["dim"])], "Current file"
    panel(ftitle, 1, lw, h_file, rw, fl)

    # AI
    ai = []
    ctx = st.get("ctx_tokens", 0)
    budget = orca.live_budget(P)
    busy_build = orca.running_pid(P) and str(st.get("phase", "")).startswith(("Qwen", "running tests", "smoke", "starting", "asking Claude", "course check"))
    ai.append(("%s  attempt %s/%s step %s%s" % (cur_task or "-", st.get("attempt", "-"), st.get("max_attempts", "-"), st.get("step", "-"),
                                                "  " + orca.orca_bar(None, 16) if busy_build else ""), A["bold"]))
    ai.append(("ctx %s %s/%s  thinking: %s" % (orca.bar(ctx / float(budget), 14), fmt_k(ctx), fmt_k(budget), st.get("think", "?")), A["info"] if ctx < budget * 0.8 else A["warn"]))
    for a_ in (st.get("actions") or [])[-4:]:
        ai.append(("> " + a_, A["info"]))
    wrap_w = max(10, w_ai - 3)
    text_lines = []
    for chunk, attr in ((st.get("ai_think", ""), A["dim"]), (st.get("ai_text", ""), A["ai"])):
        for para in (chunk or "").splitlines():
            for l in (textwrap.wrap(para, wrap_w) or [""]):
                text_lines.append((l, attr))
    room = h_low - 2 - len(ai)
    ai += text_lines[-room:] if room > 0 else []
    if design:
        busy = orca.running_pid(P)
        ai = [(("Claude  step %s  %s" % (st.get("step", 0), orca.orca_bar(None, 16))) if busy else "Claude is idle", A["bold"]),
              (str(st.get("phase", ""))[:w_ai - 3], A["info"])]      # the same orca bar as Qwen's panel
        for a_ in (st.get("actions") or [])[-6:]:
            ai.append(("> " + a_, A["info"]))
        if not busy:
            ai += [("", 0)] + [(l, A["dim"]) for l in textwrap.wrap(str(orca_design.proto(P).get("summary") or ""), wrap_w)]
        else:
            ai += [("", 0)] + [(l, A["ai"]) for para in str(st.get("ai_text") or "").splitlines() for l in (textwrap.wrap(para, wrap_w) or [""])]
        if len(ai) > h_low - 2:                # keep the header and actions, show the END of Claude's text
            head, tail = ai[:8], ai[8:]
            room = h_low - 2 - len(head)
            ai = head + (tail[-room:] if room > 0 else [])
    if st.get("phase") == "loading model":
        pct = st.get("load_pct") or 0
        ai = [("Loading model into memory", A["bold"]), (st.get("load_name", ""), A["info"]), ("", 0),
              ("%s %d%%" % (orca.orca_bar(pct / 100.0, max(10, w_ai - 12)), pct), A["ok"]),
              ("%ds elapsed" % st.get("load_secs", 0), A["dim"]), ("", 0),
              ("Other models were unloaded first.", A["dim"]), ("A 27B takes 20 to 90 seconds.", A["dim"])]
    panel(("Claude (design stage)" if design else (st.get("model") or "Qwen").split("/")[-1])[:w_ai - 6], 1 + h_file, lw, h_low, w_ai, ai)

    # messages
    msgs = []
    for l in P.rd("events.log").splitlines()[-(h_low * 2):]:
        attr = colorize_event(l)
        for k, piece in enumerate(textwrap.wrap(l, max(10, w_msg - 3), subsequent_indent="   ") or [""]):
            msgs.append((piece, attr))
    msgs = msgs[-(h_low - 2):] or [("(no messages yet)", A["dim"])]
    panel("Messages", 1 + h_file, lw + w_ai, h_low, w_msg, msgs)

    if design:
        put(stdscr, rows - 1, 0, " (q) quit  (P) see the prototype  (n) add note  (b) Claude builds / applies notes  (h) hand off to Qwen  (K) kill  (a) ask Claude  (o) settings  (t) stats  (e) edit   state %ds old" % age, A["dim"])
        stdscr.noutrefresh()
        curses.doupdate()
        return
    put(stdscr, rows - 1, 0, " (q) quit  (r) run  (n) add task  (s) stop after task  (K) kill  (a) ask Claude  (o) settings  (t) stats  (P) preview  (e) edit  (f) follow Qwen  (Up/Down) browse  (Enter) open  (PgUp/PgDn) scroll   state %ds old" % age, A["dim"])
    stdscr.noutrefresh()
    curses.doupdate()


def design_lines(P, height, width):
    """The left panel in the design stage: where the prototype stands, then your notes (open ones first)."""
    pr = orca_design.proto(P)
    busy = orca.running_pid(P)
    if not pr.get("revision"):
        head = [("Prototype: not built yet", A["warn"]),
                ("Claude is building it, watch on the right" if busy else "(b) let Claude build it", A["dim"])]
    elif pr.get("ok"):
        head = [("Prototype v%d: GREEN (tests + app)" % pr["revision"], A["ok"]),
                ("(P) look at it, then (n) notes or (h) hand off", A["dim"])]
    else:
        head = [("Prototype v%d: checks FAIL" % pr["revision"], A["bad"]),
                ("(b) Claude fixes it. Hand-off waits for green", A["dim"])]
    if pr.get("revision") and pr.get("ok") and not busy:
        head += [("", 0), ("NEXT, in your browser (P):", A["bold"])]
        steps = pr.get("try") or ["click through every screen", "judge the LOOK and layout (logic is Qwen's job later)"]
        steps = steps + ["anything off? (n) note it, (b) Claude applies notes",
                         "happy? (h) hand off. Open notes become Qwen's first tasks"]
        for i, st_ in enumerate(steps):
            for j, l in enumerate(textwrap.wrap(st_, max(10, width - 4)) or [""]):
                head.append((("%d/ " % (i + 1) if j == 0 else "   ") + l, A["info"] if i < len(steps) - 2 else A["dim"]))
    t = orca_design.design_totals(P)
    if t["calls"]:
        head.append(("Claude: %s tokens, %s cache read, %d calls" % (orca.fmt_tok(t["counted"]), orca.fmt_tok(t["cache_read"]), t["calls"]), A["ai"]))
    ns = orca_design.notes(P)
    ns = [n for n in ns if n["mark"] == " "] + [n for n in ns if n["mark"] != " "]
    body = [("", 0), ("Notes  [ ] open  [x] applied  [>] Qwen task", A["bold"])]
    for n in ns:
        attr = A["cur"] if n["mark"] == " " else A["ok"] if n["mark"] == "x" else A["dim"]
        body.append(("[%s] %s %s" % (n["mark"], n["id"], n["text"]), attr))
    if not ns:
        body.append(("(n) add a note, or Feedback in the preview", A["dim"]))
    lines = (head + body)[:height]
    return lines + [("", 0)] * (height - len(lines))


def add_task(stdscr, P):
    """(n) in the build stage: a task for Qwen, like the Feedback button in the preview. Goes to the front of the open
    tasks (after the one running now), one task per numbered request, committed on its own so a rollback cannot erase it."""
    text = ask_line(stdscr, "Add a task for Qwen", "What should change? Be specific. 1. ... 2. ... makes several tasks")
    if not text or not text.strip():
        return "no task added"
    import orca_preview
    parts = orca_preview.split_feedback(" ".join(text.split())[:400])
    first = orca_preview.add_feedback_task(P, " ".join(text.split())[:400])
    try:
        with open(str(P.o / "events.log"), "a") as f:
            f.write(time.strftime("%H:%M:%S") + " you added %s from the dashboard: %s\n" % (first, text.strip()[:100]))
    except OSError:
        pass
    more = " (+%d more)" % (len(parts) - 1) if len(parts) > 1 else ""
    return "added %s%s. %s" % (first, more, "Qwen does it right after the current task" if orca.running_pid(P) else "(r) to build it")


def design_key(stdscr, P, ch):
    """Keys of the design stage. Returns a message, or None when the key is not a design key."""
    busy = orca.running_pid(P)
    if ch == ord("n"):
        text = ask_line(stdscr, "Design note", "What should change in the prototype? 1. ... 2. ... makes several notes")
        if not text or not text.strip():
            return "no note added"
        ids = orca_design.add_note(P, text)
        return "added %s. (b) Claude applies it now, or (h) Qwen gets it as a first task" % ", ".join(ids)
    if ch == ord("b"):
        if busy:
            return "Claude is already working (pid %d)" % busy
        pr, n_open = orca_design.proto(P), len(orca_design.open_notes(P))
        if pr.get("revision") and pr.get("ok") and not n_open:
            return "prototype is green and no notes are open. Add one with (n), or (h) to hand off"
        orca_design.start_bg(P, "design")
        return ("Claude builds the prototype" if not pr.get("revision") else
                "Claude applies %d notes" % n_open if n_open else "Claude fixes the prototype checks")
    if ch == ord("h"):
        if busy:
            return "wait for Claude to finish first (or (K) to stop it)"
        pr = orca_design.proto(P)
        if not pr.get("revision"):
            return "no prototype yet, (b) builds it"
        n_open = len(orca_design.open_notes(P))
        if not confirm(stdscr, "Hand off to Qwen?", [
                "Claude checks the prototype (tests + real app), then breaks the rest of the product into small tasks.",
                "%d open notes become Qwen's first tasks. After this, Claude stops changing the prototype." % n_open,
                "y: hand off and start the build.   n: keep designing."]):
            return "still designing"
        orca_design.start_bg(P, "handoff", "--go")
        return "hand-off started: Claude plans the tasks, then Qwen starts"
    if ch in (ord("r"), ord("R")):
        return "design stage: approve the prototype first with (h), the build starts after the hand-off"
    return None


KIND = {}
EXIT = {}


def stats_attrs():
    KIND.update(h=A["title"], n=0, dim=A["dim"], ok=A["ok"], bad=A["bad"], warn=A["warn"], info=A["info"], ai=A["ai"], bold=A["bold"])


def draw_stats(stdscr, P, st, ui, note):
    rows, cols = stdscr.getmaxyx()
    stdscr.erase()
    if not KIND:
        stats_attrs()
    if not ui.get("stats") or time.time() - ui.get("stats_at", 0) > 5:
        ui["stats"] = orca.stats_lines(P, quota=QUOTA["q"], fetch=False)
        ui["stats_at"] = time.time()
    lines = ui["stats"]
    H = rows - 2
    ui["stats_scroll"] = max(0, min(ui.get("stats_scroll", 0), max(0, len(lines) - H)))
    sc = ui["stats_scroll"]
    put(stdscr, 0, 0, (" LocalOrca statistics  %s   whole project, all runs" % P.root.name).ljust(cols - 1), curses.A_REVERSE)
    for i, (text, kind) in enumerate(lines[sc:sc + H]):
        put(stdscr, 1 + i, 0, text, KIND.get(kind, 0))
    foot = " (t) back  (Up/Down) (PgUp/PgDn) scroll  (r) run  (s) stop after task  (K) kill  (o) settings  (q) quit   lines %d-%d of %d" % (sc + 1, min(sc + H, len(lines)), len(lines))
    put(stdscr, rows - 1, 0, (note or foot), A["warn"] if note else A["dim"])
    stdscr.noutrefresh()
    curses.doupdate()


# ------------------------------------------------------------------ "your turn" popups
def attention(P, st):
    """When the build or the design waits for YOU, what to tell you. Returns (id, title, lines, [(key, label)]) or None.
    The id makes each popup show once (remembered in orca/popups_seen.json)."""
    busy = orca.running_pid(P)
    if busy:
        return None
    if orca_design.stage(P) == "design":
        pr = orca_design.proto(P)
        n_open = len(orca_design.open_notes(P))
        rev = pr.get("revision") or 0
        if rev and pr.get("ok"):
            steps = pr.get("try") or ["Click through every screen.", "Judge the LOOK and layout. Logic that is still a placeholder is Qwen's job later."]
            lines = ["Tests pass and the app starts. Your turn: look at it and decide.", ""]
            lines += ["%d/ %s" % (i + 1, t) for i, t in enumerate(steps)]
            lines += ["", "Something to change? Add a note for each thing, then let Claude apply them.",
                      "Happy with it? Hand off: Claude plans the rest, Qwen builds it." +
                      (" Your %d open notes become Qwen's first tasks." % n_open if n_open else "")]
            keys = [("P", "open it in the browser"), ("n", "add a note")] + \
                   ([("b", "Claude applies the %d notes" % n_open)] if n_open else []) + [("h", "hand off to Qwen")]
            return ("ready-%d-%s" % (rev, int(pr.get("at") or 0)), "Prototype v%d is ready" % rev, lines, keys)
        if rev and not pr.get("ok"):
            fail = [l for l in str(pr.get("fail") or "").splitlines() if l.strip()][:6]
            return ("fail-%d-%s" % (rev, int(pr.get("at") or 0)), "Prototype v%d is not green yet" % rev,
                    ["The tests or the real-app check failed after Claude's fix rounds:", ""] + fail +
                    ["", "Give Claude another round, or ask it what is wrong. Hand-off waits for green."],
                    [("b", "Claude fixes it"), ("a", "ask Claude"), ("P", "look at it anyway")])
        return None
    reason = st.get("halt_reason")
    if reason and str(st.get("phase", "")).startswith("finished or halted") and P.next_task():
        blocked = [t["id"] for t in P.tasks() if t["mark"] == "!"][-3:]
        return ("halt-%s" % int(st.get("halted_at") or 0), "The build stopped: %s" % reason,
                ["Qwen could not finish %s, so the build halted instead of burning hours." % ", ".join(blocked),
                 "", "1/ (t) stats and orca/REPORT.md show why.",
                 "2/ (a) ask Claude to look at the blocked tasks, fix them or rewrite them smaller.",
                 "3/ (e) edit orca/TASKS.md yourself: delete, split or reorder tasks.",
                 "4/ (r) run again when the plan looks right."],
                [("a", "ask Claude"), ("t", "stats"), ("r", "run again")])
    return None


def popups_seen(P):
    try:
        return set(json.loads(P.rd("popups_seen.json")))
    except ValueError:
        return set()


def mark_popup_seen(P, pid):
    seen = popups_seen(P) | {pid}
    try:
        P.wr("popups_seen.json", json.dumps(sorted(seen)[-50:]))
    except OSError:
        pass


def action_dialog(stdscr, title, body, keys):
    """A popup with instructions and the keys that act on them. Returns the chosen key, or None for Esc (later)."""
    rows, cols = stdscr.getmaxyx()
    w = max(50, min(cols - 4, 92))
    lines = []
    for b in body:
        lines += textwrap.wrap(b, w - 4, subsequent_indent="   " if b[:2].rstrip("/").isdigit() else "") or [""]
    keyline = "   ".join("(%s) %s" % k for k in keys) + "   (Esc) later"
    keyl = textwrap.wrap(keyline, w - 4)
    h = min(rows - 2, len(lines) + len(keyl) + 4)
    win = curses.newwin(h, w, max(0, (rows - h) // 2), max(0, (cols - w) // 2))
    stdscr.timeout(-1)
    try:
        while True:
            win.erase()
            win.border()
            put(win, 0, 2, " %s " % title, A["title"] | curses.A_BOLD)
            for i, l in enumerate(lines[:h - len(keyl) - 3]):
                put(win, 1 + i, 2, l)
            for i, l in enumerate(keyl):
                put(win, h - 1 - len(keyl) + i, 2, l, A["ok"] | curses.A_BOLD)
            win.refresh()
            ch = stdscr.getch()
            if ch in (27, ord("q")):
                return None
            for k, _ in keys:
                if ch == ord(k):
                    return k
    finally:
        stdscr.timeout(1000)


def confirm(stdscr, title, body):
    """Small modal yes/no. Returns True only for y."""
    rows, cols = stdscr.getmaxyx()
    w = max(40, min(cols - 4, 74))
    lines = []
    for b in body:
        lines += textwrap.wrap(b, w - 4) or [""]
    h = len(lines) + 4
    win = curses.newwin(h, w, max(0, (rows - h) // 2), max(0, (cols - w) // 2))
    stdscr.timeout(-1)
    try:
        while True:
            win.erase()
            win.border()
            put(win, 0, 2, " %s " % title, A["bad"])
            for i, l in enumerate(lines):
                put(win, 1 + i, 2, l)
            put(win, h - 2, 2, "(y) yes     (n) no  (Enter = no)", A["bold"])
            win.refresh()
            ch = stdscr.getch()
            if ch in (ord("y"), ord("Y")):
                return True
            if ch in (ord("n"), ord("N"), 10, 13, curses.KEY_ENTER, 27):
                return False
    finally:
        stdscr.timeout(1000)


def kill_build(stdscr, P):
    pid = orca.running_pid(P)
    if not pid:
        msg = orca.stop_preview(P)
        return msg or "no running build to kill"
    info = run_info(P) or {}
    where = " on %s" % info["task"] if info.get("task") else ""
    if not confirm(stdscr, "Kill the build?",
                   ["Build process %d%s will be ended, and the live preview with your app. Tasks already passed stay committed." % (pid, where),
                    "Ctrl-C first, force kill if it hangs. The task in progress is not finished: your files are kept, Qwen will see them on the next run."]):
        return "kill cancelled"
    put(stdscr, stdscr.getmaxyx()[0] - 1, 0, " killing the build, up to 8 seconds... ", A["warn"])
    stdscr.refresh()
    return orca.kill_run(P, with_preview=True)


def current_model(P):
    try:
        return json.loads((P.root / "orca.json").read_text()).get("model") or "auto"
    except (OSError, ValueError):
        return "auto"


def model_rows(P):
    """[(id, label)] of downloaded chat models, from LM Studio. None when LM Studio cannot be reached."""
    ms = orca.all_models(P.cfg)
    if ms is None:
        try:
            ms = [{"id": m} for m in orca.lm_models(P.cfg)]
        except Exception:
            return None
    rows = [("auto", "auto     let LocalOrca pick the best model that fits (prefers 27B)")]
    for m in sorted(ms, key=lambda m: (m.get("state") != "loaded", m["id"])):
        if m.get("type") not in (None, "llm", "vlm") or "embed" in m["id"].lower():
            continue
        b = orca.param_b(m["id"])
        tags = []
        if m.get("state") == "loaded":
            tags.append("LOADED")
        if m.get("type") == "vlm":
            tags.append("vision")
        if b and b > P.cfg["max_model_b"]:
            tags.append("may not fit")
        elif b and b < P.cfg["min_model_b"]:
            tags.append("small")
        if m.get("quantization"):
            tags.append(str(m["quantization"]))
        rows.append((m["id"], "%-44s %6s  %s" % (m["id"][:44], ("%gB" % b) if b else "", ", ".join(tags))))
    return rows


CLAUDE_ROWS = [("claude:", "default  whatever Claude Code uses (your /model setting)"),
               ("claude:opus", "opus     strongest checks and rescues, uses the most quota"),
               ("claude:sonnet", "sonnet   good checks, lighter on quota"),
               ("claude:haiku", "haiku    cheapest, weakest. Fine for simple projects")]


def current_claude(P):
    try:
        return json.loads((P.root / "orca.json").read_text()).get("claude_model") or ""
    except (OSError, ValueError):
        return ""


def pick_model_dialog(stdscr, P):
    rows = model_rows(P)
    if rows is None:
        rows = [("-", "(LM Studio not reachable, only Claude can be changed)")]
    rows = [("#", "LOCAL MODEL (builds the code)")] + rows + [("#", ""), ("#", "CLAUDE (plans, course checks, rescues stuck tasks)")] + CLAUDE_ROWS
    cur = current_model(P)
    ccur = "claude:" + current_claude(P)
    pickable = [i for i, r in enumerate(rows) if r[0] not in ("#", "-")]
    sel = next((i for i, r in enumerate(rows) if r[0] == cur), pickable[0])
    H, W = stdscr.getmaxyx()
    w = max(60, min(W - 4, 96))
    h = max(8, min(H - 4, len(rows) + 6))
    win = curses.newwin(h, w, max(0, (H - h) // 2), max(0, (W - w) // 2))
    view = h - 5
    stdscr.timeout(-1)
    try:
        while True:
            top = window_start(len(rows), sel, view)
            win.erase()
            win.border()
            put(win, 0, 2, " Models (L) ", A["title"])
            put(win, 1, 2, "now: %s   Claude: %s" % (cur, ccur[7:] or "default"), A["info"])
            for i, (mid, label) in enumerate(rows[top:top + view]):
                k = top + i
                if mid == "#":
                    put(win, 2 + i, 2, label, A["title"])
                    continue
                mark = ">" if k == sel else " "
                attr = curses.A_REVERSE if k == sel else (A["ok"] if mid in (cur, ccur) else (A["dim"] if mid == "-" else 0))
                put(win, 2 + i, 2, ("%s %s" % (mark, label)).ljust(w - 4), attr)
            put(win, h - 2, 2, "(Up/Down) choose   (Enter) select   (Esc) cancel", A["dim"])
            win.refresh()
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                sel = max([i for i in pickable if i < sel] or [sel])
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel = min([i for i in pickable if i > sel] or [sel])
            elif ch == curses.KEY_PPAGE:
                sel = max([i for i in pickable if i <= sel - view] or [pickable[0]])
            elif ch == curses.KEY_NPAGE:
                sel = min([i for i in pickable if i >= sel + view] or [pickable[-1]])
            elif ch in (27, ord("q")):
                return "model unchanged"
            elif ch in (10, 13, curses.KEY_ENTER):
                break
    finally:
        stdscr.timeout(1000)
    mid = rows[sel][0]
    if mid.startswith("claude:"):
        name = mid[7:]
        orca.set_claude_model(P, name)
        return "Claude model set to %s (counts from the next Claude call, no restart needed)" % (name or "Claude Code default")
    if mid == cur:
        return "already using " + mid
    orca.set_model(P, mid)
    if orca.running_pid(P):
        if confirm(stdscr, "Switch model now?", ["Saved %s in orca.json." % mid,
                                                  "y: stop the build now and restart it with this model (the current task restarts, its files are kept).",
                                                  "n: keep going, the new model is used from the next start."]):
            put(stdscr, stdscr.getmaxyx()[0] - 1, 0, " stopping the build, then restarting with %s ... " % mid, A["warn"])
            stdscr.refresh()
            orca.kill_run(P)
            start_build(P)
            return "restarted with %s (loading a new model takes a minute)" % mid
        return "model set to %s, used from the next start" % mid
    if confirm(stdscr, "Start the build?", ["Saved %s in orca.json." % mid, "Start the build now with it?"]):
        start_build(P)
        return "build started with " + mid
    return "model set to " + mid


def context_dialog(stdscr, P):
    """(C): how much conversation Qwen may hold per attempt. The budget counts from the next step. A bigger model
    context needs a reload of the model (more RAM), offered here."""
    budget = orca.live_budget(P)
    rows = orca.CONTEXT_PRESETS
    try:
        have = orca.loaded_context(P.cfg, P.cfg.get("model") or "")
    except Exception:
        have = None
    sel = next((i for i, r in enumerate(rows) if r[0] == budget), 2)
    H, W = stdscr.getmaxyx()
    w = max(64, min(W - 4, 96))
    h = len(rows) + 7
    win = curses.newwin(h, w, max(0, (H - h) // 2), max(0, (W - w) // 2))
    stdscr.timeout(-1)
    try:
        while True:
            win.erase()
            win.border()
            put(win, 0, 2, " Context (C) ", A["title"])
            put(win, 1, 2, "budget now %s   model loaded with %s" % (fmt_k(budget), fmt_k(have) if have else "? (LM Studio did not say)"), A["info"])
            for i, (b, c) in enumerate(rows):
                tag = "  (default)" if b == 28000 else ""
                ram = "  needs a model reload, uses more RAM" if have and c > have else ""
                line = "%s budget %-4s  model context %-4s%s%s" % (">" if i == sel else " ", fmt_k(b), fmt_k(c), tag, ram)
                put(win, 3 + i, 2, line.ljust(w - 4), curses.A_REVERSE if i == sel else (A["ok"] if b == budget else 0))
            put(win, h - 3, 2, "Budget = our cautious estimate of the chat size. The reply (4k) must fit in the model context too.", A["dim"])
            put(win, h - 2, 2, "(Up/Down) choose   (Enter) select   (Esc) cancel", A["dim"])
            win.refresh()
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel = min(len(rows) - 1, sel + 1)
            elif ch in (27, ord("q")):
                return "context unchanged"
            elif ch in (10, 13, curses.KEY_ENTER):
                break
    finally:
        stdscr.timeout(1000)
    b, c = rows[sel]
    orca.set_context(P, b, c)
    reload_needed = bool(have) and c > have
    if not reload_needed:
        return "budget set to %s, counts from the next step (no restart needed)" % fmt_k(b)
    if orca.running_pid(P):
        if confirm(stdscr, "Reload the model now?", ["Saved budget %s and context %s in orca.json." % (fmt_k(b), fmt_k(c)),
                                                       "y: stop the build, reload the model with the bigger context (about a minute, more RAM) and restart. The current task restarts, its files are kept.",
                                                       "n: keep going. The bigger context loads at the next start. Until then keep the budget at or under what is loaded."]):
            orca.kill_run(P)
            start_build(P)
            return "restarted, reloading the model with %s context" % fmt_k(c)
    return "saved, the model reloads with %s context at the next start" % fmt_k(c)


SETTINGS_ROWS = [          # (key it triggers, label, what it does)
    ("L", "Models", "the local model that builds, and the Claude model"),
    ("C", "Context", "how much chat Qwen may hold per attempt (28k default)"),
    ("D", "Folders", "folders outside the project that Qwen may read"),
]


def settings_dialog(stdscr, P):
    """(o): one menu for everything you can look at or change. Returns the key of the chosen item, or None."""
    H, W = stdscr.getmaxyx()
    w = max(60, min(W - 4, 84))
    h = len(SETTINGS_ROWS) + 5
    win = curses.newwin(h, w, max(0, (H - h) // 2), max(0, (W - w) // 2))
    sel = 0
    stdscr.timeout(-1)
    try:
        while True:
            win.erase()
            win.border()
            put(win, 0, 2, " Settings (o) ", A["title"])
            for i, (k, name, what) in enumerate(SETTINGS_ROWS):
                line = "%s (%s) %-13s %s" % (">" if i == sel else " ", k, name, what)
                put(win, 2 + i, 2, line[:w - 4].ljust(w - 4), curses.A_REVERSE if i == sel else 0)
            put(win, h - 2, 2, "(Up/Down) choose   (Enter) open   (Esc) close", A["dim"])
            win.refresh()
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel = min(len(SETTINGS_ROWS) - 1, sel + 1)
            elif ch in (27, ord("q"), ord("o")):
                return None
            elif ch in (10, 13, curses.KEY_ENTER):
                return SETTINGS_ROWS[sel][0]
            else:
                hit = [k for k, _, _ in SETTINGS_ROWS if ord(k) == ch]
                if hit:
                    return hit[0]
    finally:
        stdscr.timeout(1000)


def clean_path(s):
    """Paths pasted or dragged in from Finder come quoted or with backslash-escaped spaces."""
    s = s.strip().strip("'\"")
    return os.path.expanduser(s.replace("\\ ", " "))


def ask_line(stdscr, title, hint):
    """One line of text input. Returns None on Esc."""
    H, W = stdscr.getmaxyx()
    w = max(50, min(W - 4, 100))
    win = curses.newwin(6, w, max(0, (H - 6) // 2), max(0, (W - w) // 2))
    buf = ""
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    stdscr.timeout(-1)
    try:
        while True:
            win.erase()
            win.border()
            put(win, 0, 2, " %s " % title, A["title"])
            put(win, 1, 2, hint, A["dim"])
            shown = buf[-(w - 6):]
            put(win, 3, 2, "> " + shown)
            put(win, 4, 2, "(Enter) ok   (Esc) cancel", A["dim"])
            try:
                win.move(3, min(w - 2, 4 + len(shown)))
            except curses.error:
                pass
            win.refresh()
            ch = stdscr.getch()
            if ch == 27:
                return None
            if ch in (10, 13, curses.KEY_ENTER):
                return buf
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                buf = buf[:-1]
            elif 32 <= ch < 0x110000 and ch not in (curses.KEY_RESIZE,):
                try:
                    buf += chr(ch)
                except ValueError:
                    pass
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.timeout(1000)


def folders_dialog(stdscr, P):
    """(D): folders outside the project that Qwen may read. Saved in orca.json, counts from the next step."""
    try:
        dirs = list(json.loads((P.root / "orca.json").read_text()).get("extra_dirs", []))
    except (OSError, ValueError):
        dirs = []
    sel, msg = 0, ""
    H, W = stdscr.getmaxyx()
    w = max(60, min(W - 4, 100))
    stdscr.timeout(-1)
    try:
        while True:
            h = max(10, min(H - 4, len(dirs) + 9))
            stdscr.touchwin()
            stdscr.refresh()          # clear leftovers of the input box and of a smaller list
            win = curses.newwin(h, w, max(0, (H - h) // 2), max(0, (W - w) // 2))
            win.erase()
            win.border()
            put(win, 0, 2, " Extra folders Qwen may READ (D) ", A["title"])
            put(win, 1, 2, "Read-only. Qwen still writes only inside the project.", A["dim"])
            if not dirs:
                put(win, 3, 2, "(none yet, press (a) to add one)", A["dim"])
            for i, d in enumerate(dirs[:h - 7]):
                ok = os.path.isdir(clean_path(d))
                label = "%s %s%s" % (">" if i == sel else " ", d, "" if ok else "   (missing)")
                put(win, 3 + i, 2, label.ljust(w - 4), curses.A_REVERSE if i == sel else (0 if ok else A["warn"]))
            put(win, h - 3, 2, msg, A["warn"])
            put(win, h - 2, 2, "(a) add   (x) remove   (Up/Down) choose   (Esc) done", A["dim"])
            win.refresh()
            ch = stdscr.getch()
            msg = ""
            if ch in (27, ord("q"), 10, 13):
                break
            if ch in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel = min(max(0, len(dirs) - 1), sel + 1)
            elif ch in (ord("x"), curses.KEY_DC, 127, curses.KEY_BACKSPACE) and dirs:
                dirs.pop(sel)
                sel = max(0, min(sel, len(dirs) - 1))
                orca.set_extra_dirs(P, dirs)
            elif ch == ord("a"):
                stdscr.touchwin()
                stdscr.refresh()
                raw = ask_line(stdscr, "Add a folder", "Type or paste a path, or drag the folder from Finder into this window.")
                if raw:
                    p = Path(clean_path(raw)).resolve()
                    if not p.is_dir():
                        msg = "not a folder: %s" % p
                    elif p in (Path("/"), Path.home().resolve()):
                        msg = "too broad (whole disk or home folder). Pick a specific folder."
                    elif p == P.root or P.root in p.parents:
                        msg = "that is inside the project already"
                    else:
                        d = str(p).replace(str(Path.home()), "~", 1) if str(p).startswith(str(Path.home()) + "/") else str(p)
                        if d not in dirs:
                            dirs.append(d)
                            orca.set_extra_dirs(P, dirs)
                        sel = dirs.index(d)
    finally:
        stdscr.timeout(1000)
    return "extra folders: %s" % (", ".join(dirs) if dirs else "none") + ("  (Qwen sees changes from its next step)" if dirs else "")


def toggle_preview(stdscr, P):
    """(P): start the live preview in the background, or open it if it already runs."""
    url = "http://localhost:%s" % (orca.preview_port_running(P) or orca.choose_preview_port(P))
    if orca.preview_pid(P):
        if confirm(stdscr, "Live preview", ["Running at %s" % url, "y: open it in your browser.   n: stop the preview."]):
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "opened " + url
        os.kill(orca.preview_pid(P), signal.SIGINT)
        return "preview stopped"
    url = orca.start_preview_bg(P)
    if not url:
        return 'set "preview_url" (and "preview_cmd") in orca.json first, see README "Live preview"'
    return "preview starting at %s (reloads after every passed task, Feedback button bottom right)" % url


def mouse_wheel(b):
    up = b & getattr(curses, "BUTTON4_PRESSED", 0x10000)
    down = b & getattr(curses, "BUTTON5_PRESSED", 0x200000)
    left = curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED
    if not (up or down) and (b & 0x8000000) and not (b & left):
        down = 1
    return -1 if up else (1 if down else 0)


def pick_editor():
    for cand in (os.environ.get("VISUAL"), os.environ.get("EDITOR"), "nano", "vim", "vi"):
        if cand:
            parts = shlex.split(cand)
            if parts and shutil.which(parts[0]):
                return parts
    return None


def claude_chat_cmd(P):
    """The command that opens an interactive Claude Code session in the project, primed with how this project works."""
    base = [a for a in (P.cfg.get("claude_cmd") or ["claude"]) if a != "-p"] or ["claude"]
    model = chat_model(P)
    tasks = P.tasks()
    n = lambda m: sum(1 for t in tasks if t["mark"] == m)
    brief = (
        "You are talking with the owner of this project, from inside the LocalOrca dashboard. LocalOrca is a build pipeline: "
        "Claude plans, a local Qwen model builds one small task at a time, tests judge, git remembers. "
        "Project: %s. Tasks: %d done, %d open, %d blocked. A build %s. "
        "Notes live in orca/: SPEC.md (what we build), TASKS.md (one [ ] line per task), LESSONS.md, CHECKPOINT.md (course checks), "
        "events.log and REPORT.md (what happened). Read them when the question needs them; do not read them all up front. "
        "RULES: if a build is running, Qwen edits files and commits all the time. Do not edit source files or run git write commands "
        "while it runs unless the owner clearly asks, and say that a task rollback could undo uncommitted edits. "
        "To change the plan, edit orca/TASKS.md (keep the one-line format with ' | done when: ...'). "
        "Never use em dashes. Keep answers short."
    ) % (P.root.name, n("x"), n(" "), n("!"), "IS RUNNING" if orca.running_pid(P) else "is not running")
    return base + ["--model", model, "--append-system-prompt", brief]


CHAT_DEFAULT_MODEL = "sonnet"


def chat_model(P):
    """The model (a) opens. orca.json "chat_model" wins, then the project's Claude model from (L), else Sonnet.
    Never Claude Code's own default, which is the biggest model (it opened Opus on a project with no model set)."""
    try:
        c = json.loads((P.root / "orca.json").read_text())
    except (OSError, ValueError):
        c = {}
    return c.get("chat_model") or c.get("claude_model") or P.cfg.get("claude_model") or CHAT_DEFAULT_MODEL


# ------------------------------------------------------------------ (a): the small ask box
def quick_ask(P, question, session=None, timeout=240):
    """One question to Claude about this project, read-only (it can Read, Glob and Grep, never edit), with the project
    brief. Follow-ups continue the same Claude session. Returns (answer, session id, error)."""
    full = claude_chat_cmd(P)
    base = full[:full.index("--model")]
    brief = full[full.index("--append-system-prompt") + 1] + " You are answering in a small box inside the dashboard: answer in a few short lines."
    cmd = base + ["-p", "--model", chat_model(P), "--append-system-prompt", brief, "--allowedTools", "Read,Glob,Grep",
                  "--output-format", "json"] + (["--resume", session] if session else []) + [question]
    try:
        r = subprocess.run(cmd, cwd=str(P.root), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return "", session, "cannot find '%s' on your PATH" % cmd[0]
    except subprocess.TimeoutExpired:
        return "", session, "no answer within %d s" % timeout
    try:
        j = json.loads(r.stdout)
    except ValueError:
        return "", session, (r.stdout + r.stderr).strip()[-300:] or "no answer"
    u = j.get("usage") or {}
    orca.record("claude", "ask", input=u.get("input_tokens"), output=u.get("output_tokens"),
                cache_read=u.get("cache_read_input_tokens"), cache_create=u.get("cache_creation_input_tokens"),
                cost_usd=j.get("total_cost_usd"))
    if j.get("is_error"):
        return "", j.get("session_id") or session, str(j.get("result") or "error")[:300]
    return (j.get("result") or "").strip(), j.get("session_id") or session, ""


def ask_box(stdscr, P):
    """(a): a small box to ask Claude about the project without leaving the dashboard. Capital A on an empty line
    opens the full Claude Code session instead. Returns "big" for that, or a status message."""
    model = chat_model(P)
    convo, buf, scroll = [], "", 0
    session, job = None, None              # job: {"thread", "result"} while Claude answers
    stdscr.timeout(200)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    try:
        while True:
            rows, cols = stdscr.getmaxyx()
            w = max(50, min(cols - 4, 110))
            h = max(12, min(rows - 2, 30))
            win = curses.newwin(h, w, max(0, (rows - h) // 2), max(0, (cols - w) // 2))
            body = []
            if not convo:
                body = [("Ask anything about %s: what is it doing, why did T05 fail, what is left..." % P.root.name, A["dim"]),
                        ("Read-only: Claude can look at files, never change them.", A["dim"]),
                        ("For changes, press A on an empty line: the full Claude Code session.", A["dim"])]
            for who, text in convo:
                for para in text.splitlines() or [""]:
                    for i, l in enumerate(textwrap.wrap(para, w - 6) or [""]):
                        body.append((("you: " if who == "q" and i == 0 else "     " if who == "q" else "") + l,
                                     A["bold"] if who == "q" else (A["bad"] if who == "e" else A["ai"])))
                body.append(("", 0))
            room = h - 5
            if job and job["thread"].is_alive():
                body.append(("Claude is thinking  " + orca.orca_bar(None, 18), A["info"]))
            maxs = max(0, len(body) - room)
            scroll = maxs if scroll is None or scroll > maxs else scroll
            win.erase()
            win.border()
            put(win, 0, 2, " Ask Claude (%s) " % model, A["title"])
            for i, (t, at) in enumerate(body[scroll:scroll + room]):
                put(win, 1 + i, 2, t, at)
            put(win, h - 3, 2, "> " + buf[-(w - 6):], A["plain"])
            put(win, h - 2, 2, "(Enter) ask   (A on an empty line) full Claude Code   (PgUp/PgDn) scroll   (Esc) close", A["dim"])
            try:
                win.move(h - 3, min(w - 2, 4 + len(buf[-(w - 6):])))
            except curses.error:
                pass
            win.refresh()
            if job and not job["thread"].is_alive():
                ans, session, err = job.get("result") or ("", session, "no answer")
                convo.append(("e", err) if err else ("a", ans))
                job, scroll = None, None
                continue
            ch = stdscr.getch()
            if ch == -1:
                continue
            if ch == 27:
                return "closed the ask box" if convo else ""
            if ch == ord("A") and not buf and not job:
                return "big"
            if ch == curses.KEY_PPAGE:
                scroll = max(0, (scroll or 0) - (room - 2))
            elif ch == curses.KEY_NPAGE:
                scroll = (scroll or 0) + room - 2
            elif ch in (10, 13, curses.KEY_ENTER):
                q = buf.strip()
                if q and not job:
                    convo.append(("q", q))
                    buf, scroll = "", None
                    job = {}

                    def work(q=q, s=session, job=job):
                        job["result"] = quick_ask(P, q, s)
                    job["thread"] = threading.Thread(target=work, daemon=True)
                    job["thread"].start()
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                buf = buf[:-1]
            elif 32 <= ch < 0x110000 and ch != curses.KEY_RESIZE:
                try:
                    buf += chr(ch)
                except ValueError:
                    pass
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.timeout(1000)


def chat_with_claude(stdscr, P):
    """(a): hand the terminal to an interactive Claude Code session in the project, then come back to the dashboard."""
    cmd = claude_chat_cmd(P)
    if shutil.which(cmd[0]) is None:
        return "cannot find '%s' on your PATH (set claude_cmd in orca.json)" % cmd[0]
    if orca.running_pid(P):
        if not confirm(stdscr, "A build is running", [
                "Qwen is editing files and committing right now. If Claude changes files too, they can collide, and a failed task rolls back uncommitted edits.",
                "Safe use: ask questions, or change only orca/TASKS.md. For code changes press (s) first and talk after the task ends.",
                "y: open Claude anyway.   n: cancel."]):
            return "cancelled"
    curses.def_prog_mode()
    curses.endwin()
    try:
        os.system("clear")
        print("Claude %s in %s (type /exit or press Ctrl-D to come back to the dashboard)\n" % (chat_model(P), P.root.name))
        subprocess.call(cmd, cwd=str(P.root))
    finally:
        curses.reset_prog_mode()
        stdscr.clear()
        stdscr.refresh()
    try:
        with open(str(P.o / "events.log"), "a") as f:
            f.write(time.strftime("%H:%M:%S") + " you talked with Claude from the dashboard\n")
    except OSError:
        pass
    return "back from Claude"


def edit_file(stdscr, P, rel):
    """Hand the terminal to the user's editor, then tell Qwen (via PROGRESS.md) if the file changed."""
    try:
        path = orca.safe_path(P.root, rel, allow_notes=True)
    except ValueError as e:
        return "cannot edit: %s" % e
    ed = pick_editor()
    if not ed:
        return "no editor found, set $EDITOR"

    def digest():
        try:
            return hashlib.md5(path.read_bytes()).hexdigest()
        except OSError:
            return ""
    try:
        before_bytes = path.read_bytes()
    except OSError:
        before_bytes = None
    before = digest()
    curses.def_prog_mode()
    curses.endwin()
    try:
        subprocess.call(ed + [str(path)], cwd=str(P.root))
    finally:
        curses.reset_prog_mode()
        stdscr.clear()
        stdscr.refresh()
    if digest() == before:
        return "no changes (for VS Code use EDITOR=\"code -w\")"

    def event(text):
        try:
            with open(str(P.o / "events.log"), "a") as f:
                f.write(time.strftime("%H:%M:%S") + " " + text + "\n")
        except OSError:
            pass
    info = run_info(P)
    choice = ask_save_choice(stdscr, rel, info) if info else "k"
    if choice == "u" and before_bytes is not None:
        path.write_bytes(before_bytes)
        event("you undid your edit of " + rel)
        return "undone, %s is back to what it was" % rel
    sfx = " and committed it" if commit_edit(P, rel) else ""
    if choice == "p":
        prot = sorted(P.protected() | {rel})
        tmp = P.o / "protected.tmp"
        tmp.write_text(json.dumps(prot))
        os.replace(str(tmp), str(P.o / "protected.json"))
        P.add_progress("[manual edit by the user] %s is PROTECTED. Do not modify it this task." % rel)
        event("you edited %s and protected it from Qwen" % rel)
        return "saved%s and PROTECTED %s until this task ends" % (sfx, rel)
    P.add_progress("[manual edit by the user] %s was changed by hand. Keep these changes." % rel)
    event("you edited " + rel)
    if choice == "s":
        (P.o / "STOP").write_text("stop")
        return "saved%s %s. The run stops after this task" % (sfx, rel)
    return "saved%s %s. Qwen will be told next task" % (sfx, rel)


def commit_edit(P, rel):
    """Commit ONLY the user's edited file right away, so a rollback of a blocked task cannot erase it."""
    q = shlex.quote(rel)
    rc1, _ = P.git("add -- " + q)
    rc2, _ = P.git("commit -qm %s -- %s" % (shlex.quote("manual edit: " + rel), q))
    return rc1 == 0 and rc2 == 0


def run_info(P):
    """Describes the running build, or None when nothing is running."""
    try:
        pid = int(P.rd("run.pid").strip())
        os.kill(pid, 0)
    except ValueError:
        return None
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    try:
        st = json.loads(P.rd("state.json"))
    except ValueError:
        st = {}
    if str(st.get("phase", "")).startswith(("finished", "stopped", "killed")):
        return None
    return {"task": st.get("task", "?"), "phase": st.get("phase", "?")}


def ask_save_choice(stdscr, rel, info):
    """Modal question after an edit while a build is running. Returns k, p, u or s."""
    rows, cols = stdscr.getmaxyx()
    w = max(40, min(cols - 4, 78))
    body = ["You changed %s while a build is running." % rel, "",
            "Qwen is on %s (%s). It reads files fresh, but if it already read this one it can overwrite your change." % (info["task"], info["phase"]), ""]
    lines = []
    for b in body:
        lines += textwrap.wrap(b, w - 4) or [""]
    opts = ["(k) Keep it and tell Qwen            (Enter = k)",
            "(p) Keep it and PROTECT it (Qwen cannot write to it this task)",
            "(u) Undo my change",
            "(s) Keep it and stop after this task, so I can review"]
    h = len(lines) + len(opts) + 4
    win = curses.newwin(h, w, max(0, (rows - h) // 2), max(0, (cols - w) // 2))
    stdscr.timeout(-1)
    try:
        while True:
            win.erase()
            win.border()
            put(win, 0, 2, " Save while a build is running ", A["warn"] | curses.A_BOLD)
            for i, l in enumerate(lines):
                put(win, 1 + i, 2, l, A["bold"] if i == 0 else A["plain"])
            for i, o in enumerate(opts):
                put(win, 2 + len(lines) + i, 2, o, A["ok"] if i == 0 else A["plain"])
            win.refresh()
            ch = stdscr.getch()
            if ch in (10, 13, curses.KEY_ENTER, 27, ord("k"), ord("K")):
                return "k"
            if ch in (ord("p"), ord("P")):
                return "p"
            if ch in (ord("u"), ord("U")):
                return "u"
            if ch in (ord("s"), ord("S")):
                return "s"
    finally:
        stdscr.timeout(1000)


def pin_current(ui):
    if not ui["pin"] and ui.get("sel"):
        ui["pin"] = ui["sel"]
        ui["scroll"] = ui.get("shown_start", 0)


def move_sel(ui, delta):
    paths = ui["paths"]
    if not paths:
        return
    base = ui.get("sel")
    if base in paths:
        i = max(0, min(len(paths) - 1, paths.index(base) + delta))
    else:
        i = 0 if delta > 0 else len(paths) - 1
    ui["pin"], ui["scroll"] = paths[i], 0


def inside(rect, y, x):
    return rect[0] <= y < rect[0] + rect[2] and rect[1] <= x < rect[1] + rect[3]


def handle_key(stdscr, P, ui, ch):
    """Returns (quit, note)."""
    page = max(3, ui.get("file_h", 10) - 2)
    if ch in (ord("q"), ord("Q")):
        return True, ""
    if ch == ord("a"):
        r = ask_box(stdscr, P)                      # the small box; capital A inside it opens the full session
        return False, chat_with_claude(stdscr, P) if r == "big" else r
    if ch == ord("A"):
        return False, chat_with_claude(stdscr, P)
    if orca_design.stage(P) == "design":
        msg = design_key(stdscr, P, ch)
        if msg is not None:
            return False, msg
    if ch in (ord("o"), ord("O")):
        key = settings_dialog(stdscr, P)
        return handle_key(stdscr, P, ui, ord(key)) if key else (False, "")
    if ch == ord("n"):
        return False, add_task(stdscr, P)
    if ch in (ord("s"), ord("S")):
        P.o.mkdir(exist_ok=True)
        (P.o / "STOP").write_text("stop")
        return False, "STOP requested, halts after this task"
    if ch in (ord("t"), ord("T")):
        ui["screen"] = "main" if ui.get("screen") == "stats" else "stats"
        ui["stats_at"], ui["stats_scroll"] = 0, 0
        return False, ""
    if ch in (ord("r"), ord("R")):
        pid = orca.running_pid(P)
        if pid:
            return False, "a build is already running (pid %d)" % pid
        if (P.o / "STOP").exists():
            (P.o / "STOP").unlink()
        if not any(x["mark"] == " " for x in P.tasks()):
            return False, "no open [ ] tasks in TASKS.md. Add one with (e), then (r)"
        start_build(P)
        ui["screen"] = "main"
        return False, "build started"
    if ch == ord("L"):
        return False, pick_model_dialog(stdscr, P)
    if ch == ord("C"):
        return False, context_dialog(stdscr, P)
    if ch == ord("D"):
        return False, folders_dialog(stdscr, P)
    if ch == ord("P"):
        return False, toggle_preview(stdscr, P)
    if ch == ord("K"):
        msg = kill_build(stdscr, P)
        if msg.startswith("build (pid"):
            EXIT["msg"] = msg      # the build is dead, so the dashboard has nothing left to show: close it too
            return True, msg
        return False, msg
    if ui.get("screen") == "stats":
        pg = max(3, stdscr.getmaxyx()[0] - 4)
        if ch in (curses.KEY_UP, ord("k")):
            ui["stats_scroll"] = max(0, ui.get("stats_scroll", 0) - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            ui["stats_scroll"] = ui.get("stats_scroll", 0) + 1
        elif ch == curses.KEY_PPAGE:
            ui["stats_scroll"] = max(0, ui.get("stats_scroll", 0) - pg)
        elif ch == curses.KEY_NPAGE:
            ui["stats_scroll"] = ui.get("stats_scroll", 0) + pg
        elif ch in (curses.KEY_HOME, ord("g")):
            ui["stats_scroll"] = 0
        elif ch in (curses.KEY_END, ord("G")):
            ui["stats_scroll"] = 10 ** 9
        elif ch in (27, 10, 13, curses.KEY_ENTER):
            ui["screen"] = "main"
        elif ch == curses.KEY_MOUSE:
            try:
                _, _, _, _, b = curses.getmouse()
            except curses.error:
                return False, ""
            ui["stats_scroll"] = max(0, ui.get("stats_scroll", 0) + 3 * mouse_wheel(b))
        return False, ""
    if ch in (curses.KEY_UP, ord("k")):
        move_sel(ui, -1)
    elif ch in (curses.KEY_DOWN, ord("j")):
        move_sel(ui, 1)
    elif ch in (10, 13, curses.KEY_ENTER):
        pin_current(ui)
        ui["scroll"] = 0
    elif ch == curses.KEY_NPAGE:
        pin_current(ui)
        ui["scroll"] += page
    elif ch == curses.KEY_PPAGE:
        pin_current(ui)
        ui["scroll"] = max(0, ui["scroll"] - page)
    elif ch in (curses.KEY_HOME, ord("g")):
        pin_current(ui)
        ui["scroll"] = 0
    elif ch in (curses.KEY_END, ord("G")):
        pin_current(ui)
        ui["scroll"] = 10 ** 9
    elif ch in (ord("f"), ord("F")):
        ui["pin"] = None
        return False, "following Qwen"
    elif ch in (ord("m"), ord("M")):
        ui["debug_mouse"] = not ui.get("debug_mouse")
        return False, "mouse debug " + ("ON: scroll or click and read the code here" if ui["debug_mouse"] else "off")
    elif ch in (ord("e"), ord("E")):
        if not ui.get("sel"):
            return False, "pick a file first"
        return False, edit_file(stdscr, P, ui["sel"])
    elif ch == curses.KEY_MOUSE:
        try:
            _, mx, my, _, b = curses.getmouse()
        except curses.error:
            return False, ""
        wheel_up = b & getattr(curses, "BUTTON4_PRESSED", 0x10000)
        wheel_down = b & getattr(curses, "BUTTON5_PRESSED", 0x200000)
        left = curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED
        # ncurses 5 (the curses built into macOS) cannot encode "button 5", so wheel-down shows up as the
        # position-report bit. We never ask for position reports, so that bit can only be a wheel-down.
        if not (wheel_up or wheel_down) and (b & 0x8000000) and not (b & left):
            wheel_down = 1
        if ui.get("debug_mouse"):
            ui["dbg"] = "mouse bstate=0x%x at row %d col %d" % (b, my, mx)
        files, viewer = ui["layout"].get("files"), ui["layout"].get("file")
        if wheel_up or wheel_down:
            d = -1 if wheel_up else 1
            if viewer and inside(viewer, my, mx):
                pin_current(ui)
                ui["scroll"] = max(0, ui["scroll"] + 3 * d)
            elif files and inside(files, my, mx):
                move_sel(ui, d)
        elif b & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED):
            if files and inside(files, my, mx):
                row = my - files[0] - 1
                if 0 <= row < len(files[4]) and files[4][row]:
                    ui["pin"], ui["scroll"] = files[4][row], 0
                    ui["sel"] = ui["pin"]
                    if b & curses.BUTTON1_DOUBLE_CLICKED:
                        return False, edit_file(stdscr, P, ui["pin"])
    return False, ui.pop("dbg", "")


def loop(stdscr, P):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    init_colors()
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(150)
    except curses.error:
        pass
    stdscr.timeout(1000)
    if P.cfg["quota_check"]:
        threading.Thread(target=poll_quota, args=(P,), daemon=True).start()
    ui = {"pin": None, "scroll": 0, "layout": {}, "paths": [], "sel": "", "shown_start": 0, "file_h": 10}
    st, ram, ram_at, note, note_at = {}, (None, None), 0, "", 0
    while True:
        if not P.o.exists() and (P.root / orca.ORCA_DIR).is_dir():
            P.o = P.root / orca.ORCA_DIR       # the build just moved .orca to orca/
        try:
            st = json.loads(P.rd("state.json")) or st
        except ValueError:
            pass
        phase_now = str(st.get("phase", ""))
        if ui.get("last_phase") and not str(ui["last_phase"]).startswith(("finished", "killed", "stopped")) and phase_now.startswith("finished"):
            ui["screen"], ui["stats_at"], ui["stats_scroll"] = "stats", 0, 0
            note, note_at = "The build is finished. Statistics shown. (t) back, (r) run again after adding tasks.", time.time()
        ui["last_phase"] = phase_now
        if time.time() - ram_at > 15:
            ram, ram_at = (orca.free_pct(), orca.swap_used_mb()), time.time()
        if note and time.time() - note_at > 8:
            note = ""
        draw(stdscr, P, st, ram, note, ui)
        try:
            att = attention(P, st)
        except Exception:                          # a popup must never break the dashboard
            att = None
        if att and att[0] not in popups_seen(P):
            mark_popup_seen(P, att[0])
            key = action_dialog(stdscr, att[1], att[2], att[3])
            if key == "P":                         # straight to the browser, no second question
                url = orca.start_preview_bg(P, open_browser=True)
                note, note_at = ("opened " + url if url else 'set "preview_url" in orca.json first'), time.time()
            elif key:
                quit_, n = handle_key(stdscr, P, ui, ord(key))
                if quit_:
                    return
                if n:
                    note, note_at = n, time.time()
            continue
        busy = orca.running_pid(P) and ui.get("screen") != "stats"
        stdscr.timeout((400 if orca_design.stage(P) == "design" else 600) if busy else 1000)   # the orca needs a few frames a second
        ch = stdscr.getch()
        if ch == -1 or ch == curses.KEY_RESIZE:
            continue
        quit_, n = handle_key(stdscr, P, ui, ch)
        if quit_:
            return
        if n:
            note, note_at = n, time.time()


def start_build(P):
    """The build runs as its own background process, so closing the dashboard does not stop it."""
    P.o.mkdir(exist_ok=True)
    out = open(str(P.o / "run.out"), "a")
    return subprocess.Popen([sys.executable, str(Path(orca.__file__).resolve()), "run", str(P.root)],
                            stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)


def run(P, start_run=False):
    proc = None
    if start_run and orca.running_pid(P):
        print(orca.already_running_help(P, orca.running_pid(P)))
        print("Not starting a second build. Opening the dashboard on the running one.")
        time.sleep(3)
        start_run = False
    if start_run and orca_design.stage(P) == "design":
        start_run = False                      # Qwen waits for the hand-off. Build the prototype if there is none yet.
        if not orca_design.proto(P).get("revision") and not orca.running_pid(P):
            orca_design.start_bg(P, "design")
    if start_run:
        proc = start_build(P)
    try:
        curses.wrapper(loop, P)
    except KeyboardInterrupt:
        pass
    if EXIT.get("msg"):
        print(EXIT["msg"])
        return
    if proc:
        alive = proc.poll() is None
        print("Build %s (pid %d). Output: %s" % ("is still running in the background" if alive else "has finished",
                                                  proc.pid, P.o / "run.out"))
        if alive:
            print("Reopen the dashboard with: python3 orca.py tui %s" % P.root)
