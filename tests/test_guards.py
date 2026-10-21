"""Regression tests for LocalOrca's safety rules. Python stdlib only, no LM Studio, no Claude account needed.

    python3 -m unittest discover -s tests -v

Every rule here exists because it went wrong in a real build. If you change orca.py and one of these fails,
you are about to reintroduce that bug. Add a test here for every new bug you fix.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orca  # noqa: E402


# ---------------------------------------------------------------- fakes
class FakeLM:
    """OpenAI-compatible chat endpoint. `brain(task_id, messages)` returns the list of tool calls for the next step."""

    def __init__(self, brain):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path.startswith("/v1/models"):
                    return self._send(200, {"data": [{"id": "fake-model"}]})
                return self._send(404, {"error": "no native api"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                msgs = body["messages"]
                user = msgs[1]["content"] if len(msgs) > 1 else msgs[0]["content"]
                if user.startswith("Compact these project lessons"):
                    return self._send(200, {"choices": [{"message": {"role": "assistant", "content": fake.compact}}],
                                            "usage": {"completion_tokens": 50}})
                if "# CURRENT TASK" not in user:          # the thinking probe
                    return self._send(200, {"choices": [{"message": {"role": "assistant", "content": "OK"}}],
                                            "usage": {"completion_tokens": 1}})
                tid = user.split("# CURRENT TASK\n", 1)[1].split(":", 1)[0].strip()
                calls = fake.brain(tid, msgs)
                tc = [{"id": "c%d_%d" % (len(msgs), i), "type": "function",
                       "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
                self._send(200, {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": tc}}],
                                 "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

        self.brain = brain
        self.compact = "- a\n- b\n- c"
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/v1" % self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def nudged(msgs):
    return any(m["role"] == "user" and "without writing" in (m.get("content") or "") for m in msgs[2:])


class Project:
    """A throwaway git project with a passing test suite and a fake `claude` on PATH."""

    def __init__(self, tasks, cfg=None, claude_reply="OK", escalate_reply=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="orca-test-"))
        self.dir = self.tmp / "app"
        (self.dir / "orca").mkdir(parents=True)
        (self.dir / "hello.py").write_text("def hi():\n    return 1\n")
        (self.dir / "test_hello.py").write_text(
            "import unittest, hello\nclass T(unittest.TestCase):\n    def test_x(self): self.assertEqual(hello.hi(), 1)\n")
        (self.dir / "orca" / "SPEC.md").write_text("# spec\n")
        (self.dir / "orca" / "TASKS.md").write_text("\n".join(tasks) + "\n")
        (self.dir / "orca" / "LESSONS.md").write_text("")
        (self.dir / "orca" / "PROGRESS.md").write_text("")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (self.tmp / "claude_reply.txt").write_text(claude_reply)
        claude = bin_dir / "claude"
        if escalate_reply is not None:           # a different answer for the escalation call than for reviews
            (self.tmp / "claude_escalate.txt").write_text(escalate_reply)
        claude.write_text("#!/bin/sh\ncat >> %s\n" % (self.tmp / "claude_stdin.txt") +
                          "F=%s\ncase \"$*\" in *\"senior engineer\"*) [ -f %s ] && F=%s;; esac\n" % (
                              self.tmp / "claude_reply.txt", self.tmp / "claude_escalate.txt", self.tmp / "claude_escalate.txt") +
                          "%s -c 'import json,sys; print(json.dumps({\"result\": open(sys.argv[1]).read(), \"usage\": {}}))' \"$F\"\n"
                          % sys.executable)
        claude.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.tmp), PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        self.cfg = dict({"test_cmd": "%s -m unittest discover -q" % sys.executable, "setup_cmd": "", "model": "fake-model",
                         "quota_check": False, "min_free_pct": 0, "max_swap_mb": 10 ** 9, "max_attempts": 2,
                         "max_steps": 20, "venv": False, "thinking": "default"}, **(cfg or {}))
        for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git"] + args, cwd=self.dir, check=True)

    def run(self, lm):
        self.cfg["base_url"] = lm.url
        (self.dir / "orca.json").write_text(json.dumps(self.cfg))
        subprocess.run(["git", "add", "-A"], cwd=self.dir, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.dir, check=True)
        r = subprocess.run([sys.executable, str(ROOT / "orca.py"), "run", str(self.dir)], env=self.env,
                           capture_output=True, text=True, timeout=180)
        return r.stdout + r.stderr

    def marks(self):
        return {t["id"]: t["mark"] for t in orca.Project(self.dir).tasks()}

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def write(path, content="x = 1\n"):
    return ("write_file", {"path": path, "content": content})


# ---------------------------------------------------------------- the build loop
class TestNoFakePasses(unittest.TestCase):
    """IslandSurvivor: Qwen only re-read files for 30 steps, the old tests stayed green, 7 features were marked
    done without a single line of code. A feature task must change the project to count as done."""

    def test_reading_only_is_not_done(self):
        lm = FakeLM(lambda tid, msgs: [("read_file", {"path": "hello.py"})])
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it"])
        try:
            out = p.run(lm)
            self.assertIn("NO FILE WAS CHANGED", out)
            self.assertEqual(p.marks()["T01"], "!")
        finally:
            lm.close()
            p.close()

    def test_nudge_then_write_passes(self):
        def brain(tid, msgs):
            if nudged(msgs):
                return [write("feature.py"), ("done", {"summary": "wrote feature.py"})]
            return [("read_file", {"path": "hello.py"})]
        lm = FakeLM(brain)
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it"])
        try:
            p.run(lm)
            self.assertEqual(p.marks()["T01"], "x")
            self.assertTrue((p.dir / "feature.py").exists())
        finally:
            lm.close()
            p.close()

    def test_fix_task_may_be_already_done(self):
        lm = FakeLM(lambda tid, msgs: [("done", {"summary": "already fixed in an earlier task"})])
        p = Project(["- [ ] R01: make sure hi() returns 1 | done when: a test shows it"])
        try:
            p.run(lm)
            self.assertEqual(p.marks()["R01"], "x")
        finally:
            lm.close()
            p.close()

    def test_fix_task_out_of_steps_without_change_is_not_done(self):
        lm = FakeLM(lambda tid, msgs: [("read_file", {"path": "hello.py"})])
        p = Project(["- [ ] R01: fix something | done when: a test shows it"])
        try:
            p.run(lm)
            self.assertNotEqual(p.marks()["R01"], "x")
        finally:
            lm.close()
            p.close()

    def test_test_cache_files_do_not_count_as_work(self):
        lm = FakeLM(lambda tid, msgs: [("run", {"cmd": "%s -m unittest discover -q" % sys.executable})])
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it"])
        try:
            p.run(lm)                                   # running the tests creates __pycache__, still no real work
            self.assertEqual(p.marks()["T01"], "!")
        finally:
            lm.close()
            p.close()

    def test_feedback_task_that_changes_nothing_is_not_done(self):
        """IslandSurvivor F02 and F03 were marked done with NO code change because F tasks were exempt."""
        lm = FakeLM(lambda tid, msgs: [("done", {"summary": "feedback handled"})])
        p = Project(["- [ ] F01: the bars do not move | done when: the user's feedback is handled"])
        try:
            out = p.run(lm)
            self.assertIn("NO FILE WAS CHANGED", out)
            self.assertEqual(p.marks()["F01"], "!")
        finally:
            lm.close()
            p.close()

    def test_a_log_the_app_writes_while_tests_run_is_not_work(self):
        """IslandSurvivor rewrote logs/day-001.txt on every test run, which made every attempt look like it changed files."""
        lm = FakeLM(lambda tid, msgs: [("done", {"summary": "did nothing"})])
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it"])
        (p.dir / "test_writes_log.py").write_text(
            "import unittest, time, os\nclass L(unittest.TestCase):\n"
            "    def test_log(self):\n        os.makedirs('logs', exist_ok=True)\n"
            "        open('logs/day-001.txt', 'a').write(str(time.time()) + '\\n')\n")
        try:
            out = p.run(lm)
            self.assertIn("NO FILE WAS CHANGED", out)
            self.assertEqual(p.marks()["T01"], "!")
        finally:
            lm.close()
            p.close()

    def test_numbered_feedback_becomes_one_task_per_request(self):
        import orca_preview
        self.assertEqual(orca_preview.split_feedback("1. the bars do not move 2. add a restart button"),
                         ["the bars do not move", "add a restart button"])
        self.assertEqual(orca_preview.split_feedback("make the sky bluer, version 2.0 looks flat"),
                         ["make the sky bluer, version 2.0 looks flat"])
        p = Project(["- [ ] T01: x | done when: y"])
        try:
            (p.dir / "orca.json").write_text(json.dumps(p.cfg))
            for args in (["add", "-A"], ["commit", "-qm", "init"]):
                subprocess.run(["git"] + args, cwd=p.dir, check=True)
            P = orca.Project(p.dir)
            orca_preview.add_feedback_task(P, "1. the bars do not move 2. add a restart button")
            ids = [t["id"] for t in orca.Project(p.dir).tasks()]
            self.assertIn("F01", ids)
            self.assertIn("F02", ids)
            text = (p.dir / "orca" / "TASKS.md").read_text()
            self.assertIn("SOURCE CODE is changed", text)
            self.assertNotIn("a test or the browser check covers it", text)
        finally:
            p.close()


class TestNoCodeReview(unittest.TestCase):
    """Code reviews are gone: on IslandSurvivor 141 of 208 task runs were review fixes and the page was still blank.
    The course check (screenshot of the running app, steers open tasks) replaces them, including at the end."""

    def test_no_review_settings_left(self):
        for k in ("review_every", "review_max_fixes", "review_mode"):
            self.assertNotIn(k, orca.DEFAULTS)
        self.assertFalse(hasattr(orca, "REVIEW_PROMPT"))

    def test_final_course_check_runs_once_and_its_tasks_are_built(self):
        lm = FakeLM(lambda tid, msgs: [write("f_%s.py" % tid), ("done", {"summary": "ok"})])
        p = Project(["- [ ] T01: a | done when: x", "- [ ] T02: b | done when: x"], {"checkpoint_minutes": 0},
                    claude_reply="STATUS: ok\nDEMO: one more thing\nADD: END | add the thing | done when: a test shows it\n")
        try:
            out = p.run(lm)
            self.assertNotIn("-- Claude review", out)
            self.assertEqual(out.count("-- course check (all tasks done)"), 1)
            self.assertIn("the final course check added tasks, building them now", out)
            self.assertEqual(p.marks()["C01"], "x")
        finally:
            lm.close()
            p.close()


class TestLocalCompaction(unittest.TestCase):
    """LESSONS.md is shortened by the local model now (it cost 13 Claude calls on one project). Its answer is checked."""

    def _project(self, lm, lessons):
        d = Path(tempfile.mkdtemp())
        (d / "orca").mkdir()
        (d / "orca.json").write_text(json.dumps({"test_cmd": "true", "base_url": lm.url, "model": "fake-model",
                                                 "lessons_max_chars": 400, "thinking": "default"}))
        (d / "orca" / "LESSONS.md").write_text(lessons)
        return orca.Project(d)

    LONG = "".join("- lesson number %d about something concrete\n" % i for i in range(20))

    def test_a_good_bullet_list_is_used(self):
        lm = FakeLM(lambda tid, msgs: [])
        lm.compact = "- keep tests small\n- use the code map\n- never edit old tests\n- pin versions"
        try:
            P = self._project(lm, self.LONG)
            P.compact_lessons()
            self.assertEqual(P.rd("LESSONS.md").splitlines(), ["- keep tests small", "- use the code map",
                                                               "- never edit old tests", "- pin versions"])
        finally:
            lm.close()

    def test_prose_is_rejected_and_the_newest_lessons_are_kept(self):
        lm = FakeLM(lambda tid, msgs: [])
        lm.compact = "Sure! Here are the compacted lessons you asked for, merged nicely."
        try:
            P = self._project(lm, self.LONG)
            P.compact_lessons()
            kept = P.rd("LESSONS.md")
            self.assertIn("lesson number 19", kept)          # newest kept
            self.assertNotIn("lesson number 0 ", kept)       # oldest dropped
            self.assertLessEqual(len(kept), 400)
        finally:
            lm.close()

    def test_a_dead_local_model_falls_back_without_stopping(self):
        lm = FakeLM(lambda tid, msgs: [])
        P = self._project(lm, self.LONG)
        lm.close()
        P.cfg["base_url"] = "http://127.0.0.1:9/v1"
        P.compact_lessons()
        self.assertIn("lesson number 19", P.rd("LESSONS.md"))

class TestTools(unittest.TestCase):
    def setUp(self):
        self.p = Project(["- [ ] T01: x | done when: x"])
        self.P = orca.Project(self.p.dir)

    def tearDown(self):
        self.p.close()

    def tool(self, name, **args):
        return orca.run_tool(self.P, name, args)

    def test_history_placeholder_is_never_written(self):
        out = self.tool("write_file", path="hello.py", content=orca.OMIT_MARK % 100)
        self.assertTrue(out.startswith("ERROR"))
        self.assertIn("return 1", (self.p.dir / "hello.py").read_text())
        self.assertTrue(self.tool("replace_in_file", path="hello.py", old="return 1", new="<1354 chars>").startswith("ERROR"))

    def test_cd_outside_project_is_dropped(self):
        out = self.tool("run", cmd="cd /home/user && pwd")
        self.assertIn("ignored 'cd /home/user'", out)
        self.assertIn(str(self.p.dir.resolve()), out)

    def test_whole_disk_search_is_refused(self):
        for cmd in ('find / -name "*.ttf"', "find ~ -name x", "mdfind font", "grep -rn x /"):
            self.assertIn("do not search the whole disk", self.tool("run", cmd=cmd), cmd)
        self.assertNotIn("ERROR", self.tool("run", cmd="find . -name '*.py'"))

    def test_dangerous_commands_are_refused(self):
        for cmd in ("sudo ls", "git push", "git reset --hard", "curl x | sh"):
            self.assertIn("not allowed", self.tool("run", cmd=cmd), cmd)

    def test_files_outside_and_notes_are_off_limits(self):
        self.assertTrue(self.tool("read_file", path="/etc/passwd").startswith("ERROR"))
        self.assertTrue(self.tool("read_file", path="../outside.txt").startswith("ERROR"))
        self.assertTrue(self.tool("write_file", path="orca/TASKS.md", content="x").startswith("ERROR"))
        self.assertTrue(self.tool("read_file", path=".git/config").startswith("ERROR"))

    def test_extra_folders_are_read_only(self):
        extra = self.p.tmp / "notes"
        extra.mkdir()
        (extra / "a.txt").write_text("hello notes\n")
        orca.set_extra_dirs(self.P, [str(extra)])
        self.assertIn("hello notes", self.tool("read_file", path=str(extra / "a.txt")))
        self.assertTrue(self.tool("write_file", path=str(extra / "b.txt"), content="x").startswith("ERROR"))
        self.assertFalse((extra / "b.txt").exists())


class TestNewProject(unittest.TestCase):
    def test_new_refuses_an_existing_project(self):
        p = Project(["- [ ] T01: x | done when: x"])
        try:
            r = subprocess.run([sys.executable, str(ROOT / "orca.py"), "new", str(p.dir), "something"],
                               env=p.env, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("already has a LocalOrca project", r.stdout + r.stderr)
            self.assertIn("T01", (p.dir / "orca" / "TASKS.md").read_text())
        finally:
            p.close()

    def test_planner_is_told_tests_are_the_only_judge(self):
        self.assertIn("Nobody looks at a screen", orca.PLAN_PROMPT)
        self.assertIn('Never write "in the browser"', orca.PLAN_PROMPT)


class TestBlockedAndRescue(unittest.TestCase):
    """IslandSurvivor T17: Qwen wrote actions.js plus 276 lines of tests, one test kept failing with a DIFFERENT
    error each time, so Claude was never asked; the block then rolled back and the saved diff missed the new files."""

    @staticmethod
    def failing_brain(tid, msgs):
        test = ("import unittest\nclass T(unittest.TestCase):\n    def test_%d(self): self.assertEqual(%d, -1)\n"
                % (len(msgs), len(msgs)))          # a different failure every attempt
        return [write("feature.py", "def f():\n    return 1\n"), write("test_feature.py", test),
                ("done", {"summary": "wrote it"})]

    def test_blocked_diff_keeps_new_files(self):
        lm = FakeLM(self.failing_brain)
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it"], {"max_attempts": 2})
        try:
            p.run(lm)
            self.assertEqual(p.marks()["T01"], "!")
            diff = (p.dir / "orca" / "failed" / "T01.diff").read_text()
            self.assertIn("feature.py", diff)
            self.assertIn("def f():", diff)
        finally:
            lm.close()
            p.close()

    def test_claude_is_asked_after_3_different_failures_and_sees_new_files(self):
        lm = FakeLM(self.failing_brain)
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it"], {"max_attempts": 5},
                    claude_reply="Make the test expect 1.")
        try:
            out = p.run(lm)
            self.assertIn("escalating to Claude", out)
            seen = (p.tmp / "claude_stdin.txt").read_text()
            self.assertIn("feature.py", seen)
            self.assertIn("def f():", seen)
        finally:
            lm.close()
            p.close()


class TestAutoSplit(unittest.TestCase):
    """IslandSurvivor: actions.js grew to 584 lines and the small model could neither read it nor rewrite it.
    After a passing task LocalOrca now queues a fix task to split any source file over the limit."""

    BIG = "".join("def f%d():\n    return %d\n" % (i, i) for i in range(130))          # 260 lines

    def brain(self, tid, msgs):
        if tid == "T01":
            return [write("feature.py", self.BIG), ("done", {"summary": "wrote it"})]
        return [write("other_%s.py" % tid.lower(), "x = 1\n"), ("done", {"summary": "ok"})]

    def tasks_text(self, p):
        return (p.dir / "orca" / "TASKS.md").read_text()

    def test_big_file_queues_one_split_task_before_the_next_task(self):
        lm = FakeLM(self.brain)
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it", "- [ ] T02: more | done when: x"])
        try:
            out = p.run(lm)
            text = self.tasks_text(p)
            self.assertIn("feature.py has 261 lines (limit 200): queued a split task", out)
            self.assertEqual(text.count("Split feature.py ("), 1)                  # one split task, not one per pass
            self.assertLess(text.index("Split feature.py ("), text.index("T02:"))  # and it runs before T02
            self.assertIn("keep feature.py as a thin module that re-exports", text)
        finally:
            lm.close()
            p.close()

    def test_switch_and_limit_and_tests_files(self):
        p = Project(["- [ ] T01: x | done when: y"], {"auto_split": False})
        try:
            (p.dir / "orca.json").write_text(json.dumps(p.cfg))
            (p.dir / "big.py").write_text(self.BIG)
            (p.dir / "test_big.py").write_text(self.BIG)                           # test files are never split
            P = orca.Project(p.dir)
            self.assertIsNone(orca.queue_splits(P))                                # switched off
            P.cfg["auto_split"] = True
            P.cfg["file_warn_lines"] = 1000
            self.assertIsNone(orca.queue_splits(P))                                # under the limit
            P.cfg["file_warn_lines"] = 200
            self.assertEqual(orca.queue_splits(P), "big.py")
            self.assertIsNone(orca.queue_splits(P))                                # one split task per file, ever
            self.assertNotIn("test_big.py (", (p.dir / "orca" / "TASKS.md").read_text())
        finally:
            p.close()

    def test_biggest_file_first_one_at_a_time(self):
        p = Project(["- [ ] T01: x | done when: y"])
        try:
            (p.dir / "orca.json").write_text(json.dumps(p.cfg))
            (p.dir / "mid.py").write_text("x = 1\n" * 230)
            (p.dir / "huge.py").write_text("x = 1\n" * 500)
            P = orca.Project(p.dir)
            self.assertEqual(orca.queue_splits(P), "huge.py")
            self.assertEqual(orca.queue_splits(P), "mid.py")
        finally:
            p.close()


class TestRollbackKeepsNotes(unittest.TestCase):
    """A blocked task is rolled back. That must undo the CODE, never the notes you edit while it runs
    (orca/TASKS.md, SPEC.md, LESSONS.md). It used to be `git reset --hard`, which wiped them."""

    def test_user_edits_to_notes_survive_a_block_but_code_is_rolled_back(self):
        holder = {}

        def brain(tid, msgs):
            if len(msgs) <= 2 and "edited" not in holder:           # the user edits the notes during the first attempt
                holder["edited"] = True
                (holder["p"].dir / "orca" / "LESSONS.md").write_text("- my own lesson written during the task\n")
                (holder["p"].dir / "orca" / "SPEC.md").write_text("# spec\nnew rule from me\n")
            return [write("feature.py", "def f():\n    return 1\n"), write("hello.py", "def hi():\n    return 99\n"),
                    write("test_feature.py", "import unittest\nclass T(unittest.TestCase):\n    def test_%d(self): self.assertEqual(1, 2)\n" % len(msgs)),
                    ("done", {"summary": "wrote it"})]
        lm = FakeLM(brain)
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it"], {"max_attempts": 2})
        holder["p"] = p
        try:
            p.run(lm)
            self.assertEqual(p.marks()["T01"], "!")
            self.assertIn("my own lesson written during the task", (p.dir / "orca" / "LESSONS.md").read_text())
            self.assertIn("new rule from me", (p.dir / "orca" / "SPEC.md").read_text())
            self.assertFalse((p.dir / "feature.py").exists())                          # code rolled back
            self.assertFalse((p.dir / "test_feature.py").exists())
            self.assertEqual((p.dir / "hello.py").read_text(), "def hi():\n    return 1\n")
            self.assertIn("feature.py", (p.dir / "orca" / "failed" / "T01.diff").read_text())
        finally:
            lm.close()
            p.close()


class TestEscalationLessons(unittest.TestCase):
    """IslandSurvivor T17 and T18 both died on the same pattern (asserting the exact wording of a reason string).
    When Claude is asked for help it may now also teach ONE general rule for the rest of the run."""

    HINT = "Make the test check ok only."

    def run_build(self, reply, seed_lessons="", cfg=None):
        prompts = []

        def brain(tid, msgs):
            prompts.append(msgs[1]["content"])
            return TestBlockedAndRescue.failing_brain(tid, msgs)
        lm = FakeLM(brain)
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it"], dict({"max_attempts": 5}, **(cfg or {})),
                    escalate_reply=reply)
        if seed_lessons:
            (p.dir / "orca" / "LESSONS.md").write_text(seed_lessons)
        try:
            out = p.run(lm)
            return out, prompts, (p.dir / "orca" / "LESSONS.md").read_text()
        finally:
            lm.close()
            p.close()

    def test_lesson_goes_to_lessons_and_hint_goes_to_qwen(self):
        out, prompts, lessons = self.run_build(self.HINT + "\nLESSON: Assert ok and effects, never the wording of reason text.")
        self.assertIn("escalating to Claude", out)
        self.assertIn("never the wording of reason text", lessons)
        after = prompts[-1]                                        # the attempt after the escalation
        self.assertIn(self.HINT, after)                            # the hint reaches Qwen...
        self.assertNotIn("LESSON: ", after)                        # ...without the raw LESSON line
        self.assertIn("# LESSONS (follow these)", after)           # and the lesson is now in every prompt
        self.assertIn("never the wording of reason text", after)

    def test_no_lesson_line_means_lessons_untouched(self):
        out, prompts, lessons = self.run_build(self.HINT)
        self.assertIn("escalating to Claude", out)
        self.assertNotIn("LESSON", lessons.replace("was blocked", ""))
        self.assertNotIn("lesson from Claude", out)

    def test_a_repeat_of_a_known_lesson_is_not_added_again(self):
        known = "- Assert ok and effects, never the wording of the reason text.\n"
        out, prompts, lessons = self.run_build(self.HINT + "\nLESSON: Never assert the wording of reason text, assert ok and effects.", known)
        self.assertEqual(lessons.count("wording"), 1)
        self.assertNotIn("lesson from Claude", out)

    def test_split_and_filters(self):
        hint, les = orca.split_lesson("Do X.\nDo Y.\n**LESSON:** Keep functions pure.")
        self.assertEqual((hint, les), ("Do X.\nDo Y.", "Keep functions pure."))
        self.assertEqual(orca.split_lesson("LESSON: In T18 the move test was wrong")[1], "")         # tied to one task
        self.assertEqual(orca.split_lesson("LESSON: " + "word " * 40)[1], "")                         # too long
        self.assertEqual(orca.split_lesson("")[0], "")
        self.assertTrue(orca.lesson_is_new("- Use tiny fixtures.\n", "Never assert reason wording"))
        self.assertFalse(orca.lesson_is_new("- Never assert reason wording in tests.\n", "Do not assert reason wording"))

    def test_switch_turns_it_off(self):
        out, prompts, lessons = self.run_build(self.HINT + "\nLESSON: Assert ok only.", cfg={"escalate_lessons": False})
        self.assertNotIn("Assert ok only", lessons)
        self.assertNotIn("LESSON: ", prompts[-1])                  # the line is still stripped from the hint
        self.assertIn(self.HINT, prompts[-1])


class TestBuilderPrompt(unittest.TestCase):
    """Rules that live only in the builder's system prompt. A wording change must not silently drop them."""

    def test_builder_is_told_to_leave_old_tests_alone_and_keep_tests_small(self):
        s = orca.SYSTEM
        self.assertIn("Never edit a test file", s)         # IslandSurvivor T18 broke T17's tests by editing them
        self.assertIn("NEW test file", s)
        self.assertIn("at most 6 tests", s)                # T17: 227 lines of tests on a random world
        self.assertIn("CODE MAP", s)


