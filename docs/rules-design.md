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
- **Rules must not be too broad.** A rule that matches all or nearly all
  lines (from its source, or overall) is rejected or flagged. Measure this by
  replaying it against a recent journal window.
  Since 2026-10-14 a rule is rejected only when (a) it covers more than 20
  shapes and over 95% of the source's shapes in the batch, or (b) it matches
  other-shaped lines more severe than any of its examples, at warning or
  worse. More than 20 shapes otherwise is accepted and flagged for review:
  the earlier hard "more than 20 shapes" limit rejected good rules.
- **Fallbacks are templates, and they count and keep lines.** When the
  model's rules are all rejected, argus writes `unlearned_<source>_N` with a
  regex built from the group's template (numbers, IPs, hex and UUIDs become
  named groups), so one fallback covers the whole group instead of one exact
  line. Its actions are a daily count and `collect` (20 lines), so the
  matching lines can be read. The model's last rejected attempt is kept in
  the rule as `rejected`, and the review note says plainly that these are
  that attempt's problems, not the fallback's.
- **Each rule keeps its first 5 whole events** (every journal field) in the
  `rule_events` table, and an alert keeps the whole event that raised it, so
  the labeling page can show all fields and what the regex captured.
  `python -m argus rules examples --since -7d` fills them in for rules from a
  journal window; it only reads otherwise (no actions, counts or cursor).
- **Rules are scoped to a source** in general. Exactly what counts as a
  source is still open (see 3).
- **No approval needed for new rules**, but the page lists *all* rules, not
  only flagged ones.
- **Every rule keeps a hit counter and last-hit time**, separately from its
  action, even for `ignore`. This is metadata, not the action's state. It
  lets us find dead rules and see what's noisy.
- **Rule files are TOML**, in their own local git repo on the box
  (`~/work/argus-rules`, not on GitHub).
- **Read every priority level.** Audit entries have no priority at all.
- **Build order:** the normal path first (new lines only), then backfill
  as a trial run before it goes live. If backfill shows problems, rework
  and restart from scratch. The old daily scan keeps running until then.
- **Backfill** (the first run over existing history) needs extra machinery
  and probably a different strategy for the LLM part. It's handled
  separately from the steady state.

## Proposals under discussion (Andy's ideas, 2026-10-13; not decided)

- **Actions get the full journal event.** Every field goes in, with the regex
  groups merged on top, so `extract` can take `AUDIT_FIELD_ADDR` just as
  easily as a regex group. The LLM also sees full events when writing a
  rule, so it can choose `MESSAGE_ID` / `CODE_FILE` matches over a regex.
- **Rules match on any fields, with minimum specificity.** A rule must name
  a `_TRANSPORT` plus at least one trusted field that identifies the source.
  Which field depends on the transport:
  - journal/stdout/syslog: `_SYSTEMD_UNIT` / `_SYSTEMD_USER_UNIT` / `_EXE`
  - audit: `_AUDIT_TYPE_NAME`
  - kernel: 61% of entries have no subsystem, so a non-trivial `MESSAGE`
    regex is required instead
  Sender-set fields (`MESSAGE_ID`, `CODE_FILE`, ...) may narrow a match, but
  can't serve as the identity.
