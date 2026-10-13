"""Minimal client for an OpenAI-compatible chat endpoint (llama.cpp server).

Stdlib only, so it runs on any box with python3 and nothing installed.
"""

import json
import os
import urllib.request

DEFAULT_URL = os.environ.get("ARGUS_LLM_URL", "http://framework:8081/v1")
DEFAULT_MODEL = os.environ.get("ARGUS_LLM_MODEL", "gpt-oss-120b")


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
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = json.load(resp)
        return raw["choices"][0]["message"], raw.get("usage", {}), raw
