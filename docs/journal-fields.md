# What the journal records for each entry

This was surveyed on the Framework box on 2026-10-13, using
`journalctl -o json --all` over the whole journal: 3 boots, **37,035
entries** and **199 distinct field names**. The examples below have had IDs
and addresses replaced with placeholders, because this repo is public.

A journal entry isn't a line of text. It's a bag of `KEY=value` fields.
`MESSAGE` is the text you see in `journalctl`. Everything else is metadata,
and a lot of it is structured data we can match on without a regex.

## Three kinds of field

| prefix | who sets it | can the sender fake it? | count here |
|---|---|---|---:|
| `__X` (address) | journalctl, on output | no | 5 |
| `_X` (trusted) | journald itself, from the kernel's view of the sender | **no** | 63 |
| `X` (user) | the program that logged it | **yes** | 131 |

The trust difference matters for argus. Any process can send
`SYSLOG_IDENTIFIER=sshd` or `CONTAINER_NAME=whatever`, but none can fake
`_SYSTEMD_UNIT` or `_EXE`. **Rules should prefer trusted fields for deciding
the source.** Today's container filter relies on `CONTAINER_NAME`, which a
process could set to hide lines from it. That's low risk on this box, but
worth fixing.

## On every entry

`__CURSOR`, `__REALTIME_TIMESTAMP`, `__MONOTONIC_TIMESTAMP`, `__SEQNUM`,
`__SEQNUM_ID`, `_TRANSPORT`, `_HOSTNAME`, `_BOOT_ID`, `_MACHINE_ID`,
`_RUNTIME_SCOPE` (`initrd` or `system`), `MESSAGE`, `SYSLOG_IDENTIFIER`.

Notably **not** on every entry:

- **`PRIORITY`** is missing on 21.5% of entries, and those are exactly the
  audit entries. argus's current `-p 4` filter silently skips the whole
  audit stream, including failed SSH logins (`AUDIT_FIELD_RES=failed`).
- **`_SYSTEMD_UNIT`** is missing on 33% of entries, mostly kernel and audit.

## How it got into the journal: `_TRANSPORT`

| transport | entries | what it is |
|---|---:|---|
| `journal` | 16,001 | native journal API: richest metadata (CODE_*, MESSAGE_ID, ...) |
| `audit` | 7,949 | kernel audit subsystem: logins, service start/stop, BPF, firewall, dnf |
| `stdout` | 7,665 | a service's stdout/stderr, **one entry per line** |
| `kernel` | 4,302 | kernel log (dmesg) |
| `syslog` | 1,097 | classic syslog() calls |
| `driver` | 21 | journald's own messages |

## Trusted fields worth knowing (`_`)

| field | % | what it tells us |
|---|---:|---|
| `_PID`, `_UID`, `_GID` | 87 | sender process and user |
| `_COMM`, `_EXE`, `_CMDLINE` | 67 | program name, binary path, full command line |
| `_SYSTEMD_UNIT` | 67 | the systemd unit (164 distinct) |
| `_SYSTEMD_USER_UNIT` | 17 | the real service for user units (`llm-server.service`, `open-webui.service`, ...) when `_SYSTEMD_UNIT` is just `user@1000.service` |
| `_SYSTEMD_SLICE`, `_SYSTEMD_CGROUP` | 67 | where in the cgroup tree |
| `_SYSTEMD_INVOCATION_ID` | 41 | one run of a unit, which distinguishes restarts |
| `_STREAM_ID` | 21 | one stdout stream. **This is the key for gluing multi-line output back together.** |
| `_AUDIT_TYPE_NAME` | 21 | audit record type (`SERVICE_START`, `USER_LOGIN`, `BPF`, ...; 26 distinct) |
| `_AUDIT_SESSION`, `_AUDIT_LOGINUID` | 55 | login session and user |
| `_KERNEL_SUBSYSTEM`, `_KERNEL_DEVICE`, `_UDEV_SYSNAME`, `_UDEV_DEVNODE` | 4–5 (39% of kernel entries) | which device a kernel message is about (`pci`, `usb`, `acpi`, ...) |
| `_SOURCE_REALTIME_TIMESTAMP` | 68 | when the sender produced it, not when journald received it |
| `_SELINUX_CONTEXT`, `_CAP_EFFECTIVE` | 67–86 | security context |

## User fields worth knowing (no `_`)

