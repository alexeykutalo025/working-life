"""Attachments: a content-addressed store, and text extraction for search.

The same PDF sent to forty people and forwarded twice is one file on disk. The
store is addressed by BLAKE2b-256 of the contents, so identical bytes land in
the same place no matter what they were called:

    blobs/<h[0:2]>/<h[2:4]>/<hash><ext>

Two levels of two hex characters keeps any one directory under a few thousand
entries, which matters on NTFS when the archive holds a hundred thousand
attachments.

Text extraction is for search. A PDF nobody can search is a PDF nobody will
find, and in an archive of a career the attachments are often where the actual
work is. Extraction never fails a run: a file that cannot be read is stored
anyway and marked ``unsupported`` or ``failed``, with the reason.
"""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import get_logger
from ..models import ParsedAttachment
from ..scan.fingerprint import hash_bytes

log = get_logger("normalize.attachments")

#: Extensions text can be pulled out of, and what pulls it.
TEXT_EXTRACTORS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".pptx": "pptx",
    ".txt": "plain",
    ".log": "plain",
    ".csv": "plain",
    ".md": "plain",
    ".rtf": "rtf",
    ".htm": "html",
    ".html": "html",
    ".eml": "eml",
    ".msg": "msg",
    ".vcf": "plain",
    ".ics": "plain",
    ".xml": "plain",
    ".json": "plain",
}

#: Stored, never text-extracted. An inline signature image has no words in it,
#: and running OCR over a million of them would take a week.
BINARY_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".ico", ".webp",
    ".zip", ".rar", ".7z", ".gz", ".tar", ".cab", ".exe", ".dll", ".msi",
    ".mp3", ".wav", ".mp4", ".avi", ".mov", ".wmv", ".mpg",
})

#: How much extracted text to keep per attachment. A 900-page scanned contract
#: is worth indexing; keeping every word of it in the row is not.
MAX_EXTRACTED_CHARS = 400_000


@dataclass(slots=True)
class StoredBlob:
    content_hash: str
    path: Path
    size_bytes: int
    already_present: bool


