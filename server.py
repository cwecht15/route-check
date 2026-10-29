#!/usr/bin/env python3
"""Route Check web front end: a small phone page that runs route_check.py and shows the map.

    python server.py                 http://127.0.0.1:8000 (this PC only; Tailscale serve shares it)
    python server.py --lan           also reachable from other devices on the home Wi-Fi
    python server.py --port 8080

Pages:  /  pick a direction  ->  /check (spinner)  ->  the interactive map in reports\\.
serve.bat starts this and shares it over Tailscale (README.md explains the one-time steps).
Standard library only; route_check.py does the TomTom work.  One check runs at a time.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

HERE = Path(__file__).resolve().parent
ROUTE_CHECK = HERE / "route_check.py"
REPORT_DIR = HERE / "reports"
CACHE_DIR = HERE / "cache"
LOG_FILE = CACHE_DIR / "server.log"

DIRS = {"there": "Hospital \u2192 Home", "back": "Home \u2192 Hospital"}   # generic fallback
KINDS = {"map": "html", "pdf": "pdf", "txt": "txt"}
CTYPES = {"html": "text/html; charset=utf-8", "pdf": "application/pdf",
          "txt": "text/plain; charset=utf-8"}
NAME_RE = re.compile(r"^route_(there|back)_(\d{4}-\d{2}-\d{2}_\d{4})\.(html|pdf|txt)$")
STEM_RE = re.compile(r"^route_(there|back)_(\d{4}-\d{2}-\d{2}_\d{4})$")
KEEP_PER_DIR = 30            # report stems kept per direction; older .html/.pdf/.txt are deleted
RUN_TIMEOUT = 150            # seconds before a route_check.py run is given up on
MAX_BODY = 16_384            # bytes accepted in a POST body
MAX_AT = 80                  # characters accepted in the "leaving" box
TAIL_CHARS = 1500            # how much of the output an error reply carries
TAILSCALE_EXE = Path(r"C:\Program Files\Tailscale\tailscale.exe")

def place_labels() -> tuple[str, str]:
    """(hospital label, home label) from route_check.load_places() (.env / environment), read
    fresh on every page so an edit to .env shows up; "Hospital" / "Home" if not configured."""
    try:
        import route_check
        places = route_check.load_places()
        return places["hospital"][0], places["home"][0]
    except Exception:
        return "Hospital", "Home"


def dir_names() -> dict[str, str]:
    hosp, home = place_labels()
    return {"there": f"{hosp} \u2192 {home}", "back": f"{home} \u2192 {hosp}"}


# ---- report files ----------------------------------------------------------------------------

def stem_time(stem: str) -> datetime:
    return datetime.strptime(STEM_RE.match(stem).group(2), "%Y-%m-%d_%H%M")


def report_stems(direction: str) -> list[str]:
    """Every report stem for a direction, newest first (the names sort by date and time)."""
    stems = set()
    if REPORT_DIR.is_dir():
        for p in REPORT_DIR.iterdir():
            m = NAME_RE.match(p.name)
            if m and m.group(1) == direction and p.is_file():
                stems.add(p.name.rsplit(".", 1)[0])
    return sorted(stems, reverse=True)


def newest_file(direction: str, ext: str) -> str | None:
    for stem in report_stems(direction):
        if (REPORT_DIR / f"{stem}.{ext}").is_file():
            return f"{stem}.{ext}"
    return None


def prune(direction: str, keep: int = KEEP_PER_DIR) -> list[str]:
    """Keep the newest `keep` stems of a direction; delete the older files.  Returns what went."""
    gone = []
    for stem in report_stems(direction)[keep:]:
        for ext in ("html", "pdf", "txt"):
            p = REPORT_DIR / f"{stem}.{ext}"
            try:
                if p.is_file():
                    p.unlink()
                    gone.append(p.name)
            except OSError:
                pass             # open in a viewer, say; next run tries again
    return gone


def ago(then: datetime, now: datetime) -> str:
    mins = int((now - then).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} days ago"


def clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def last_checked(direction: str, now: datetime) -> dict | None:
    stems = report_stems(direction)
    if not stems:
        return None
    stem = stems[0]
    t = stem_time(stem)
    when = clock(t) if t.date() == now.date() else f"{t:%b} {t.day}, {clock(t)}"
    return {"stem": stem, "time": t.isoformat(), "label": f"Last checked {when}, {ago(t, now)}",
            "map": (REPORT_DIR / f"{stem}.html").is_file(),
            "pdf": (REPORT_DIR / f"{stem}.pdf").is_file(),
            "txt": (REPORT_DIR / f"{stem}.txt").is_file()}

# ---- running route_check.py ------------------------------------------------------------------

class Job:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: dict = {}


_run_lock = threading.Lock()        # one route_check.py at a time
_state_lock = threading.Lock()      # guards _inflight / _running
_inflight: dict[tuple, Job] = {}    # (dir, at, no_tolls) -> the job doing exactly that
_running: str | None = None         # direction being checked right now


def tail(text: str) -> str:
    text = re.sub(r"(?i)(key=)[A-Za-z0-9_-]+", r"\1***", text.strip())   # never echo an API key
    return text[-TAIL_CHARS:] if len(text) > TAIL_CHARS else text


def _run_route_check(direction: str, at: str, no_tolls: bool) -> dict:
    cmd = [sys.executable, str(ROUTE_CHECK), direction, "--pdf", "--map", "--json", "--no-open"]
    cmd += [f"--at={at}"] if at else ["--now"]          # --at=... so a leading "-" stays a value
    if no_tolls:
        cmd.append("--no-tolls")
    try:
        cp = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=RUN_TIMEOUT, stdin=subprocess.DEVNULL,
                            env=_child_env())
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return {"ok": False, "error": f"The check took longer than {RUN_TIMEOUT} s and was "
                                      f"stopped.\n{tail(out)}"}
    except OSError as e:
        return {"ok": False, "error": f"Could not start route_check.py: {e}"}
    out = cp.stdout or ""
    both = out + ("\n" + cp.stderr if cp.stderr else "")
    if cp.returncode != 0:
        return {"ok": False, "error": tail(both) or f"route_check.py exited with code {cp.returncode}."}
    m = re.search(r"^Map saved: (.+?)\s*$", out, re.M)
    name = Path(m.group(1)).name if m else ""
    if not NAME_RE.match(name):
        return {"ok": False, "error": "The check ran but no map was written.\n" + tail(both)}
    stem = name.rsplit(".", 1)[0]
    p = re.search(r"^PDF saved: (.+?)\s*$", out, re.M)
    pdf = Path(p.group(1)).name if p else ""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{stem}.txt").write_text(out, encoding="utf-8")
    prune(direction)
    return {"ok": True, "dir": direction, "map": f"/reports/{stem}.html",
            "pdf": f"/reports/{pdf}" if NAME_RE.match(pdf) else None,
            "txt": f"/reports/{stem}.txt", "text": f"/text/{stem}.txt",
            "generated": stem_time(stem).isoformat()}


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_check(direction: str, at: str, no_tolls: bool) -> dict:
    """Run one check, or join an identical one already in progress and share its result."""
    global _running
    key = (direction, at.lower(), no_tolls)
    with _state_lock:
        job = _inflight.get(key)
        owner = job is None
        if owner:
            job = _inflight[key] = Job()
    if not owner:
        job.done.wait()
        return dict(job.result, joined=True)
    try:
        with _run_lock:
            with _state_lock:
                _running = direction
            try:
                job.result = _run_route_check(direction, at, no_tolls)
            except Exception as e:          # never leave the waiters hanging
                job.result = {"ok": False, "error": f"Server error: {type(e).__name__}: {e}"}
            finally:
                with _state_lock:
                    _running = None
    finally:
        with _state_lock:
            _inflight.pop(key, None)
        job.done.set()
    return job.result


def validate(direction, at, no_tolls) -> tuple[str, str, bool] | str:
    """Returns the cleaned (dir, at, no_tolls), or an error message."""
    if direction not in DIRS:
        return "Pick a direction: there or back."
    at = at if isinstance(at, str) else ""
    at = " ".join(at.split())
    if len(at) > MAX_AT:
        return "That departure time is too long."
    if at.lower() in ("now", "leaving now", "leave now"):
        at = ""
    return direction, at, bool(no_tolls)

# ---- page templates (filled with .replace) ---------------------------------------------------

STYLE = """
:root { --bg:#f3f5f8; --surface:#ffffff; --text:#14181f; --muted:#5a6473; --border:#d5dbe3;
  --accent:#1859c9; --accent-2:#0f7a55; --accent-text:#ffffff; --danger:#b3261e; --code:#eef1f5; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1318; --surface:#1a2028; --text:#e8ecf1; --muted:#9aa5b4; --border:#2e3742;
    --accent:#4c8dff; --accent-2:#2fb37f; --accent-text:#08111f; --danger:#ff8a80; --code:#141a21; }
}
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:var(--bg); color:var(--text);
  font:18px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width:560px; margin:0 auto; padding:20px 16px 40px; }
