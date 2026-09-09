"""Generates the executive weekly management report as a PDF document using ReportLab.

Presents the same factual data model and situations as WeeklyDeckBuilder, formatted
for executive reading, printing, and boardroom circulation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.priority.briefs import _clip
from app.reporting.weekly_deck import WeeklyReportData, collect_weekly_report_data

logger = logging.getLogger("reporting.weekly_pdf")

# Palette
COLOR_PRIMARY = colors.HexColor("#1A365D")    # Deep Executive Navy
COLOR_SECONDARY = colors.HexColor("#2B6CB0")  # Slate Blue
COLOR_ACCENT = colors.HexColor("#D69E2E")     # Gold/Amber
COLOR_MUTED = colors.HexColor("#718096")      # Muted Gray
COLOR_LINE = colors.HexColor("#E2E8F0")       # Border / Divider
COLOR_BG_ALT = colors.HexColor("#F7FAFC")     # Table Alternating Row
COLOR_CALLOUT = colors.HexColor("#EDF2F7")    # Recommendation Callout


class NumberedCanvas(canvas.Canvas):
    """Canvas that prints 'Page X of Y' on all pages after knowing total page count."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_number(num_pages)
            super().showPage()
        super().save()

    def draw_page_number(self, page_count: int):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(COLOR_MUTED)
        page_text = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(A4[0] - 36, 24, page_text)
        self.drawString(36, 24, "Tara Angkor Hotel — Executive Management Weekly Brief · Confidential")
        self.setStrokeColor(COLOR_LINE)
        self.setLineWidth(0.5)
        self.line(36, 34, A4[0] - 36, 34)
        self.restoreState()