class BlobStore:
    """The content-addressed attachment store."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, content_hash: str, suffix: str = "") -> Path:
        return self.root / content_hash[:2] / content_hash[2:4] / (content_hash + suffix)

    def put(self, data: bytes, filename: str | None = None) -> StoredBlob:
        """Store bytes once. Returns where they went and whether they were new."""
        from ..scan.onedrive import assert_not_onedrive

        assert_not_onedrive(self.root)

        content_hash = hash_bytes(data)
        suffix = safe_suffix(filename)
        target = self.path_for(content_hash, suffix)

        if target.exists() and target.stat().st_size == len(data):
            return StoredBlob(content_hash, target, len(data), already_present=True)

        target.parent.mkdir(parents=True, exist_ok=True)
        # Written to a temporary name and renamed, so a crash never leaves a
        # half-written blob that looks complete.
        temporary = target.with_name(target.name + ".part")
        temporary.write_bytes(data)
        temporary.replace(target)

        return StoredBlob(content_hash, target, len(data), already_present=False)

    def locate(self, content_hash: str, suffix: str = "") -> Path | None:
        """The file holding these bytes, or None.

        The path rather than the bytes, because a caller serving a download
        wants the file and reading a 40 MB attachment into memory to decide it
        is there helps nobody. Everything else here is expressed in terms of
        this, so "where is it" cannot disagree with "is it there".
        """
        path = self.path_for(content_hash, suffix)
        if path.exists():
            return path
        # The suffix is cosmetic, so a hash whose extension was recorded
        # differently is still found. Sorted, so that when several match every
        # caller picks the same one.
        folder = self.root / content_hash[:2] / content_hash[2:4]
        if folder.is_dir():
            for candidate in sorted(folder.glob(content_hash + "*")):
                if candidate.is_file() and not candidate.name.endswith(".part"):
                    return candidate
        return None

    def get(self, content_hash: str, suffix: str = "") -> bytes | None:
        found = self.locate(content_hash, suffix)
        return found.read_bytes() if found is not None else None

    def exists(self, content_hash: str, suffix: str = "") -> bool:
        return self.locate(content_hash, suffix) is not None

    def total_bytes(self) -> int:
        if not self.root.exists():
            return 0
        return sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())


def safe_suffix(filename: str | None) -> str:
    """A file extension safe to put on a path, or nothing.

    Never trusts the attachment's name: "..\\..\\startup\\evil.exe" must not
    become part of a path, and an extension of 200 characters is not one.
    """
    if not filename:
        return ""
    suffix = Path(str(filename).replace("\\", "/")).suffix.lower()
    if not suffix or len(suffix) > 12:
        return ""
    if not re.fullmatch(r"\.[a-z0-9]+", suffix):
        return ""
    return suffix


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExtractedText:
    text: str | None
    state: str        # done | unsupported | failed | skipped
    reason: str | None = None


def extract_text(
    data: bytes, filename: str | None, mime_type: str | None, *, size_cap: int = 0
) -> ExtractedText:
    """Pull searchable text out of an attachment. Never raises."""
    suffix = safe_suffix(filename)
    if not suffix and mime_type:
        guessed = mimetypes.guess_extension(mime_type.split(";")[0].strip())
        suffix = (guessed or "").lower()

    if size_cap and len(data) > size_cap:
        return ExtractedText(
            None, "skipped",
            f"larger than the {size_cap // (1024 * 1024)} MB limit for searching "
            "inside attachments",
        )

    if suffix in BINARY_EXTENSIONS:
        return ExtractedText(None, "unsupported", "there is no text in this kind of file")

    kind = TEXT_EXTRACTORS.get(suffix)
    if kind is None:
        return ExtractedText(
            None, "unsupported",
            f"Recall cannot read text out of {suffix or 'this kind of'} files",
        )

    try:
        extractor = {
            "pdf": _from_pdf,
            "docx": _from_docx,
            "xlsx": _from_xlsx,
            "pptx": _from_pptx,
            "plain": _from_plain,
            "rtf": _from_rtf,
            "html": _from_html,
            "eml": _from_eml,
            "msg": _from_msg,
        }[kind]
        text = extractor(data)
    except ImportError as exc:
        return ExtractedText(
            None, "unsupported",
            f"the library needed to read {suffix} files is not installed ({exc})",
        )
    except Exception as exc:  # noqa: BLE001 - one attachment never fails a run
        log.debug("Text extraction failed for %s: %s", filename, exc)
        return ExtractedText(None, "failed", f"{exc.__class__.__name__}: {exc}")

    if not text or not text.strip():
        return ExtractedText(
            None, "done", "the file opened but contained no text"
        )

    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_EXTRACTED_CHARS:
        text = text[:MAX_EXTRACTED_CHARS] + "…"
    return ExtractedText(text, "done")


def _from_pdf(data: bytes) -> str:
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")   # many PDFs are "encrypted" with an empty password
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"the PDF is password-protected ({exc})") from exc

    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 - one bad page, not the document
            log.debug("A PDF page could not be read: %s", exc)
    return "\n".join(parts)


def _from_docx(data: bytes) -> str:
    import io

    import docx

    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def _from_xlsx(data: bytes) -> str:
    import io

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts = []
    try:
        for sheet in workbook.worksheets:
            parts.append(f"[{sheet.title}]")
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append("\t".join(cells))
    finally:
        workbook.close()
    return "\n".join(parts)


def _from_pptx(data: bytes) -> str:
    import io

    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))
    parts = []
    for slide in presentation.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text_frame.text)
    return "\n".join(parts)


def _from_plain(data: bytes) -> str:
    from .text import clean_text

    return clean_text(data).text


def _from_rtf(data: bytes) -> str:
    from .text import clean_text, rtf_to_text

    return rtf_to_text(clean_text(data).text)


def _from_html(data: bytes) -> str:
    from .text import clean_text, html_to_text

    return html_to_text(clean_text(data).text)


def _from_eml(data: bytes) -> str:
    """A forwarded message attached as .eml - often the only copy of it."""
    import email
    import email.policy

    from ..parsers.eml import build_message

    message = email.message_from_bytes(data, policy=email.policy.default)
    item = build_message(message, backend="attachment")
    parts = [
        f"Subject: {item.subject or ''}",
        f"Date: {item.occurred.local or 'unknown'}",
    ]
    for participant in item.participants:
        parts.append(f"{participant.role}: {participant.display_name or ''} "
                     f"{participant.address or ''}".strip())
    if item.body_text:
        parts.append("")
        parts.append(item.body_text)
    return "\n".join(parts)


def _from_msg(data: bytes) -> str:
    """A nested Outlook message. Written to a temporary file because
    extract-msg needs a path, and removed immediately afterwards."""
    import tempfile

    import extract_msg

    with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as handle:
        handle.write(data)
        temporary = handle.name
    try:
        message = extract_msg.openMsg(temporary)
        try:
            parts = []
            for label, attr in (
                ("Subject", "subject"), ("Date", "date"), ("From", "sender"),
                ("To", "to"), ("Cc", "cc"),
            ):
                value = getattr(message, attr, None)
                if value:
                    parts.append(f"{label}: {value}")
            body = getattr(message, "body", None)
            if body:
                parts.append("")
                parts.append(str(body))
            return "\n".join(parts)
        finally:
            message.close()
    finally:
        try:
            Path(temporary).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Writing attachments for one item
# ---------------------------------------------------------------------------


def store_attachments(
    conn,
    store: BlobStore,
    item_id: int,
    attachments: list[ParsedAttachment],
    *,
    size_cap: int = 0,
) -> tuple[int, int]:
    """Store and index one item's attachments. Returns (stored, failed)."""
    stored = 0
    failed = 0

    for attachment in attachments:
        if attachment.data is None:
            conn.execute(
                "INSERT INTO attachments(item_id, filename, mime_type, size_bytes, "
                "content_hash, is_inline, content_id, extract_state, extracted_text) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, 'failed', NULL)",
                (
                    item_id, attachment.filename, attachment.mime_type,
                    attachment.size_bytes, int(attachment.is_inline),
                    attachment.content_id,
                ),
            )
            failed += 1
            continue

        try:
            blob = store.put(attachment.data, attachment.filename)
        except OSError as exc:
            log.warning("Attachment %s could not be saved: %s", attachment.filename, exc)
            conn.execute(
                "INSERT INTO attachments(item_id, filename, mime_type, size_bytes, "
                "content_hash, is_inline, content_id, extract_state) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, 'failed')",
                (
                    item_id, attachment.filename, attachment.mime_type,
                    attachment.size_bytes, int(attachment.is_inline),
                    attachment.content_id,
                ),
            )
            failed += 1
            continue

        # Inline images are stored but never text-extracted: a signature logo
        # has no words, and there are a great many of them.
        if attachment.is_inline and safe_suffix(attachment.filename) in BINARY_EXTENSIONS:
            extracted = ExtractedText(None, "unsupported", "an inline image")
        else:
            extracted = extract_text(
                attachment.data, attachment.filename, attachment.mime_type,
                size_cap=size_cap,
            )

        conn.execute(
            "INSERT INTO attachments(item_id, filename, mime_type, size_bytes, "
            "content_hash, is_inline, content_id, extracted_text, extract_state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                attachment.filename,
                attachment.mime_type,
                len(attachment.data),
                blob.content_hash,
                int(attachment.is_inline),
                attachment.content_id,
                extracted.text,
                extracted.state,
            ),
        )
        stored += 1

    return stored, failed
