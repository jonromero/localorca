#!/usr/bin/env python3
"""LocalOrca live preview. Python stdlib only.

    python3 orca.py preview ~/proj/app        (or (P) in the dashboard)

Runs your app in the background ("preview_cmd" in orca.json) and puts a small proxy in front of it
(http://localhost:7777 by default). Through the proxy every HTML page gets a tiny LocalOrca badge:
  - the page reloads by itself after every PASSED task (the app is restarted first, so you never see
    half-written code),
  - it shows what the build is doing right now,
  - a Feedback button turns what you type into an F task at the front of orca/TASKS.md
    (in the DESIGN stage it becomes a design note for Claude's prototype instead, see orca_design.py).
Your app itself is not changed. Open the app's own URL to see it without the badge.
"""

import http.server
import json
import os
import re
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import orca

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "content-encoding"}

LIVE_JS = r"""
(function () {
  if (window.__orcaLive) return; window.__orcaLive = true;
  var host = document.createElement('div');
  host.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:2147483647';
  document.documentElement.appendChild(host);
  var root = host.attachShadow ? host.attachShadow({mode: 'open'}) : host;
  root.innerHTML = '<style>' +
    '.b{font:12px/1.35 -apple-system,system-ui,sans-serif;background:#111827;color:#e5e7eb;border:1px solid #374151;' +
    'border-radius:10px;padding:8px 10px;box-shadow:0 6px 24px rgba(0,0,0,.35);max-width:320px}' +
    '.t{display:flex;gap:8px;align-items:center;cursor:pointer}.d{width:8px;height:8px;border-radius:50%;background:#22d3ee;flex:none}' +
    '.d.w{background:#f59e0b}.d.e{background:#ef4444}.s{opacity:.8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
    'textarea{width:100%;box-sizing:border-box;height:70px;margin-top:8px;background:#0b1220;color:#e5e7eb;border:1px solid #374151;border-radius:6px;padding:6px;font:12px -apple-system,system-ui,sans-serif}' +
    'button{margin-top:6px;background:#2563eb;color:#fff;border:0;border-radius:6px;padding:5px 10px;font:12px -apple-system,system-ui,sans-serif;cursor:pointer}' +
    '.x{background:#374151}.m{margin-top:6px;color:#a7f3d0}.err{margin-top:6px;color:#fca5a5;white-space:pre-wrap;max-height:120px;overflow:auto}' +
    '.h{display:none}</style>' +
    '<div class="b"><div class="t" id="t"><span class="d" id="d"></span><b>LocalOrca</b><span class="s" id="s">connecting</span></div>' +
    '<div id="err" class="err h"></div>' +
    '<div id="f" class="h"><textarea id="txt" placeholder="What should change? Be specific: “the Add button is too small on mobile”"></textarea>' +
    '<button id="send">Add as task</button> <button class="x" id="close">Close</button></div>' +
    '<button id="open">Feedback</button><div id="m" class="m h"></div></div>';
  var $ = function (id) { return root.getElementById ? root.getElementById(id) : root.querySelector('#' + id); };
  var head = null;
  $('open').onclick = function () { $('f').classList.remove('h'); $('open').classList.add('h'); $('txt').focus(); };
  $('close').onclick = function () { $('f').classList.add('h'); $('open').classList.remove('h'); };
  $('send').onclick = function () {
    var t = $('txt').value.trim(); if (!t) return;
    fetch('/__orca/feedback', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: t})})
      .then(function (r) { return r.json(); }).then(function (j) {
        $('txt').value = ''; $('close').onclick();
        $('m').textContent = j.ok ? (j.msg || ('Added ' + j.id + ', it runs right after the current task')) : ('Not added: ' + j.error);
        $('m').classList.remove('h'); setTimeout(function () { $('m').classList.add('h'); }, 6000);
      });
  };
  function poll() {
    fetch('/__orca/status', {cache: 'no-store'}).then(function (r) { return r.json(); }).then(function (s) {
      if (head === null) head = s.head;
      else if (s.head !== head && s.app === 'running') { location.reload(); return; }
      $('s').textContent = s.line; $('d').className = 'd' + (s.app === 'crashed' ? ' e' : (s.app === 'restarting' ? ' w' : ''));
      if (s.app === 'crashed' && s.app_log) { $('err').textContent = s.app_log; $('err').classList.remove('h'); }
      else $('err').classList.add('h');
    }).catch(function () { $('s').textContent = 'preview not reachable'; $('d').className = 'd e'; })
      .then(function () { setTimeout(poll, 2000); });
  }
  poll();
})();
"""


