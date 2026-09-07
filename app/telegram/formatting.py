"""Make model output survive Telegram's very small markup vocabulary.

Telegram renders bold, italic, code and links -- nothing else. Models reach
for markdown headings, tables and stray HTML, all of which arrive as literal
characters: '### 1. Field operations', '|---|---|', '<br>'. Convert what has
an equivalent, drop what does not.
"""

from __future__ import annotations

import re

_BR = re.compile(r"<br\s*/?>", re.I)
_INLINE_TAGS = re.compile(r"</?(?:p|div|span|b|i|u|strong|em|ul|ol|li)\s*/?>", re.I)
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\|[\s:|-]+\|$")
_BLANK_RUN = re.compile(r"\n{3,}")


def render_for_telegram(text: str) -> str:
    if not text:
        return text

    # Inline, so it must not become a newline: that would split a table row
    # before the row below can be recognised as one.
    text = _BR.sub(" ", text)
    text = _INLINE_TAGS.sub("", text)

    lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()

        heading = _HEADING.match(stripped)
        if heading:
            title = heading.group(1).strip().rstrip(":")
            lines.append(f"**{title}**" if title else "")
            continue

        # The |---|---| row carries no information once the table is gone.
        if _TABLE_SEPARATOR.match(stripped):
            continue

        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 3:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            cells = [cell for cell in cells if cell]
            if cells:
                lines.append("• " + " — ".join(cells))
            continue

        lines.append(line)

    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()