class TestReport(unittest.TestCase):
    """`orca.py report` / orca/REPORT.md: built only from the files a build leaves behind, no model involved."""

    def test_report_after_a_build(self):
        import orca_report
        lm = FakeLM(lambda tid, msgs: [("read_file", {"path": "hello.py"}), write("feature.py", "def f():\n    return 1\n"),
                                       ("done", {"summary": "wrote it"})])
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it",
                     "- [!] T09: an old blocked task | done when: y"])
        try:
            p.run(lm)
            (p.dir / "orca" / "failed").mkdir(exist_ok=True)
            (p.dir / "orca" / "failed" / "T09.txt").write_text("noise\n✖ the broken test (1ms)\n  1 !== 20\n  }\n")
            (p.dir / "weak.test.js").write_text(
                "test('has no check', () => {\n  const x = 1;\n});\n"
                "test('named import counts', () => {\n  ok(true);\n});\n"
                "test('plain assert counts', () => {\n  assert.equal(1, 1);\n});\n")
            text = orca_report.write_report(orca.Project(p.dir))
            self.assertTrue((p.dir / "orca" / "REPORT.md").exists())
            self.assertIn("1 done, 1 blocked", text)
            self.assertIn("passing on the FIRST attempt: 1 of 1", text)
            self.assertIn("T09: ✖ the broken test (1ms) | 1 !== 20", text)       # not just the trailing "}"
            self.assertIn("Wasted steps", text)
            self.assertIn("Tests with no assertion (heuristic): 1", text)
            self.assertIn("has no check", text)
            self.assertNotIn("named import counts", text)
        finally:
            lm.close()
            p.close()

    def test_blocked_lesson_names_the_failing_test_not_a_stack_frame(self):
        txt = "ok 1 - fine\n✖ apiLog loses entries (12ms)\n  1 !== 20\n  at async Test.run (node:internal/test_runner/test:1:1)\n  }\n"
        self.assertEqual(orca.why_blocked(txt), "✖ apiLog loses entries (12ms) | 1 !== 20")
        self.assertEqual(orca.why_blocked(""), "no error saved")
        self.assertEqual(orca.why_blocked("something odd\nlast line"), "last line")

    def test_report_on_an_empty_project_does_not_crash(self):
        import orca_report
        p = Project(["- [ ] T01: x | done when: y"])
        try:
            (p.dir / "orca.json").write_text(json.dumps(p.cfg))
            self.assertIn("# Build report", orca_report.build_report(orca.Project(p.dir)))
        finally:
            p.close()

    def test_report_is_written_when_a_build_ends(self):
        lm = FakeLM(lambda tid, msgs: [write("feature.py"), ("done", {"summary": "ok"})])
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it"])
        try:
            out = p.run(lm)
            self.assertIn("report written to orca/REPORT.md", out)
            self.assertTrue((p.dir / "orca" / "REPORT.md").exists())
        finally:
            lm.close()
            p.close()


