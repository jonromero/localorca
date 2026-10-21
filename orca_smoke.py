"""Real app smoke test. Starts the app for real, opens it in a headless browser, fails if it is broken.

    python3 orca_smoke.py DIR           (Orca runs this after test_cmd on every task once the walking skeleton exists)

Reads preview_cmd and preview_url from DIR/orca.json. Starts the app on a free port (PORT env), loads the page,
and fails on: the app not answering, any console error, any uncaught JS error, any failed request, or a blank page.
No fakes, no injected state. Exit codes: 0 ok, 1 broken app, 3 cannot run (playwright missing): Orca then skips with a warning.
Setup once: see README (python3 -m venv .smoke-venv, then pip install playwright and playwright install chromium inside it)
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

WAIT_UP = 20            # seconds for the server to answer
SETTLE = 2.0            # seconds the page runs before we look at it
IGNORE = re.compile(r"favicon\.ico|DevTools|GPU stall|swiftshader|WebGL: (INVALID|CONTEXT)|ReadPixels", re.I)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_up(url, secs):
    end = time.time() + secs
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2).read(1)
            return True
        except Exception as e:
            if getattr(e, "code", None):          # an HTTP error page still means the server is up
                return True
            time.sleep(0.4)
    return False


def blank(png):
    """True if the screenshot is one flat colour (a dead page)."""
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(png)).convert("RGB").resize((64, 40))
        return len(set(im.getdata())) < 6
    except ImportError:
        return len(png) < 6000                    # a flat 1280x720 PNG is tiny


def check(url):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SMOKE SKIPPED: playwright is not installed. Run in the LocalOrca folder: python3 -m venv .smoke-venv && .smoke-venv/bin/pip install playwright && .smoke-venv/bin/playwright install chromium")
        return 3
    problems, shot = [], b""
    with sync_playwright() as p:
        try:
            br = p.chromium.launch(args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        except Exception as e:
            print("SMOKE SKIPPED: could not start Chromium (%s). Run: .smoke-venv/bin/playwright install chromium" % str(e).splitlines()[0])
            return 3
        page = br.new_page(viewport={"width": 1280, "height": 720})
        page.on("console", lambda m: m.type == "error" and not IGNORE.search(m.text) and problems.append("console error: " + m.text[:300]))
        page.on("pageerror", lambda e: problems.append("uncaught error: " + str(e)[:300]))
        page.on("requestfailed", lambda r: not IGNORE.search(r.url) and problems.append("request failed: %s" % r.url))
        page.on("response", lambda r: r.status >= 400 and not IGNORE.search(r.url) and problems.append("HTTP %d: %s" % (r.status, r.url)))
        try:
            page.goto(url, wait_until="load", timeout=15000)
            page.wait_for_timeout(int(SETTLE * 1000))
            shot = page.screenshot()
            if os.environ.get("ORCA_SHOT"):                    # the course check shows this picture to Claude
                Path(os.environ["ORCA_SHOT"]).write_bytes(shot)
            if os.environ.get("ORCA_SHOTS"):                   # the design stage: Claude LOOKS at its own work
                take_shots(page, Path(os.environ["ORCA_SHOTS"]), shot)
        except Exception as e:
            problems.append("page did not load: " + str(e).splitlines()[0])
        br.close()
    if shot and blank(shot):
        problems.append("the page is BLANK (one flat colour). Nothing is drawn.")
    if problems:
        print("APP SMOKE TEST FAILED. The real app, started and opened in a browser, is broken:")
        seen = set()
        for x in problems:
            if x not in seen:
                seen.add(x)
                print(" - " + x)
        return 1
    print("smoke ok: app started, page loaded, no errors, something is drawn")
    return 0


def take_shots(page, out, first):
    """Screenshots over time and at two widths, so a layout that jumps while text types in, or breaks on a smaller
    window, is visible. (Real failure: Island 2's thinking box kept resizing; two blind CSS fixes did not fix it.)"""
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    names = []
    (out / "1-at-2s-1280.png").write_bytes(first)
    names.append("1-at-2s-1280.png")
    for name, wait in (("2-at-6s-1280.png", 4000), ("3-at-12s-1280.png", 6000)):
        page.wait_for_timeout(wait)
        page.screenshot(path=str(out / name))
        names.append(name)
    for name, w, h in (("4-wide-1680.png", 1680, 1000), ("5-small-1024.png", 1024, 700)):
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / name))
        names.append(name)
    print("screenshots in %s: %s" % (out, ", ".join(names)))


def main(argv):
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    cfg = json.loads((root / "orca.json").read_text()) if (root / "orca.json").exists() else {}
    cmd, url = cfg.get("preview_cmd") or "", (cfg.get("preview_url") or "").rstrip("/")
    if not url:
        print("SMOKE SKIPPED: set preview_url (and preview_cmd) in orca.json")
        return 3
    proc = None
    if cmd:
        port = free_port()
        if "PORT" in url:                                                   # a placeholder like http://localhost:PORT
            url = url.replace("PORT", str(port))
        elif re.search(r":\d+(?=/|$)", url):
            url = re.sub(r":\d+(?=/|$)", ":%d" % port, url, count=1)
        else:
            url = "%s:%d" % (url, port)
        env = dict(os.environ, PORT=str(port))
        cmd = re.sub(r"\bPORT=\d+", "PORT=%d" % port, cmd)      # a hardcoded "PORT=8080 node server.js" must follow the free port too
        proc = subprocess.Popen(cmd, shell=True, cwd=str(root), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, preexec_fn=os.setsid)
    try:
        if not wait_up(url, WAIT_UP):
            tail = ""
            if proc and proc.poll() is not None:
                tail = (proc.stdout.read() or "")[-1200:]
            print("APP SMOKE TEST FAILED. The app did not answer at %s within %ds. It must read the PORT env variable.\n%s" % (url, WAIT_UP, tail))
            return 1
        return check(url)
    finally:
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main(sys.argv))
