# argus

A small, homemade agent harness around a local LLM (gpt-oss-120b served by
llama.cpp on the Framework box), built to learn how agents work and to try
ideas. Its first job is watching unattended machines' logs.

Stdlib-only Python 3; nothing to install.

**[How it works](docs/how-it-works.md)** covers the design, the details and what we learned from the first runs.

## Pieces

- `argus/llm.py`: client for an OpenAI-compatible endpoint
  (`ARGUS_LLM_URL`, default `http://framework:8081/v1`).
- `argus/agent.py`: the loop. It asks the model, runs the tools it calls, and
  repeats. Every step is logged to a JSONL transcript.
- `argus/tools.py`: read-only tools. Commands run without a shell and must
  pass an allowlist. Other hosts are reached over ssh.
- `argus/serve.py`: the findings page.
- `argus/patterns.py`: plain code collapses journal lines into patterns
  (numbers, IPs, UUIDs stripped) and keeps counts in SQLite, so only new or
  spiking patterns reach the model.

## Use

    python3 -m argus ask "is anything wrong with the zpool?"
    python3 -m argus scan --baseline          # first run: today's noise is normal
    python3 -m argus scan                     # investigate new or spiking patterns
    python3 -m argus scan --host moneta       # needs ssh to moneta from this box
    python3 -m argus serve                    # findings page on :8082

On the box, `systemd/install.sh` installs the page and a daily 06:00 scan
as user services.

Run data (pattern DB, findings, transcripts) goes to `~/.local/share/argus`
(`ARGUS_DATA`), never into the repo.

The page lists open findings (attention and watch first). You can agree with
or dismiss each one and leave a note. The next time the same service comes
up, the model sees its earlier verdicts along with your feedback and notes.
That way argus learns what you consider normal.

Container output is kept only when it looks like trouble (errors,
tracebacks, OOM and similar). podman logs everything a container prints at
error level, so the priority tells us nothing.

## Next

- moneta (needs an ssh key from the box)
- notifications (later)