class WeeklyPdfBuilder:
    """Builds the weekly executive brief as a printable PDF report."""

    def __init__(self, message_db: Any, business_repository: Any):
        self.db = message_db
        self.business_repository = business_repository

    async def build(
        self,
        output_path: str,
        allowed_chat_ids: Optional[set[int]] = None,
        title: str = "Weekly Management Brief",
    ) -> str:
        data = await collect_weekly_report_data(self.db, self.business_repository, allowed_chat_ids, title)

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=36,
            rightMargin=36,
            topMargin=36,
            bottomMargin=44,
        )

        sample_styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            "DocTitle",
            parent=sample_styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            textColor=COLOR_PRIMARY,
            spaceAfter=3,
        )

        subtitle_style = ParagraphStyle(
            "DocSubtitle",
            parent=sample_styles["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=COLOR_MUTED,
            spaceAfter=10,
        )

        section_heading = ParagraphStyle(
            "SectionHeading",
            parent=sample_styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=COLOR_PRIMARY,
            spaceBefore=12,
            spaceAfter=6,
        )

        body_style = ParagraphStyle(
            "DocBody",
            parent=sample_styles["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=13,
            textColor=colors.HexColor("#2D3748"),
        )

        bullet_style = ParagraphStyle(
            "DocBullet",
            parent=body_style,
            leftIndent=12,
            firstLineIndent=-8,
            spaceAfter=3,
        )

        table_header_style = ParagraphStyle(
            "TableHeader",
            parent=body_style,
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=11,
            textColor=colors.white,
        )

        table_cell_style = ParagraphStyle(
            "TableCell",
            parent=body_style,
            fontSize=8.5,
            leading=11,
        )

        disclaimer_style = ParagraphStyle(
            "Disclaimer",
            parent=body_style,
            fontName="Helvetica-Oblique",
            fontSize=8,
            leading=11,
            textColor=COLOR_MUTED,
            spaceBefore=6,
        )

        story = []

        # 1. Header Banner
        story.append(Paragraph(data.title, title_style))
        date_range_str = f"Period: {data.week_start.strftime('%d %b %Y')} – {data.now.strftime('%d %b %Y')} · Generated {data.now.strftime('%d %b %Y %H:%M UTC')}"
        story.append(Paragraph(date_range_str, subtitle_style))
        story.append(HRFlowable(width="100%", thickness=1.5, color=COLOR_ACCENT, spaceAfter=8))

        # 2. Executive KPI Summary Box
        kpi_cells = [
            [
                Paragraph(f"<b>{len(data.open_situations)}</b><br/><font color='#718096' size='7'>OPEN ISSUES</font>", body_style),
                Paragraph(f"<b><font color='#9B2C2C'>{len(data.critical)}</font></b><br/><font color='#718096' size='7'>CRITICAL (P0)</font>", body_style),
                Paragraph(f"<b><font color='#276749'>{len(data.resolved)}</font></b><br/><font color='#718096' size='7'>RESOLVED THIS WEEK</font>", body_style),
                Paragraph(f"<b>{len(data.pending_actions)}</b><br/><font color='#718096' size='7'>PENDING DECISIONS</font>", body_style),
                Paragraph(f"<b>{len(data.by_group)}</b><br/><font color='#718096' size='7'>UNITS ACTIVE</font>", body_style),
            ]
        ]
        kpi_table = Table(kpi_cells, colWidths=[105, 105, 105, 105, 103])
        kpi_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), COLOR_CALLOUT),
            ("BOX", (0, 0), (-1, -1), 0.5, COLOR_LINE),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, COLOR_LINE),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(kpi_table)
        story.append(Spacer(1, 10))

        # 3. Department Status Table
        story.append(Paragraph("Department Operational Status", section_heading))
        if data.department_rows:
            table_rows = [[
                Paragraph("<b>Department / Unit</b>", table_header_style),
                Paragraph("<b>Status</b>", table_header_style),
                Paragraph("<b>Key Operational Situation</b>", table_header_style),
            ]]
            for idx, row in enumerate(data.department_rows):
                group, status_sym, key_issue = row[0], row[1], row[2]
                status_text = "NORMAL"
                if "🔴" in status_sym:
                    status_text = "<font color='#9B2C2C'><b>CRITICAL</b></font>"
                elif "🟡" in status_sym:
                    status_text = "<font color='#B7791F'><b>ATTENTION</b></font>"
                else:
                    status_text = "<font color='#276749'><b>NORMAL</b></font>"

                table_rows.append([
                    Paragraph(f"<b>{group}</b>", table_cell_style),
                    Paragraph(status_text, table_cell_style),
                    Paragraph(key_issue, table_cell_style),
                ])

            dept_table = Table(table_rows, colWidths=[140, 80, 303])
            dept_style = [
                ("BACKGROUND", (0, 0), (-1, 0), COLOR_PRIMARY),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("GRID", (0, 0), (-1, -1), 0.5, COLOR_LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
            for r_idx in range(1, len(table_rows)):
                if r_idx % 2 == 0:
                    dept_style.append(("BACKGROUND", (0, r_idx), (-1, r_idx), COLOR_BG_ALT))
            dept_table.setStyle(TableStyle(dept_style))
            story.append(dept_table)
        else:
            story.append(Paragraph("<i>No active department situations recorded for this period.</i>", body_style))
        story.append(Spacer(1, 10))

        # 4. Major Operational Issues
        story.append(Paragraph("Major Issues Requiring Attention", section_heading))
        if data.major_issues:
            for s in data.major_issues[:8]:
                color_code = "#9B2C2C" if s.priority in ("P0", "P1") else "#2D3748"
                story.append(Paragraph(
                    f"• <b><font color='{color_code}'>[{s.priority}]</font> {s.chat_title}:</b> {_clip(s.title, 90)} "
                    f"<i>({s.status})</i>",
                    bullet_style,
                ))
        else:
            story.append(Paragraph("<i>No major issues flagged this week.</i>", body_style))
        story.append(Spacer(1, 8))

        # 5. Resolved This Week
        story.append(Paragraph("Resolved This Week", section_heading))
        if data.resolved:
            for s in data.resolved[:8]:
                story.append(Paragraph(
                    f"• <b><font color='#276749'>[RESOLVED]</font> {s.chat_title}:</b> {_clip(s.title, 90)}",
                    bullet_style,
                ))
        else:
            story.append(Paragraph("<i>No issues were closed as resolved during this window.</i>", body_style))
        story.append(Spacer(1, 8))

        # 6. Management Decisions & Approvals
        story.append(Paragraph("Management Decisions & Approvals", section_heading))
        if data.management_decisions:
            for decision in data.management_decisions[:8]:
                story.append(Paragraph(f"• <b>GM Decision:</b> {decision}", bullet_style))
        else:
            story.append(Paragraph("<i>No pending outbound drafts or approvals awaiting management sign-off.</i>", body_style))
        story.append(Spacer(1, 8))

        # 7. Impact Label (Guest Affected Issues for Hotels)
        story.append(Paragraph(data.impact_label, section_heading))
        if data.impact_issues:
            for s in data.impact_issues[:8]:
                story.append(Paragraph(
                    f"• <b><font color='#9B2C2C'>[IMPACT]</font> {s.chat_title}:</b> {_clip(s.title, 90)} "
                    f"<i>(Status: {s.status})</i>",
                    bullet_style,
                ))
        else:
            story.append(Paragraph(f"<i>No {data.impact_label.lower()} reported this week.</i>", body_style))
        story.append(Spacer(1, 8))

        # 8. Follow-Up Actions
        story.append(Paragraph("Action Items & Follow-Up Plan", section_heading))
        if data.followups:
            for action in data.followups[:10]:
                story.append(Paragraph(f"• {action}", bullet_style))
        else:
            story.append(Paragraph("<i>No specific action items currently assigned.</i>", body_style))
        story.append(Spacer(1, 10))

        # 9. Recommendations & Strategic Observations
        recs_flowables = [
            Paragraph("Strategic Observations & Recommendations", section_heading),
        ]
        for note in data.recommendations:
            if "Heuristic observations" in note:
                recs_flowables.append(Paragraph(note, disclaimer_style))
            else:
                recs_flowables.append(Paragraph(f"• {note}", bullet_style))

        story.append(KeepTogether(recs_flowables))

        doc.build(story, canvasmaker=NumberedCanvas)
        return output_path
