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


def latest_cursor(host="local"):
    """Cursor of the newest entry, to start watching from "now"."""
    events = list(read(host, extra=("-n", "1")))
    return events[-1]["__CURSOR"] if events else None


def summarize(event, max_message=500):
    """The event without its volatile fields, for showing to the model."""
    out = {k: v for k, v in event.items()
           if k in MATCHABLE or k in ("MESSAGE", "SYSLOG_IDENTIFIER")}
    out["MESSAGE"] = out.get("MESSAGE", "")[:max_message]
    out["source"] = source(event)
    return out
