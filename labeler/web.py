"""The labeling pages, mountable under a prefix in any stdlib HTTP server.

    app = App(store, sources, prefix="/label")
    resp = app.handle("GET", "/label/i/3", query, body)   # None if not ours
    status, content_type, payload = resp

Pages:
    {prefix}                the queue: what's waiting, per source
    {prefix}/next           redirect to the next unlabeled item
    {prefix}/i/<id>         the review screen for one item version
    {prefix}/api/label      POST a label as JSON
    {prefix}/api/group_duplicates  POST {item_id, ids?}: skip its group (or just
                                   ids) as duplicates of it
    {prefix}/export.jsonl   every label with its item snapshot
"""

import html
import json
import time
import urllib.parse
from pathlib import Path

STATIC = Path(__file__).parent / "static"

BASE_CSS = """
:root { --bg:#fff; --fg:#1b1b1b; --muted:#6b6b6b; --line:#e3e3e3; --panel:#f6f6f6;
        --wrong:#d64545; --good:#2e8b57; --from:#b9770e; --missing:#6a5acd;
        --unsupported:#c2185b; --unclear:#5f6b7a; --should:#0277bd; --nbo:#8d6e63; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161616; --fg:#e8e8e8; --muted:#9a9a9a; --line:#333; --panel:#1f1f1f;
          --wrong:#ff7b6b; --good:#7fd19b; --from:#f2b84b; --missing:#a99cff;
          --unsupported:#f48fb1; --unclear:#a7b1bd; --should:#6cc3f2; --nbo:#c9a99b; } }
body { background:var(--bg); color:var(--fg); margin:0 auto; max-width:1300px;
       padding:16px; font:14px/1.45 system-ui, sans-serif; }
a { color:inherit; }
nav a { margin-right:12px; }
table { border-collapse:collapse; width:100%; }
td, th { border-bottom:1px solid var(--line); padding:6px 8px; text-align:left;
         vertical-align:top; }
th { color:var(--muted); font-weight:500; }
.muted { color:var(--muted); }
code, pre { font:12px/1.4 ui-monospace, monospace; }
"""


def esc(x):
    return html.escape(str(x if x is not None else ""))


def shell(title, body, nav="", head=""):
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport "
            f"content='width=device-width,initial-scale=1'><title>{esc(title)}</title>"
            f"<style>{BASE_CSS}</style>{head}{nav}{body}").encode()


