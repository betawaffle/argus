# Design: model-written rules

Status: **draft, being designed together (Andy + Claude). Nothing is built yet.**

## The idea

Read the journal one line at a time. If a line matches a rule we already
have, handle it the way that rule says, with no model involved. If it
doesn't match any rule, send it to the LLM. The LLM:

1. writes a rule that matches this line and lines like it,
2. decides what should happen to such lines (the rule's action), and
3. applies that action to this line.

Over time argus builds up a set of rules that can match and dispatch almost
every line quickly. The LLM only sees what's genuinely new. In effect the
model acts as a compiler: the slow path runs once per new kind of line and
leaves a fast path behind.

The actions are where the interesting things can happen: counting with
thresholds, parsing values out of lines and storing them, and more creative
things later.

## Decided

- **Batch, not live.** A run processes the new journal lines since the last
  cursor, every few minutes or on a timer. A model call takes 10 to 60
  seconds, so this behaves about the same as live and is simpler.
- **A rule is data:**
  - `name`: a unique `snake_case` slug, e.g. `i8042_probe_failed`
  - `regex`: with named groups for the parts that vary
  - `action`: built from a fixed set of building blocks (below)
- **The LLM may propose new building blocks.** Until a human reviews the
  proposal, the rule falls back to a safe default: flag for review and
  collect every matching line.
- **How a rule proves itself:**
  - It must match the line that caused it.
  - Its name must be unique. On a name conflict, the LLM is given the
    existing rule and decides whether the two can be combined.
  - Any problem creating a rule (bad regex, test failure, unknown building
    block, ...) means the rule is still created but **flagged for review**,
    and every matching line is collected until review.
  - The worst case is that we can't get a working regex at all. Then the rule
    is the escaped line itself, matching only identical lines. Timestamps and
    similar varying parts will need some care.
- **Rules live in git** (a repo on the box), so every rule the LLM writes or
  changes is a reviewable, revertable diff. **Runtime state lives in SQLite**:
  counters, extracted values, collected lines, review flags. Lines and
  extracts never go in git.
- **Overlaps: the older rule always wins** at dispatch.
- **The LLM hands back rules through a `propose_rule` tool call**, and
  validation errors come back as the tool's result.
- **Every rule keeps a hit counter and last-hit time**, separately from its
  action, even for `ignore`. This is metadata, not the action's state. It
  lets us find dead rules and see what's noisy.
- **Backfill** (the first run over existing history) needs extra machinery
  and probably a different strategy for the LLM part. It's handled
  separately from the steady state.

## Open questions

### 1. Building blocks

Andy's descriptions of the first set:

| block | what it does |
|---|---|
| `ignore` | drop the line: no count, nothing collected (only the rule's own hit counter) |
| `count(window)` | keep a counter per window (hour/day), plus simple stats such as first/last seen and peak rate |
| `threshold(n, window, then)` | when there are n lines in a window, run `then` *in addition to* counting |
| `extract(...)` | do something with the named groups: store them as a row, or feed one of the blocks below |
| `flag(severity, summary)` | mark the rule so it shows up on the page as needing attention |
| `collect` | keep the matching lines, by journal cursor or by copying (see 5) |
| `investigate` | hand the lines to the LLM, which writes a report with recommendations for Andy (details to work out) |

More suggestions, many of which build on `extract`:

| block | what it does | example |
|---|---|---|
| `expect_every(period)` | a heartbeat: flag if the line *stops* appearing. On unattended machines, silence is often the real problem. | "zfs scrub finished" weekly; backup completed nightly; the sanoid/syncoid snapshot line hourly |
| `track(field)` | remember the latest value of a field and fire when it changes | pool state `ONLINE` → `DEGRADED`; a link going down/up; the kernel version at boot |
| `series(field)` | store a numeric field over time and fire when it crosses a limit or keeps rising | SMART reallocated sectors, temperatures, a backup's bytes or duration |
| `pair(start_rule, end_rule, within)` | match start/end lines: record the duration, and flag if the end never comes | a backup job started/finished; a long gpu-run starting and exiting |
| `rate_change` | learn the usual rate and fire when it's well above (or below) normal | auth failures, OOM kills, tailscale retries |
| `distinct(field)` | track the set of values seen and fire on a new one | a new USB device, a new IP failing SSH auth, a new container name |
| `run_check(command)` | run one allowlisted read-only command and attach its output to whatever fires next | `zpool status` when a ZFS error line appears; `smartctl -H` on an I/O error |
| `digest` | roll matches into a periodic summary instead of one item each | "47 tailscale DNS retries today" |

Actions compose as a list, e.g.
`[extract, series(realloc, rising → flag(attention))]` or
`[count(day), threshold(5, day, investigate)]`.

**`then` / what fires:** `flag`, `investigate`, `run_check` then `flag`, and
later `notify`.

### 2. Detecting overlapping regexes

Decided: the older rule wins at dispatch. Detecting overlaps is an extra.

In general it's hard. For pure regular expressions, "can these two match the
same text?" is answerable by intersecting their automata. Python regexes have
backreferences and lookarounds, which break that, and no stdlib tool does it.
The practical version: when a rule is created, run it against the stored
sample lines of every other rule (and the reverse). That catches real
overlaps on real data, which is what matters. It misses lines nobody has
seen yet, but the older-wins rule covers those.

### 3. What a regex matches against

Proposal: the message text only, plus an optional source filter (systemd
unit or syslog identifier) as a separate field of the rule. Dispatch looks
up the rules for a line's source first, then the source-less rules, which
keeps it fast. Priority could be another optional filter.

### 4. How the LLM hands back a rule (decided: tool call)

Asking gpt-oss for JSON in plain text breaks llama.cpp (HTTP 500, see
how-it-works.md). Tool calls work reliably, though, so the model would
call a `propose_rule(name, regex, source, action)` tool. argus validates the
rule and returns the result as the tool's output, so the model can fix
problems in the same conversation before we give up and flag it.

The model would also get a `test_regex(regex, lines)` tool to try regexes
before proposing, and `find_rule(name)` / `list_rules(source)` to see what
exists.

### 5. Collecting lines until review

By reference (a journal cursor) is cheapest. On the box, journald uses the
defaults: persistent storage, capped at 10% of the filesystem (max 4 GB),
and no time limit. At about 24 MB/day that's roughly 5 months before the
oldest lines rotate out. So references are safe for weeks. Proposal: store
cursors, and copy the line text into SQLite once a rule is flagged or
investigated, so the evidence survives rotation. moneta will have its own
retention settings, so check per host.

Some unmatched things are huge, e.g. an installer's file listing logged
line by line (3,259 lines in one session on the box). Proposal: keep the
first N lines (e.g. 200) plus a total count per rule.

### 6. The review flow

On the page: list rules flagged for review, with sample lines. Actions:
approve, edit (regex, action), or reject with a note. A rejected rule goes
back to the LLM with the note. Should approval also be required for every
new rule at first, until we trust it?

### 7. Where rules are stored (decided: git for rules, SQLite for state)

Still open: one file per rule (clean diffs, easy per-rule history) or one
file per source?

### 8. Multi-line events

Tracebacks, boot sequences and table dumps span many lines. One option is a
`window` action: "collect the next lines from this source for N seconds and
handle them as one event". This is probably for later.

## Numbers to keep in mind

On the box, the journal covers 2026-10-12 to 2026-10-13 and holds 36,564
lines at all priorities. The current regex templater turns them into 10,417
distinct templates, and 4,093 of those were still new on day 2. Most are
dumps logged one row at a time: installer file lists, dnf transaction
tables, llama-server's `--help` inside containers, and kernel boot output.
A good first measurement for the new design is how many LLM calls it takes
to cover 95% and 99% of lines.
