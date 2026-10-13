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
- **State lives in SQLite**, unless we hit something it can't do.
- **Backfill** (the first run over existing history) needs extra machinery
  and probably a different strategy for the LLM part. It's handled
  separately from the steady state.

## Open questions

### 1. The first set of building blocks

A starting proposal:

| block | what it does |
|---|---|
| `ignore` | do nothing (the rule's hit counter still counts) |
| `count(window)` | count matches per window (hour/day) |
| `threshold(n, window, then)` | when the count in a window reaches n, run `then` |
| `extract(table)` | store the named groups as a row in a per-rule table, with the timestamp |
| `flag(severity, summary)` | create a finding on the page (`watch` / `attention`) |
| `collect` | keep the full lines (the review default) |
| `investigate` | run the current agent investigation with these lines as context |

Actions compose as a list, e.g. `[extract, threshold(5, day, flag(watch))]`.
What's missing? What would you want `then` to be able to do?

### 2. Overlapping rules

Name uniqueness doesn't stop two *regexes* from matching the same line.
Options:
- At creation, test the new regex against the stored sample lines of every
  other rule from the same source. Treat an overlap like a name conflict:
  give the LLM both rules and ask it to merge or narrow them.
- At dispatch, first match wins, in creation order (older rules win).

Proposal: both. Check at creation, and use first-match-wins as the
tiebreaker.

### 3. What a regex matches against

Proposal: the message text only, plus an optional source filter (systemd
unit or syslog identifier) as a separate field of the rule. Dispatch looks
up the rules for a line's source first, then the source-less rules, which
keeps it fast. Priority could be another optional filter.

### 4. How the LLM hands back a rule

Asking gpt-oss for JSON in plain text breaks llama.cpp (HTTP 500, see
how-it-works.md). Tool calls work reliably, though, so the model would
call a `propose_rule(name, regex, source, action)` tool. argus validates the
rule and returns the result as the tool's output, so the model can fix
problems in the same conversation before we give up and flag it.

The model would also get a `test_regex(regex, lines)` tool to try regexes
before proposing, and `find_rule(name)` / `list_rules(source)` to see what
exists.

### 5. Collecting lines until review

Some unmatched things are huge, e.g. an installer's file listing logged
line by line (3,259 lines in one session on the box). Proposal: keep the
first N lines (e.g. 200) plus a total count per rule.

### 6. The review flow

On the page: list rules flagged for review, with sample lines. Actions:
approve, edit (regex, action), or reject with a note. A rejected rule goes
back to the LLM with the note. Should approval also be required for every
new rule at first, until we trust it?

### 7. Where rules are stored

- In SQLite with everything else, or
- as files (one per rule, or a single rules file) in a git repo on the box,
  so every change the LLM makes is a reviewable diff, with history and
  revert for free.

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