- **Volatile fields can't be matched on.** These are fields that would go
  stale: PIDs, boot/stream/invocation/job IDs, cursors, timestamps, random
  container names, transient scope names. Rather than a hand-made list,
  measure it: a field whose distinct-values-to-occurrences ratio is near 1
  is volatile. `journal-fields.md` has the counts, and the check adapts per
  host.
  Measured on the box: the share of each field's values that recur across
  boots (rough, since there are only 3 boots):
  - never recur (0-8%): cursors, timestamps, `_BOOT_ID`, `_PID`, `TID`,
    `SYSLOG_PID`, all `*INVOCATION_ID`, `_STREAM_ID`, `JOB_ID`, `LEADER`,
    `CONTAINER_ID/NAME`, `PODMAN_ID/NAME`, `SYSLOG_TIMESTAMP`
  - unstable (16-48%), usable only after normalizing: `_CMDLINE`,
    `_SYSTEMD_CGROUP`, `_SYSTEMD_UNIT` (session/run scopes), `_SYSTEMD_SESSION`,
    `SESSION_ID`, `_AUDIT_ID`, `SYSLOG_IDENTIFIER` (random container names)
  - stable (70-100%): `_TRANSPORT`, `_EXE`, `_COMM`, `_UID`,
    `_SELINUX_CONTEXT`, `_SYSTEMD_SLICE`, `PRIORITY`, `MESSAGE_ID`, `CODE_*`,
    `_KERNEL_*`, `_UDEV_SYSNAME`, `_AUDIT_TYPE_NAME`, `PODMAN_EVENT`, `NM_DEVICE`,
    and the normalized units

  This suggests an **allowlist of matchable fields** backed by this
  measurement. Volatile fields remain useful for **grouping**: `_STREAM_ID`,
  `_PID` and `INVOCATION_ID` identify one traceback or one run of a service.
- Actions get a read-only view of the event. `MESSAGE` can be a byte array,
  so decode it once, up front. Actions that store data store chosen fields,
  not whole entries, because those carry command lines, IPs and so on.
- **Different code paths per transport:**
  - **stdout:** group lines into bursts by `_STREAM_ID` and timing *before*
    matching. This is where multi-line output and noise get handled. On the
    box, 7,665 stdout lines came from just 59 streams, mostly an installer
    listing, llm-server, tailscaled, dnf5-automatic and open-webui.
  - **container output is not stdout.** podman's log driver writes it as
    `_TRANSPORT=journal` with `CONTAINER_*` fields, so it needs the same noisy
    path, grouped by container (or image) and `_PID`. Rules see both the
    whole group and the single line.
  - **audit:** almost all field rules, no regex.
  - **kernel:** treat boot-time output (low monotonic timestamp) as its own
    phase, since that's where most kernel noise is.
  - **journal (native):** try `MESSAGE_ID` and `CODE_FILE:CODE_LINE` first.

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

### 6. The review flow (decided: no approval by default, page shows all rules)

On the page: list rules flagged for review, with sample lines. Actions:
approve, edit (regex, action), or reject with a note. A rejected rule goes
back to the LLM with the note. Should approval also be required for every
new rule at first, until we trust it?

### 7. Where rules are stored (decided: git for rules, SQLite for state)

Decided: **one file per source** (e.g. `rules/kernel.toml`,
`rules/llm-server.service.toml`). Each file holds that source's rules in
order, oldest first, which matches "older rule wins" during dispatch.

### 8. Multi-line events and noisy sources (needs several ideas)

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

## First test on the box (2026-10-13)

Two runs over a 3-hour window of the box's journal with gpt-oss-120b: 30
rules learned, all 30 accepted, no fallbacks, no errors. After one pass the
rules matched 99.8% of the window without the model. Typical cost per rule:
2-4 model steps and 6-55 s; worst case 121 s.

What it taught us, and what changed:

- **Values that are always new.** llama-server prefixes each line with an
  uptime stamp like `330.46.111.636`; the model took it for a client IP and
  used `distinct` on it, so every line raised "new client". Validation now
  rejects `distinct`/`track` with an alert on a field that differs on more
  than 80% of the lines it sees.
- **Container lines logged twice.** A unit that runs podman attached logs
  every container line once with CONTAINER_ID and once more as the unit's
  stdout. The engine now skips the stdout copy when the container copy is
  within 2,000 events of it. Running the container detached would avoid the
  duplicate at the source.
- **argus watching itself.** Each model request makes about 8 llama-server
  lines, so the second run spent its whole learn budget on argus's own
  traffic. Once those rules exist this costs nothing, so it is left alone.
- **CODE_FILE on container output** is conmon's forwarding call, the same for
  every line, so it no longer counts as narrowing for `<unit>/container`.
- **SYSLOG_IDENTIFIER** is now matchable: the model reached for it constantly,
  and inside an already-trusted source it can only narrow.
