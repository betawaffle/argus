# argus

A small, homemade agent harness around a local LLM (gpt-oss-120b served by
llama.cpp on the Framework box), built to learn how agents work and to try
ideas. Its first job is watching unattended machines' logs.

Stdlib-only Python 3; nothing to install.

## Pieces

- `argus/llm.py`: client for an OpenAI-compatible endpoint
  (`ARGUS_LLM_URL`, default `http://framework:8081/v1`).
- `argus/agent.py`: the loop. It asks the model, runs the tools it calls, and
  repeats. Every step is logged to a JSONL transcript.
- `argus/tools.py`: read-only tools. Commands run without a shell and must
  pass an allowlist. Other hosts are reached over ssh.
- `argus/patterns.py`: plain code collapses journal lines into patterns
  (numbers, IPs, UUIDs stripped) and keeps counts in SQLite, so only new or
  spiking patterns reach the model.

## Use

    python3 -m argus ask "is anything wrong with the zpool?"
    python3 -m argus scan --baseline          # first run: today's noise is normal
    python3 -m argus scan                     # investigate new or spiking patterns
    python3 -m argus scan --host moneta       # needs ssh to moneta from this box

Run data (pattern DB, findings, transcripts) goes to `~/.local/share/argus`
(`ARGUS_DATA`), never into the repo.

## Next

- a page to browse findings and mark them agreed or dismissed
- feed past verdicts back to the model, so it gets quieter over time
- notifications (later)