h1 { font-size:28px; margin:4px 0 2px; letter-spacing:-0.01em; }
.sub { color:var(--muted); margin:0 0 20px; font-size:16px; }
a { color:var(--accent); }
.card { background:var(--surface); border:1px solid var(--border); border-radius:14px;
  padding:16px; margin:0 0 16px; }
label.field { display:block; font-weight:600; margin:0 0 6px; }
input[type=text] { width:100%; font:inherit; font-size:18px; padding:12px 12px; min-height:52px;
  border:1px solid var(--border); border-radius:10px; background:var(--bg); color:var(--text); }
input[type=text]::placeholder { color:var(--muted); opacity:1; font-size:14px; }
.hint { color:var(--muted); font-size:15px; margin:6px 2px 0; }
.check { display:flex; align-items:center; gap:12px; margin-top:14px; min-height:44px; }
.check input { width:26px; height:26px; margin:0; accent-color:var(--accent); }
button.go { display:block; width:100%; min-height:64px; border:0; border-radius:12px;
  font:inherit; font-size:22px; font-weight:700; color:var(--accent-text); background:var(--accent);
  cursor:pointer; padding:12px; }
button.go.back { background:var(--accent-2); }
button.go:active { transform:translateY(1px); filter:brightness(0.95); }
.last { margin:10px 2px 0; color:var(--muted); font-size:16px; }
.links { display:flex; gap:10px; margin-top:8px; flex-wrap:wrap; }
.links a, .links span { flex:1 1 0; min-width:80px; text-align:center; padding:10px 6px;
  border:1px solid var(--border); border-radius:10px; text-decoration:none; font-weight:600;
  min-height:44px; }