class Preview:
    def __init__(self, P):
        self.P = P
        c = P.cfg
        self.cmd = c.get("preview_cmd") or ""
        self.target = (c.get("preview_url") or "").rstrip("/")
        self.port = orca.choose_preview_port(P)
        self.proc = None
        self.app = "starting"
        self.head = self.git_head()
        self.lock = threading.Lock()
        self.retries = 0                   # automatic restarts of a dead app since the last commit
        self.started = 0.0

    def git_head(self):
        """The last commit that changed the APP. Commits that only touch Orca's notes (a feedback task, a design note)
        must not restart the app or reload the page: the "Added F01" confirmation vanished with the reload."""
        rc, out = self.P.git("log -1 --format=%H -- . ':(exclude)orca' ':(exclude).orca'")
        if rc == 0 and out.strip():
            return out.strip()
        rc, out = self.P.git("rev-parse HEAD")
        return out.strip() if rc == 0 else ""

    # ---- the app
    def start_app(self):
        if not self.cmd:
            self.app = "running"           # nothing to run, just proxy an app you started yourself
            return
        log = open(str(self.P.o / "preview.out"), "a")
        log.write("\n--- %s starting: %s\n" % (time.strftime("%H:%M:%S"), self.cmd))
        log.flush()
        env = dict(os.environ)
        self.started = time.time()
        self.proc = subprocess.Popen(self.cmd, shell=True, cwd=str(self.P.root), stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        self.app = "restarting"
        for _ in range(60):                # up to 30 s for the app to answer
            if self.proc.poll() is not None:
                self.app = "crashed"
                return
            if self.reachable():
                self.app = "running"
                return
            time.sleep(0.5)
        self.app = "running" if self.reachable() else "crashed"

    def stop_app(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(5)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        self.proc = None

    def reachable(self):
        try:
            urllib.request.urlopen(self.target + "/", timeout=2)
            return True
        except urllib.error.HTTPError:
            return True                    # it answered, even if with an error page
        except Exception:
            return False

    def app_log(self):
        try:
            return "\n".join((self.P.o / "preview.out").read_text(errors="replace").splitlines()[-12:])
        except OSError:
            return ""

    REVIVE_AFTER = 10.0                    # seconds a dead app waits before the next automatic restart
    REVIVE_MAX = 3                         # then stop, so a crash loop does not spin forever

    def should_revive(self, now=None):
        """A dead app is started again, up to REVIVE_MAX times per commit, so a killed or crashed app does not stay down
        until the next commit. (IslandSurvivor's app died after the last task and the preview just said 'not answering'.)"""
        now = time.time() if now is None else now
        return bool(self.cmd and self.proc is not None and self.proc.poll() is not None
                    and self.retries < self.REVIVE_MAX and now - self.started >= self.REVIVE_AFTER)

    def watch(self):
        """Restart the app whenever a task is committed (git HEAD moves). Mid-task edits are never shown."""
        while True:
            time.sleep(3)
            h = self.git_head()
            died = self.cmd and self.proc is not None and self.proc.poll() is not None and self.app == "running"
            if died:
                self.app = "crashed"
            if h and h != self.head:
                self.head = h
                self.retries = 0
                with self.lock:
                    self.stop_app()
                    self.start_app()
            elif self.should_revive():
                self.retries += 1
                orca.log("preview: your app was not running, starting it again (%d of %d)" % (self.retries, self.REVIVE_MAX))
                with self.lock:
                    self.stop_app()
                    self.start_app()

    # ---- status for the badge
    def status(self):
        try:
            st = json.loads(self.P.rd("state.json"))
        except ValueError:
            st = {}
        phase = str(st.get("phase") or "idle")
        import orca_design
        if orca_design.stage(self.P) == "design" and self.app not in ("crashed", "restarting"):
            pr, o, applied = orca_design.summary_lines(self.P)
            busy = orca.running_pid(self.P)
            line = ("Claude is working: %s" % phase.replace("design: ", "")[:60] if busy else
                    "PROTOTYPE v%s%s. Feedback adds a design note (%d open)" % (
                        pr.get("revision", 0), (" (applied %s)" % ", ".join(pr["applied"])) if pr.get("applied") else "", o))
            return {"head": self.head, "app": self.app, "line": line, "app_log": ""}
        if self.app == "crashed":
            line = "your app crashed on the last restart (log below)"
        elif self.app == "restarting":
            line = "restarting your app with the latest code"
        elif orca.running_pid(self.P) and st.get("task"):
            line = "building %s: %s" % (st.get("task"), str(st.get("title") or "")[:60])
        else:
            line = "build idle (%s)" % phase[:40]
        tasks = self.P.tasks()
        done = sum(1 for t in tasks if t["mark"] == "x")
        return {"head": self.head, "app": self.app, "line": "%s  (%d/%d)" % (line, done, len(tasks)),
                "app_log": self.app_log() if self.app == "crashed" else ""}

    def feedback(self, text):
        text = " ".join(str(text or "").split())[:400]
        if len(text) < 3:
            return {"ok": False, "error": "empty"}
        import orca_design
        if orca_design.stage(self.P) == "design":
            ids = orca_design.add_note(self.P, text)
            orca.log("design note from the preview: %s: %s" % (", ".join(ids), text[:100]))
            return {"ok": True, "id": ids[0], "msg": "Added %s. In the dashboard: (b) Claude applies notes now, "
                                                     "(h) hand off and Qwen gets them as first tasks" % ", ".join(ids)}
        tid = add_feedback_task(self.P, text)
        orca.log("feedback from the preview added as %s: %s" % (tid, text[:100]))
        return {"ok": True, "id": tid}


FEEDBACK_DONE = ("done when: the SOURCE CODE is changed for exactly this feedback (a pass with no code change does not count) and a new or "
                 "changed test fails without that change and passes with it; at most 3 tests")


def split_feedback(text):
    """'1. fix A 2. add B' becomes two tasks. One task per request keeps each small and checkable."""
    parts = [p.strip(" ;,") for p in re.split(r"(?:^|\s)\d+[.)]\s+", text)]
    parts = [p for p in parts if len(p) >= 3]
    return parts if len(parts) >= 2 else [text]


def add_feedback_task(P, text):
    """One F task per request, at the front of the open tasks, committed on its own so a blocked-task rollback cannot erase
    it. Returns the first id."""
    ids = []
    for part in split_feedback(text):
        ids.append(_add_one_feedback(P, part))
    return ids[0]


def _add_one_feedback(P, text):
    lines = P.rd("TASKS.md").splitlines()
    n = len([1 for t in P.tasks() if t["id"].startswith("F")])
    tid = "F%02d" % (n + 1)
    new = "- [ ] %s: %s | %s" % (tid, text, FEEDBACK_DONE)
    idx = next((i for i, l in enumerate(lines) if P.TASK_RE.match(l) and l.startswith("- [ ]")), len(lines))
    st = {}
    try:
        st = json.loads(P.rd("state.json"))
    except ValueError:
        pass
    # Never insert before the task that is running right now, it keeps its place.
    if orca.running_pid(P) and st.get("task") and idx < len(lines) and re.match(r"- \[ \] %s:" % re.escape(st["task"]), lines[idx]):
        idx += 1
    while idx < len(lines) and re.match(r"- \[ \] F\d+:", lines[idx]):
        idx += 1                           # first come, first served among feedback tasks
    P.wr("TASKS.md", "\n".join(lines[:idx] + [new] + lines[idx:]) + "\n")
    rel = "%s/TASKS.md" % P.o.name
    P.git("add -- %s" % rel)
    P.git("commit -qm %s -- %s" % (json.dumps("feedback: %s" % tid), rel))
    return tid


def make_handler(pv):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def _orca(self):
            if self.path.startswith("/__orca/live.js"):
                return self._send(200, LIVE_JS, "application/javascript")
            if self.path.startswith("/__orca/status"):
                return self._send(200, json.dumps(pv.status()))
            if self.path.startswith("/__orca/feedback") and self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    data = {}
                return self._send(200, json.dumps(pv.feedback(data.get("text"))))
            return self._send(404, json.dumps({"error": "not found"}))

        def _proxy(self):
            if self.path.startswith("/__orca/"):
                return self._orca()
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP and k.lower() not in ("host", "accept-encoding")}
            req = urllib.request.Request(pv.target + self.path, data=body, headers=hdrs, method=self.command)
            try:
                r = urllib.request.urlopen(req, timeout=600)
            except urllib.error.HTTPError as e:
                r = e
            except Exception as e:
                msg = ("<html><body style='font:16px system-ui;padding:40px;background:#111827;color:#e5e7eb'>"
                       "<h2>Your app is not answering yet</h2><p>%s</p><p>This page retries every 2 seconds.</p>"
                       "<script src='/__orca/live.js'></script><script>setTimeout(function(){location.reload()},2000)</script>"
                       "</body></html>") % str(e).replace("<", "&lt;")
                return self._send(502, msg, "text/html; charset=utf-8")
            ctype = r.headers.get("Content-Type", "")
            self.send_response(r.status if hasattr(r, "status") else r.code)
            for k, v in r.headers.items():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            if "text/html" in ctype:
                html = r.read()
                tag = b"<script src=\"/__orca/live.js\"></script>"
                i = html.lower().rfind(b"</body>")
                html = html[:i] + tag + html[i:] if i != -1 else html + tag
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)
                return
            # Everything else is streamed through as it arrives (works for streaming LLM replies too).
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _proxy
    return H


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run(P):
    pv = Preview(P)
    if not pv.target:
        sys.exit('Set "preview_url" in orca.json (the URL your app answers on, e.g. "http://localhost:8080"),\n'
                 'and "preview_cmd" to start it (e.g. "node server.js"). Leave preview_cmd out if you start the app yourself.')
    P.wr("preview.pid", "%d %d" % (os.getpid(), pv.port))
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))   # a plain `kill` also stops your app and cleans up
    srv = Server(("127.0.0.1", pv.port), make_handler(pv))
    print("starting your app: %s" % (pv.cmd or "(not managed, start it yourself)"), flush=True)
    pv.start_app()
    print("app: %s. Preview with live reload and feedback: http://localhost:%d" % (pv.app, pv.port), flush=True)
    orca.log("preview on http://localhost:%d (app %s)" % (pv.port, pv.app))
    threading.Thread(target=pv.watch, daemon=True).start()
    try:
        srv.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        pv.stop_app()
        try:
            (P.o / "preview.pid").unlink()
        except OSError:
            pass