class TestCodeMap(unittest.TestCase):
    """IslandSurvivor T17: Qwen read 7 files (all finished by earlier tasks) before writing a line, and filled 14k of
    its 22k context. Every task prompt now carries a map of what exists, built from the files by plain regex."""

    JS = ("export function moveTo(state, params) {\n  return 1;\n}\nexport const BLOCKS = {};\n"
          "export default class World {}\nfunction hidden() {}\nexport { a, b as c };\n")

    def proj(self, files, cfg=None):
        p = Project(["- [ ] T01: x | done when: y"], cfg)
        for path, body in files.items():
            f = p.dir / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body)
        (p.dir / "orca.json").write_text(json.dumps(p.cfg))
        return p, orca.Project(p.dir)

    def test_exports_are_listed_and_private_code_is_not(self):
        p, P = self.proj({"public/js/world.js": self.JS})
        try:
            text, covered = orca.code_map(P)
            self.assertIn("function moveTo(state, params)", text)
            self.assertIn("const BLOCKS", text)
            self.assertIn("class World", text)
            self.assertIn("export c", text)
            self.assertNotIn("hidden", text)
            self.assertIn("public/js/world.js", covered)
        finally:
            p.close()

    def test_python_defs_and_tests_are_summarised(self):
        p, P = self.proj({"lib.py": "def public(a, b):\n    pass\ndef _private():\n    pass\nclass K:\n    pass\n"})
        try:
            text, _ = orca.code_map(P)
            self.assertIn("def public(a, b)", text)
            self.assertIn("class K", text)
            self.assertNotIn("_private", text)
            self.assertIn("test_hello.py  (1 tests, 4 lines)", text)
        finally:
            p.close()

    def test_vendor_and_minified_files_are_skipped(self):
        p, P = self.proj({"public/vendor/three.module.js": "export function huge() {}\n",
                          "public/js/alpine.min.js": "export function tiny() {}\n"})
        try:
            text, _ = orca.code_map(P)
            self.assertNotIn("huge", text)
            self.assertNotIn("tiny", text)
        finally:
            p.close()

    def test_size_cap_holds_and_task_files_stay_in_full(self):
        files = {"src/m%02d.js" % i: "".join("export function f%d_%d(a, b) {}\n" % (i, j) for j in range(8)) for i in range(40)}
        p, P = self.proj(files, {"map_max_chars": 2500})
        try:
            text, _ = orca.code_map(P, {"id": "T09", "text": "T09: add m07.js behaviour"})
            self.assertLessEqual(len(text), 2500)
            self.assertIn("function f7_3(a, b)", text)           # the file the task names is complete
            self.assertIn("src/m30.js", text)                     # the others are still listed, shrunk
        finally:
            p.close()

    def test_big_files_are_flagged_in_the_map(self):
        body = "".join("export function f%d() { return %d; }\n" % (i, i) for i in range(230))
        p, P = self.proj({"src/big.js": body, "src/small.js": "export function s() {}\n",
                          "test/big.test.js": "test('x', () => {});\n" * 320, "test/ok.test.js": "test('y', () => {});\n" * 20})
        try:
            text, _ = orca.code_map(P)
            self.assertIn("231 lines, TOO BIG", text)
            self.assertIn("do not read it whole", text)
            self.assertNotIn("TOO BIG", text.split("src/small.js")[1].split("\n")[0])
            self.assertIn("test/big.test.js  (320 tests, 321 lines, TOO BIG)", text)
            self.assertNotIn("test/ok.test.js  (20 tests, 21 lines, TOO BIG", text)
        finally:
            p.close()

    def test_limits_are_configurable(self):
        p, P = self.proj({"src/mid.js": "export function a() {}\n" * 60}, {"file_warn_lines": 50})
        try:
            self.assertIn("TOO BIG", orca.code_map(P)[0])
        finally:
            p.close()

    def test_planner_and_house_rules_ask_for_small_files(self):
        self.assertIn("one file per function group", orca.PLAN_PROMPT.lower().replace("small files too: ", ""))
        self.assertIn("Never plan \"part 1 / part 2\"", orca.PLAN_PROMPT)
        self.assertIn("part 1 / part 2", (ROOT / "house_rules.md").read_text())

    def test_next_task_prompt_contains_what_the_first_task_built(self):
        seen = {}

        def brain(tid, msgs):
            seen[tid] = msgs[1]["content"]
            if tid == "T01":
                return [write("feature.py", "def first_feature(x):\n    return x\n"), ("done", {"summary": "wrote it"})]
            return [write("two.py", "def second():\n    return 2\n"), ("done", {"summary": "wrote two"})]
        lm = FakeLM(brain)
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it",
                     "- [ ] T02: build two.py | done when: a test shows it"])
        try:
            p.run(lm)
            self.assertEqual(p.marks(), {"T01": "x", "T02": "x"})
            self.assertNotIn("first_feature", seen["T01"])
            self.assertIn("# CODE MAP", seen["T02"])
            self.assertIn("def first_feature(x)", seen["T02"])
            self.assertIn("MAP.md", os.listdir(p.dir / "orca"))
            self.assertIn("first_feature", (p.dir / "orca" / "MAP.md").read_text())
        finally:
            lm.close()
            p.close()


