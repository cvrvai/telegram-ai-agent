"""Telegram cannot render headings, tables or HTML; these arrived as literal
'###', '|---|' and '<br>' in a real reply. Lock the conversions."""

from __future__ import annotations

import unittest

from app.telegram.formatting import render_for_telegram


class RenderForTelegramTests(unittest.TestCase):
    def test_headings_become_bold(self) -> None:
        out = render_for_telegram("### 1. Field operations\nbody")
        self.assertIn("**1. Field operations**", out)
        self.assertNotIn("#", out)

    def test_table_becomes_bullets_and_separator_is_dropped(self) -> None:
        out = render_for_telegram(
            "| Chat | Summary |\n|------|---------|\n| AIC | No messages |"
        )
        self.assertNotIn("|", out)
        self.assertIn("• Chat — Summary", out)
        self.assertIn("• AIC — No messages", out)

    def test_br_stays_inline_so_the_row_is_still_a_table(self) -> None:
        # Converting <br> to a newline first would split the row and leave
        # raw pipes in the output.
        out = render_for_telegram("| Dai Vai | 01:56 hello.<br>02:09 replied |")
        self.assertNotIn("|", out)
        self.assertNotIn("<br>", out)
        self.assertIn("01:56 hello. 02:09 replied", out)

    def test_ordinary_markdown_is_left_alone(self) -> None:
        text = "**Bold** and _italic_ and `code`\n- a bullet"
        self.assertEqual(render_for_telegram(text), text)

    def test_empty_input_is_safe(self) -> None:
        self.assertEqual(render_for_telegram(""), "")


if __name__ == "__main__":
    unittest.main()
