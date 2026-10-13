"""Read-only tools the model may call.

Commands run without a shell, and only if they pass an allowlist, so the
model can look around but can't change anything. Remote hosts are reached
over ssh with the same check applied before the command leaves this box.
"""

import glob
import os
import shlex
import subprocess

MAX_OUTPUT = 12_000  # chars; prompt processing is ~1K tok/s, keep results short

# command -> allowed first argument (None means any arguments are fine)
ALLOWED = {
    "journalctl": None,
    "systemctl": {"status", "list-units", "list-timers", "show", "is-failed",
                  "is-active", "--failed"},
    "df": None, "free": None, "uptime": None, "uname": None, "lsblk": None,
    "ip": {"addr", "link", "route", "-br"},
    "zpool": {"status", "list", "iostat", "history"},
    "zfs": {"list", "get"},
    "smartctl": {"-H", "-a", "-A", "-i", "-l"},
    "sensors": None, "dmesg": None, "cat": None, "ls": None, "head": None,
    "tail": None, "wc": None, "stat": None, "findmnt": None,
    "podman": {"ps", "inspect", "logs", "images"},
    "rpm": {"-q", "-qa", "-qf", "-qi", "-ql"},
}
# Flags that change state or never return (follow modes).
FORBIDDEN = {
    "journalctl": {"--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate",
                   "--flush", "--relinquish-var", "--sync", "-f", "--follow"},
    "dmesg": {"-C", "--clear", "-c", "--read-clear", "-w", "--follow", "-W"},
    "tail": {"-f", "-F", "--follow"},
    "podman": {"-f", "--follow"},
}
SHELL_OPS = {"|", "||", "&&", ";", ">", ">>", "<", "&", "2>&1"}
# Readable by the user, but nothing the model needs to see.
SECRET_PATHS = ("~/.ssh", "~/.gnupg", "~/.config/gh", "~/.netrc", "/etc/shadow",
                "/etc/gshadow", "/etc/ssh/ssh_host_", "/var/lib/tailscale")
SECRET_PATHS = tuple(os.path.expanduser(p) for p in SECRET_PATHS)

# Commands where privileged reads are expected and harmless.
NEEDS_SUDO = {"smartctl", "dmesg"}


def check(argv):
    """Return None if argv is allowed, else a reason string."""
    if not argv:
        return "empty command"
    cmd, args = argv[0], argv[1:]
    shellish = [a for a in argv if a in SHELL_OPS or a.endswith("|")]
    if shellish:
        return (f"there is no shell, so {shellish[0]} doesn't work; run one "
                "command (journalctl -g PATTERN filters, -n N limits)")
    secret = [a for a in args if os.path.abspath(os.path.expanduser(a))
              .startswith(SECRET_PATHS)]
    if secret:
        return f"{secret[0]} holds secrets and is off limits"
    if cmd not in ALLOWED:
        return f"{cmd} is not on the read-only allowlist"
    first = ALLOWED[cmd]
    if first is not None and (not args or args[0] not in first):
        return f"{cmd} only allows: {', '.join(sorted(first))}"
    bad = [a for a in args if a.split("=")[0] in FORBIDDEN.get(cmd, ())]
    if bad:
        return f"flag {bad[0]} is not allowed"
    return None


def expand(arg):
    """Expand a path glob the way a shell would; leave anything else alone."""
    if "/" in arg and any(c in arg for c in "*?["):
        return sorted(glob.glob(arg)) or [arg]
    return [arg]


def run(command, host="local", timeout=60):
    """Run an allowlisted command on host ("local" or an ssh destination)."""
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return f"error: could not parse command: {e}"
    reason = check(argv)
    if reason:
        return f"refused: {reason}"
    if host == "local":
        argv = [m for a in argv for m in expand(a)]
        # Re-check: a glob could have expanded into a secret path.
        reason = check(argv)
        if reason:
            return f"refused: {reason}"
    if argv[0] in NEEDS_SUDO:
        argv = ["sudo", "-n"] + argv
    if host != "local":
        argv = ["ssh", "-o", "BatchMode=yes", host, shlex.join(argv)]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"error: timed out after {timeout}s"
    out = p.stdout + (f"\n[stderr]\n{p.stderr}" if p.stderr.strip() else "")
    if p.returncode:
        out += f"\n[exit {p.returncode}]"
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n[truncated {len(out) - MAX_OUTPUT} chars]"
    return out or "(no output)"


SCHEMAS = [{
    "type": "function",
    "function": {
        "name": "run",
        "description": "Run a read-only diagnostic command (no shell, no pipes). "
                       "Allowed: " + ", ".join(sorted(ALLOWED)) + ". "
                       "Output is truncated, so prefer narrow queries "
                       "(e.g. journalctl -u NAME --since -1d -n 50).",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "host": {"type": "string",
                         "description": 'Host to run on, default "local".'},
            },
            "required": ["command"],
        },
    },
}]

FUNCS = {"run": run}
