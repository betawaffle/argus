"""Shared bits for the pages: style, page shell, escaping, times."""

import html
import time

STYLE = """
:root { --bg:#fff; --fg:#1b1b1b; --muted:#6b6b6b; --line:#e3e3e3;
        --attention:#c0392b; --watch:#b9770e; --ignore:#5f7d5f; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161616; --fg:#e8e8e8; --muted:#9a9a9a; --line:#333;
          --attention:#ff7b6b; --watch:#f2b84b; --ignore:#8fb98f; } }
body { background:var(--bg); color:var(--fg); margin:0 auto; max-width:1100px;
       padding:16px; font:14px/1.45 system-ui, sans-serif; }
table { border-collapse:collapse; width:100%; }
td, th { border-bottom:1px solid var(--line); padding:6px 8px; text-align:left;
         vertical-align:top; }
th { color:var(--muted); font-weight:500; }
code, pre { font:12px/1.4 ui-monospace, monospace; }
pre { white-space:pre-wrap; border:1px solid var(--line); padding:8px; }
.attention { color:var(--attention); font-weight:600; }
.watch { color:var(--watch); font-weight:600; }
.ignore, .baseline { color:var(--ignore); }
.muted { color:var(--muted); }
a { color:inherit; }
form { display:inline; }
button { font:inherit; padding:2px 8px; }
nav a { margin-right:12px; }
"""


def page(title, body):
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport "
            f"content='width=device-width,initial-scale=1'><title>{title}</title>"
            f"<style>{STYLE}</style><h1>argus</h1>{body}").encode()


def esc(x):
    return html.escape(str(x if x is not None else ""))


def when(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""


