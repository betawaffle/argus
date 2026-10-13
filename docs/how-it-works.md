# How argus works

argus is a small agent harness around a local LLM. An agent harness is the
code that wraps a model: it sends the model a task, runs the tools the model
asks for, and feeds the results back until the model is done. Claude Code is
one of these. argus is a homemade one, about 700 lines of standard-library
Python, so every part of it can be read and changed.

Its first job is a log watcher. It reads the systemd journal of an unattended
machine, picks out what looks new or unusual, has the model investigate each
item with read-only commands, and records a verdict you can browse on a web
page. The verdicts and your feedback on them are remembered, and the model
sees them next time.

This document covers the whole thing: the moving parts, why each one is
shaped the way it is, what we learned from the first runs, and what's still
open.

## The big picture

```
 journalctl -o json -p warning          (plain code, no model)
          │
          ▼
 patterns.py: template each line  ──►  SQLite: patterns, daily counts, cursor
          │
          │  only patterns never judged, or spiking today
          ▼
 agent.py: one agent run per pattern                       (the model)
   ├─ system prompt: you are argus, investigate, end with VERDICT: ...
   ├─ task: the pattern + earlier verdicts on the same service + your notes
   └─ loop: model ⇄ tools.run (read-only commands, allowlisted)
          │
          ▼
 SQLite: findings (verdict, summary, full answer, transcript path)
          │
          ▼
 serve.py: page on :8082. You agree/dismiss/annotate ──► feeds the next run
```

There are two kinds of work here, and the split between them is the main
design decision.

- **Plain code does the bulk work.** Reading tens of thousands of log lines
  and grouping them is fast, deterministic and free in code. It would be slow
  and unreliable in a model.
- **The model does the judgment.** It gets one pattern at a time, looks
  around the machine, and says whether a human should care.

The model would be a poor fit for the bulk work because of speed. gpt-oss-120b
on the box reads prompts at roughly 1,000 tokens per second and writes at
roughly 50. A day of warning-level journal on the box is a few hundred lines.
On a NAS it can be thousands, and verbose lines run 50 to 100 tokens each.
Feeding all of that in raw would take minutes per scan before the model even
started thinking, and the context would fill with repeats. Collapsing to
patterns first means the model sees a few dozen distinct things instead.

## The parts

### `llm.py`: talking to the model

llama.cpp's server speaks the OpenAI chat-completions API, so the client is a
single POST to `/v1/chat/completions` with `urllib`. No SDK is needed. The
request contains:

- `messages`: the conversation so far (system, user, assistant and tool
  messages).
- `tools`: JSON Schema descriptions of the functions the model may call.

The response's `choices[0].message` is either a final answer (`content`) or a
request to call tools (`tool_calls`, each with an id, a function name and
JSON-encoded arguments). llama.cpp turns gpt-oss's native output format
("harmony") into this OpenAI shape for us.

The defaults are `ARGUS_LLM_URL=http://framework:8081/v1` and
`ARGUS_LLM_MODEL=gpt-oss-120b`. The timeout is 15 minutes, because the model
unloads after 15 idle minutes and the first request reloads 63 GB. In
practice the reload took about 10 seconds.

