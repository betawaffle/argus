# Design: a labeling tool for Andy

Status: **design agreed in outline (Andy + Claude, 2026-10-14). Nothing is built yet.**

## The idea

Some judgement calls only a human can grade: is this rule any good, is this
report right, where did the model go off the rails? The labeling tool gets
as much of that judgement as possible out of a few minutes of Andy's time,
and stores it in a form we can use later. That could mean checking how much
to trust the local model, correcting what it made, or fine-tuning it.

argus is the first user, but **the tool is generic**. It doesn't know what a
log line or a rule is. We'll learn how to generalize it by using it.

Background: the idea was parked in the project's brainstorm queue from the
Concepts thread (pairwise questions, active learning, pre-labels with review
of disagreements, a fast keyboard flow, optional confidence and notes).

## Decided so far

- **Start with argus**: grade the rules and reports the local model writes.
- **Keep it generic.** argus-specific code lives only in an adapter.
- **More than a thumbs-up or thumbs-down.** On top of the overall verdict,
  Andy can point at specific parts: this bit is wrong, this bit is good, it
  went wrong *from here*.
- **No fixed categories for marks.** Each mark has a free-text note.
  Categories can grow out of the notes later.
- **It lives inside the argus repo for now**, as a separate package that
  never imports argus, so it can move out later. argus itself may become
  more generic than log lines over time.
- **Mark kinds** start as `wrong`, `good`, `from_here`, `missing` (2026-10-14).
- **Verdict** is `good / bad / unsure` with an optional confidence.
- **JavaScript is fine** for the review screen.
- **Adapters:** reports, rules and alerts. If only one can come first, reports.

## The pieces

```
source adapter  --items-->  label core  --labels-->  storage
  (argus rules,              (queue, review           (SQLite)
   argus reports, ...)        screen, keyboard)
```

### Item: the thing being judged

An item is an ordered list of **parts**, plus **context** shown alongside but
not markable.

```python
Item(
    source  = "argus.rule",          # which adapter
    key     = "i8042_probe_failed",  # stable id within the source
    title   = "i8042_probe_failed (kernel)",
    parts   = [                      # ordered: "from here" means this and after
        Part("match",   "code", '{ _KERNEL_SUBSYSTEM = "serio" }'),
        Part("regex",   "code", r"probe .* failed with error -(?P<err>\d+)"),
        Part("actions", "code", 'count(window="day")'),
    ],
    context = [
        Part("samples", "text", "serio: i8042 KBD port probe failed with error -5\n..."),
        Part("why",     "text", "the model's stated reason"),
    ],
)
```

A part has an id, a kind (`text`, `code` or `field` for now; the kind only
changes how it's displayed), and its content as a string. Every mark points
into a string, so marks work the same for every source.

**The core stores a snapshot of exactly what was shown**, and a hash of it.
Rules get edited and reports get rewritten, so a label must always point at
the version Andy actually saw. If a source later offers a changed item under
the same key, it shows up as a new version that hasn't been labeled yet.

### Label: one judgement of one item version

```python
Label(
    item     = <item version>,
    by       = "andy",           # later also "claude", "gpt-oss" for pre-labels
    verdict  = "bad",            # good | bad | unsure | skip
    confidence = None,           # optional, 1-3
    note     = "",               # optional free text
    marks    = [Mark(...), ...],
    t        = <time>,
)
```

Labels are append-only: relabeling adds a new label, and the latest one from
each labeler counts. So the history of a changed mind is kept.

### Mark: pointing at something specific

```python
Mark(part="regex", span=(6, 8), kind="wrong", note="too broad, matches every probe")
```

- `part`: which part (context parts can be marked too, e.g. "this sample
  doesn't belong")
- `span`: character range within the part, or none for the whole part
- `kind`:
  - `wrong`: this bit is wrong
  - `good`: this bit is right, or especially good
  - `from_here`: it went wrong starting here. At most one per item.
    Everything before it, in part order, is implicitly OK, which is a lot of
    labels for one keypress. This matters most for long text like reports.
  - `missing`: something that should be there isn't ("should also extract
    the PID"). Its span marks where it should go, or it's on the whole item.
- `note`: optional free text

### Related items: when the fault is upstream

An item can list **refs** to other items, e.g. an alert refs the rule that
raised it. A bad alert is evidence against its rule, so the review screen
shows the ref, and a mark can point into the referenced item's parts too
("the threshold in this rule is too low"). The core only stores the link.
What it means is up to the adapter, which in v0 does nothing with it.

### Source adapter: the only source-specific code

```python
class Source:
    name: str
    def items(self) -> Iterable[Item]: ...         # what could be labeled now
    def labeled(self, item, label) -> None: ...    # optional: react to a label
```

The core asks each adapter for items, keeps what's new, and calls back when
a label is saved. In v0 nothing calls back. Labels are just stored.

## The first two argus adapters

1. **`argus.rule`**: one item per rule, from the TOML files. The parts are
   `match`, `regex` and `actions`. The context is the samples, the model's
   reason, the hit count, and the review note if the rule is flagged. The
   question: is this a good rule? Flagged rules and rules with many hits
   come first.
2. **`argus.report`**: one item per investigation report. The report text
   is split into paragraphs, and each paragraph is one part, so
   `from_here` works naturally. The events it looked at are the context.
   The question: is this report right, and is the verdict right?

3. **`argus.alert`**: one item per alert a rule raised. The part is the
   alert's summary. The context is the matching events and a link to the
   rule that raised it. The question: was this worth your attention? A
   `bad` verdict here means "this alert shouldn't have been created", which
   is a fault in the rule's action (wrong threshold, wrong severity, or it
   shouldn't alert at all), not in the alert text.

The first two are deliberately different shapes, short structured code versus
long prose, so the core gets tested on both from the start.

## The review screen

One item per screen at `/label` on the existing argus page (:8082):

- The parts, in order, with the context to the side or below.
- Select some text with the mouse, then press a key to mark it: `w` wrong,
  `g` good, `h` from here, `m` missing. Pressing the key with nothing
  selected marks the whole part under the cursor. Click a mark to add a note
  or delete it.
- Overall verdict: `1` good, `2` bad, `3` unsure; `s` skips. `n` writes a
  note on the whole item.
- `Enter` saves and moves to the next item. `u` goes back.

This is argus's first JavaScript; every other page is plain HTML forms. It
will be one small vanilla-JS file with no build step.

## Storage

Its own SQLite file, `label.db`, next to `argus.db`, with three tables:
`items` (source, key, version hash, snapshot JSON, first seen), `labels` and
`marks`. A separate file makes the separation real and the labels easy to
copy or export on their own.

## Not in v0 (later, as we learn)

- **Pre-labels and disagreements**: gpt-oss (or Claude) labels first, and
  Andy only sees the items where it disagrees with itself or with him.
- **Active learning**: once something produces a score, show the items it's
  least sure about.
- **Pairwise questions**: "which of these two rules is better?" Same core,
  with an item that holds two candidates.
- **Feeding labels back**: e.g. a `bad` rule gets flagged and re-learned
  with Andy's marks as the hint.
- **Export** to JSONL for fine-tuning.
- **Categories** for marks, once the notes show which ones keep coming up.

## Open questions

None right now. The first round was answered on 2026-10-14 (see Decided).
