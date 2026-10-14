"""A generic labeling tool: get judgements out of a human, quickly.

It knows nothing about argus. Sources (adapters) turn their data into Items;
the core stores snapshots and labels; web.py serves the review screen.
See docs/labeling-design.md.
"""

from .core import KINDS, VERDICTS, Item, Part, Source, Store, paragraphs  # noqa: F401