- **`{new}` in distinct's on_new**, and unknown placeholders in a flag summary
  stay as written instead of leaving the whole summary unformatted.
- **The proposal cap is enforced**: past 4 proposals the model is refused and
  argus stores a fallback.
- **Prompt**: collect only what a human should read; routine changes (DHCP
  leases, NAT endpoints) are watch at most; distinct/track need a field that
  repeats.

## Trial run 1 (2026-10-14)

6 hours of the box's journal, real state: 4,526 lines (1,004 container echoes
skipped), 181 groups, 40 rules learned (38 accepted, 2 fallbacks) in 19
minutes, 37 alerts. Not good enough to go live, so it was reset. What went
wrong, and the fix:

- **A traceback became ~8 rules.** Each line of one repeating Open WebUI
  traceback was its own group, learned separately with different severities
  (one was `ignore`), and it raised 6 of the 7 attention alerts. Fix:
  `events.join_multiline` joins a Python traceback (same process or
  container, lines that look like frames, at most 2 s apart) into one event
  before grouping; `summarize` shows its start and end to the model.
- **Cold start called everything new.** On an empty database every unit and
  every DERP server fired distinct's on_new. Fix: in the run a rule is
  learned, its lines are a baseline (`ctx["quiet"]`): distinct and track
  record values but don't alert. Backfill gives every rule a long baseline.
- **`{n}` in threshold summaries** wasn't filled in. Now it is.
- Not fixed yet: several rules alerting on the same event (service start in
  systemd and audit, DERP in three tailscale rules); many tiny rules that
  each count one HTTP endpoint.

`python -m argus rules reset` sets the rules repo aside (renamed with a
timestamp) and clears the engine's tables, for starting over.

## Trial run 2 (2026-10-14)

Same 6-hour window size, after the fixes above: 6,908 lines, 235 groups, 40
rules (all accepted), 10.5 minutes, **3 alerts** (trial 1: 37). Tracebacks
joined cleanly (28 incidents, 23 lines each; loguru's `> File` line needed
adding). What was still wrong, and the fix:

- **Thresholds below normal volume.** DHCP renewals (72 in 6 h) and DERP idle
  closures (50 in 6 h) got thresholds of 10 a day. The model now sees the
  group's rate ("about N a day") and is told to set n well above it, and that
  routine lines at that rate shouldn't alert.
- **`{n}` reads like the real count.** The alert fires once when the count
  reaches n, so summaries should say "reached {n}"; the prompt says so.
- **One rule per URL / per step.** Open WebUI got 12 rules for 12 GET
  endpoints, llm-server 11 for one request's steps. The model now sees the
  other unmatched shapes from the same source and is asked to cover the same
  kind of line with one rule.

## Trial run 3 (2026-10-14)

8,774 lines, 241 groups, 40 rules but 10 of them fallbacks, 50 minutes, 0
alerts. The prompt now asked for one rule per kind of line, but the "too
broad" check rejected exactly those rules (33-109 shapes against a limit of
20), so the model retried until it ran out of attempts. Fixes:

- **Broad isn't rejected, it's flagged.** A rule covering more than 20 shapes
  is accepted and flagged for review. It's only rejected when it covers more
  than 80% of the source's shapes (a match-almost-everything rule).
- **Merging is spelled out.** The overlap message now says how to merge into
  the older rule (`name=<older>`, `replace=true`), since an early broad rule
  otherwise blocks every later one.
- **Thresholds**: 3-5x the normal daily volume, not "well above" (trial 3 set
  one at 40x, so it would never fire).
- **Shapes with durations.** tailscaled logs connection ages glued to words
  (`derp-9=cr10h28m0s`), so its line had a new shape every minute. Every
  learn slot of a 10-minute batch went to those, and each exact-line
  fallback never matched again. `template()` now turns durations into
  `<DUR>`, even inside a word. Fallbacks now match the line's shape (its
  text, with the varying parts left open) rather than the exact line.

## Trial run 4 (2026-10-14)

