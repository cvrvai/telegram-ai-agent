"""Generates the weekly management meeting PowerPoint from this week's
Situations, pending approvals, and activity -- case study sections 30-34.

"Department" is approximated from each situation's `responsible` field
(falling back to its source chat title), since Situations don't carry a
formal department_id link yet -- an approximation, not a claim of a real
department rollup elsewhere in the data model.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pptx import Presentation
from pptx.util import Inches

from config import config
from app.core.models import Situation
from app.priority.briefs import _clip

STATUS_DOT = {"P0": "🔴", "P1": "🔴", "P2": "🟡", "P3": "🟢"}
STALE_HOURS = 48


def _is_stale(situation: Situation, hours: int = STALE_HOURS, now: Optional[datetime] = None) -> bool:
    try:
        updated = datetime.fromisoformat(situation.last_update_at)
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - updated).total_seconds() > hours * 3600


def _add_title_slide(prs: Presentation, title: str, subtitle: str):
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = title
    if len(slide.placeholders) > 1:
        slide.placeholders[1].text = subtitle
    return slide


def _add_bullet_slide(prs: Presentation, title: str, bullets: list[str]):
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = title
    body = slide.placeholders[1].text_frame
    body.clear()
    if not bullets:
        body.text = "Nothing to report this week."
        return slide
    body.text = bullets[0]
    for bullet in bullets[1:]:
        body.add_paragraph().text = bullet
    return slide


def _add_table_slide(prs: Presentation, title: str, headers: list[str], rows: list[list[str]]):
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = title
    if not rows:
        box = slide.shapes.add_textbox(Inches(0.5), Inches(1.6), Inches(12), Inches(1))
        box.text_frame.text = "Nothing to report this week."
        return slide
    table = slide.shapes.add_table(len(rows) + 1, len(headers), Inches(0.5), Inches(1.4), Inches(12.3), Inches(0.4 * (len(rows) + 1))).table
    for col, header in enumerate(headers):
        table.cell(0, col).text = header
    for row_idx, row in enumerate(rows, start=1):
        for col, value in enumerate(row):
            table.cell(row_idx, col).text = str(value)
    return slide


class WeeklyDeckBuilder:
    """Builds the weekly management presentation as a .pptx file."""

    def __init__(self, message_db: Any, business_repository: Any):
        self.db = message_db
        self.business_repository = business_repository

    async def build(self, output_path: str, allowed_chat_ids: Optional[set[int]] = None, title: str = "Weekly Management Brief") -> str:
        # Slide title comes from the business profile: "Guest / Customer Issues"
        # for a hotel, "Crop / Livestock Issues" for a farm.
        impact_label = f"{config.profile.business.impact_label} Issues"
        now = datetime.now(timezone.utc)
        week_start = now - timedelta(days=7)
        situations = await self.db.list_situations_active_since(week_start.isoformat(), allowed_chat_ids)
        pending_actions = await self.business_repository.list_actions("pending", limit=50)

        open_situations = [s for s in situations if s.status != "resolved"]
        resolved = [s for s in situations if s.status == "resolved"]
        critical = [s for s in open_situations if s.priority == "P0"]
        impact_issues = [s for s in situations if s.critical_impact]
        major_issues = critical or [s for s in open_situations if s.priority == "P1"][:5] or open_situations[:5]

        by_group: dict[str, list[Situation]] = defaultdict(list)
        for situation in open_situations:
            by_group[situation.responsible or situation.chat_title].append(situation)

        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)

        _add_title_slide(
            prs, title,
            f"{week_start.date()} – {now.date()}\n"
            f"{len(open_situations)} open · {len(critical)} critical · {len(resolved)} resolved this week",
        )

        priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        department_rows = []
        for group, items in sorted(by_group.items()):
            worst = min(items, key=lambda s: priority_rank.get(s.priority, 3))
            department_rows.append([group, STATUS_DOT.get(worst.priority, "🟢"), _clip(worst.title, 60)])
        _add_table_slide(prs, "Department Status", ["Department", "Status", "Key Issue"], department_rows)

        _add_bullet_slide(prs, "Major Issues", [f"{s.chat_title} — {_clip(s.title, 80)}" for s in major_issues[:8]])
        _add_bullet_slide(prs, "Resolved This Week", [f"{s.chat_title} — {_clip(s.title, 80)}" for s in resolved[:10]])
        _add_bullet_slide(
            prs, "Pending Issues",
            [f"{s.chat_title} — {_clip(s.title, 70)} ({s.status})" for s in open_situations if s.priority != "P0"][:10],
        )
        _add_bullet_slide(
            prs, "Management Decisions",
            [f"Approve {str(a.get('action_type', 'draft')).title()} draft #{a.get('id')}" for a in pending_actions[:10]],
        )
        _add_bullet_slide(
            prs, impact_label,
            [f"{s.chat_title} — {_clip(s.title, 70)} ({s.status})" for s in impact_issues[:10]],
        )

        followups = [f"{s.chat_title}: {_clip(s.current_action, 70)}" for s in open_situations if s.current_action]
        followups += [f"GM: Approve {str(a.get('action_type', 'draft'))} draft #{a.get('id')}" for a in pending_actions]
        _add_bullet_slide(prs, "Follow-Up Actions", followups[:12])

        _add_bullet_slide(prs, "Recommendations", self._recommendations(open_situations, by_group))

        prs.save(output_path)
        return output_path

    def _recommendations(self, open_situations: list[Situation], by_group: dict[str, list[Situation]]) -> list[str]:
        """Heuristic observations, not verified facts -- kept separate from
        the factual slides above per the case study's own principle that the
        assistant must never present an assumption as a confirmed fact."""
        notes: list[str] = []
        stale = [s for s in open_situations if _is_stale(s)]
        if stale:
            notes.append(f"{len(stale)} situation(s) have been open for more than {STALE_HOURS} hours without resolution.")
        if by_group:
            busiest = max(by_group.items(), key=lambda kv: sum(1 for s in kv[1] if s.priority in ("P0", "P1")))
            busy_count = sum(1 for s in busiest[1] if s.priority in ("P0", "P1"))
            if busy_count:
                notes.append(f"{busiest[0]} has the most unresolved high-priority issues this week ({busy_count}).")
        if not notes:
            notes.append("No systemic pattern detected this week.")
        notes.append("(Heuristic observations, not verified facts -- review before presenting.)")
        return notes