class TestSmoke(unittest.TestCase):
    """The walking skeleton: from the skeleton task on, the REAL app must run after every task."""

    def _lm(self):
        return FakeLM(lambda tid, msgs: [write("f_%s.py" % tid), ("done", {"summary": "ok"})])

    def test_smoke_blocks_a_task_whose_real_app_is_broken(self):
        lm = self._lm()
        p = Project(["- [ ] T01: a | done when: x", "- [ ] T02: skeleton | done when: x"],
                    {"smoke_cmd": "test -f app_ok && echo ok || { echo APP IS DEAD; exit 1; }", "smoke_from": "T02", "halt_after_blocked": 1})
        try:
            out = p.run(lm)
            self.assertEqual(p.marks(), {"T01": "x", "T02": "!"})      # T01 is before the skeleton, T02 must really run
            self.assertIn("REAL APP SMOKE TEST FAILED", out)
        finally:
            lm.close()
            p.close()

    def test_smoke_stays_on_after_the_skeleton(self):
        lm = FakeLM(lambda tid, msgs: [write("f_%s.py" % tid)] + ([write("app_ok")] if tid == "T01" else []) + [("done", {"summary": "ok"})])
        p = Project(["- [ ] T01: skeleton | done when: x", "- [ ] T02: next | done when: x"],
                    {"smoke_cmd": "test -f app_ok && echo ok || { echo DEAD; exit 1; }",
                     "smoke_from": "T01", "halt_after_blocked": 1})
        try:
            out = p.run(lm)
            self.assertEqual(p.marks(), {"T01": "x", "T02": "x"})
            self.assertEqual(out.count("REAL APP SMOKE TEST FAILED"), 0)
        finally:
            lm.close()
            p.close()

    def test_smoke_that_cannot_run_is_skipped_with_a_warning(self):
        lm = self._lm()
        p = Project(["- [ ] T01: skeleton | done when: x"], {"smoke_cmd": "echo playwright missing; exit 3", "smoke_from": "T01"})
        try:
            out = p.run(lm)
            self.assertEqual(p.marks(), {"T01": "x"})
            self.assertIn("smoke test skipped", out)
        finally:
            lm.close()
            p.close()

    def test_planner_demands_a_real_product_check_for_every_stack(self):
        for word in ("smoke_cmd", "scripts/smoke", "WALKING SKELETON", "Every project gets exactly one"):
            self.assertIn(word, orca.PLAN_PROMPT)
        src = Path(ROOT / "orca.py").read_text()
        self.assertIn('"preview_url", "smoke_cmd", "smoke_from"', src)       # cmd_new keeps these keys from the plan

    def test_custom_smoke_cmd_is_used_as_is_and_empty_turns_it_off(self):
        d = Path(tempfile.mkdtemp())
        (d / "orca").mkdir()
        (d / "orca.json").write_text(json.dumps({"smoke_cmd": "sh scripts/smoke.sh", "smoke_from": "T02", "test_cmd": "true"}))
        self.assertEqual(orca.smoke_command(orca.Project(d)), "sh scripts/smoke.sh")
        (d / "orca.json").write_text(json.dumps({"smoke_cmd": "", "smoke_from": "T02", "preview_url": "http://x", "test_cmd": "true"}))
        self.assertEqual(orca.smoke_command(orca.Project(d)), "")

    def test_auto_smoke_needs_preview_url_and_skeleton_id(self):
        d = Path(tempfile.mkdtemp())
        (d / "orca").mkdir()
        (d / "orca.json").write_text(json.dumps({"preview_url": "http://localhost:1", "smoke_from": "T05", "test_cmd": "true"}))
        P = orca.Project(d)
        self.assertIn("orca_smoke.py", orca.smoke_command(P))
        (d / "orca.json").write_text(json.dumps({"test_cmd": "true"}))
        self.assertEqual(orca.smoke_command(orca.Project(d)), "")


