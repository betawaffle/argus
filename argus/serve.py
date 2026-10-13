"""A small page to browse findings and agree with or dismiss them.

Stdlib http.server, one table, no JavaScript. Feedback is stored with the
finding and shown to the model the next time the same service comes up.
"""

import html
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from .patterns import Store

ORDER = "CASE f.verdict WHEN 'attention' THEN 0 WHEN 'watch' THEN 1 WHEN '?' THEN 2 ELSE 3 END"

STYLE = """
:root { --bg:#fff; --fg:#1b1b1b; --muted:#6b6b6b; --line:#e3e3e3;
        --attention:#c0392b; --watch:#b9770e; --ignore:#5f7d5f; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161616; --fg:#e8e8e8; --muted:#9a9a9a; --line:#333;
          --attention:#ff7b6b; --watch:#f2b84b; --ignore:#8fb98f; } }
body { background:var(--bg); color:var(--fg); margin:0 auto; max-width:1100px;
       padding:16px; font:14px/1.45 system-ui, sans-serif; }
table { border-collapse:collapse; width:100%; }
td, th { border-bottom:1px solid var(--line); padding:6px 8px; text-align:left;
         vertical-align:top; }
th { color:var(--muted); font-weight:500; }
code, pre { font:12px/1.4 ui-monospace, monospace; }
pre { white-space:pre-wrap; border:1px solid var(--line); padding:8px; }
.attention { color:var(--attention); font-weight:600; }
.watch { color:var(--watch); font-weight:600; }
.ignore, .baseline { color:var(--ignore); }
.muted { color:var(--muted); }
a { color:inherit; }
form { display:inline; }
button { font:inherit; padding:2px 8px; }
nav a { margin-right:12px; }
"""


def page(title, body):
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport "
            f"content='width=device-width,initial-scale=1'><title>{title}</title>"
            f"<style>{STYLE}</style><h1>argus</h1>{body}").encode()


def esc(x):
    return html.escape(str(x if x is not None else ""))


def when(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""


def feedback_form(fid, current):
    buttons = "".join(
        f"<form method=post action=/feedback><input type=hidden name=id value={fid}>"
        f"<button name=value value={v}{' disabled' if current == v else ''}>{v}</button></form> "
        for v in ("agree", "dismiss"))
    return buttons + (f"<span class=muted>({esc(current)})</span>" if current else "")


class Handler(BaseHTTPRequestHandler):
    store = None

    def send(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            return self.send(self.index(show_all="all" in q))
        if url.path.startswith("/f/"):
            return self.send(self.detail(int(url.path[3:])))
        self.send(page("not found", "<p>Not found.</p>"), 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        fid = int(form["id"][0])
        value = form.get("value", [""])[0]
        note = form.get("note", [None])[0]
        if value in ("agree", "dismiss", ""):
            self.store.db.execute(
                "UPDATE findings SET feedback=?, note=coalesce(?, note) WHERE id=?",
                (value or None, note, fid))
            self.store.db.commit()
        self.send_response(303)
        self.send_header("Location", self.headers.get("Referer") or "/")
        self.end_headers()

    def index(self, show_all):
        # Latest finding per pattern.
        rows = self.store.db.execute(f"""
            SELECT f.id, f.verdict, f.summary, f.feedback, f.t,
                   p.host, p.ident, p.template, p.total, p.last_seen
            FROM findings f JOIN patterns p ON p.id = f.pattern_id
            WHERE f.id = (SELECT max(id) FROM findings WHERE pattern_id = p.id)
              {"" if show_all else "AND f.verdict NOT IN ('baseline', 'ignore') "
                                 "AND coalesce(f.feedback, '') != 'dismiss'"}
            ORDER BY {ORDER}, f.t DESC""").fetchall()
        counts = dict(self.store.db.execute(
            "SELECT verdict, count(*) FROM findings GROUP BY verdict").fetchall())
        nav = (f"<nav><a href=/>open findings</a><a href=/?all>everything</a>"
               f"<span class=muted>" + ", ".join(f"{v}: {c}" for v, c in sorted(counts.items()))
               + "</span></nav>")
        body = "".join(
            f"<tr><td class={esc(v)}>{esc(v)}</td>"
            f"<td><a href=/f/{fid}>{esc(summary) or '<span class=muted>(no summary)</span>'}</a>"
            f"<br><code class=muted>{esc(host)} {esc(ident)}: {esc(tmpl)}</code></td>"
            f"<td>{total}<br><span class=muted>{when(last)}</span></td>"
            f"<td>{feedback_form(fid, fb)}</td></tr>"
            for fid, v, summary, fb, t, host, ident, tmpl, total, last in rows)
        if not rows:
            body = "<tr><td colspan=4 class=muted>Nothing open.</td></tr>"
        return page("argus", nav + "<table><tr><th>verdict</th><th>finding</th>"
                    "<th>seen</th><th>you</th></tr>" + body + "</table>")

    def detail(self, fid):
        r = self.store.db.execute("""
            SELECT f.verdict, f.summary, f.answer, f.transcript, f.feedback, f.note, f.t,
                   p.host, p.ident, p.template, p.sample, p.total, p.first_seen, p.last_seen
            FROM findings f JOIN patterns p ON p.id = f.pattern_id WHERE f.id=?""",
            (fid,)).fetchone()
        if not r:
            return page("not found", "<p>No such finding.</p>")
        (v, summary, answer, transcript, fb, note, t, host, ident, tmpl, sample,
         total, first, last) = r
        return page(f"argus: {esc(summary)}", f"""
            <p><a href=/>&larr; findings</a></p>
            <h2 class={esc(v)}>{esc(v)}: {esc(summary)}</h2>
            <p><code>{esc(host)} {esc(ident)}</code><br>
               seen {total} times, {when(first)} to {when(last)}; judged {when(t)}</p>
            <h3>Pattern</h3><pre>{esc(tmpl)}</pre>
            <h3>Example</h3><pre>{esc(sample)}</pre>
            <h3>Investigation</h3><pre>{esc(answer)}</pre>
            <p class=muted>transcript: {esc(transcript)}</p>
            <h3>Your call</h3>
            <p>{feedback_form(fid, fb)}</p>
            <form method=post action=/feedback><input type=hidden name=id value={fid}>
              <input type=hidden name=value value="{esc(fb or '')}">
              <input name=note size=60 placeholder="note for argus (e.g. this is expected, ignore it)"
                     value="{esc(note)}"> <button>save note</button></form>""")

    def log_message(self, *args):
        pass


def serve(db_path, bind="0.0.0.0", port=8082):
    Handler.store = Store(db_path)
    print(f"argus page on http://{bind}:{port}/")
    # Single-threaded on purpose: one reader, tiny requests, one SQLite handle.
    HTTPServer((bind, port), Handler).serve_forever()
