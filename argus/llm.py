"""Minimal client for an OpenAI-compatible chat endpoint (llama.cpp server).

Stdlib only, so it runs on any box with python3 and nothing installed.
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = os.environ.get("ARGUS_LLM_URL", "http://framework:8081/v1")
DEFAULT_MODEL = os.environ.get("ARGUS_LLM_MODEL", "gpt-oss-120b")
# When argus's own requests ran, so the engine can tell the model server's log
# lines about them (or, after an error, the model's whole output dumped one
# line at a time) from everyone else's. Tab-separated start and end times.
REQUEST_LOG = Path(os.environ.get("ARGUS_DATA", Path.home() / ".local/share/argus")) \
    / "llm-requests.tsv"


class LLM:
    def __init__(self, url=DEFAULT_URL, model=DEFAULT_MODEL, timeout=900):
        self.url = url.rstrip("/")
        self.model = model
        # First request after idle loads the model (~63 GB), so be patient.
        self.timeout = timeout

    def chat(self, messages, tools=None, **params):
        """One completion. Returns (assistant_message, usage, raw_response)."""
        body = {"model": self.model, "messages": messages, **params}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            self.url + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = json.load(resp)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"LLM server: HTTP {e.code}: "
                               f"{e.read().decode(errors='replace')[:500]}") from None
        finally:
            _log_request(t0, time.time())
        return raw["choices"][0]["message"], raw.get("usage", {}), raw


def _log_request(t0, t1):
    try:
        REQUEST_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(REQUEST_LOG, "a") as f:
            f.write(f"{t0:.3f}\t{t1:.3f}\n")
    except OSError:
        pass  # bookkeeping only; never fail a request over it


def own_requests(since):
    """[(start, end)] of argus's requests that ended after since, sorted."""
    try:
        rows = [line.split("\t") for line in REQUEST_LOG.read_text().splitlines()]
    except OSError:
        return []
    return sorted((float(a), float(b)) for a, b in rows if float(b) >= since)


def prune_requests(since):
    """Forget requests that ended before since."""
    keep = own_requests(since)
    try:
        REQUEST_LOG.write_text("".join(f"{a:.3f}\t{b:.3f}\n" for a, b in keep))
    except OSError:
        pass


def import_transcripts(transcript_dir, since):
    """Recover request windows from agent transcripts, for model traffic from
    before argus recorded it. Each transcript counts as one window from its
    first record to its last; one that ends without a final answer (the
    request failed, which is when the server dumps the model's output) gets
    15 more minutes. Returns how many windows were added."""
    found = []
    for path in sorted(Path(transcript_dir).glob("*.jsonl")):
        try:
            recs = [json.loads(line) for line in path.read_text().splitlines() if line]
        except (OSError, ValueError):
            continue
        ts = [r["t"] for r in recs if "t" in r]
        if not ts or ts[-1] < since:
            continue
        last = recs[-1]
        answered = (last.get("kind") == "assistant"
                    and not (last.get("message") or {}).get("tool_calls"))
        found.append((ts[0], ts[-1] if answered else ts[-1] + 900))
    known = own_requests(since)
    new = [w for w in found if w not in known]
    for a, b in new:
        _log_request(a, b)
    return len(new)