class TestSmokeScript(unittest.TestCase):
    """orca_smoke.py against a tiny real server and a real headless browser."""

    def _run(self, html):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright not installed")
        d = Path(tempfile.mkdtemp())
        (d / "index.html").write_text(html)
        (d / "orca.json").write_text(json.dumps({"preview_cmd": "%s -m http.server $PORT --bind 127.0.0.1" % sys.executable,
                                                 "preview_url": "http://127.0.0.1:1"}))
        r = subprocess.run([sys.executable, str(ROOT / "orca_smoke.py"), str(d)], capture_output=True, text=True, timeout=90)
        return r.returncode, r.stdout

    GOOD = ("<body style='margin:0'><div style='background:red;height:100px'>a</div><div style='background:blue;height:100px'>b</div>"
            "<div style='background:green;height:100px'>c</div><div style='background:#fa0;height:100px'>d</div>"
            "<div style='background:#0af;height:100px'>e</div><div style='background:#a0f;height:100px'>f</div>"
            "<h1>hello world, a real page</h1></body>")

    def test_a_port_placeholder_in_preview_url_is_replaced(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright not installed")
        d = Path(tempfile.mkdtemp())
        (d / "index.html").write_text(self.GOOD)
        (d / "orca.json").write_text(json.dumps({"preview_cmd": "%s -m http.server $PORT --bind 127.0.0.1" % sys.executable,
                                                 "preview_url": "http://127.0.0.1:PORT"}))
        r = subprocess.run([sys.executable, str(ROOT / "orca_smoke.py"), str(d)], capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_a_hardcoded_port_in_preview_cmd_is_replaced(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright not installed")
        d = Path(tempfile.mkdtemp())
        (d / "index.html").write_text(self.GOOD)
        (d / "orca.json").write_text(json.dumps({"preview_cmd": "PORT=8080 %s -m http.server $PORT --bind 127.0.0.1" % sys.executable,
                                                 "preview_url": "http://127.0.0.1:8080"}))
        r = subprocess.run([sys.executable, str(ROOT / "orca_smoke.py"), str(d)], capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_a_working_page_passes(self):
        rc, out = self._run(self.GOOD)
        self.assertEqual(rc, 0, out)

    def test_a_console_error_fails(self):
        rc, out = self._run(self.GOOD + "<script>console.error('boom state is undefined')</script>")
        self.assertEqual(rc, 1)
        self.assertIn("boom state is undefined", out)

    def test_a_blank_page_fails(self):
        rc, out = self._run("<body style='background:#111'></body>")
        self.assertEqual(rc, 1)
        self.assertIn("BLANK", out)

    def test_an_uncaught_error_fails(self):
        rc, out = self._run(self.GOOD + "<script>null.x.y</script>")
        self.assertEqual(rc, 1)
        self.assertIn("uncaught error", out)


class TestProgress(unittest.TestCase):
    """`orca.py new` sat silent for minutes while Claude planned."""

    def test_heartbeat_prints_elapsed_lines_when_not_a_terminal(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with orca.Heartbeat("Claude is writing the plan", "(usually 1 to 4 min)", every=0.3, tty=False):
                time.sleep(1.0)
        out = buf.getvalue()
        self.assertIn("... Claude is writing the plan, 0:00 so far (usually 1 to 4 min)", out)
        self.assertGreaterEqual(out.count("so far"), 2)
        self.assertIn("done: Claude is writing the plan", out)

    def test_ask_claude_shows_progress_when_asked_and_stays_quiet_otherwise(self):
        import io
        import contextlib
        p = Project(["- [ ] T01: x | done when: y"], claude_reply="PLAN")
        try:
            cfg = dict(orca.DEFAULTS, claude_cmd=["claude", "-p"], progress_every=0.3, claude_timeout=30)
            old, old_cur = os.environ.copy(), orca.CUR
            os.environ.update(p.env)
            orca.CUR = None                      # earlier tests leave a deleted project here
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(orca.ask_claude(cfg, "hi", purpose="plan", progress="Claude is writing the plan"), "PLAN")
                self.assertIn("done: Claude is writing the plan", buf.getvalue())
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    orca.ask_claude(cfg, "hi", purpose="plan")
                self.assertNotIn("done:", buf.getvalue())
            finally:
                orca.CUR = old_cur
                os.environ.clear()
                os.environ.update(old)
        finally:
            p.close()


class TestCourseCheck(unittest.TestCase):
    """Every N passing tasks (and right after the skeleton) Claude looks at the running app and steers the OPEN tasks."""

    def _lm(self):
        return FakeLM(lambda tid, msgs: [write("f_%s.py" % tid), ("done", {"summary": "ok"})])

    def test_fires_every_n_tasks_and_applies_changes_to_open_tasks_only(self):
        reply = ("STATUS: nothing usable yet\nDEMO: draw the map first\n"
                 "REMOVE: T05 | polish, not needed for the demo\n"
                 "REMOVE: T01 | done tasks are never touched\n"
                 "ADD: T04 | draw the map | done when: a test shows tiles are drawn\n"
                 "MOVE: T06 | T04\n")
        lm = self._lm()
        p = Project(["- [ ] T0%d: t%d | done when: x" % (i, i) for i in range(1, 7)],
                    {"checkpoint_every": 2, "checkpoint_minutes": 0, "review_mode": "features_first"}, claude_reply=reply)
        try:
            out = p.run(lm)
            self.assertIn("-- course check (2 tasks)", out)
            order = [t["id"] for t in orca.Project(p.dir).tasks()]
            self.assertNotIn("T05", order)                       # removed while open
            self.assertIn("T01", order)                          # done tasks are never removed
            self.assertLess(order.index("C01"), order.index("T04"))   # added before T04
            self.assertLess(order.index("T06"), order.index("T04"))   # moved before T04
            note = (p.dir / "orca" / "CHECKPOINT.md").read_text()
            self.assertIn("STATUS: nothing usable yet", note)
            self.assertIn("REJECTED REMOVE: T01", note)
            self.assertEqual(p.marks()["C01"], "x")              # the new task was built
        finally:
            lm.close()
            p.close()

    def test_fires_right_after_the_skeleton_task(self):
        lm = self._lm()
        p = Project(["- [ ] T01: a | done when: x", "- [ ] T02: skeleton | done when: x", "- [ ] T03: c | done when: x"],
                    {"checkpoint_every": 8, "checkpoint_minutes": 0, "smoke_from": "T02", "smoke_cmd": "echo ok"},
                    claude_reply="STATUS: runs\nDEMO: on track\n")
        try:
            out = p.run(lm)
            self.assertEqual(out.count("-- course check (the skeleton runs)"), 1)
        finally:
            lm.close()
            p.close()

    def test_caps_hold_and_bad_lines_are_rejected(self):
        d = Path(tempfile.mkdtemp())
        (d / "orca").mkdir()
        (d / "orca.json").write_text(json.dumps({"test_cmd": "true", "checkpoint_max_add": 1, "checkpoint_max_remove": 1}))
        (d / "orca" / "TASKS.md").write_text("- [x] T01: a\n- [ ] T02: b\n- [ ] T03: c\n- [!] T04: d\n")
        P = orca.Project(d)
        applied, rejected = orca.apply_checkpoint(P, "REMOVE: T02 | x\nREMOVE: T03 | over cap\nREMOVE: T04 | blocked\n"
                                                     "ADD: END | one | done when: y\nADD: END | two | done when: y\n"
                                                     "ADD: END | no done-when here\nMOVE: T09 | T03\n")
        ids = [t["id"] for t in P.tasks()]
        self.assertEqual(ids, ["T01", "T03", "T04", "C01"])
        self.assertEqual(len(applied), 2)
        self.assertEqual(len(rejected), 5)

    def test_off_when_zero(self):
        lm = self._lm()
        p = Project(["- [ ] T0%d: t | done when: x" % i for i in range(1, 5)], {"checkpoint_every": 0, "checkpoint_minutes": 0})
        try:
            self.assertNotIn("course check", p.run(lm))
        finally:
            lm.close()
            p.close()


class TestContextSetting(unittest.TestCase):
    """The prompt budget is 28k by default, can be changed live, and the model is reloaded if LM Studio has less context."""

    def _project(self, cfg=None):
        d = Path(tempfile.mkdtemp())
        (d / "orca").mkdir()
        (d / "orca.json").write_text(json.dumps(cfg or {"test_cmd": "true"}))
        return orca.Project(d)

    def test_default_is_28k_and_fits_the_default_window(self):
        self.assertEqual(orca.DEFAULTS["prompt_budget_tokens"], 28000)
        # our estimate runs about 15% high, plus the 4096 reply, must fit the loaded context
        self.assertLessEqual(orca.DEFAULTS["prompt_budget_tokens"] * 0.85 + orca.DEFAULTS["max_output_tokens"],
                             orca.DEFAULTS["context_tokens"])

    def test_every_preset_fits_its_context(self):
        for budget, ctx in orca.CONTEXT_PRESETS:
            self.assertLessEqual(budget * 0.85 + 4096, ctx, (budget, ctx))

    def test_a_change_counts_without_a_restart(self):
        P = self._project()
        self.assertEqual(orca.live_budget(P), 28000)
        orca.set_context(P, 40000, 49152)
        self.assertEqual(orca.live_budget(P), 40000)
        saved = json.loads((P.root / "orca.json").read_text())
        self.assertEqual((saved["prompt_budget_tokens"], saved["context_tokens"], saved["test_cmd"]), (40000, 49152, "true"))

    def test_a_broken_orca_json_falls_back(self):
        P = self._project()
        (P.root / "orca.json").write_text("{not json")
        self.assertEqual(orca.live_budget(P), 28000)

    def test_settings_menu_keys_are_unique_and_do_not_clash_with_stop(self):
        import orca_tui
        keys = [k for k, _, _ in orca_tui.SETTINGS_ROWS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn("s", [k.lower() for k in keys])      # (s) stops the build, it must never be a menu shortcut
        self.assertNotIn("o", [k.lower() for k in keys])      # (o) opens the menu itself

    def test_ask_claude_opens_an_interactive_session_with_the_project_brief(self):
        import orca_tui
        P = self._project({"test_cmd": "true", "claude_model": "sonnet", "claude_cmd": ["claude", "-p"]})
        cmd = orca_tui.claude_chat_cmd(P)
        self.assertEqual(cmd[0], "claude")
        self.assertNotIn("-p", cmd)                         # -p is one-shot mode, the dashboard wants a real chat
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        brief = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertIn("not running", brief)
        self.assertIn("TASKS.md", brief)

    def test_ask_claude_never_falls_back_to_the_biggest_model(self):
        import orca_tui
        P = self._project({"test_cmd": "true"})                       # no model chosen for this project
        cmd = orca_tui.claude_chat_cmd(P)
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        P = self._project({"test_cmd": "true", "claude_model": "opus", "chat_model": "haiku"})
        cmd = orca_tui.claude_chat_cmd(P)
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")      # chat_model wins for the chat

    def test_preview_restarts_a_dead_app_a_few_times_then_stops(self):
        import orca_preview
        pv = orca_preview.Preview.__new__(orca_preview.Preview)
        pv.cmd, pv.retries, pv.started = "node server.js", 0, 100.0

        class Dead:
            def poll(self):
                return 1
        pv.proc = Dead()
        self.assertFalse(pv.should_revive(now=105.0))          # died just now, give it a moment
        self.assertTrue(pv.should_revive(now=120.0))
        pv.retries = 3
        self.assertFalse(pv.should_revive(now=999.0))          # a crash loop stops after 3 tries

        class Alive:
            def poll(self):
                return None
        pv.proc, pv.retries = Alive(), 0
        self.assertFalse(pv.should_revive(now=999.0))          # running apps are left alone

    def test_every_project_gets_its_own_preview_port(self):
        """ReadAlong's preview could not start because IslandSurvivor's held 7777, and the browser showed the wrong app."""
        import socket
        a, b = self._project(), self._project()
        pa, pb = orca.choose_preview_port(a), orca.choose_preview_port(b)
        self.assertEqual(pa, orca.choose_preview_port(a))                 # stable
        self.assertTrue(7777 <= pa < 7837 and 7777 <= pb < 7837)
        sk = socket.socket()
        sk.bind(("127.0.0.1", pa))                                         # taken: the next call moves on
        try:
            self.assertNotEqual(orca.choose_preview_port(a), pa)
        finally:
            sk.close()
        c = self._project({"test_cmd": "true", "preview_port": 9123})
        self.assertEqual(orca.choose_preview_port(c), 9123)                # an explicit port always wins
        self.assertIsNone(orca.preview_port_running(a))

    def test_kill_also_stops_the_preview(self):
        """Killing Orca left the preview server running, still holding its port."""
        import subprocess as sp
        P = self._project()
        proc = sp.Popen([sys.executable, "-c", "import time; time.sleep(60)  # orca preview"])
        (P.root / "orca" / "preview.pid").write_text("%d 7790" % proc.pid)
        try:
            self.assertEqual(orca.preview_port_running(P), 7790)
            msg = orca.kill_run(P, with_preview=True)
            self.assertIn("preview", msg)
            self.assertIsNotNone(proc.wait(timeout=10))
            self.assertIsNone(orca.preview_pid(P))
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_loaded_context_is_read_from_lm_studio(self):
        real = orca.all_models
        orca.all_models = lambda cfg: [{"id": "a", "state": "loaded", "loaded_context_length": 16384},
                                       {"id": "b", "state": "not-loaded", "loaded_context_length": 99}]
        try:
            self.assertEqual(orca.loaded_context({}, "a"), 16384)
            self.assertIsNone(orca.loaded_context({}, "b"))
        finally:
            orca.all_models = real


if __name__ == "__main__":
    unittest.main()
