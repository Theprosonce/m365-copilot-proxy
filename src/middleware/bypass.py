"""Single source of truth for tool-bypass detection.

A reply "bypasses" when the model ducks the tools — offers its own canvas/Loop, a link, or
"copy it manually" instead of emitting a tool block. Shared by the translator and the
emulation pipeline so the heuristic lives in exactly one place.
"""
from __future__ import annotations

import re

_BYPASS_PATTERNS = re.compile(
    r"teams\.microsoft\.com|sharepoint|\boffice\.com\b|loop\.|asyncgw|"
    r"\bsandbox\b|copiarlo|copialo|copia (?:il|qui)|incolla|paste it|copy (?:it|the file)|"
    r"caricar|carica il file|nella mia|in my (?:sandbox|canvas|environment)|"
    # "here's the content, paste it yourself" write-bypass
    r"copy[\- ]?paste|paste (?:it|the|this)|copia[\- ]?incolla|"
    r"(?:don'?t|do not|non) (?:have|hanno|ho) (?:the )?(?:same |stesso )?access|"
    r"the write tool|can'?t write|cannot write|non posso scrivere|salvalo (?:tu|nel)|save it (?:yourself|manually|to)|"
    # identity refusals / "do it in Claude Code yourself"
    r"microsoft (?:enterprise )?copilot|enterprise copilot|i'?m not claude code|"
    r"i (?:don'?t|do not) have access to (?:your |the )?(?:local )?(?:file ?system|filesystem|tools)|"
    r"non ho accesso (?:al|ai|diretto)|different runtime|run (?:this|it).{0,20}claude code|"
    r"i can'?t do this task|cannot do this task|those are claude code|enterprise search",
    re.IGNORECASE,
)


def looks_like_bypass(text: str) -> bool:
    """True if the reply ducked the tools (own canvas/Loop, a link, or 'copy it manually')."""
    return bool(text) and bool(_BYPASS_PATTERNS.search(text))
