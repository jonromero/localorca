"""Guards from the PaperGlide / Cargo Hold / Hearth benchmarks (Claude alone vs Qwen alone vs LocalOrca).

    python3 -m unittest tests.test_efficiency -v

What went wrong there, and what each test here holds in place:
  - plans drifted from the brief and Qwen's own tests only tested the plan (126 to 230 green tests, 11/18 and 6/33 on
    the brief's checks): acceptance tests from the brief judge each task, are read-only, and never regress
  - `node --test` with zero test files exits 0 and counted as a pass
  - the same failure was retried 5 times (22 min) with no help left
  - `orca.py stop --now` did nothing to a build started in the background (SIGINT inherited as ignored)
  - every task ran the whole suite even when its own test file failed
"""
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import orca  # noqa: E402
import orca_accept  # noqa: E402
from test_guards import FakeLM, Project, write  # noqa: E402

ACC = ("import unittest, hello\n"
       "class T(unittest.TestCase):\n"
       "    def test_A01_feature_doubles(self):\n"
       "        import feature\n"
       "        self.assertEqual(feature.f(), 2)\n"
       "    def test_A02_hi_is_one(self):\n"
       "        self.assertEqual(hello.hi(), 1)\n")
ACC_CMD = "%s -m unittest -v acceptance/acc_basic.py" % sys.executable


def acc_project(tasks, passing=(), **cfg):
    p = Project(tasks, dict({"acceptance_cmd": ACC_CMD, "max_attempts": 3, "max_escalations": 0}, **cfg))
    p.env["PYTHONDONTWRITEBYTECODE"] = "1"      # files rewritten within one second at the same size: no stale .pyc
    (p.dir / "acceptance").mkdir()
    (p.dir / "acceptance" / "acc_basic.py").write_text(ACC)
    (p.dir / "acceptance" / "__init__.py").write_text("")
    (p.dir / "orca" / "ACCEPTANCE.md").write_text("A01: feature.f() returns 2 | brief: \"f gives 2\"\nA02: hi() is 1\n")
    (p.dir / "orca" / "ACCEPTANCE.json").write_text(json.dumps({"passing": list(passing)}))
    return p


class TestParse(unittest.TestCase):
    def test_runners(self):
        out = ("ok 1 - A01 title\nnot ok 2 - A02 left moves\n    ok 3 - A03 nested\n✔ A04 spec (1ms)\n✖ A05 spec\n"
               "PASSED acceptance/acc_api.py::test_A06_signup\nFAILED acceptance/acc_api.py::test_A07_threads - assert 404\n"
               "acceptance/acc_api.py::test_A08_x PASSED   [ 50%]\n"
               "test_A09_a (acc.T.test_A09_a) ... ok\ntest_A10_b (acc.T.test_A10_b) ... FAIL\n# Subtest: A11 header only\n")
        self.assertEqual(orca_accept.parse(out), {"A01": True, "A02": False, "A03": True, "A04": True, "A05": False,
                                                  "A06": True, "A07": False, "A08": True, "A09": True, "A10": False})

    def test_task_ids_and_coverage(self):
        lines = ["- [ ] T01: a | done when: x | accept: A01, A03", "- [ ] T02: b | done when: y"]
        self.assertEqual(orca_accept.task_ids(lines[0]), ["A01", "A03"])
        self.assertEqual(orca_accept.uncovered(["A01", "A02", "A03"], lines), ["A02"])
        extra = orca_accept.cover_tasks(["A02", "A04", "A05", "A06"], {"A02": "rate limit 5 per 60 s"}, 3)
        self.assertEqual(len(extra), 2)
        self.assertTrue(extra[0].startswith("- [ ] T03: "))
        self.assertEqual(orca_accept.task_ids(extra[0]), ["A02", "A04", "A05"])
        self.assertIn("done when:", extra[0])


