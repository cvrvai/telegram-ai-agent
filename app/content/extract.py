"""Extract bounded text for the AI provider without pretending files are trusted."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class ContentExtractionError(ValueError):
    pass


@dataclass(frozen=True)
class ExtractedContent:
    path: str
    content_type: str
    text: str
    truncated: bool = False


class DocumentExtractor:
    """Extract bounded text from supported business documents and images."""

    SUPPORTED_TEXT = {".txt", ".md", ".csv", ".json", ".yaml", ".yml"}
    SUPPORTED_SPREADSHEETS = {".xlsx", ".xls"}
    SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    def __init__(self, max_bytes: int = 10_000_000, max_chars: int = 80_000):
        self.max_bytes = max_bytes
        self.max_chars = max_chars

    def extract(self, path: str) -> ExtractedContent:
        file_path = Path(path)
        if not file_path.is_file():
            raise ContentExtractionError("The supplied file does not exist")
        if file_path.stat().st_size > self.max_bytes:
            raise ContentExtractionError("The supplied file is larger than the allowed limit")
        suffix = file_path.suffix.lower()
        if suffix in self.SUPPORTED_TEXT:
            text = file_path.read_text(encoding="utf-8", errors="replace")
            if suffix == ".json":
                try:
                    text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
                except json.JSONDecodeError:
                    pass
            return self._bounded(file_path, "text", text)
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise ContentExtractionError("PDF support requires the optional pypdf package") from exc
            try:
                text = "\n".join(page.extract_text() or "" for page in PdfReader(str(file_path)).pages)
            except Exception as exc:
                raise ContentExtractionError("The PDF could not be read") from exc
            return self._bounded(file_path, "pdf", text)
        if suffix in self.SUPPORTED_SPREADSHEETS:
            return self._extract_spreadsheet(file_path, suffix)
        if suffix in self.SUPPORTED_IMAGES:
            return ExtractedContent(str(file_path), "image", f"Image file: {file_path.name}")
        raise ContentExtractionError(f"Unsupported file type: {suffix or 'unknown'}")

    def _extract_spreadsheet(self, file_path: Path, suffix: str) -> ExtractedContent:
        """Convert workbook cells to bounded, sheet-labelled text for the AI."""
        try:
            if suffix == ".xlsx":
                from openpyxl import load_workbook

                workbook = load_workbook(file_path, read_only=True, data_only=True)
                chunks: list[str] = []
                for sheet in workbook.worksheets:
                    chunks.append(f"SHEET: {sheet.title}")
                    for row in sheet.iter_rows(values_only=True):
                        values = ["" if value is None else str(value) for value in row]
                        if any(values):
                            chunks.append("\t".join(values))
                workbook.close()
            else:
                import xlrd

                workbook = xlrd.open_workbook(file_path, on_demand=True)
                chunks = []
                for sheet in workbook.sheets():
                    chunks.append(f"SHEET: {sheet.name}")
                    for row in sheet.get_rows():
                        values = [cell.value if cell.value is not None else "" for cell in row]
                        rendered = [str(value) for value in values]
                        if any(rendered):
                            chunks.append("\t".join(rendered))
                workbook.release_resources()
        except ImportError as exc:
            package = "openpyxl" if suffix == ".xlsx" else "xlrd"
            raise ContentExtractionError(f"{suffix} support requires the optional {package} package") from exc
        except Exception as exc:
            raise ContentExtractionError("The spreadsheet could not be read") from exc
        return self._bounded(file_path, "spreadsheet", "\n".join(chunks))

    def _bounded(self, file_path: Path, content_type: str, text: str) -> ExtractedContent:
        truncated = len(text) > self.max_chars
        return ExtractedContent(str(file_path), content_type, text[: self.max_chars], truncated)
