"""Document processing tools — extract text from PDFs and spreadsheets.

Dependencies are optional — tools degrade gracefully if packages are missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.tool_framework import Tool
from core.tools.file_io import _resolve, _DEFAULT_WORKSPACE


class ExtractTextTool(Tool):
    name = "extract_text"
    description = (
        "Extract readable text from a file in the workspace. "
        "Supports PDF (.pdf) and plain text (.txt, .md, .py, etc.) files. "
        "Provide the relative path to the file."
    )
    when_to_use = "To extract readable text from PDF or text files in the workspace."
    how_to_use = "Provide a relative path to the file within the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file within the workspace.",
            },
        },
        "required": ["path"],
    }

    def __init__(self, workspace: Path | None = None):
        self.workspace = Path(workspace or _DEFAULT_WORKSPACE).resolve()

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            target = _resolve(path, self.workspace)
        except ValueError as e:
            return f"Error: {e}"

        if not target.exists():
            return f"File not found: {path}"

        suffix = target.suffix.lower()

        if suffix == ".pdf":
            return self._extract_pdf(target, path)
        if suffix in (".txt", ".md", ".py", ".js", ".ts", ".html", ".css", ".json", ".xml", ".yaml", ".yml", ".csv", ".log"):
            return self._extract_text(target, path)
        return f"Unsupported file type: {suffix}. Supported: .pdf, .txt, .md, .csv, and common code files."

    def _extract_text(self, target: Path, display_path: str) -> str:
        text = target.read_text(encoding="utf-8", errors="replace")
        return f"Extracted from {display_path}:\n\n{text}"

    def _extract_pdf(self, target: Path, display_path: str) -> str:
        try:
            import fitz
        except ImportError:
            return "PDF extraction requires pymupdf. Install with: pip install pymupdf"

        try:
            doc = fitz.open(str(target))
            pages: list[str] = []
            for page in doc:
                pages.append(page.get_text())
            doc.close()
            text = "\n\n".join(pages)
            return f"Extracted from {display_path} ({len(pages)} pages):\n\n{text}"
        except Exception as e:
            return f"Error extracting PDF: {e}"


class ReadSpreadsheetTool(Tool):
    name = "read_spreadsheet"
    description = (
        "Read a spreadsheet file from the workspace. "
        "Supports .xlsx, .xlsm, and .csv formats. "
        "Returns a summary with row count and column headers, plus the data."
    )
    when_to_use = "To read and summarize data from spreadsheet files (.xlsx, .csv)."
    how_to_use = "Provide a relative path to the spreadsheet within the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the spreadsheet within the workspace.",
            },
        },
        "required": ["path"],
    }

    def __init__(self, workspace: Path | None = None):
        self.workspace = Path(workspace or _DEFAULT_WORKSPACE).resolve()

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            target = _resolve(path, self.workspace)
        except ValueError as e:
            return f"Error: {e}"

        if not target.exists():
            return f"File not found: {path}"

        suffix = target.suffix.lower()

        if suffix == ".csv":
            return self._read_csv(target, path)
        if suffix in (".xlsx", ".xlsm"):
            return self._read_xlsx(target, path)
        return f"Unsupported format: {suffix}. Supported: .xlsx, .xlsm, .csv"

    def _read_csv(self, target: Path, display_path: str) -> str:
        import csv
        text = target.read_text(encoding="utf-8", errors="replace")
        reader = csv.reader(text.splitlines())
        rows = list(reader)
        if not rows:
            return f"Empty CSV: {display_path}"
        headers = rows[0]
        data_rows = rows[1:]
        lines = [
            f"Spreadsheet: {display_path}",
            f"Columns: {', '.join(headers)}",
            f"Rows: {len(data_rows)}",
            "",
        ]
        # Show headers + first 50 data rows
        lines.append("\t".join(headers))
        for row in data_rows[:50]:
            lines.append("\t".join(row))
        if len(data_rows) > 50:
            lines.append(f"... {len(data_rows) - 50} more rows")
        return "\n".join(lines)

    def _read_xlsx(self, target: Path, display_path: str) -> str:
        try:
            import openpyxl
        except ImportError:
            return "Spreadsheet reading requires openpyxl. Install with: pip install openpyxl"

        try:
            wb = openpyxl.load_workbook(str(target), read_only=True, data_only=True)
            sheets_info: list[str] = []
            for name in wb.sheetnames:
                ws = wb[name]
                rows = list(ws.iter_rows(values_only=True))
                if not rows:
                    sheets_info.append(f"  {name}: empty")
                    continue
                headers = [str(c) if c is not None else "" for c in rows[0]]
                data_rows = rows[1:]
                sheets_info.append(f"  {name}: {len(data_rows)} rows, columns: {', '.join(headers[:10])}")
            wb.close()
            return (
                f"Spreadsheet: {display_path}\n"
                f"Sheets: {len(wb.sheetnames)}\n"
                + "\n".join(sheets_info)
                + f"\n\nUse read_file to read individual sheet content."
            )
        except Exception as e:
            return f"Error reading spreadsheet: {e}"
