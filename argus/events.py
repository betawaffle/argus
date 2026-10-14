"""Journal events: reading them, and deciding which source each came from.

An event is the journal entry as a dict of fields (see docs/journal-fields.md),
with MESSAGE already decoded to a string. The source is computed by argus
from trusted fields only, so a program can't pretend to be another one.
"""

import json
import re
import shlex
import subprocess

# Fields a rule may match on: ones whose values stay meaningful across boots
# and restarts (measured on the box; see docs/rules-design.md). Everything
# else (PIDs, IDs, timestamps, cursors, random container names) goes stale.
MATCHABLE = {
    "_TRANSPORT", "_EXE", "_COMM", "_UID", "_GID", "_SELINUX_CONTEXT",
    "_SYSTEMD_SLICE", "_SYSTEMD_USER_SLICE", "PRIORITY", "SYSLOG_FACILITY",
    "MESSAGE_ID", "CODE_FILE", "CODE_LINE", "CODE_FUNC",
    "_KERNEL_SUBSYSTEM", "_KERNEL_DEVICE", "_UDEV_SYSNAME", "_UDEV_DEVNODE",
    "_AUDIT_TYPE_NAME", "PODMAN_EVENT", "PODMAN_TYPE", "PODMAN_IMAGE",
    "PODMAN_EXIT_CODE", "NM_DEVICE", "NM_LOG_DOMAINS", "UNIT", "USER_UNIT",
    "JOB_TYPE", "JOB_RESULT", "UNIT_RESULT", "EXIT_CODE", "EXIT_STATUS",
    "ERRNO", "INTERFACE",
    # Set by the sender, but rules are already scoped to a source, so this
    # only narrows within it.
    "SYSLOG_IDENTIFIER",
} | {f"AUDIT_FIELD_{f}" for f in (
    "RES", "OP", "EXE", "TERMINAL", "ACCT", "KIND", "GRANTORS", "UNIT", "COMM",
    "DIRECTION", "SW_TYPE")}

# Per-unit numbering that changes every time: collapse to one name.
UNIT_NORMALIZE = [
    (re.compile(r"^run-p\d+-i\d+\.scope$"), "run.scope"),
    (re.compile(r"^run-[ru]?[0-9a-f]+\.(scope|service)$"), r"run.\1"),
    (re.compile(r"^session-\d+\.scope$"), "session.scope"),
    (re.compile(r"^podman-\d+\.scope$"), "podman.scope"),
    (re.compile(r"^libpod-(conmon-)?[0-9a-f]{12,}\.scope$"), r"libpod-\1ID.scope"),
    (re.compile(r"^user@\d+\.service$"), "user@.service"),
    (re.compile(r"^user-runtime-dir@\d+\.service$"), "user-runtime-dir@.service"),
]


def normalize_unit(unit):
    for rx, rep in UNIT_NORMALIZE:
        if rx.match(unit):
            return rx.sub(rep, unit)
    return unit


def message_text(event):
    m = event.get("MESSAGE", "")
    if isinstance(m, list):  # journald encodes non-UTF-8 messages as byte lists
        m = bytes(m).decode("utf-8", "replace")
    return m


def source(event):
    """Which source an event belongs to. Rules are kept and matched per source.

    - kernel             all kernel messages (rules may narrow by subsystem)
    - audit/<TYPE>       audit records, by record type
    - <unit>             the systemd (user) unit, with per-run numbering removed
    - <unit>/container   container output, under the unit that runs it
    - exe/<binary>       anything else that has no unit
    """
    transport = event.get("_TRANSPORT")
    if transport == "kernel":
        return "kernel"
    if transport == "audit":
        return "audit/" + event.get("_AUDIT_TYPE_NAME", "unknown")
    unit = event.get("_SYSTEMD_USER_UNIT") or event.get("_SYSTEMD_UNIT")
    if unit:
        name = normalize_unit(unit)
        # CONTAINER_NAME is sender-set, but here it only adds a suffix under a
        # trusted unit, so faking it can't move a line to another unit.
        return name + "/container" if "CONTAINER_ID" in event else name
    exe = event.get("_EXE") or event.get("_COMM")
    if exe:
        return "exe/" + exe.rsplit("/", 1)[-1]
    return "unknown/" + event.get("SYSLOG_IDENTIFIER", "?")