If the server returns an HTTP error, the client raises with the server's
error text. That is how we found the JSON problem described under
[Lessons from the first runs](#lessons-from-the-first-runs).

### `agent.py`: the loop

The loop is the core of any harness, and it's short:

```
messages = [system, user(task)]
repeat up to 20 times:
    reply = model(messages, tools)
    append reply to messages
    if reply has no tool calls: return reply.content      # done
    for each tool call:
        result = run the tool
        append {"role": "tool", "tool_call_id": ..., "content": result}
```

The model decides when it's finished: a reply with no tool calls is the
answer. The 20-step cap is a backstop. In the first real scan, investigations
took 3 to 13 steps.

Some details that matter:

- **Reasoning is dropped from history.** gpt-oss writes its chain of thought
  into `reasoning_content`. We log it but don't send it back on later steps.
  Every token in the history is re-read on every step, and the model doesn't
  need its old thoughts, only what it did and what came back. This keeps
  prompts small. Per step they stayed under about 3K tokens on the first
  test.
- **Tool errors go back to the model.** If a tool raises, the error text
  becomes the tool result rather than crashing the run. The model usually
  recovers by trying something else.
- **Everything is logged.** Each run writes a JSONL transcript to
  `~/.local/share/argus/transcripts/`. It records every user message, every
  assistant message (including reasoning), every tool call and result, and
  per-step timings and token usage. Nothing a run did is thrown away, so any
  run can be analyzed later.
- **Prompt caching comes for free.** llama.cpp reuses its cache for the
  unchanged prefix of the conversation. Each step only pays for the new tool
  result, not the whole history. The box test showed `cached_tokens` close
  to the previous step's prompt every time.

### `tools.py`: what the model is allowed to do

There's one tool, `run(command, host="local")`. It runs a diagnostic command
and returns its output. The model sees it as:

> Run a read-only diagnostic command (no shell, no pipes). Allowed:
> cat, df, dmesg, ... Output is truncated, so prefer narrow queries
> (e.g. journalctl -u NAME --since -1d -n 50).

Read-only is enforced by code, not by asking nicely:

1. **No shell.** The command string is split with `shlex` and executed
   directly. `;`, `|`, `&&` and `$(...)` are not interpreted. If the model
   writes them, they are refused with a hint (use `journalctl -g` to filter,
   `-n` to limit).
2. **Allowlisted commands.** Only commands in `ALLOWED` can run. Some are
   restricted by their first argument: `systemctl` may run `status`, `cat`,
   `show`, `list-units` and the like, but not `restart`; `zpool` may run
   `status` and `list`, but not `scrub`.
3. **Forbidden flags.** Some allowed commands have flags that change state or
   never return. `journalctl --vacuum-*` deletes logs, `dmesg -C` clears the
   kernel buffer, `tail -f` follows forever, and `grep -r` has no bound.
   These are refused.
4. **Secret paths.** The model can read anything the user can, so paths like
   `~/.ssh`, `/etc/shadow` and `/var/lib/tailscale` are refused outright.
5. **Output cap.** Results over 12,000 characters are truncated. A huge
   result would cost the model many seconds to read on every later step.

Some conveniences make up for the missing shell:

- **Path globs are expanded** (`ls /usr/lib*/libseccomp*`), and the result is
  re-checked against the secret paths.
- **Trailing `|| true`, `2>/dev/null` and `2>&1` are silently dropped.** The
  model adds these out of shell habit and wouldn't stop, even after being
  told. In the first scan they caused about 24 of 109 commands to be refused.
- **`smartctl` and `dmesg` run under `sudo -n`**, since they need root to read
  anything useful.

**Remote hosts:** with `host="moneta"`, the same check runs on the box first.
Then the command goes over `ssh -o BatchMode=yes moneta '<quoted argv>'`.
Arguments are quoted with `shlex.join`, so the remote shell doesn't expand
anything either. This needs an ssh key from the box to moneta, which isn't
set up yet.

### `patterns.py`: turning logs into patterns

`read_journal` runs `journalctl -o json -p 4`. JSON output includes the
metadata fields (unit, priority, timestamp, cursor), and `-p 4` means
warning and worse.

Each entry gets two things:

- **A source (`ident`).** This is usually the systemd unit. A few special
  cases came out of the first run:
  - User services all report `_SYSTEMD_UNIT=user@1000.service`, which lumped
    Open WebUI, llama-server and everything else together. We use
    `_SYSTEMD_USER_UNIT` instead.
  - Each `systemd-run` / `gpu-run` job gets a fresh scope name like
    `run-p5734-i57425.scope`. These are collapsed to `run-<N>.scope`.
  - Container output is named `container:<name>`.
- **A template.** Variable parts of the message are replaced with
  placeholders, in this order: UUIDs, IPv4, IPv6, hex, then plain numbers.
  For example:

  ```
  i8042: probe with driver i8042 failed with error -6
  → i8042: probe with driver i8042 failed with error -<N>
  ```

  Numbers glued to letters (`i8042`, `sda1`, `ext4`) are left alone, because
  they are names, not values.

`(host, ident, template)` is a pattern. Each pattern has a row in the
`patterns` table with its first and last time seen, a total count and one
example line. A `daily` table has counts per pattern per day.

**Cursors:** after each scan, the journal cursor of the last entry read is
saved per host. The next scan asks for entries after that cursor, so
nothing is counted twice and nothing is skipped. `--since` only applies to a
host's first scan.

**Container output** gets special treatment. podman's journald log driver
writes everything a container prints at priority `err`, so the priority means
nothing for those lines. On the box, two thirds of all warning-or-worse lines
were Open WebUI migrations, llama-server load messages and the like. Container
lines are kept only if they match trouble words (error, exception, traceback,
fail, fatal, panic, OOM, killed, denied, refused, timed out, segfault,
corrupt). That cut the box's 3-day pattern count from about 300 to 85.

### Choosing what to investigate

A scan (`python3 -m argus scan`) investigates:

- **Every pattern that has never been judged.** That includes ones left over
  from a previous run that hit `--limit`.
- **Every pattern that's spiking today.** That means at least 20 occurrences
  today and more than 5 times its average daily count before today.

The first scan of a busy machine would otherwise investigate everything. You
can run `scan --baseline` instead, which records every current pattern as
`baseline` (normal background) without calling the model. We didn't use
baseline on the box, because the backlog was small enough to just
investigate.

### The investigation prompt

Each pattern gets a fresh agent with this system prompt (abridged):

> You are argus, watching unattended Linux machines. You are given one log
> pattern that is new or suddenly frequent. Investigate it with the run tool
> ..., then decide whether a human should care. You may be shown earlier
> verdicts on the same service and the owner's feedback on them; respect that
> feedback. Machines: "local" is a Fedora workstation used for ML experiments;
> "moneta" is a NixOS NAS with ZFS.
>
> End your answer with one plain-text line, nothing after it:
> `VERDICT: ignore|watch|attention - <one sentence summary>`

The task message is the pattern as JSON (host, ident, priority, template,
counts, first and last seen, one example line). That's followed by a list of
earlier verdicts on other patterns from the same service, with your feedback
and notes, if there are any.

Each pattern gets its own fresh agent, rather than one long conversation, so
that one investigation's output doesn't fill the context for the next.

### Verdict memory: getting quieter over time

This is the part that's most "ours". Every finding is stored. On the page you
can mark a finding **agree** or **dismiss** and leave a **note**, such as "this
box has no PS/2 port, i8042 errors are expected". When a later scan
investigates any pattern from the same host and service, the model is shown
up to six earlier verdicts on that service, those with your feedback first.

The idea is that your judgment accumulates without retraining anything.
Dismiss enough noise from a service, and the model learns from context that
this service's chatter is normal for this machine. It's cheap,
inspectable (you can read exactly what it was shown) and reversible (change
the feedback).

It's also the newest and least proven piece. See
[Open questions](#open-questions-and-ideas).

### `serve.py`: the page

`python3 -m argus serve` runs a small stdlib `http.server` on port 8082. It's
single-threaded and uses no JavaScript.

- **`/`** lists open findings: the latest finding per pattern, with verdict
  attention or watch, and not dismissed. Attention comes first. Each row has
  agree and dismiss buttons.
- **`/?all`** shows everything, including ignore and baseline.
- **`/f/<id>`** shows one finding: the pattern, the example line, the model's
  full answer, the transcript path, the feedback buttons and a note field.

There's no login. The box's firewall allows only SSH on the LAN, and
Tailscale is trusted, so only your tailnet can reach it.

### Running it on a schedule

`systemd/` has three user units and an install script:

- `argus-web.service` keeps the page running.
- `argus-scan.service` runs one `scan --limit 10`.
- `argus-scan.timer` triggers the scan daily at 06:00. It's `Persistent`, so
  a missed run happens at the next boot.

`systemd/install.sh` copies them to `~/.config/systemd/user` and enables
them. Linger is on for `andy`, so they run without a login session.

## Where the data lives

Everything is under `~/.local/share/argus/` (override with `ARGUS_DATA`),
never in the repo:

```
argus.db                 SQLite
  patterns   (id, host, ident, priority, template, first_seen, last_seen, total, sample)
  daily      (pattern_id, day, count)
  cursors    (host, cursor)
  findings   (id, pattern_id, t, verdict, summary, answer, transcript, feedback, note)
transcripts/
  YYYYMMDD-HHMMSS.jsonl  one per agent run
```

`sqlite3 ~/.local/share/argus/argus.db` is the quickest way to poke at it.
For example:

```sql
SELECT verdict, count(*) FROM findings GROUP BY verdict;
SELECT ident, count(*) FROM patterns GROUP BY ident ORDER BY 2 DESC LIMIT 10;
```

## Using it by hand

```
cd ~/work/argus
python3 -m argus ask "Is the zpool healthy?"     # free-form question, same tools
python3 -m argus scan --since -3d --limit 15     # investigate
python3 -m argus scan --baseline                 # accept current noise without the model
python3 -m argus serve                           # page on :8082
```

`ask` uses the same loop and tools with a general sysadmin prompt. It's handy
for seeing how the model investigates something you choose.

## Lessons from the first runs

These came from the first two runs on the box (2026-10-13), and most of them
shaped the code above.

- **Don't ask gpt-oss for JSON through llama.cpp.** We first asked for a
  final line of JSON. gpt-oss then switched to its "constrained JSON" output
  channel, which llama.cpp's response parser doesn't accept, so the server
  returned HTTP 500 on every scan. A plain-text `VERDICT:` line works. The
  parser still accepts JSON in case a future version fixes this.
- **The model thinks it has a shell.** It reached for pipes constantly
  (`journalctl ... | grep`, `dmesg | grep`, `... || true`). The refusal hint
  helped somewhat: it discovered `journalctl -g` on its own. The cheapest fix
  was to quietly drop harmless suffixes and to allow `grep` with file
  arguments.
- **Missing tools lead to wrong conclusions.** Without glob expansion,
  `ls /usr/lib64/libseccomp*` returned "no such file", and the model
  confidently decided libseccomp wasn't installed (it is). Without `getent`
  or `id`, it couldn't check whether the `tss` user exists, so it guessed.
  When a tool is missing, the model doesn't say "I couldn't check". It
  reasons from the failure. Giving it the right read-only tools matters more
  than prompt wording.
- **Verdicts lean cautious.** Summaries sometimes say "benign" next to a
  `watch` verdict.
- **It found something real.** In the first scan, the only `attention`
  verdict was correct: the `wireless-regdb` package isn't installed, so the
  kernel can't load its Wi-Fi regulatory data.
- **Speed is fine for batch work.** Steps took 2 to 11 seconds, and a whole
  investigation took 9 to 66 seconds. The first scan (15 patterns, 109
  commands) took 8 minutes.

## Open questions and ideas

These are deliberately left open. They're the interesting part.

- **Does the verdict memory actually work?** We need a few weeks of feedback
  to see whether dismissals make later verdicts quieter, or whether the model
  ignores them. Every transcript records exactly what the model was shown, so
  this is measurable. Comparing verdicts with and without the history block
  on the same patterns would be a good small experiment.
- **Is "same service" the right unit of memory?** History is matched by host
  and ident. That's too narrow for some things (the same udev rule warning
  shows up as two patterns) and too broad for kernel messages. An
  alternative is to match on template similarity.
- **Spikes are re-investigated every scan on the day they spike**, even if
  that pattern was already judged. That could get repetitive.
- **Better grouping.** The regex templating is simple. Log-parsing
  algorithms like Drain learn templates from the data instead, and would
  catch variable words that aren't numbers (usernames, device names).
- **Smarter triage before the model.** Cheap signals like "first seen on
  this host", "only at boot" or "correlates with a reboot" could be computed
  in code and handed to the model, so it doesn't spend steps rediscovering
  them.
- **Self-checking.** A second short pass where the model re-reads its own
  evidence and is asked "did any command fail? did you conclude anything from
  a failure?" would have caught the libseccomp mistake.
- **More tools versus a sandbox.** The allowlist will keep growing. Another
  approach is a real shell inside a read-only container or namespace, which
  would let the model use pipes safely.
- **moneta.** It needs an ssh key from the box and a list of NixOS/ZFS checks
  (`zpool status`, scrub results, `systemctl --failed`). Its logs are larger,
  so it'll be the real test of the pattern filter.
- **Notifications.** On hold by choice. When wanted, ntfy (self-hosted) for
  `attention` findings is the obvious first step.