class App:
    def __init__(self, store, sources, by="andy", prefix="/label", nav=""):
        self.store = store
        self.sources = {s.name: s for s in sources}
        self.by = by
        self.prefix = prefix.rstrip("/")
        self.nav = nav
        self.synced = 0

    def sync(self, force=False):
        # Sources read files and other databases: at most every few seconds.
        if force or time.time() - self.synced > 5:
            self.store.sync(self.sources.values())
            self.synced = time.time()

    def handle(self, method, path, query, body=b""):
        p = self.prefix
        if path != p and not path.startswith(p + "/"):
            return None
        rest = path[len(p):] or "/"
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
        try:
            if method == "GET" and rest == "/":
                return 200, "text/html; charset=utf-8", self.queue_page(q)
            if method == "GET" and rest == "/next":
                return self.redirect(self.next_url(q.get("source"), q.get("after")))
            if method == "GET" and rest.startswith("/i/"):
                return self.item_page(int(rest[3:]), q)
            if method == "GET" and rest.startswith("/static/"):
                f = STATIC / rest[len("/static/"):]
                if f.parent == STATIC and f.is_file():
                    ctype = {".js": "text/javascript", ".css": "text/css"}.get(f.suffix,
                                                                             "text/plain")
                    return 200, ctype + "; charset=utf-8", f.read_bytes()
            if method == "POST" and rest == "/api/label":
                return self.post_label(json.loads(body or b"{}"))
            if method == "POST" and rest == "/api/group_duplicates":
                d = json.loads(body or b"{}")
                n = self.store.mark_group_duplicates(int(d["item_id"]), self.by,
                                                     d.get("ids"))
                return 200, "application/json", json.dumps({"ok": True, "n": n}).encode()
            if method == "GET" and rest == "/export.jsonl":
                lines = "".join(json.dumps(x, ensure_ascii=False) + "\n"
                                for x in self.store.export())
                return 200, "application/x-ndjson", lines.encode()
        except ValueError as e:
            return 400, "text/plain; charset=utf-8", str(e).encode()
        return 404, "text/html; charset=utf-8", shell("not found", "<p>Not found.</p>",
                                                      self.nav)

    def redirect(self, url):
        return 303, "redirect", url.encode()

    # --- queue ----------------------------------------------------------------
    def next_url(self, source=None, after=None):
        self.sync()
        for row in self.store.queue(self.by, source):
            if str(row["id"]) != str(after):
                qs = f"?source={urllib.parse.quote(source)}" if source else ""
                return f"{self.prefix}/i/{row['id']}{qs}"
        qs = f"?source={urllib.parse.quote(source)}&done=1" if source else "?done=1"
        return f"{self.prefix}{qs}"

    def queue_page(self, q):
        self.sync(force=True)
        source = q.get("source")
        show_all = "all" in q
        counts = self.store.counts(self.by)
        p = self.prefix
        tabs = [f"<a href={p}{'?all' if show_all else ''}>{'<b>all</b>' if not source else 'all'}</a>"]
        for s, (n, done) in counts.items():
            name = f"<b>{esc(s)}</b>" if s == source else esc(s)
            tabs.append(f"<a href='{p}?source={urllib.parse.quote(s)}"
                        f"{'&all' if show_all else ''}'>{name}</a> "
                        f"<span class=muted>{done}/{n}</span>")
        rows = self.store.queue(self.by, source, include_labeled=show_all)
        start = (f"{p}/next?source={urllib.parse.quote(source)}" if source else f"{p}/next")
        body = [f"<h2>Labeling</h2><p>{' · '.join(tabs)}</p>"]
        if q.get("done"):
            body.append("<p>Nothing left to label here. 🎉</p>")
        todo = sum(r["state"] == "todo" for r in rows)
        later = sum(r["state"] == "later" for r in rows)
        body.append(f"<p><a href='{start}'><b>Start labeling &rarr;</b></a> "
                    f"<span class=muted>({todo} waiting"
                    + (f", then {later} you skipped for later" if later else "")
                    + f")</span> · <a href='{p}?{'' if show_all else 'all&'}"
                    f"{'source=' + urllib.parse.quote(source) if source else ''}'>"
                    f"{'hide' if show_all else 'show'} labeled</a> · "
                    f"<a href={p}/export.jsonl>export</a></p>")
        # Near-duplicates (same group) collapse into their first item.
        shown, extra = [], {}
        for r in rows:
            g = (r["source"], r["group"], r["state"]) if r["group"] else None
            if g and g in extra:
                extra[g] += 1
                continue
            if g:
                extra[g] = 0
            shown.append((r, g))
        trs = "".join(
            f"<tr><td><a href={p}/i/{r['id']}>{esc(r['title'])}</a>"
            + (f" <span class=muted>+{extra[g]} like this</span>" if g and extra[g] else "")
            + f"</td><td class=muted>{esc(r['source'])}</td>"
            f"<td>{'skipped for later' if r['state'] == 'later' else esc(r['verdict'] or '')}"
            f"</td></tr>" for r, g in shown)
        body.append("<table><tr><th>item</th><th>source</th><th>your verdict</th></tr>"
                    + (trs or "<tr><td colspan=3 class=muted>Nothing here.</td></tr>")
                    + "</table>")
        return shell("labeling", "".join(body), self.nav)

    # --- one item -------------------------------------------------------------
    def item_page(self, item_id, q):
        item = self.store.item(item_id)
        if item is None:
            return 404, "text/html; charset=utf-8", shell("not found",
                                                          "<p>No such item.</p>", self.nav)
        refs = []
        for source, key in item.get("refs", []):
            rid = self.store.current_id(source, key)
            ref = self.store.item(rid) if rid else None
            refs.append(ref or {"id": None, "source": source, "key": key,
                                "title": f"{source}: {key} (not available)",
                                "parts": [], "context": []})
        prev_id, prev = (None, None)
        label = self.store.latest_label(item_id, self.by)
        if label is None:
            prev_id, prev = self.store.previous_label(item_id, self.by)
        source = q.get("source")
        data = {
            "item": item, "refs": refs, "label": label,
            "previous": prev and {**prev, "item_id": prev_id},
            "api": f"{self.prefix}/api/label",
            "queue": f"{self.prefix}" + (f"?source={urllib.parse.quote(source)}"
                                         if source else ""),
            "next": f"{self.prefix}/next?after={item_id}" + (
                f"&source={urllib.parse.quote(source)}" if source else ""),
            "prefix": self.prefix,
            "group": [self.preview(r) for r in self.store.group_of(item_id, self.by)[:50]],
            "state": next((r["state"] for r in self.store.queue(
                self.by, item["source"], include_labeled=True, limit=None)
                if r["id"] == item_id), None),
            "todo_left": any(r["state"] == "todo" for r in self.store.queue(self.by)),
            "recent": self.store.recent(self.by, exclude=item_id),
            "group_api": f"{self.prefix}/api/group_duplicates",
        }
        blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        head = f"<link rel=stylesheet href={self.prefix}/static/label.css>"
        body = (f"<div id=app></div><script>window.LABEL = {blob};</script>"
                f"<script src={self.prefix}/static/label.js></script>")
        return 200, "text/html; charset=utf-8", shell(f"label: {item['title']}", body,
                                                      self.nav, head)

    def preview(self, row):
        """A group member, with enough of its text to tell it apart."""
        it = self.store.item(row["id"]) or {}
        bits = [p["text"] for p in (it.get("parts") or [])[:1] + (it.get("context") or [])[:2]]
        text = " · ".join(" ".join(b.split())[:200] for b in bits if b)
        return {"id": row["id"], "title": row["title"], "state": row["state"],
                "preview": text}

    def post_label(self, d):
        item_id = int(d["item_id"])
        item = self.store.item(item_id)
        if item is None:
            raise ValueError("no such item")
        lid = self.store.save_label(
            item_id, self.by, d.get("verdict"), d.get("marks") or [],
            fixable=d.get("fixable"), confidence=d.get("confidence"), note=d.get("note"),
            links=d.get("links") or [])
        src = self.sources.get(item["source"])
        if src:
            src.labeled(item, self.store.latest_label(item_id, self.by))
        return 200, "application/json", json.dumps({"ok": True, "label": lid}).encode()