def read(host="local", cursor=None, since=None, extra=()):
    """Yield events after cursor (or since a time), all priorities.

    MESSAGE is decoded to a string up front, so nothing else has to care.
    """
    argv = ["journalctl", "-o", "json", "--no-pager", *extra]
    if cursor:
        argv += ["--after-cursor", cursor]
    elif since:
        argv += ["--since", since]
    if host != "local":
        argv = ["ssh", "-o", "BatchMode=yes", host, shlex.join(argv)]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True,
                            errors="replace")
    for line in proc.stdout:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        e["MESSAGE"] = message_text(e)
        yield e
    proc.wait()


def mark_mirrored(events, chunk=2000):
    """Mark unit-stdout copies of container lines with __MIRRORED.

    A unit that runs podman attached logs each container line twice: once
    from the journald log driver (with CONTAINER_ID) and once as podman's
    own stdout. The container copy is the one kept. Works in chunks, so the
    two copies only have to land within `chunk` events of each other.
    """
    buf = []

    def flush():
        container = {(e.get("_SYSTEMD_UNIT"), e["MESSAGE"].rstrip("\n"))
                     for e in buf if "CONTAINER_ID" in e}
        for e in buf:
            if ("CONTAINER_ID" not in e
                    and (e.get("_SYSTEMD_UNIT"), e["MESSAGE"].rstrip("\n")) in container):
                e["__MIRRORED"] = True
            yield e
        buf.clear()

    for e in events:
        buf.append(e)
        if len(buf) >= chunk:
            yield from flush()
    yield from flush()


# A block starts at a traceback, or at asyncio's "Task exception was never
# retrieved" (logged just before its future: line and traceback).
TRACEBACK = re.compile(r"Traceback \(most recent call last\):\s*$"
                       r"|Task exception was never retrieved\s*$")
CONTINUES = re.compile(
    r"^(\s|$)"                                    # indented frame / code line
    r"|^\^+\s*$"                                  # caret markers
    r"|^>\s*File \""                              # loguru marks the catching frame
    r"|^future: <"                                 # asyncio's task line
    r"|^(During handling of the above|The above exception was)"
    r"|^Traceback \(most recent call last\)"
    r"|^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt|Warning|Group)\b")
MAX_JOIN = 200       # lines per joined event
JOIN_GAP = 2.0       # seconds between lines of one traceback, at most


def _t(e):
    return int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6


def join_multiline(events):
    """Join each Python traceback into one event.

    Programs write a traceback as one journal line per frame. Learned line by
    line, one error becomes a dozen rules that disagree with each other.
    A block starts at "Traceback (most recent call last):" and takes the
    following lines from the same process (or container) that look like
    part of a traceback. The joined event keeps the first line's fields;
    MESSAGE holds all the lines, __LINES their count.

    Joined events carry no __CURSOR (other processes' lines may already have
    been passed on with later cursors), except the last one flushed at the
    end, which gets the stream's final cursor so nothing is read twice.
    """
    open_blocks = {}   # key -> [events]
    last_cursor = None

    def key(e):
        return (e.get("_SYSTEMD_UNIT"), e.get("CONTAINER_ID") or e.get("_PID"))

    def joined(block):
        first = dict(block[0])
        first.pop("__CURSOR", None)
        first["MESSAGE"] = "\n".join(e["MESSAGE"].rstrip("\n") for e in block)
        first["__LINES"] = len(block)
        return first

    for e in events:
        last_cursor = e.get("__CURSOR", last_cursor)
        if e.get("__MIRRORED"):
            yield e
            continue
        k = key(e)
        block = open_blocks.get(k)
        if block is not None:
            if (CONTINUES.match(e["MESSAGE"]) and len(block) < MAX_JOIN
                    and _t(e) - _t(block[-1]) <= JOIN_GAP):
                block.append(e)
                continue
            yield joined(open_blocks.pop(k))
        if TRACEBACK.search(e["MESSAGE"]):
            open_blocks[k] = [e]
        else:
            yield e
    rest = [joined(b) for b in open_blocks.values()]
    if rest and last_cursor:
        rest[-1]["__CURSOR"] = last_cursor
    yield from rest


def latest_cursor(host="local"):
    """Cursor of the newest entry, to start watching from "now"."""
    events = list(read(host, extra=("-n", "1")))
    return events[-1]["__CURSOR"] if events else None


def summarize(event, max_message=500):
    """The event without its volatile fields, for showing to the model."""
    out = {k: v for k, v in event.items()
           if k in MATCHABLE or k in ("MESSAGE", "SYSLOG_IDENTIFIER")}
    msg = out.get("MESSAGE", "")
    if len(msg) > max_message:  # keep the end too: a traceback's error is last
        msg = msg[:max_message // 2] + "\n…\n" + msg[-max_message // 2:]
    out["MESSAGE"] = msg
    out["source"] = source(event)
    return out
