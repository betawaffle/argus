"""Collapse journal lines into patterns, with plain code and no model.

"sda: I/O error, sector 1234" and "sda: I/O error, sector 98" both become
"sda: I/O error, sector <N>". Patterns are remembered in SQLite with daily
counts, so a scan can report what's new or suddenly more frequent, and only
those go to the model.
"""

import json
import re
import shlex
import sqlite3
import subprocess
import time

SUBS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b[0-9a-f:]*:[0-9a-f:]*:[0-9a-f:]+\b", re.I), "<IP6>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<HEX>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<HEX>"),
    # Durations, also glued to a word: "cr10h28m0s", "took 250ms".
    (re.compile(r"(?<![\d.])(?:\d+(?:\.\d+)?(?:ms|us|µs|ns|h|m|s|d))+(?![A-Za-z\d])"), "<DUR>"),
    # Not digits inside names like i8042, sda1 or ext4.
    (re.compile(r"(?<![A-Za-z_\d])\d+(?:\.\d+)?"), "<N>"),
]
PRIORITY_NAMES = "emerg alert crit err warning notice info debug".split()


def template(message):
    for rx, rep in SUBS:
        message = rx.sub(rep, message)
    return message[:300]


def read_journal(host="local", since="-1d", cursor=None, max_priority=4):
    """Yield journal entries at or above max_priority (4 = warning).

    With a cursor, only entries after it, so repeated scans don't double count.
    """
    argv = ["journalctl", "-o", "json", "--no-pager", "-p", str(max_priority)]
    argv += ["--after-cursor", cursor] if cursor else ["--since", since]
    if host != "local":
        argv = ["ssh", "-o", "BatchMode=yes", host, shlex.join(argv)]
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


TRANSIENT_SCOPE = re.compile(r"^run-p\d+-i\d+\.scope$")


# podman logs everything a container prints at priority err, so for container
# output the priority means nothing. Keep only lines that look like trouble.
CONTAINER_TROUBLE = re.compile(
    r"error|exception|traceback|fail|fatal|panic|oom|out of memory|killed|"
    r"denied|refused|timed? ?out|segfault|corrupt", re.I)


def interesting(entry):
    if "CONTAINER_NAME" in entry:
        return bool(CONTAINER_TROUBLE.search(message_text(entry)))
    return True


def ident(entry):
    # User services all share _SYSTEMD_UNIT=user@UID.service; the user unit
    # says which one it really is.
    if "CONTAINER_NAME" in entry:
        return "container:" + entry["CONTAINER_NAME"]
    unit = entry.get("_SYSTEMD_USER_UNIT") or entry.get("_SYSTEMD_UNIT")
    if unit and TRANSIENT_SCOPE.match(unit):  # systemd-run: new name every run
        unit = "run-<N>.scope"
    return unit or entry.get("SYSLOG_IDENTIFIER") or entry.get("_COMM") or "?"


def message_text(entry):
    m = entry.get("MESSAGE", "")
    if isinstance(m, list):  # journald encodes non-UTF-8 messages as byte lists
        m = bytes(m).decode("utf-8", "replace")
    return m


SCHEMA = """
CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY,
    host TEXT, ident TEXT, priority INTEGER, template TEXT,
    first_seen REAL, last_seen REAL, total INTEGER, sample TEXT,
    UNIQUE (host, ident, template)
);
CREATE TABLE IF NOT EXISTS cursors (host TEXT PRIMARY KEY, cursor TEXT);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY, pattern_id INTEGER, t REAL,
    verdict TEXT, summary TEXT, answer TEXT, transcript TEXT,
    feedback TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS daily (
    pattern_id INTEGER, day TEXT, count INTEGER,
    PRIMARY KEY (pattern_id, day)
);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        for col in ("feedback", "note"):  # databases from before feedback
            try:
                self.db.execute(f"ALTER TABLE findings ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass

    def cursor(self, host):
        row = self.db.execute("SELECT cursor FROM cursors WHERE host=?",
                              (host,)).fetchone()
        return row[0] if row else None

    def ingest(self, host, entries):
        """Count entries into patterns. Returns ids of patterns new this call."""
        new = set()
        for e in entries:
            if "__CURSOR" in e:
                self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)",
                                (host, e["__CURSOR"]))
            msg = message_text(e)
            if not msg or not interesting(e):
                continue
            ts = int(e.get("__REALTIME_TIMESTAMP", time.time() * 1e6)) / 1e6
            day = time.strftime("%Y-%m-%d", time.localtime(ts))
            key = (host, ident(e), template(msg))
            row = self.db.execute(
                "SELECT id FROM patterns WHERE host=? AND ident=? AND template=?",
                key).fetchone()
            if row:
                pid = row[0]
                self.db.execute(
                    "UPDATE patterns SET last_seen=max(last_seen, ?), total=total+1 "
                    "WHERE id=?", (ts, pid))
            else:
                pid = self.db.execute(
                    "INSERT INTO patterns (host, ident, priority, template, "
                    "first_seen, last_seen, total, sample) VALUES (?,?,?,?,?,?,1,?)",
                    (*key[:2], int(e.get("PRIORITY", 6)), key[2], ts, ts, msg[:1000]),
                ).lastrowid
                new.add(pid)
            self.db.execute(
                "INSERT INTO daily VALUES (?,?,1) ON CONFLICT DO UPDATE "
                "SET count=count+1", (pid, day))
        self.db.commit()
        return new

    def spikes(self, day, factor=5, min_count=20):
        """Patterns whose count on day is far above their usual daily count."""
        return [r[0] for r in self.db.execute(
            """SELECT d.pattern_id FROM daily d
               JOIN (SELECT pattern_id, avg(count) AS avg FROM daily
                     WHERE day < ? GROUP BY pattern_id) h USING (pattern_id)
               WHERE d.day = ? AND d.count >= ? AND d.count > h.avg * ?""",
            (day, day, min_count, factor))]

    def describe(self, pid):
        host, ident_, prio, tmpl, first, last, total, sample = self.db.execute(
            "SELECT host, ident, priority, template, first_seen, last_seen, total, "
            "sample FROM patterns WHERE id=?", (pid,)).fetchone()
        return {"id": pid, "host": host, "ident": ident_,
                "priority": PRIORITY_NAMES[prio] if 0 <= prio < 8 else prio,
                "template": tmpl, "total": total, "sample": sample,
                "first_seen": time.strftime("%Y-%m-%d %H:%M", time.localtime(first)),
                "last_seen": time.strftime("%Y-%m-%d %H:%M", time.localtime(last))}

    def history(self, pid, limit=6):
        """Earlier judged findings from the same source, feedback first.

        This is argus's memory: the model sees what it concluded before about
        this service and whether Andy agreed, so it repeats itself less.
        """
        return self.db.execute(
            """SELECT p.template, f.verdict, f.summary, f.feedback, f.note
               FROM findings f JOIN patterns p ON p.id = f.pattern_id
               JOIN patterns q ON q.id = ?
               WHERE p.host = q.host AND p.ident = q.ident AND p.id != q.id
                 AND f.verdict != 'baseline'
               ORDER BY f.feedback IS NULL, f.t DESC LIMIT ?""",
            (pid, limit)).fetchall()