class TestZeroTests(unittest.TestCase):
    def test_count(self):
        self.assertEqual(orca.test_count("TAP version 13\n# tests 0\n# pass 0\n"), 0)
        self.assertEqual(orca.test_count("ℹ tests 12\n"), 12)
        self.assertEqual(orca.test_count("no tests ran in 0.01s"), 0)
        self.assertEqual(orca.test_count("3 passed, 1 failed in 0.2s"), 4)
        self.assertEqual(orca.test_count("Ran 1 test in 0.001s\n\nOK"), 1)
        self.assertIsNone(orca.test_count("all good"))

    def test_a_suite_with_zero_tests_never_passes(self):
        lm = FakeLM(lambda tid, msgs: [write("feature.py"), ("done", {"summary": "ok"})])
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it"],
                    {"test_cmd": "echo '# tests 0'", "max_attempts": 2})
        try:
            out = p.run(lm)
            self.assertEqual(p.marks()["T01"], "!")
            self.assertIn("NO TESTS RAN", (p.dir / "orca" / "failed" / "T01.txt").read_text())
        finally:
            lm.close()
            p.close()


class TestTargetedTests(unittest.TestCase):
    def test_task_test_files_first(self):
        tmp = Path(__file__).resolve().parent
        name = Path(__file__).name
        c = {"test_cmd": "node --test", "targeted_tests": True}
        task = "T05: x | files: src/a.js, new tests/%s | done when: y" % name
        self.assertEqual(orca.targeted_cmd(c, task, tmp.parent), "node --test tests/%s" % name)
        c2 = {"test_cmd": "python -m pytest -q", "targeted_tests": True}
        self.assertEqual(orca.targeted_cmd(c2, task, tmp.parent), "python -m pytest -q tests/%s" % name)
        self.assertIsNone(orca.targeted_cmd({"test_cmd": "npm test", "targeted_tests": True}, task, tmp.parent))
        self.assertIsNone(orca.targeted_cmd(dict(c, targeted_tests=False), task, tmp.parent))
        self.assertIsNone(orca.targeted_cmd(c, "T05: x | files: tests/missing.test.mjs | done when: y", tmp.parent))
        self.assertIsNone(orca.targeted_cmd({"test_cmd": "npm run lint && node --test", "targeted_tests": True}, task, tmp.parent))


class TestAcceptanceFilesAreTests(unittest.TestCase):
    def test_never_split_and_never_in_the_targeted_run(self):
        """Hearth rerun: the auto-split queued "Split acceptance/acc_content.py", a read-only file (a task that can never pass)."""
        self.assertTrue(orca.is_test_file("acceptance/acc_content.py"))
        self.assertTrue(orca.is_test_file("acceptance/hold.acc.mjs"))
        self.assertFalse(orca.is_test_file("src/accounts.py"))
        p = acc_project([])
        try:
            (p.dir / "acceptance" / "acc_basic.py").write_text(ACC + "\n" * 400)
            (p.dir / "big.py").write_text("x = 1\n" * 400)
            for args in (["add", "-A"], ["commit", "-qm", "x"]):
                subprocess.run(["git"] + args, cwd=p.dir, check=True)
            P = orca.Project(p.dir)
            self.assertEqual(orca.queue_splits(P), "big.py")
            self.assertNotIn("acceptance/", P.rd("TASKS.md"))
            c = {"test_cmd": "python -m pytest -q", "targeted_tests": True}
            self.assertIsNone(orca.targeted_cmd(c, "T01: x | files: acceptance/acc_basic.py | done when: y", p.dir))
        finally:
            p.close()


