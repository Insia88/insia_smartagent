"""Paste/upload-ready exports of content items and whole runs.

    from insia_agents.exporters import export_item, export_run_zip
    file = export_item(detail, "docx", profile)   # -> ExportFile(filename, content_type, data)
    file.save("exports/")                          # or send file.data with file.content_disposition()

Formats per channel (``formats_for``): bizplan docx/md/txt/zip, naver_blog
html/md/txt/docx/zip, linkedin txt/md/docx/zip, instagram zip/txt/md/docx.
Optional extras are imported lazily: python-docx (``[export]``) for .docx and
Playwright (``[render]``) for carousel PNGs (falls back to ``slides.html``).
"""

from __future__ import annotations

from .api import (CHANNEL_FORMATS, FORMAT_LABELS, capabilities, export_item, export_run_zip, format_label,
                  formats_for, sources_markdown)
from .common import ExportError, ExportFile, MissingDependencyError, export_filename, slugify

__all__ = [
    "CHANNEL_FORMATS",
    "FORMAT_LABELS",
    "ExportError",
    "ExportFile",
    "MissingDependencyError",
    "capabilities",
    "export_filename",
    "export_item",
    "export_run_zip",
    "format_label",
    "formats_for",
    "slugify",
    "sources_markdown",
]
