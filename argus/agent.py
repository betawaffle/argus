"""The agent loop: ask the model, run the tools it asks for, repeat.

Every message, tool call, tool result and token count is appended to a JSONL
transcript, so any run can be replayed or analyzed later.
"""

import json
import os
import time
from pathlib import Path

from . import tools as default_tools
from .llm import LLM

DATA_DIR = Path(os.environ.get("ARGUS_DATA", Path.home() / ".local/share/argus"))


class Agent:
    _n = 0

    def __init__(self, system, llm=None, tools=default_tools, max_steps=20,
                 transcript_dir=DATA_DIR / "transcripts"):
        self.llm = llm or LLM()
        self.tools = tools
        self.max_steps = max_steps
        self.messages = [{"role": "system", "content": system}]
        transcript_dir.mkdir(parents=True, exist_ok=True)
        # Several agents can start within one second, so add pid and a counter.
        Agent._n += 1
        self.transcript = transcript_dir / (time.strftime("%Y%m%d-%H%M%S")
                                            + f"-{os.getpid()}-{Agent._n}.jsonl")

    def log(self, kind, **data):
        with open(self.transcript, "a") as f:
            f.write(json.dumps({"t": time.time(), "kind": kind, **data}) + "\n")

    def run(self, task, on_event=None):
        """Work on task until the model answers without calling a tool."""
        on_event = on_event or (lambda kind, data: None)
        self.messages.append({"role": "user", "content": task})
        self.log("user", content=task)
        for step in range(self.max_steps):
            t0 = time.time()
            msg, usage, _ = self.llm.chat(self.messages, tools=self.tools.SCHEMAS)
            self.log("assistant", step=step, message=msg, usage=usage,
                     seconds=round(time.time() - t0, 2))
            # Keep reasoning out of the history: it costs prompt time on every
            # later step and the model doesn't need its old thoughts.
            self.messages.append({k: v for k, v in msg.items()
                                  if k != "reasoning_content"})
            on_event("assistant", msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                return msg.get("content", "")
            for call in calls:
                result = self.call_tool(call)
                self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                      "content": result})
                self.log("tool", call=call, result=result)
                on_event("tool", {"call": call, "result": result})
        self.log("gave_up", max_steps=self.max_steps)
        return f"(stopped after {self.max_steps} steps without a final answer)"

    def call_tool(self, call):
        fn = call["function"]
        func = self.tools.FUNCS.get(fn["name"])
        if func is None:
            return f"error: no tool named {fn['name']}"
        try:
            args = json.loads(fn.get("arguments") or "{}")
            return func(**args)
        except Exception as e:  # report back to the model rather than crash
            return f"error: {type(e).__name__}: {e}"
