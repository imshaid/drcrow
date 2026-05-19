"""
Text extraction from uploaded files.
Used to populate search_index.text_content for FTS.
PDF → PyMuPDF, DOCX → python-docx.
Runs as a background task after upload.
"""

import logging
import tempfile
import os
from typing import Optional

logger = logging.getLogger(__name__)


async def extract_text_from_file(bot, file_id: str, file_type: str) -> Optional[str]:
    """
    Download file from Telegram and extract text.
    Returns extracted text or None if not extractable.
    """
    if file_type not in ("document", "pdf", "docx"):
        return None

    try:
        tg_file = await bot.get_file(file_id)
        suffix = ".pdf" if "pdf" in (tg_file.file_path or "").lower() else ".docx"

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name

        await tg_file.download_to_drive(tmp_path)

        if suffix == ".pdf":
            text = _extract_pdf(tmp_path)
        elif suffix == ".docx":
            text = _extract_docx(tmp_path)
        else:
            text = None

        os.unlink(tmp_path)
        return text

    except Exception as e:
        logger.warning(f"Text extraction failed for {file_id}: {e}")
        return None


def _extract_pdf(path: str) -> str:
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(path)
        pages = []
        for page in doc:
            pages.append(page.get_text())
        doc.close()
        return "\n".join(pages)[:50000]  # cap at 50k chars
    except Exception as e:
        logger.warning(f"PDF extraction error: {e}")
        return ""


def _extract_docx(path: str) -> str:
    try:
        from docx import Document
        doc = Document(path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs)[:50000]
    except Exception as e:
        logger.warning(f"DOCX extraction error: {e}")
        return ""
