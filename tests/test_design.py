"""Design stage: Claude builds the prototype, the owner leaves notes, Claude revises, then hands off to Qwen.
A fake `claude` on PATH speaks the same stream-json as the real one and writes real files."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orca  # noqa: E402
import orca_design  # noqa: E402

FAKE = r'''#!%(py)s
import json, os, sys, pathlib
prompt = sys.argv[-1]
cwd = pathlib.Path.cwd()
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"args": sys.argv[1:-1], "prompt": prompt[:300]}) + "\n")
def emit(o):
    print(json.dumps(o), flush=True)
def write(rel, text):
    (cwd / rel).write_text(text)
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": str(cwd / rel), "content": text}}]}})
emit({"type": "system", "subtype": "init", "session_id": "S1"})
TEST = "import unittest, app\nclass T(unittest.TestCase):\n    def test_greet(self): self.assertEqual(app.greet(), %%r)\n"
if "RESEARCH, then the SPEC" in prompt:
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebSearch", "input": {"query": "phonics apps"}}]}})
    res = ("=====RESEARCH.md=====\n- kids like big buttons (https://example.com)\n=====SPEC.md=====\n# Spec\n## Stack\npython stdlib\n"
           "=====CONFIG=====\n" + json.dumps({"test_cmd": os.environ["FAKE_TESTCMD"], "setup_cmd": "", "preview_cmd": "x"}))
elif "building the PROTOTYPE" in prompt:
    write("app.py", "def greet():\n    return 'hi'  # TODO(qwen): real greeting\n")
    write("test_app.py", TEST %% ("WRONG" if os.environ.get("FAKE_BREAK") else "hi"))
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "The page shows a greeting."}]}})
    res = "You will see a greeting. Sample data only."
elif "The checks failed" in prompt:
    write("test_app.py", TEST %% "hi")
    res = "fixed the test"
elif "left these notes" in prompt:
    write("app.py", "def greet():\n    return 'hi'  # bigger now. TODO(qwen): real greeting\n")
    res = "N01: done\nN02: done"
elif "Plan the REST" in prompt:
    res = ("thinking out loud first\n=====TASKS.md=====\n- [ ] T00: the prototype again | done when: a test shows nothing\n"
           "- [ ] T01: [N03] blue header in ui/header.py | done when: a test shows the header color\n"
           "- [ ] T02: real greeting in app.py | done when: a test shows greet uses the name\n"
           "- [ ] T02: duplicate | done when: a test shows x\n"
           "- [ ] T03: score counter in ui/score.py | done when: a test shows the score goes up\n")
else:
    res = "?"
emit({"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 7, "output_tokens": 3}, "content": [{"type": "text", "text": "working"}]}})
if os.environ.get("FAKE_DIE") and "PROTOTYPE" in prompt:
    sys.exit(1)                                   # killed mid-run: no result event
emit({"type": "result", "result": res, "session_id": "S1", "is_error": False, "num_turns": 4,
      "usage": {"input_tokens": 100, "output_tokens": 20},
      "modelUsage": {"claude-opus-x": {"inputTokens": 1000, "outputTokens": 200, "cacheReadInputTokens": 50000,
                                       "cacheCreationInputTokens": 3000, "costUSD": 0.5},
                     "claude-haiku-x": {"inputTokens": 10, "outputTokens": 5, "cacheReadInputTokens": 0,
                                        "cacheCreationInputTokens": 0, "costUSD": 0.01}}})
'''


class DesignProject:
    def __init__(self, broken=False):
        self.tmp = Path(tempfile.mkdtemp(prefix="orca-design-"))
        self.dir = self.tmp / "app"
        (self.dir / "orca").mkdir(parents=True)
        (self.dir / "orca" / "BRIEF.md").write_text("A phonics app for kids.\n")
        (self.dir / "orca.json").write_text(json.dumps({"quota_check": False, "venv": False, "smoke_cmd": ""}))
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        claude = bin_dir / "claude"
        claude.write_text(FAKE % {"py": sys.executable})
        claude.chmod(0o755)
        self.log = self.tmp / "claude.log"
        self.env = dict(os.environ, HOME=str(self.tmp), PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                        FAKE_LOG=str(self.log), FAKE_TESTCMD="%s -m unittest discover -q" % sys.executable,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        if broken:
            self.env["FAKE_BREAK"] = "1"
        subprocess.run(["git", "init", "-q"], cwd=self.dir, check=True)

    def orca(self, *args):
        r = subprocess.run([sys.executable, str(ROOT / "orca.py")] + list(args), env=self.env,
                           capture_output=True, text=True, timeout=180)
        return r.returncode, r.stdout + r.stderr

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def P(self):
        return orca.Project(self.dir)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestDesignStage(unittest.TestCase):
    def test_prototype_notes_revision_and_handoff(self):
        d = DesignProject()
        try:
            rc, out = d.orca("design", str(d.dir))
            self.assertEqual(rc, 0, out)
            P = d.P()
            pr = orca_design.proto(P)
            self.assertEqual((pr["revision"], pr["ok"]), (1, True), out)
            self.assertTrue((d.dir / "app.py").exists())
            self.assertEqual(orca_design.stage(P), "design")
            self.assertIn("kids like big buttons", P.rd("RESEARCH.md"))
            conf = json.loads((d.dir / "orca.json").read_text())
            self.assertEqual(conf["smoke_from"], "T00")
            self.assertIn("unittest", conf["test_cmd"])
            self.assertIn("claude: write app.py", P.rd("events.log"))       # the dashboard sees what Claude does
            self.assertIn("search: phonics apps", P.rd("events.log"))
            self.assertEqual(json.loads(P.rd("usage.json"))["claude"]["prototype"]["calls"], 1)
            # Claude runs IN the project with file tools, streaming
            proto_call = [c for c in d.calls() if "PROTOTYPE" in c["prompt"]][0]
            self.assertIn("stream-json", proto_call["args"])
            self.assertIn("Write", ",".join(proto_call["args"]))
            self.assertNotIn("--add-dir", proto_call["args"])           # only when orca.json lists extra_dirs

            rc, out = d.orca("run", str(d.dir))                               # Qwen waits for the hand-off
            self.assertNotEqual(rc, 0)
            self.assertIn("DESIGN stage", out)

            rc, out = d.orca("note", str(d.dir), "1. bigger button 2. friendlier font")
            self.assertIn("N01, N02", out)
            rc, out = d.orca("design", str(d.dir))
            self.assertEqual(rc, 0, out)
            P = d.P()
            self.assertEqual(orca_design.proto(P)["revision"], 2)
            self.assertEqual([n["mark"] for n in orca_design.notes(P)], ["x", "x"])
            self.assertIn("bigger now", (d.dir / "app.py").read_text())
            revise = [c for c in d.calls() if "left these notes" in c["prompt"]]
            self.assertEqual(len(revise), 1)

            d.orca("note", str(d.dir), "blue header")                          # left open: becomes Qwen's first task
            rc, out = d.orca("handoff", str(d.dir))
            self.assertEqual(rc, 0, out)
            P = d.P()
            tasks = P.tasks()
            self.assertEqual([(t["id"], t["mark"]) for t in tasks], [("T00", "x"), ("T01", " "), ("T02", " "), ("T03", " ")])
            self.assertIn("[N03]", tasks[1]["text"])
            self.assertEqual(orca_design.notes(P)[-1]["mark"], ">")
            self.assertEqual(orca_design.stage(P), "build")
            self.assertTrue(orca.smoke_active(P, "T01") or not orca.smoke_command(P))   # real-app check on from Qwen's first task
            rc, log = subprocess.run(["git", "log", "--oneline"], cwd=d.dir, capture_output=True, text=True).returncode, \
                subprocess.run(["git", "log", "--oneline"], cwd=d.dir, capture_output=True, text=True).stdout
            self.assertIn("hand-off to Qwen", log)
            self.assertIn("prototype revision 2", log)
            breakdown = [c for c in d.calls() if "Plan the REST" in c["prompt"]][0]
            self.assertNotIn("Write", ",".join(breakdown["args"]))             # planning cannot edit code
        finally:
            d.close()

    def test_failed_checks_go_back_to_the_same_claude_session(self):
        d = DesignProject(broken=True)
        try:
            rc, out = d.orca("design", str(d.dir))
            self.assertEqual(rc, 0, out)
            fix = [c for c in d.calls() if "The checks failed" in c["prompt"]]
            self.assertEqual(len(fix), 1, out)
            self.assertIn("--resume", fix[0]["args"])
            self.assertIn("S1", fix[0]["args"])
            self.assertTrue(orca_design.proto(d.P())["ok"])
        finally:
            d.close()

    def test_handoff_waits_for_green(self):
        d = DesignProject()
        try:
            d.orca("design", str(d.dir))
            (d.dir / "test_app.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_x(self): self.fail('red')\n")
            rc, out = d.orca("handoff", str(d.dir))
            self.assertNotEqual(rc, 0)
            self.assertIn("HAND-OFF STOPPED", out)
            P = d.P()
            self.assertEqual(orca_design.stage(P), "design")
            self.assertFalse(P.tasks())
            self.assertFalse([c for c in d.calls() if "Plan the REST" in c["prompt"]])   # no Claude tokens spent on a red base
        finally:
            d.close()

    def test_exact_claude_tokens_are_logged_per_call_and_in_total(self):
        d = DesignProject()
        try:
            rc, out = d.orca("design", str(d.dir))
            self.assertEqual(rc, 0, out)
            P = d.P()
            u = json.loads(P.rd("usage.json"))["claude"]["prototype"]
            # modelUsage wins (all models, incl. the side model), not result.usage
            self.assertEqual((u["input"], u["output"], u["cache_read"], u["cache_create"]), (1010, 205, 50000, 3000))
            rows = [json.loads(l) for l in P.rd("design_usage.jsonl").splitlines()]
            self.assertEqual([r["purpose"] for r in rows], ["research", "prototype"])
            self.assertEqual(rows[1]["counted"], 1010 + 205 + 3000)
            self.assertEqual(sorted(rows[1]["models"]), ["claude-haiku-x", "claude-opus-x"])
            ev = P.rd("events.log")
            self.assertIn("CLAUDE TOKENS prototype: 1,010 in, 205 out, 3,000 cache write, 50,000 cache read (4,215 counted)", ev)
            self.assertIn("Claude design so far: 8,430 tokens counted", ev)
            t = orca_design.design_totals(P)
            self.assertEqual((t["calls"], t["counted"]), (2, 8430))
        finally:
            d.close()

    def test_a_killed_claude_run_still_counts_its_tokens(self):
        d = DesignProject()
        d.env["FAKE_DIE"] = "1"
        try:
            d.orca("design", str(d.dir))
            u = json.loads(d.P().rd("usage.json"))["claude"]
            self.assertEqual((u["prototype-unfinished"]["input"], u["prototype-unfinished"]["output"]), (7, 3))
        finally:
            d.close()

    def test_old_projects_stay_in_the_build_stage(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "orca").mkdir()
            (d / "orca" / "TASKS.md").write_text("- [ ] T01: x | done when: y\n")
            self.assertEqual(orca_design.stage(orca.Project(d)), "build")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestDesignPieces(unittest.TestCase):
    def test_numbered_notes_split(self):
        self.assertEqual(orca_design.split_note("1. bigger button 2) blue header"), ["bigger button", "blue header"])
        self.assertEqual(orca_design.split_note("make it blue"), ["make it blue"])

    def test_try_lines_become_the_next_steps(self):
        text = "You will see the map.\nTRY: walk the agent with Resume\n- **TRY:** open the God panel\nPlaceholder: weather."
        summary, tries = orca_design.split_try(text)
        self.assertEqual(tries, ["walk the agent with Resume", "open the God panel"])
        self.assertEqual(summary, "You will see the map.\nPlaceholder: weather.")

    def test_claude_looks_at_the_page_and_knows_the_earlier_notes(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "orca").mkdir()
            (d / "orca.json").write_text(json.dumps({"preview_url": "http://localhost:8080", "preview_cmd": "node s.js", "smoke_from": "T00"}))
            P = orca.Project(d)
            look = orca_design.look_note(P)
            self.assertIn("ORCA_SHOTS=%s" % (P.o / "shots"), look)
            self.assertIn("BEFORE you change anything", look)
            prompt = orca_design.REVISE_PROMPT.format(test_cmd="t", notes="N03: NO, IT STILL DOESNT WORK!", smoke_note="s",
                                                     look=look, history="N02: the thinking box should be a fixed size")
            self.assertIn("EARLIER NOTES", prompt)
            self.assertIn("N02: the thinking box", prompt)
            self.assertIn("ORCA_SHOTS", orca_design.PROTOTYPE_PROMPT.format(test_cmd="t", preview_cmd="p", smoke_note="s", look=look))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_screenshots_over_time_and_widths(self):
        import orca_smoke

        class Page:
            def __init__(self):
                self.sizes = []

            def wait_for_timeout(self, ms):
                pass

            def screenshot(self, path=None):
                Path(path).write_bytes(b"png")

            def set_viewport_size(self, v):
                self.sizes.append(v["width"])
        d = Path(tempfile.mkdtemp())
        try:
            (d / "stale.png").write_bytes(b"old")
            pg = Page()
            orca_smoke.take_shots(pg, d, b"first")
            names = sorted(p.name for p in d.glob("*.png"))
            self.assertEqual(names, ["1-at-2s-1280.png", "2-at-6s-1280.png", "3-at-12s-1280.png", "4-wide-1680.png", "5-small-1024.png"])
            self.assertEqual(pg.sizes, [1680, 1024])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_parse_tasks_drops_t00_duplicates_and_chatter(self):
        out = ("intro\n=====TASKS.md=====\n- [ ] T00: x | done when: y\n- [ ] T01: a | done when: a test shows a\n"
               "- [ ] T01: again | done when: z\nnot a task\n- [ ] T02: b | done when: a test shows b\n")
        self.assertEqual([l.split(":")[0] for l in orca_design.parse_tasks(out)], ["- [ ] T01", "- [ ] T02"])

    def test_tool_descriptions_are_relative(self):
        desc, path = orca_design.describe_tool("Edit", {"file_path": "/x/app/ui/home.js"}, Path("/x/app"))
        self.assertEqual((desc, path), ("edit ui/home.js", "ui/home.js"))
        self.assertTrue(orca_design.describe_tool("Bash", {"command": "npm test"}, Path("/x"))[0].startswith("run npm test"))

    def test_preview_feedback_becomes_a_design_note(self):
        d = DesignProject()
        try:
            P = d.P()
            orca_design.set_stage(P, "design")
            os.environ.update({k: d.env[k] for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")})
            import orca_preview
            r = orca_preview.Preview(P).feedback("the letters are too small")
            self.assertTrue(r["ok"])
            self.assertEqual(r["id"], "N01")
            self.assertIn("(b) Claude applies", r["msg"])
            self.assertFalse(P.rd("TASKS.md").strip())                        # no Qwen task during design
            self.assertEqual(orca_design.open_notes(P)[0]["text"], "the letters are too small")
        finally:
            d.close()

    def test_dashboard_panel_lists_open_notes_first(self):
        import orca_tui
        orca_tui.A.update({k: 0 for k in ("ok", "bad", "warn", "dim", "cur", "bold", "info", "ai", "title", "plain")})
        d = Path(tempfile.mkdtemp())
        try:
            (d / "orca").mkdir()
            (d / "orca" / "DESIGN_NOTES.md").write_text("- [x] N01: done one\n- [ ] N02: open one\n")
            (d / "orca" / "PROTOTYPE.json").write_text(json.dumps({"revision": 2, "ok": True}))
            lines = [t for t, _ in orca_tui.design_lines(orca.Project(d), 20, 60)]
            self.assertIn("Prototype v2: GREEN (tests + app)", lines[0])
            self.assertIn("NEXT, in your browser (P):", lines)
            self.assertLess(lines.index("[ ] N02 open one"), lines.index("[x] N01 done one"))
            self.assertEqual(len(lines), 20)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestDesignFreesMemory(unittest.TestCase):
    def _P(self, cfg):
        d = Path(tempfile.mkdtemp())
        (d / "orca").mkdir()
        (d / "orca.json").write_text(json.dumps(cfg))
        return d, orca.Project(d)

    def test_qwen_is_unloaded_when_no_build_uses_it(self):
        d, P = self._P({})
        calls = []
        real = (orca.sh, orca.all_models, orca_design.shutil.which)
        try:
            orca.sh = lambda cmd, **k: (calls.append(cmd), (0, "python3 orca.py design x\n"))[1]
            orca.all_models = lambda cfg: [{"id": "qwen/qwen3.6-35b-a3b", "state": "loaded"}, {"id": "other", "state": "not-loaded"}]
            orca_design.shutil.which = lambda n: "/usr/local/bin/lms"
            self.assertEqual(orca_design.free_local_model(P), "unloaded qwen/qwen3.6-35b-a3b")
            self.assertIn("lms unload --all", calls)
        finally:
            orca.sh, orca.all_models, orca_design.shutil.which = real
            shutil.rmtree(d, ignore_errors=True)

    def test_a_running_build_keeps_its_model(self):
        d, P = self._P({})
        calls = []
        real = orca.sh
        try:
            orca.sh = lambda cmd, **k: (calls.append(cmd), (0, "Python /x/orca.py run /x/ReadAlong\n"))[1]
            self.assertEqual(orca_design.free_local_model(P), "kept (another build uses it)")
            self.assertNotIn("lms unload --all", calls)
        finally:
            orca.sh = real
            shutil.rmtree(d, ignore_errors=True)

    def test_it_can_be_turned_off(self):
        d, P = self._P({"design_unload_model": False})
        try:
            self.assertTrue(orca_design.free_local_model(P).startswith("kept"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestYourTurnPopups(unittest.TestCase):
    def _P(self, files):
        d = Path(tempfile.mkdtemp())
        (d / "orca").mkdir()
        for k, v in files.items():
            (d / "orca" / k).write_text(v)
        return d, orca.Project(d)

    def test_ready_prototype_tells_you_what_to_do_once(self):
        import orca_tui
        d, P = self._P({"STAGE": "design\n", "PROTOTYPE.json": json.dumps({"revision": 1, "ok": True, "at": 5, "try": ["press Resume"]}),
                        "DESIGN_NOTES.md": "- [ ] N01: bigger map\n"})
        try:
            pid, title, lines, keys = orca_tui.attention(P, {})
            self.assertEqual(title, "Prototype v1 is ready")
            self.assertIn("1/ press Resume", lines)
            self.assertEqual([k for k, _ in keys], ["P", "n", "b", "h"])
            self.assertNotIn(pid, orca_tui.popups_seen(P))
            orca_tui.mark_popup_seen(P, pid)
            self.assertIn(pid, orca_tui.popups_seen(P))              # shown once, also after a dashboard restart
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_failed_prototype_and_halted_build(self):
        import orca_tui
        d, P = self._P({"STAGE": "design\n", "PROTOTYPE.json": json.dumps({"revision": 2, "ok": False, "fail": "TESTS FAILED\nboom"})})
        try:
            pid, title, lines, keys = orca_tui.attention(P, {})
            self.assertIn("not green", title)
            self.assertIn("boom", lines)
            self.assertEqual(keys[0][0], "b")
        finally:
            shutil.rmtree(d, ignore_errors=True)
        d, P = self._P({"TASKS.md": "- [x] T01: a | done when: x\n- [!] T02: b | done when: x\n- [ ] T03: c | done when: x\n"})
        try:
            st = {"phase": "finished or halted", "halt_reason": "2 tasks blocked in a row", "halted_at": 9}
            pid, title, lines, keys = orca_tui.attention(P, st)
            self.assertIn("2 tasks blocked in a row", title)
            self.assertIn("T02", lines[0])
            self.assertIsNone(orca_tui.attention(P, {"phase": "finished or halted", "halt_reason": ""}))   # a STOP is not a problem
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestAddTaskKey(unittest.TestCase):
    def test_n_adds_qwen_tasks_at_the_front_in_the_build_stage(self):
        import orca_tui
        d = Path(tempfile.mkdtemp())
        try:
            (d / "orca").mkdir()
            (d / "orca" / "TASKS.md").write_text("- [x] T01: a | done when: x\n- [ ] T02: b | done when: y\n")
            env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            os.environ.update(env)
            subprocess.run(["git", "init", "-q"], cwd=d, check=True)
            P = orca.Project(d)
            real = orca_tui.ask_line
            orca_tui.ask_line = lambda *a: "1. bigger map 2. slower typing"
            try:
                msg = orca_tui.handle_key(None, P, {}, ord("n"))[1]
            finally:
                orca_tui.ask_line = real
            self.assertIn("added F01 (+1 more)", msg)
            self.assertEqual([t["id"] for t in P.tasks()], ["T01", "F01", "F02", "T02"])
            self.assertIn("bigger map", P.tasks()[1]["text"])
            log = subprocess.run(["git", "log", "--oneline"], cwd=d, capture_output=True, text=True).stdout
            self.assertIn("feedback: F02", log)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestBinaryFilesInTheViewer(unittest.TestCase):
    def test_a_database_file_does_not_crash_the_dashboard(self):
        import orca_tui
        orca_tui.A.update({k: 0 for k in ("dim", "plain")})
        d = Path(tempfile.mkdtemp())
        try:
            (d / "orca").mkdir()
            (d / "cache.sqlite").write_bytes(b"SQLite format 3\x00\x10\x00" + b"\x00" * 100)
            lines, n, start = orca_tui.read_view(orca.Project(d), "cache.sqlite", "read_file", 10, None)
            self.assertIn("binary file", lines[0][0])

            class Win:
                def getmaxyx(self):
                    return (5, 40)

                def addnstr(self, y, x, text, n, attr):
                    if "\x00" in text:
                        raise ValueError("embedded null character")
                    self.text = text
            w = Win()
            orca_tui.put(w, 1, 1, "a\x00b")
            self.assertEqual(w.text, "ab")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestAskBox(unittest.TestCase):
    def test_quick_ask_is_read_only_small_and_keeps_the_session(self):
        import orca_tui
        d = Path(tempfile.mkdtemp())
        try:
            (d / "app" / "orca").mkdir(parents=True)
            (d / "app" / "orca.json").write_text(json.dumps({"test_cmd": "true", "claude_model": "sonnet"}))
            (d / "bin").mkdir()
            fake = d / "bin" / "claude"
            fake.write_text("#!%s\nimport json,sys\nopen(%r,'a').write(json.dumps(sys.argv[1:])+'\\n')\n"
                            "print(json.dumps({'result':'answer to '+sys.argv[-1],'session_id':'S1','usage':{'input_tokens':3}}))\n"
                            % (sys.executable, str(d / "calls.log")))
            fake.chmod(0o755)
            P = orca.Project(d / "app")
            P.cfg["claude_cmd"] = [str(fake), "-p"]
            ans, sid, err = orca_tui.quick_ask(P, "what is left?")
            self.assertEqual((ans, sid, err), ("answer to what is left?", "S1", ""))
            ans, sid, err = orca_tui.quick_ask(P, "and then?", sid)
            calls = [json.loads(l) for l in (d / "calls.log").read_text().splitlines()]
            self.assertEqual(calls[0][calls[0].index("--allowedTools") + 1], "Read,Glob,Grep")    # it can look, never edit
            self.assertEqual(calls[0][calls[0].index("--model") + 1], "sonnet")
            self.assertNotIn("--resume", calls[0])
            self.assertEqual(calls[1][calls[1].index("--resume") + 1], "S1")                    # a follow-up keeps the context
            self.assertEqual(json.loads(P.rd("usage.json"))["claude"]["ask"]["calls"], 2)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestPreviewReload(unittest.TestCase):
    def test_a_notes_only_commit_does_not_reload_the_page(self):
        import orca_preview
        d = Path(tempfile.mkdtemp())
        try:
            env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            os.environ.update(env)
            (d / "orca").mkdir()
            (d / "app.py").write_text("x = 1\n")
            (d / "orca" / "TASKS.md").write_text("- [ ] T01: a | done when: b\n")
            for c in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
                subprocess.run(["git"] + c, cwd=d, check=True)
            P = orca.Project(d)
            pv = orca_preview.Preview.__new__(orca_preview.Preview)
            pv.P = P
            h1 = pv.git_head()
            orca_preview.add_feedback_task(P, "make the button orange")      # commits orca/TASKS.md only
            self.assertEqual(pv.git_head(), h1)
            (d / "app.py").write_text("x = 2\n")
            subprocess.run(["git", "commit", "-qam", "code"], cwd=d, check=True)
            self.assertNotEqual(pv.git_head(), h1)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestOrcaProgress(unittest.TestCase):
    def test_the_orca_swims_with_the_progress(self):
        for f in (0.0, 0.3, 0.7, 0.95):
            b = orca.orca_bar(f, 30, t=0)
            self.assertEqual(len(b), 30, b)
            self.assertIn("/\\", b)
        self.assertLess(orca.orca_bar(0.2, 30, t=0).index("/"), orca.orca_bar(0.8, 30, t=0).index("/"))
        self.assertIn("SPLOOSH", orca.orca_bar(1.0, 30, t=0))
        self.assertNotEqual(orca.orca_bar(None, 20, t=0), orca.orca_bar(None, 20, t=3))    # busy: it moves


class TestShKillsChildren(unittest.TestCase):
    def test_timeout_ends_grandchildren(self):
        d = Path(tempfile.mkdtemp())
        try:
            pidf = d / "child.pid"
            rc, out = orca.sh("sleep 30 & echo $! > %s; wait" % pidf, timeout=1)
            self.assertEqual(rc, 124)
            pid = int(pidf.read_text())
            import time
            time.sleep(0.3)
            alive = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
            self.assertTrue(not alive or alive.startswith("Z"), "the test's child survived the timeout")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