17,340 lines (llm-server was busy), 287 groups, 28 rules and 12 fallbacks,
43 minutes, 0 alerts. Open WebUI went from 12 rules to 5. Fixes:

- **Fallbacks blocked real rules.** "Overlaps with older rule
  unlearned_tailscaled_service_1" was the most common rejection: a fallback
  from earlier in the same run, which the model can't merge into. Fallbacks
  are now marked `fallback = true`, don't count in the overlap check, and a
  real rule that covers a fallback's sample replaces it.
- **Too broad at 80% was too strict.** Open WebUI is almost all GET requests,
  so one GET rule covered 32 of its 36 shapes. Now only above 95% is rejected;
  anything over 20 shapes is still flagged for review.
- **Thresholds overshot** (6-17x normal). The task now names a number:
  "about n=X per day (4x normal)".

## Trial run 5 (2026-10-14)

23,768 lines, 150 groups, **22 rules, all accepted, nothing pending, 9
minutes**. Follow-up 10-minute batches took under a second and learned
nothing new. tailscaled went from 17 rules to 2.

The one bad part was thresholds: from 75,000x normal to 1.05x. The rate the
model was shown came from the 20 example lines over their own time span (a
burst of seconds read as millions a day), and for a rule covering several
groups it saw one group's rate. Fix: argus now checks thresholds itself. It
counts what the proposed regex matches in the batch, over the hours the batch
covers, and rejects n below 2x normal or above 10x (with a suggested n of 4x).
The task shows the group's rate over the whole batch. Batches shorter than an
hour aren't used for this.

## Trial run 6 (2026-10-14)

25,049 lines, 155 groups, 27 rules (4 fallbacks, all one-off llm-server
startup lines), 14.5 minutes. Thresholds are now in range (3.6x and 4x); the
check rejected one proposal and the model fixed it.

The follow-up batch found a **feedback loop**: one of argus's own requests got
HTTP 500, llama-server dumped the model's whole output into its journal one
line at a time ("Thus propose rule.", `"actions": [`), and the next batch spent
all 10 learn calls writing rules for argus's own reasoning. Fixes:

- `llm.py` records when each of argus's requests ran
  (`$ARGUS_DATA/llm-requests.tsv`, last 3 days kept). Unmatched lines from the
  model's unit (`ARGUS_LLM_UNIT`, default `llm-server.service`) written during
  one of them (2 s before to 10 s after) are counted and skipped, never
  learned. Lines a rule matches are still handled as usual.
- **Echoes with trailing spaces**: the container check compared lines with only
  the newline stripped, so `[42823] ` and `[42823]` didn't match. It strips all
  trailing whitespace now.
- **Action shapes**: 8 proposals were rejected for writing a block as `"count"`
  or `{"count": {...}}`. Those are accepted and normalized now.

## Trial run 7 (2026-10-14)

The skip works on fresh data: two new HTTP 500 dumps during the follow-up
batches (41 lines) were skipped and nothing was learned from them. But trial
6's dump, from before argus recorded its requests, was still in the 6-hour
window, and about 30 rules were learned from it. `python -m argus rules
import-requests` recovers request windows for the last 3 days from the agent
transcripts (a transcript that ends without an answer, i.e. a failed request,
gets 15 extra minutes, since that's when the dump is written), so a reset and
re-run doesn't learn it again.

## Trial run 8 (2026-10-14)

After `reset` and `import-requests`: 33,445 lines, 15,329 skipped as argus's
own model traffic, 13 rules learned (all accepted), nothing pending, 0
alerts, **3 m 52 s**. A follow-up 11-minute batch took 0.13 s. No rule came
from model output.

Known gap: the request windows covered 83% of those 6 hours because trials
ran nearly nonstop, so llm-server's ordinary lines (slots, timings, proxying)
were skipped too and will be learned later, when the model serves someone
else while argus is idle. In steady state, other people's model traffic
during an argus request isn't learned from. Narrowing the skip to argus's own
llama-server slot or task would fix that if it matters.
