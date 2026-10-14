"""A small page to browse findings and agree with or dismiss them.

Stdlib http.server, one table, no JavaScript. Feedback is stored with the
finding and shown to the model the next time the same service comes up.
"""

import urllib.parse
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

from labeler import Store as LabelStore
from labeler.web import App as LabelApp

from . import label_sources, rules_page
from .patterns import Store
from .serve_util import esc, page, when

ORDER = "CASE f.verdict WHEN 'attention' THEN 0 WHEN 'watch' THEN 1 WHEN '?' THEN 2 ELSE 3 END"


def feedback_form(fid, current):
    buttons = "".join(
        f"<form method=post action=/feedback><input type=hidden name=id value={fid}>"
        f"<button name=value value={v}{' disabled' if current == v else ''}>{v}</button></form> "
        for v in ("agree", "dismiss"))
    return buttons + (f"<span class=muted>({esc(current)})</span>" if current else "")


class Handler(BaseHTTPRequestHandler):
    store = None
    labels = None   # the labeling tool, mounted at /label

    def label(self, method, body=b""):
        url = urllib.parse.urlparse(self.path)
        resp = self.labels and self.labels.handle(method, url.path, url.query, body)
        if not resp:
            return False
        status, ctype, payload = resp
        self.send_response(status)
        if ctype == "redirect":
            self.send_header("Location", payload.decode())
            payload = b""
        else:
            self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(payload)
        return True

    def send(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.label("GET"):
            return
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            return self.send(self.index(show_all="all" in q))
        if url.path.startswith("/f/"):
            return self.send(self.detail(int(url.path[3:])))
        if url.path == "/rules":
            return self.send(rules_page.rules_index(self.store.db))
        if url.path.startswith("/rules/"):
            name = urllib.parse.unquote(url.path[len("/rules/"):])
            return self.send(rules_page.rule_detail(self.store.db, name))
        self.send(page("not found", "<p>Not found.</p>"), 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        if self.label("POST", raw):
            return
        form = urllib.parse.parse_qs(raw.decode())
        if self.path == "/ack":
            rules_page.ack(self.store.db, int(form["id"][0]))
            self.send_response(303)
            self.send_header("Location", self.headers.get("Referer") or "/rules")
            self.end_headers()
            return
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
        nav = ("<nav><a href=/>open findings</a><a href=/?all>everything</a>"
               "<a href=/rules>rules</a><a href=/label>label</a>"
               "<span class=muted>" + ", ".join(f"{v}: {c}" for v, c in sorted(counts.items()))
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
    Handler.labels = LabelApp(LabelStore(Path(db_path).with_name("label.db")),
                              label_sources.sources(Handler.store.db),
                              nav=rules_page.NAV)
    print(f"argus page on http://{bind}:{port}/")
    # Single-threaded on purpose: one reader, tiny requests, one SQLite handle.
    HTTPServer((bind, port), Handler).serve_forever()