class TestGiveUpEarly(unittest.TestCase):
    def test_same_failure_with_no_help_left_stops_at_3_not_5(self):
        bad = "import unittest\nclass T(unittest.TestCase):\n    def test_x(self): self.assertEqual(1, 2)\n"
        lm = FakeLM(lambda tid, msgs: [write("feature.py"), write("test_feature.py", bad), ("done", {"summary": "ok"})])
        p = Project(["- [ ] T01: build feature.py | done when: a test shows it"], {"max_attempts": 5, "max_escalations": 0})
        try:
            out = p.run(lm)
            self.assertEqual(p.marks()["T01"], "!")
            self.assertIn("giving up early", out)
            self.assertNotIn("attempt 4/5", out)
        finally:
            lm.close()
            p.close()


class TestAcceptance(unittest.TestCase):
    def test_task_must_make_its_acceptance_tests_pass(self):
        """Cargo Hold: Left did nothing while the task's own tests passed. The acceptance failure goes back to Qwen."""
        def brain(tid, msgs):
            told = any("must also make these ACCEPTANCE tests pass" in (m.get("content") or "") for m in msgs)
            val = 2 if told else 1
            test = "import unittest, feature\nclass T(unittest.TestCase):\n    def test_f(self): self.assertEqual(feature.f(), %d)\n" % val
            body = "def f():\n    return 1\n" if val == 1 else "def f():\n    return 1 + 1  # fixed\n"   # new size: no stale .pyc
            return [write("feature.py", body), write("test_feature.py", test), ("done", {"summary": "ok"})]
        lm = FakeLM(brain)
        p = acc_project(["- [ ] T01: feature f | done when: a test shows f | accept: A01"], passing=["A02"])
        try:
            out = p.run(lm)
            self.assertEqual(p.marks()["T01"], "x")
            self.assertIn("needs A01", out)
            st = json.loads((p.dir / "orca" / "ACCEPTANCE.json").read_text())
            self.assertEqual(st["passing"], ["A01", "A02"], out)
            self.assertIn("2 of 2 acceptance tests pass", orca_accept.summary(orca.Project(p.dir)))
        finally:
            lm.close()
            p.close()

    def test_a_task_that_breaks_a_passing_acceptance_test_fails(self):
        def brain(tid, msgs):
            test = "import unittest, hello\nclass T(unittest.TestCase):\n    def test_x(self): self.assertEqual(hello.hi(), 5)\n"
            return [write("hello.py", "def hi():\n    return 5\n"), write("test_hello.py", test), ("done", {"summary": "ok"})]
        lm = FakeLM(brain)
        p = acc_project(["- [ ] T01: change hi | done when: a test shows hi"], passing=["A02"], max_attempts=2)
        try:
            out = p.run(lm)
            self.assertEqual(p.marks()["T01"], "!")
            self.assertIn("breaks A02", out)
        finally:
            lm.close()
            p.close()

    def test_acceptance_tests_are_read_only(self):
        """Qwen's write tools refuse the folder, and a shell change to it is undone before the tests run."""
        def brain(tid, msgs):
            return [write("acceptance/acc_basic.py", "# gone\n"), ("run", {"cmd": "rm acceptance/acc_basic.py"}),
                    write("acceptance/acc_new.py", "x = 1\n"),
                    write("feature.py", "def f():\n    return 2\n"), ("done", {"summary": "ok"})]
        lm = FakeLM(brain)
        p = acc_project(["- [ ] T01: feature f | done when: a test shows f | accept: A01"], passing=["A02"])
        try:
            p.run(lm)
            self.assertEqual((p.dir / "acceptance" / "acc_basic.py").read_text(), ACC)
            self.assertFalse((p.dir / "acceptance" / "acc_new.py").exists())
            self.assertEqual(p.marks()["T01"], "x")
            log = json.loads(sorted((p.dir / "orca" / "logs").glob("T01-a1.json"))[0].read_text())
            self.assertTrue(any("ACCEPTANCE tests, written from the brief. They are read-only" in (m.get("content") or "")
                                for m in log))
        finally:
            lm.close()
            p.close()

    def test_good_work_is_kept_when_only_acceptance_fails(self):
        """Hearth rerun: T03 blocked only on its acceptance id and was rolled back, so T04, built on it, blocked too."""
        def brain(tid, msgs):
            return [write("feature.py", "def f():\n    return 1\n"), ("done", {"summary": "ok"})]
        lm = FakeLM(brain)
        p = acc_project(["- [ ] T01: feature f | done when: a test shows f | accept: A01"], passing=["A02"], max_attempts=5)
        try:
            out = p.run(lm)
            self.assertIn("giving up early", out)
            self.assertIn("PASS with open acceptance tests", out)
            self.assertEqual(p.marks()["T01"], "x")
            self.assertTrue((p.dir / "feature.py").exists())
            self.assertEqual(json.loads((p.dir / "orca" / "ACCEPTANCE.json").read_text())["debt"], ["A01"])
        finally:
            lm.close()
            p.close()

    def test_claude_can_suspend_a_wrong_acceptance_test(self):
        """A42 crashed in its own helper call (TypeError): the escalation sees the test code and may judge it wrong."""
        def brain(tid, msgs):
            return [write("feature.py", "def f():\n    return 1\n"), ("done", {"summary": "ok"})]
        lm = FakeLM(brain)
        p = acc_project(["- [ ] T01: feature f | done when: a test shows f | accept: A01"], passing=["A02"],
                        max_attempts=4, max_escalations=1)
        (p.tmp / "claude_escalate.txt").write_text("The product is fine.\nACCEPTANCE_WRONG: A01 crashes in its own helper\n")
        try:
            out = p.run(lm)
            self.assertIn("acceptance test A01 is WRONG", out)
            st = json.loads((p.dir / "orca" / "ACCEPTANCE.json").read_text())
            self.assertIn("A01", st["suspended"])
            self.assertEqual(p.marks()["T01"], "x")
            self.assertIn("test_A01_feature_doubles", (p.tmp / "claude_stdin.txt").read_text())
            self.assertIn("SUSPENDED", orca_accept.summary(orca.Project(p.dir)))
        finally:
            lm.close()
            p.close()

    def test_builder_prompt_names_the_tasks_acceptance_tests(self):
        P = orca.Project(Path(acc_project([]).dir))
        P.cfg["acceptance_cmd"] = ACC_CMD
        text = orca.build_prompt(P, {"id": "T01", "text": "feature | done when: x | accept: A01"}, "", "")
        self.assertIn("ACCEPTANCE TESTS THIS TASK MUST MAKE PASS", text)
        self.assertIn("A01: feature.f() returns 2", text)