.links span { color:var(--muted); opacity:0.6; }
.busy { background:var(--code); border-radius:10px; padding:10px 12px; font-size:16px; }
.foot { color:var(--muted); font-size:14px; margin-top:24px; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; word-break:break-word; font-size:13px;
  line-height:1.4; background:var(--code); border:1px solid var(--border); border-radius:10px;
  padding:12px; margin:0; font-family:ui-monospace, Consolas, "Cascadia Mono", monospace; }
.err { color:var(--danger); font-weight:600; }
.spinner { width:64px; height:64px; margin:28px auto 18px; border-radius:50%;
  border:6px solid var(--border); border-top-color:var(--accent); animation:spin 0.9s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.center { text-align:center; }
.btnlink { display:inline-block; margin-top:16px; padding:12px 20px; border-radius:10px;
  background:var(--accent); color:var(--accent-text); text-decoration:none; font-weight:700; }
"""

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#1859c9">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Route Check">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%231859c9'/%3E%3Cpath d='M18 46 C18 30 46 34 46 18' stroke='white' stroke-width='6' fill='none' stroke-linecap='round'/%3E%3Ccircle cx='18' cy='46' r='5' fill='white'/%3E%3Ccircle cx='46' cy='18' r='5' fill='white'/%3E%3C/svg%3E">
<title>{{TITLE}}</title>
<style>{{STYLE}}</style>
</head>
"""

INDEX_PAGE = """<body>
<main>
<h1>Route Check</h1>
<p class="sub">Live traffic, accidents and closures &middot; TomTom + 511NJ</p>
<form action="/check" method="get">
<div class="card">
<label class="field" for="at">Leaving</label>
<input type="text" id="at" name="at" placeholder="leaving now, or e.g. 5:30pm, tomorrow 7am"
  autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="go" maxlength="80">
<p class="hint">Leave it empty for now. Also: 17:30, fri 6:15am</p>
<label class="check"><input type="checkbox" name="tolls" value="no"> Avoid tolls</label>
</div>
{{BUSY}}
<div class="card">
<button class="go" type="submit" name="dir" value="there">{{NAME_THERE}}</button>
{{LAST_THERE}}
</div>
<div class="card">
<button class="go back" type="submit" name="dir" value="back">{{NAME_BACK}}</button>
{{LAST_BACK}}
</div>
</form>
<p class="foot">A check takes about half a minute; around 80 a day are free.</p>
</main>
</body>
</html>
"""

LAST_BLOCK = """<p class="last">{{LABEL}}</p>
<div class="links">{{LINKS}}</div>"""

CHECK_PAGE = """<body>
<main class="center">
<h1>Checking traffic&hellip;</h1>
<p class="sub" id="what">{{DIRNAME}}{{DETAIL}}</p>
<div class="spinner" id="spin" aria-hidden="true"></div>
<p id="msg">Asking TomTom and 511NJ. This usually takes 15&ndash;40 seconds.</p>
<p class="sub" id="secs"></p>
<div id="fail" hidden>
<p class="err">The check did not work.</p>
<pre id="errtext"></pre>
<a class="btnlink" href="/">Back</a>
</div>
<noscript>
<form action="/run" method="post">
<input type="hidden" name="dir" value="{{DIR}}">
<input type="hidden" name="at" value="{{AT}}">
<input type="hidden" name="tolls" value="{{TOLLS}}">
<p>JavaScript is off.</p>
<button class="go" type="submit">Run the check</button>
</form>
</noscript>
<p class="foot"><a href="/">Back to the start</a></p>
</main>
<script>
(function () {
  var job = {{JOB}};
  var t0 = Date.now();
  var secs = document.getElementById("secs");
  var tick = setInterval(function () {
    secs.textContent = Math.round((Date.now() - t0) / 1000) + " s";
  }, 1000);
  function fail(text) {
    clearInterval(tick);
    document.getElementById("spin").hidden = true;
    document.getElementById("msg").hidden = true;
    secs.textContent = "";
    document.getElementById("errtext").textContent = text || "Unknown error.";
    document.getElementById("fail").hidden = false;
  }
  fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(job)
  }).then(function (r) {
    return r.json();
  }).then(function (res) {
    if (res && res.ok && res.map) {
      clearInterval(tick);
      document.getElementById("msg").textContent = "Done. Opening the map\\u2026";
      location.replace(res.map);
    } else {
      fail(res && res.error);
    }
  }).catch(function (e) {
    fail("Could not reach the Route Check PC (" + e + "). Is it on and online?");
  });
})();
</script>
</body>
</html>
"""

TEXT_PAGE = """<body>
<main style="max-width:900px">
<p><a href="/">&larr; Back</a> &nbsp; {{LINKS}}</p>
<h1 style="font-size:22px">{{HEADING}}</h1>
<pre>{{TEXT}}</pre>
</main>
</body>
</html>
"""

MESSAGE_PAGE = """<body>
<main>
<h1>{{HEADING}}</h1>
<pre class="err">{{TEXT}}</pre>
<a class="btnlink" href="/">Back</a>
</main>
</body>
</html>
"""


# Floating link bar injected into a served map, before </body>.  The map (route_check.py) has
# its panel top-left on desktop (12 px + 430 px wide, z-index 1000) and at the bottom under
# 701 px; zoom and layer controls are top-right (Leaflet z-index 1000).  So the bar sits at the
# top between the panel (desktop) or the left edge (phone) and the controls.  z-index 900 keeps
# it above the map and under the panel and controls.  Leaflet popups live inside the map pane's
# own stacking context (z-index 400), so no element outside the map can sit between the tiles
# and a popup; the bar is small and the map pans, so a popup near the top can be dragged clear.
MAP_BAR = """
<style>
.rcbar-wrap { position:fixed; z-index:900; top:max(10px, env(safe-area-inset-top)); left:10px;
  right:56px; display:flex; justify-content:center; pointer-events:none; }
.rcbar { display:flex; pointer-events:auto; max-width:100%; overflow:hidden; border-radius:10px;
  background:rgba(255,255,255,0.96); border:1px solid #c9d1db; box-shadow:0 2px 10px rgba(0,0,0,0.2);
  font:600 14px/1 system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
.rcbar a { display:flex; align-items:center; min-height:42px; padding:0 12px; color:#1a5fb4;
  text-decoration:none; white-space:nowrap; }
.rcbar a + a { border-left:1px solid #dde3ea; }
.rcbar a:hover, .rcbar a:focus-visible { background:#e3eefc; }
@media (prefers-color-scheme: dark) {
  .rcbar { background:rgba(30,34,40,0.96); border-color:#3a434d; box-shadow:0 2px 10px rgba(0,0,0,0.55); }
  .rcbar a { color:#8ab4f8; }
  .rcbar a + a { border-left-color:#3a434d; }
  .rcbar a:hover, .rcbar a:focus-visible { background:#1f3654; }
}
@media (min-width: 701px) { .rcbar-wrap { left:454px; } }
@media (min-width: 701px) and (max-width: 760px) { .rcbar a { padding:0 7px; font-size:13px; } }
@media print { .rcbar-wrap { display:none; } }
</style>
<nav class="rcbar-wrap" aria-label="Route Check"><div class="rcbar">{{LINKS}}</div></nav>
"""


def page(title: str, body: str) -> str:
    return HEAD.replace("{{TITLE}}", html.escape(title)).replace("{{STYLE}}", STYLE) + body


def links_for(stem: str, have: dict) -> str:
    s = html.escape(quote(stem))
    parts = []
    for label, kind, href in (("Map", "map", f"/reports/{s}.html"), ("PDF", "pdf", f"/reports/{s}.pdf"),
                              ("Text", "txt", f"/text/{s}.txt")):
        parts.append(f'<a href="{href}">{label}</a>' if have.get(kind) else f"<span>{label}</span>")
    return "".join(parts)


def map_bar(stem: str) -> str:
    """The Home / PDF / Text / Refresh bar for a map; `stem` has already matched STEM_RE."""
    s = html.escape(quote(stem))
    links = [("Home", "/"), ("PDF", f"/reports/{s}.pdf")]
    if (REPORT_DIR / f"{stem}.txt").is_file():
        links.append(("Text", f"/text/{s}.txt"))
    links.append(("Refresh", "/check?dir=" + html.escape(quote(STEM_RE.match(stem).group(1)))))
    return MAP_BAR.replace("{{LINKS}}", "".join(f'<a href="{h}">{t}</a>' for t, h in links))


def with_map_bar(data: bytes, stem: str) -> bytes:
    """The map HTML with the link bar added before its last </body> (in memory only)."""
    bar = map_bar(stem).encode("utf-8")
    i = data.rfind(b"</body>")
    return data[:i] + bar + data[i:] if i >= 0 else data + bar


def index_html() -> str:
    now = datetime.now()
    names = dir_names()
    body = INDEX_PAGE
    for direction in DIRS:
        body = body.replace("{{NAME_" + direction.upper() + "}}", html.escape(names[direction]))
        info = last_checked(direction, now)
        if info is None:
            block = LAST_BLOCK.replace("{{LABEL}}", "Last checked: not yet").replace("{{LINKS}}", "")
            block = block.replace('<div class="links"></div>', "")
        else:
            block = (LAST_BLOCK.replace("{{LABEL}}", html.escape(info["label"]))
                     .replace("{{LINKS}}", links_for(info["stem"], info)))
        body = body.replace("{{LAST_" + direction.upper() + "}}", block)
    with _state_lock:
        running = _running
    busy = (f'<p class="busy">A check is running now ({html.escape(names[running])}). '
            "Yours will start right after it.</p>") if running else ""
    return page("Route Check", body.replace("{{BUSY}}", busy))


def check_html(direction: str, at: str, no_tolls: bool) -> str:
    detail = ""
    if at:
        detail += f" &middot; leaving {html.escape(at)}"
    if no_tolls:
        detail += " &middot; no tolls"
    job = json.dumps({"dir": direction, "at": at, "no_tolls": no_tolls})
    job = job.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    body = (CHECK_PAGE.replace("{{DIRNAME}}", html.escape(dir_names()[direction])).replace("{{DETAIL}}", detail)
            .replace("{{DIR}}", html.escape(direction, quote=True))
            .replace("{{AT}}", html.escape(at, quote=True))
            .replace("{{TOLLS}}", "no" if no_tolls else "")
            .replace("{{JOB}}", job))
    return page("Checking traffic\u2026 \u2013 Route Check", body)


def message_html(heading: str, text: str) -> str:
    body = MESSAGE_PAGE.replace("{{HEADING}}", html.escape(heading)).replace("{{TEXT}}", html.escape(text))
    return page(heading + " \u2013 Route Check", body)

# ---- HTTP ------------------------------------------------------------------------------------

_log_lock = threading.Lock()


def log_line(text: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text}"
    with _log_lock:
        print(line, flush=True)
        try:
            CACHE_DIR.mkdir(exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "RouteCheck/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing
    def log_message(self, fmt: str, *args) -> None:
        who = self.headers.get("Tailscale-User-Login") if getattr(self, "headers", None) else None
        log_line(f"{self.client_address[0]}{' ' + who if who else ''}  {fmt % args}")

    def send(self, status: int, body: bytes | str, ctype: str = "text/html; charset=utf-8",
             headers: dict | None = None) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def html_page(self, text: str, status: int = 200) -> None:
        self.send(status, text, headers={"Cache-Control": "no-store"})

    def json_reply(self, obj: dict, status: int = 200) -> None:
        self.send(status, json.dumps(obj), "application/json; charset=utf-8",
                  {"Cache-Control": "no-store"})

    def redirect(self, where: str, status: int = 302) -> None:
        self.send(status, b"", "text/plain; charset=utf-8",
                  {"Location": where, "Cache-Control": "no-store"})

    def not_found(self) -> None:
        self.send(404, message_html("Not found", "There is nothing at this address."),
                  headers={"Cache-Control": "no-store"})

    def read_body(self) -> bytes | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n < 0 or n > MAX_BODY:
            return None
        return self.rfile.read(n) if n else b""

    # -- routes
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self) -> None:
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _get(self) -> None:
        url = urlsplit(self.path)
        path, q = url.path, parse_qs(url.query)
        first = lambda k: (q.get(k) or [""])[0]
        if path in ("/", "/index.html"):
            return self.html_page(index_html())
        if path == "/check":
            v = validate(first("dir"), first("at"), first("tolls") == "no")
            if isinstance(v, str):
                return self.html_page(message_html("Route Check", v), 400)
            return self.html_page(check_html(*v))
        if path.startswith("/reports/"):
            return self.serve_report(path[len("/reports/"):])
        if path.startswith("/text/"):
            return self.serve_text(path[len("/text/"):])
        if path == "/latest":
            direction, ext = first("dir"), KINDS.get(first("kind") or "map")
            name = newest_file(direction, ext) if direction in DIRS and ext else None
            return self.redirect(f"/reports/{name}") if name else self.not_found()
        if path == "/api/status":
            now = datetime.now()
            with _state_lock:
                running = _running
            out = {d: last_checked(d, now) for d in DIRS}
            return self.json_reply({**out, "running": running is not None, "running_dir": running})
        if path == "/favicon.ico":
            return self.send(204, b"", headers={"Cache-Control": "max-age=86400"})
        return self.not_found()

    def _post(self) -> None:
        path = urlsplit(self.path).path
        if path not in ("/api/run", "/run"):
            return self.not_found()
        raw = self.read_body()
        if raw is None:
            return self.json_reply({"ok": False, "error": "Request too large."}, 413)
        if path == "/api/run":
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(data, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                return self.json_reply({"ok": False, "error": "Bad request (not JSON)."}, 400)
            nt = data.get("no_tolls")
            v = validate(data.get("dir"), data.get("at") or "", nt is True or nt in ("1", "true", "yes"))
            if isinstance(v, str):
                return self.json_reply({"ok": False, "error": v}, 400)
            return self.json_reply(run_check(*v))
        form = parse_qs(raw.decode("utf-8", "replace"))
        first = lambda k: (form.get(k) or [""])[0]
        v = validate(first("dir"), first("at"), first("tolls") == "no")
        if isinstance(v, str):
            return self.html_page(message_html("Route Check", v), 400)
        res = run_check(*v)
        if res.get("ok"):
            return self.redirect(res["map"], 303)
        return self.html_page(message_html("The check did not work", res.get("error") or ""), 502)

    def serve_report(self, name: str) -> None:
        # The raw (still %-encoded) name must match, so ..%2F and friends never get through.
        m = NAME_RE.match(name)
        p = REPORT_DIR / name if m else None
        if p is None or not p.is_file():
            return self.not_found()
        ext = m.group(3)
        headers = {"Cache-Control": "no-cache"}
        if ext == "pdf":
            headers["Content-Disposition"] = f'inline; filename="{name}"'
        try:
            data = p.read_bytes()
        except OSError:
            return self.not_found()
        if ext == "html":                   # map: add the link bar, which depends on the .txt
            data = with_map_bar(data, name[:-5])
            headers["Cache-Control"] = "no-store"
        self.send(200, data, CTYPES[ext], headers)

    def serve_text(self, name: str) -> None:
        m = NAME_RE.match(name)
        if not m or m.group(3) != "txt" or not (REPORT_DIR / name).is_file():
            return self.not_found()
        stem = name[:-4]
        try:
            text = (REPORT_DIR / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return self.not_found()
        have = {"map": (REPORT_DIR / f"{stem}.html").is_file(), "pdf": (REPORT_DIR / f"{stem}.pdf").is_file()}
        links = " &nbsp; ".join(f'<a href="/reports/{html.escape(stem)}.{ext}">{label}</a>'
                                for label, kind, ext in (("Map", "map", "html"), ("PDF", "pdf", "pdf"))
                                if have[kind])
        t = stem_time(stem)
        heading = f"{dir_names()[m.group(1)]} \u00b7 {t:%a %b} {t.day}, {clock(t)}"
        body = (TEXT_PAGE.replace("{{LINKS}}", links).replace("{{HEADING}}", html.escape(heading))
                .replace("{{TEXT}}", html.escape(text)))
        self.html_page(page(heading + " \u2013 Route Check", body))

# ---- startup ---------------------------------------------------------------------------------

def tailscale_exe() -> str | None:
    if TAILSCALE_EXE.is_file():
        return str(TAILSCALE_EXE)
    return shutil.which("tailscale")


def tailscale_info() -> list[str]:
    """Read-only look at Tailscale: this PC's tailnet IP and the https address `serve` shares."""
    exe = tailscale_exe()
    if not exe:
        return ["Tailscale is not installed, so only this PC (or --lan devices) can reach it. "
                "README.md: 'Sharing it with another phone'."]
    def ts(*a: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run([exe, *a], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=8, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
    ip = ts("ip", "-4")
    ip4 = (ip.stdout.strip().splitlines() or [""])[0] if ip and ip.returncode == 0 else ""
    if not ip4:
        return ["Tailscale is installed but not signed in: open the Tailscale app and log in, "
                "then run serve.bat."]
    lines = [f"Tailscale IP:     {ip4}"]
    st = ts("serve", "status")
    urls = re.findall(r"https://[^\s()]+", st.stdout if st else "")
    if urls:
        lines.append(f"Phones (Tailscale): {urls[0].rstrip('/')}")
    else:
        lines.append("Tailscale serve is not sharing this yet: start it with serve.bat.")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    ap.add_argument("--lan", action="store_true",
                    help="listen on all interfaces (home Wi-Fi); Windows Firewall asks once")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    host = "0.0.0.0" if args.lan else args.host
    REPORT_DIR.mkdir(exist_ok=True)
    CACHE_DIR.mkdir(exist_ok=True)
    try:
        httpd = ThreadingHTTPServer((host, args.port), Handler)
    except OSError as e:
        print(f"Could not listen on {host}:{args.port} ({e}). Is the server already running?")
        return 1
    httpd.daemon_threads = True
    print()
    print("ROUTE CHECK server")
    print(f"This PC:          http://127.0.0.1:{args.port}/")
    if args.lan:
        try:
            lan_ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            lan_ip = "<this-pc-ip>"
        print(f"Home Wi-Fi:       http://{lan_ip}:{args.port}/")
    for line in tailscale_info():
        print(line)
    print("Close this window (or Ctrl+C) to stop.")
    print()
    log_line(f"server started on {host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        log_line("server stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