| field | % | what it is |
|---|---:|---|
| `MESSAGE` | 100 | the text |
| `SYSLOG_IDENTIFIER` | 100 | the tag in front of the message (84 distinct). For containers it's the random container name, e.g. `busy_darwin`. |
| `PRIORITY` | 79 | 0 emerg … 7 debug |
| `CODE_FILE`, `CODE_LINE`, `CODE_FUNC` | 42 | the source code location that logged it, mostly systemd |
| `MESSAGE_ID` | 26 | a catalog ID for well-known events (24 distinct: unit started/stopped/failed, session opened, ...) |
| `UNIT`, `JOB_TYPE`, `JOB_RESULT`, `INVOCATION_ID` | 18–22 | which unit an event from systemd is *about* |
| `EXIT_CODE`, `EXIT_STATUS`, `UNIT_RESULT`, `N_RESTARTS` | rare | the details of a unit failure |
| `CPU_USAGE_NSEC`, `MEMORY_PEAK` | rare | resources used by a unit when it stops |
| `AUDIT_FIELD_*` | up to 19 | **already-parsed audit data**: `ACCT`, `ADDR`, `RES` (success/failed), `EXE`, `TERMINAL`, `OP`, `CMD`, `CWD`, ssh `CIPHER` and key `FP`, ... (42 fields) |
| `CONTAINER_ID`, `CONTAINER_NAME` | 13 | podman's log driver |
| `PODMAN_EVENT`, `PODMAN_IMAGE`, `PODMAN_EXIT_CODE`, ... | 1 | podman lifecycle events (create, start, died, with exit code) |
| `NM_DEVICE`, `NM_LOG_DOMAINS`, `NM_LOG_LEVEL` | 1 | NetworkManager |
| `DBUS_BROKER_*` | rare | dbus-broker metrics and launch errors |
| journald self-report (`CURRENT_USE`, `MAX_USE`, ...) | rare | the journal's own disk usage |

## What counts as a source

The source fields don't nest neatly:

- **`SYSLOG_IDENTIFIER`** is always present, but the sender sets it, and for
  containers it's a random name.
- **`_SYSTEMD_UNIT`** is trusted, but present on only 67% of entries, and it
  includes noise:
  - `user@1000.service` (6,339 entries) hides the real service, which is in
    `_SYSTEMD_USER_UNIT`.
  - Transient `run-pN-iN.scope` and `session-N.scope` units get new names
    all the time.
- **Kernel entries** have no unit (99.8%). About 39% of them carry
  `_KERNEL_SUBSYSTEM` / `_KERNEL_DEVICE`.
- **Audit entries** have no unit and no priority. `_AUDIT_TYPE_NAME` says what
  kind of record each one is.

A candidate definition:

```
source = _SYSTEMD_USER_UNIT or _SYSTEMD_UNIT     (transient scopes collapsed)
       | "kernel" [/ _KERNEL_SUBSYSTEM]          for _TRANSPORT=kernel
       | "audit/" + _AUDIT_TYPE_NAME             for _TRANSPORT=audit
       | "container/" + image                    for container output (image, not the random name)
```

Top sources by volume on the box:
- by unit: init.scope (8,057), llm-server (3,369), session scopes, tailscaled
  (1,078), open-webui (956), dnf5-automatic (728), systemd-logind (550),
  NetworkManager (512)
- by identifier: systemd (9,870), audit (7,949), kernel (3,902), dracut
  (3,410), podman (2,466)

## Implications for rules

- **Many lines don't need a regex.** A rule can match on structured fields
  first: `(source, MESSAGE_ID)`, `(source, CODE_FILE:CODE_LINE)`,
  `AUDIT_FIELD_RES=failed`, `PODMAN_EXIT_CODE!=0`, or `JOB_RESULT`. Those
  matches are exact, cheap and can't be too broad. A rule could be "field
  conditions + optional regex on MESSAGE".
- **Fields can be extracted for free.** Audit, podman and unit-failure events
  arrive already parsed, so `extract` can take fields directly, not only
  regex groups.
- **Multi-line:** no entry has a newline inside `MESSAGE`. stdout output is
  split one line per entry, so tracebacks and tables arrive as runs of
  entries with the same `_STREAM_ID` (59 streams here) and close timestamps.
  That gives us a natural way to group them back together before matching.
- **Don't filter on `PRIORITY` alone**, or audit disappears.