class TestCourseCheckPrompt(unittest.TestCase):
    def test_course_check_sees_the_brief_and_the_acceptance_results(self):
        self.assertIn("{brief}", orca.CHECKPOINT_PROMPT)
        self.assertIn("{acceptance}", orca.CHECKPOINT_PROMPT)
        self.assertIn("not only the task list", orca.CHECKPOINT_PROMPT)

    def test_prompt_only_claude_calls_get_no_tools(self):
        self.assertEqual(orca.NO_TOOLS, ["--tools", ""])


class TestStopNow(unittest.TestCase):
    def test_stop_now_ends_a_build_that_ignores_ctrl_c(self):
        """A background build inherits SIGINT as ignored; stop --now must still end it."""
        p = Project([])
        try:
            code = "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\ntime.sleep(120)\n"
            proc = subprocess.Popen([sys.executable, "-c", code, "orca-fake-build"])
            (p.dir / "orca" / "run.pid").write_text(str(proc.pid))
            time.sleep(0.5)
            r = subprocess.run([sys.executable, str(ROOT / "orca.py"), "stop", str(p.dir), "--now"],
                               capture_output=True, text=True, timeout=60)
            self.assertIsNotNone(proc.wait(timeout=20), r.stdout + r.stderr)
            self.assertIn("force killed", r.stdout)
        finally:
            p.close()


if __name__ == "__main__":
    unittest.main()
