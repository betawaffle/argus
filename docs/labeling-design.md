# Design: a labeling tool for Andy

Status: **v0 built (2026-10-14).** Code: `labeler/` (the generic tool) and
`argus/label_sources.py` (argus's adapters). The page is `/label` on the argus
page. How to use it is at the end.

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
  After the first labeling pass, `unsupported`, `unclear`, `should_be` and
  `no_better_option` were added, picked from Andy's notes.
- **Duplicates and related items are links, not copied labels.** Andy links
  an item to one he already judged and leaves it to a later pass (a person
  or an LLM) to work out what he meant. Labels are never carried across.
- **Verdict** is `good / bad / unsure` with an optional confidence.
- **JavaScript is fine** for the review screen.
- **Adapters:** reports, rules and alerts. If only one can come first, reports.
- **The verdict judges the item as it stands.** Separately, a `bad` verdict
  can say it's **fixable**: it would be good if the marked things were fixed.
- **Marks carry a reason flag**: is this *why* it's good or bad? By default,
  marks that point the same way as the verdict are reasons (wrong, missing
  and from-here on a bad; good on a good). Andy can flip any of them, so a
  minor nit can be noted without counting as a cause. Together with
  `fixable`, this records cause, not just verdict.

## The pieces

```
source adapter  --items-->  label core  --labels-->  storage
  (argus rules,              (queue, review           (SQLite)
   argus reports, ...)        screen, keyboard)
```

### Item: the thing being judged

An item is an ordered list of **parts**, plus **context** shown alongside.
Both can be marked; only the parts' order matters for "from here".

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
    fixable  = True,             # bad only: good if the marked things were fixed
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
Mark(part="regex", span=(6, 8), kind="wrong", reason=True,
     note="too broad, matches every probe")
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
  - `unsupported`: a claim with no evidence behind it ("the disk is
    failing" with nothing that shows it).
  - `unclear`: hard to understand, or ambiguous.
  - `should_be`: this bit should say something else. The mark has a `fix`
    field with the replacement text ("ignore", "watch").
  - `no_better_option`: not ideal, but nothing better was available. It is
    never a reason for a bad verdict by default.
  - `should_use` / `should_not_use`: this (usually a field) should, or
    shouldn't, have been relied on, e.g. "it should have matched on this
    field".
- `reason`: is this mark a reason for the verdict? (see Decided)
- `note`: optional free text

In storage, `span` is two columns, `start` and `end`: both empty means the
whole part, and `start = end` is a point (where something is missing, or
where it went wrong). A mark also records which item version its part
belongs to, since it may point into a related item.

### Related items: when the fault is upstream

An item can list **refs** to other items, e.g. an alert refs the rule that
raised it. A bad alert is evidence against its rule, so the review screen
shows the ref, and a mark can point into the referenced item's parts too
("the threshold in this rule is too low"). The core only stores the link.
What it means is up to the adapter, which in v0 does nothing with it.

### Links: duplicates and related items

A label can carry **links** to other items: `duplicate` (this is the same as
that one) or `related` (look at these together). A link points at the
target's current version and has an optional note. Nothing interprets links
yet. They are there so a later pass, by a person or an LLM, can work out
what was meant, without Andy having to label the same thing twice.

### Groups: near-duplicates

An adapter can give an item a **group** key. Items with the same key are
near-duplicates: the same finding about different files, alerts from one
rule, fallback rules from one source. The group is not part of the item's
version, so regrouping never makes an item look new. The queue shows one
unlabeled item per group with "+N like this", and an item's screen offers to
skip the rest of its group as duplicates of it (`D`), which saves a `skip`
label with a `duplicate` link on each. `v` lists the others first, with a
bit of each one's text, and any that aren't really the same can be unticked
so `D` leaves them in the queue.

### Fields parts and live parts

A part of kind `fields` holds one `key=value` per line, e.g. a whole journal
event or the groups a regex captured. Keys are drawn muted, and clicking a
line then pressing a mark key marks that whole field.

An item can also have **live** parts: shown with the context, but not part
of its version, for facts that change all the time like hit counts. Without
this, every hit would make a rule look new and send it back to the queue.

### When an item changes

A changed item is a new version, and the old version's labels stay with it.
If nothing that was judged changed (the parts are identical and no mark
points at changed text, e.g. only an example or a hit count moved), the
latest label is carried onto the new version automatically, marked as
carried (`carried_from`). A plain skip on the new version (no marks,
links or note) doesn't block this, since a skip isn't a judgment; but once a
label has been carried, skipping it sticks. Otherwise the item comes back with a banner saying
what changed, and `K` starts from the old label, leaving out marks on text
that changed.

### Skip for now

`s` skips an item to come back to: skipped items come after everything not
yet labeled, oldest skip first, and the item's screen says when you've
reached them. A skip with a duplicate link counts as done. Pressing `s` on an
item you've already judged, without changing anything, keeps your label.

### Source adapter: the only source-specific code

```python
class Source:
    name: str
    def items(self) -> Iterable[Item]: ...         # what could be labeled now
    def labeled(self, item, label) -> None: ...    # optional: react to a label
```

The core asks each adapter for items, keeps what's new, and calls back when
a label is saved. In v0 no adapter does anything with it. Labels are just
stored.

## The argus adapters

1. **`argus.rule`**: one item per rule, from the TOML files. The parts are
   `match`, `regex` and `actions`. The context is the samples, the model's
   reason, the hit count, and the review note if the rule is flagged. The
   question: is this a good rule? Flagged rules and rules with many hits
   come first.
2. **`argus.report`**: one item per model-written report. That's both the
   daily scan's findings (live now) and the rule engine's investigations.
   The text is split into paragraphs, one part each, so `from_here` works
   naturally. The context is the log pattern and example line, or the
   events the investigation looked at. An investigation also refs its rule.
   The question: is this report right, and is the verdict right?
3. **`argus.alert`**: one item per alert a rule raised. The part is the
   alert's summary. The context is the matching events and a link to the
   rule that raised it. The question: was this worth your attention? A
   `bad` verdict here means "this alert shouldn't have been created", which
   is a fault in the rule's action (wrong threshold, wrong severity, or it
   shouldn't alert at all), not in the alert text.

Rules and reports are deliberately different shapes, short structured code versus
long prose, so the core gets tested on both from the start.

## The review screen

One item per screen at `/label/i/<id>` on the argus page (:8082). The item's
parts come first, then its context, then any related items. A panel on the
right holds the verdict, fixable, confidence, note and the list of marks.
`?` shows every key.

- Select some text, then press `w` (wrong) or `g` (good). With nothing
  selected, the key marks the whole part you last clicked (or moved to with
  `j`/`k`).
- Also on a selection or a part: `e` unsupported, `i` unclear, `o` no
  better option, and `b` should be, which opens a box for the replacement
  text.
- Click a spot, then `h` (went wrong from here) or `m` (missing here).
  `M` says something is missing from the item as a whole.
- `d` links this item as a duplicate of one you already labeled, `l` as
  related. A picker lists your recent items with a filter; `Enter` takes
  the first match. A duplicate link alone is enough to save, as a skip.
- `D` skips every other unlabeled item in this item's group as a duplicate
  of this one, after a confirm.
- Everything after a "from here" mark is dimmed, so you can see what you've
  implicitly called OK.
- Click a highlight (or use `,` `.`) to select a mark. Then `r` flips whether
  it's a reason, `x` deletes it, and `n` adds a note to it. Reasons have a
  solid underline; other marks are dashed.
- `1` good, `2` bad, `3` unsure; `f` fixable; `c` cycles confidence; `N`
  writes a note on the item.
- `Enter` saves and moves to the next item; `s` skips; `u` back; `q` queue.

This is argus's first JavaScript: `labeler/static/label.js`, vanilla, with no
build step. A mark's span is a character offset into the part's text, so the
page must show the text exactly as stored. Point marks are drawn with CSS so
they don't add characters.

## Storage

Its own SQLite file, `label.db`, next to `argus.db`
(`~/.local/share/argus/`), with four tables: `items` (source, key, version
hash, snapshot JSON, group, first and last seen, current), `labels`, `marks`
and `links`. New columns are added on startup, so an older `label.db`
upgrades itself. A separate file makes the separation real and the labels easy to
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
- **Categories** for marks, once the notes show which ones keep coming up.

## How it's put together

| File | What it does |
|---|---|
| `labeler/core.py` | `Item`, `Part`, `Source`; `Store`: syncs items into snapshots and versions, queues them, saves labels, exports |
| `labeler/web.py` | `App`: the queue, the review screen and the label API, mountable under a prefix in any stdlib server |
| `labeler/static/label.js`, `label.css` | the review screen |
| `argus/label_sources.py` | argus's three adapters |
| `argus/serve.py` | mounts the app at `/label` |
| `tests/test_labeler.py` | core, web and adapter tests |

The queue page re-reads the sources each time it loads. Any review page
re-reads them if they're more than five seconds old, so new findings, alerts
and rule edits show up without a restart. A changed item gets a new version
and goes back in the queue. If you labeled an older version, the page says so
and links to it.

## Using it

- Open `http://framework:8082/label`. Pick a source, or just press
  "Start labeling".
- `http://framework:8082/label/export.jsonl` gives every label with the exact
  item it judged, one per line. That's the starting point for measuring
  gpt-oss against you, or for fine-tuning.
- The page needs the argus serve unit restarted after pulling:
  `cd ~/work/argus && git pull && systemctl --user restart argus-web`.

## Open questions

None right now.
