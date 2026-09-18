"""Turning decades of badly-encoded bytes back into readable words.

Mail from the 1990s is CP1252, Latin-1, Mac Roman, or UTF-8 that has already
been round-tripped through something that did not understand it. The classic
symptom is mojibake: an apostrophe stored as UTF-8 (0xE2 0x80 0x99) and read
back as CP1252 becomes the three characters "a-circumflex, euro, trademark".

Spec section 8 makes repair mandatory, and section 9.4 makes honesty about it
mandatory too: whenever an encoding is guessed rather than declared, the item's
``parse_confidence`` drops below 1.0 and it is counted as ``low_confidence_text``
so the viewer can mark it.

Nothing here ever discards bytes it cannot decode. The worst case is a decode
with replacement characters and a confidence of 0.5, which keeps the message
readable and says plainly that it may be wrong.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

from ..logging_setup import get_logger

log = get_logger("normalize.text")


@dataclass(slots=True)
class DecodedText:
    """Text, plus how sure we are of it."""

    text: str
    encoding: str
    confidence: float
    repaired_mojibake: bool = False

    @property
    def was_guessed(self) -> bool:
        return self.confidence < 1.0


# Encodings a declared charset label might mean. Old mail lies about this
# constantly - "iso-8859-1" almost always means cp1252 in practice, because
# Windows mailers wrote cp1252 bytes and labelled them Latin-1.
_CHARSET_ALIASES = {
    "iso-8859-1": "cp1252",
    "iso8859-1": "cp1252",
    "latin-1": "cp1252",
    "latin1": "cp1252",
    "us-ascii": "cp1252",
    "ascii": "cp1252",
    "ansi_x3.4-1968": "cp1252",
    "windows-1252": "cp1252",
    "unicode-1-1-utf-7": "utf-7",
    "utf8": "utf-8",
    "utf_8": "utf-8",
    "macintosh": "mac_roman",
    "x-mac-roman": "mac_roman",
}

#: Tried in order when nothing is declared and charset-normalizer is unavailable.
_FALLBACKS = ("utf-8", "cp1252", "mac_roman", "cp850", "latin-1")

#: Below this many bytes, statistical encoding detection is guessing. Subject
#: lines and display names are routinely shorter than this.
_SHORT_RUN = 64


def decode_bytes(data: bytes, declared: str | None = None) -> DecodedText:
    """Bytes to text, using the declared charset as a hint and not as gospel.

    Confidence:
      1.0  the declared charset decoded cleanly, or the bytes are plain ASCII
      0.9  no charset declared, but the bytes are valid UTF-8 (near-certain)
      0.8  charset-normalizer identified an encoding confidently
      0.6  a fallback encoding decoded without error
      0.5  nothing decoded cleanly; decoded with replacement characters
    """
    if not data:
        return DecodedText("", "ascii", 1.0)

    try:
        return DecodedText(data.decode("ascii"), "ascii", 1.0)
    except UnicodeDecodeError:
        pass

    if declared:
        label = _CHARSET_ALIASES.get(declared.strip().lower(), declared.strip().lower())
        try:
            text = data.decode(label)
            return DecodedText(text, label, 1.0)
        except (UnicodeDecodeError, LookupError):
            log.debug("Declared charset %r did not decode; falling back", declared)

    try:
        text = data.decode("utf-8")
        # Well-formed UTF-8 by accident is vanishingly unlikely.
        return DecodedText(text, "utf-8", 0.9)
    except UnicodeDecodeError:
        pass

    # Statistical detection needs enough bytes to have anything to work with.
    # On a short header it is guessing: "Café du Nord" in cp1252 is twelve
    # bytes, and charset-normalizer reads it as Arabic. For short undeclared
    # runs in mail the answer is cp1252 - that is what Windows mailers wrote -
    # so it is used directly rather than put to a vote it cannot win.
    if len(data) < _SHORT_RUN:
        try:
            return DecodedText(data.decode("cp1252"), "cp1252", 0.7)
        except UnicodeDecodeError:
            pass

    try:
        from charset_normalizer import from_bytes

        matches = from_bytes(data)
        best = matches.best()
        if best is not None:
            return DecodedText(str(best), best.encoding or "unknown", 0.8)
    except ImportError:
        log.debug("charset-normalizer is not installed; using the fallback list")
    except Exception as exc:  # noqa: BLE001 - detection must never be fatal
        log.debug("charset-normalizer failed on %d bytes: %s", len(data), exc)

    for encoding in _FALLBACKS:
        try:
            return DecodedText(data.decode(encoding), encoding, 0.6)
        except (UnicodeDecodeError, LookupError):
            continue

    # Nothing decoded. Keep the text with replacement characters rather than
    # losing the message: an unreadable word is better than a missing letter.
    return DecodedText(data.decode("utf-8", errors="replace"), "utf-8/replace", 0.5)


# ---------------------------------------------------------------------------
# Mojibake
# ---------------------------------------------------------------------------

# The common sequences, written as the characters they appear as. Each is
# UTF-8 bytes that were read as cp1252. The full repair below is general; this
# table is the fast path and the documented behaviour.
_MOJIBAKE_MAP = {
    "\u00e2\u20ac\u2122": "\u2019",  # right single quote
    "\u00e2\u20ac\u02dc": "\u2018",  # left single quote
    "\u00e2\u20ac\u0153": "\u201c",  # left double quote
    "\u00e2\u20ac\u009d": "\u201d",  # right double quote
    "\u00e2\u20ac\u009c": "\u201c",
    "\u00e2\u20ac\u201d": "\u2014",  # em dash
    "\u00e2\u20ac\u201c": "\u2013",  # en dash
    "\u00e2\u20ac\u00a6": "\u2026",  # ellipsis
    "\u00e2\u20ac\u00a2": "\u2022",  # bullet
    "\u00c2\u00a0": "\u00a0",        # non-breaking space
    "\u00c3\u00a9": "\u00e9",        # e-acute
    "\u00c3\u00a8": "\u00e8",
    "\u00c3\u00a0": "\u00e0",
    "\u00c3\u00b6": "\u00f6",
    "\u00c3\u00bc": "\u00fc",
    "\u00c3\u00a4": "\u00e4",
    "\u00c3\u00b1": "\u00f1",
    "\u00c3\u00a7": "\u00e7",
    "\u00c3\u0178": "\u00df",
    "\u00e2\u201e\u00a2": "\u2122",  # trademark
    "\u00c2\u00a9": "\u00a9",
    "\u00c2\u00ae": "\u00ae",
    "\u00c2\u00b0": "\u00b0",
    "\u00c2\u00a3": "\u00a3",
    "\u00e2\u201a\u00ac": "\u20ac",  # euro
}

#: Characters that appear when UTF-8 is read as cp1252. Their presence in
#: quantity is the giveaway.
_MOJIBAKE_MARKERS = re.compile(
    "[\u00c2\u00c3\u00e2](?=[\u0080-\u00bf\u2000-\u20ff\u0152-\u0178])"
)


def looks_like_mojibake(text: str) -> bool:
    """Does this text contain UTF-8 that was read as cp1252?

    Deliberately conservative. Genuine French or German text contains the same
    characters, so a single match proves nothing; the test is a marker followed
    by a continuation byte, which ordinary prose does not produce.
    """
    if not text:
        return False
    if any(seq in text for seq in _MOJIBAKE_MAP):
        return True
    hits = len(_MOJIBAKE_MARKERS.findall(text))
    return hits >= 2 or (hits == 1 and len(text) < 200)


def repair_mojibake(text: str) -> tuple[str, bool]:
    """Undo a UTF-8-read-as-cp1252 round trip. Returns (text, was_repaired).

    The general repair - re-encode as cp1252, decode as UTF-8 - is tried first
    because it fixes every case at once. If it fails or makes things worse, the
    specific sequences are replaced individually, which is always safe.
    """
    if not looks_like_mojibake(text):
        return text, False

    try:
        candidate = text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
        if not looks_like_mojibake(candidate):
            return candidate, True
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass

    repaired = text
    for bad, good in _MOJIBAKE_MAP.items():
        repaired = repaired.replace(bad, good)
    return repaired, repaired != text


def clean_text(
    data: bytes | str, declared: str | None = None
) -> DecodedText:
    """Decode, repair mojibake, and normalise - the whole pipeline.

    Unicode is normalised to NFC so that "e" plus a combining acute and a
    precomposed e-acute compare equal, which matters for search and for dedup.
    """
    if isinstance(data, str):
        decoded = DecodedText(data, "str", 1.0)
    else:
        decoded = decode_bytes(data, declared)

    repaired, was_repaired = repair_mojibake(decoded.text)
    confidence = decoded.confidence
    if was_repaired:
        # Repair is an improvement, but it is still an inference about what the
        # bytes were meant to be, so it is recorded as such.
        confidence = min(confidence, 0.85)

    text = unicodedata.normalize("NFC", repaired)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")

    return DecodedText(text, decoded.encoding, confidence, repaired_mojibake=was_repaired)


# ---------------------------------------------------------------------------
# HTML to readable plain text
# ---------------------------------------------------------------------------

_BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "table", "section", "article", "hr",
}
_SKIP_CONTENT = {"script", "style", "head", "title", "meta", "link"}


class _HtmlToText(HTMLParser):
    """Enough HTML handling for twenty years of email, and no more.

    Mail HTML is not documents: it is Word exports, Outlook tables and
    one-pixel spacer images. The goal is readable text, not fidelity.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_CONTENT:
            self._skip_depth += 1
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(f"[image: {alt}]")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("#", "javascript:", "cid:")):
                self._pending_href = href

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            href = getattr(self, "_pending_href", None)
            if href:
                # Keep the address: in an archive, where a link pointed is often
                # the only surviving record of a supplier or a project.
                self.parts.append(f" <{href}>")
                self._pending_href = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def html_to_text(html_source: str) -> str:
    """Readable plain text from mail HTML. The HTML itself is kept elsewhere."""
    if not html_source:
        return ""

    parser = _HtmlToText()
    try:
        parser.feed(html_source)
        parser.close()
        text = parser.text()
    except Exception as exc:  # noqa: BLE001 - malformed HTML is the norm here
        log.debug("HTML could not be parsed (%s); falling back to tag stripping", exc)
        text = re.sub(r"<[^>]+>", " ", html_source)
        text = html.unescape(text)

    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# RTF
# ---------------------------------------------------------------------------


def rtf_to_text(rtf: str) -> str:
    """Plain text out of RTF, for messages that have no other body.

    Not a full RTF implementation - a control-word stripper that handles the
    escapes Outlook actually emits, including \\'xx hex bytes and \\uN Unicode
    runs. Anything it cannot interpret is dropped rather than shown as markup.
    """
    if not rtf:
        return ""

    # Drop whole groups that contain no readable text.
    for control in ("fonttbl", "colortbl", "stylesheet", "info", "generator",
                    "pntext", "pntxta", "pntxtb", "listtable", "listoverridetable",
                    "rsidtbl", "themedata", "colorschememapping", "latentstyles",
                    "datastore", "xmlnstbl"):
        rtf = _drop_group(rtf, control)

    out: list[str] = []
    i = 0
    n = len(rtf)
    skip_unicode = 0

    while i < n:
        ch = rtf[i]

        if ch == "\\":
            if i + 1 >= n:
                break
            nxt = rtf[i + 1]

            if nxt == "'":                    # \'xx - a raw byte
                hex_pair = rtf[i + 2 : i + 4]
                try:
                    byte = int(hex_pair, 16)
                    if skip_unicode > 0:
                        skip_unicode -= 1
                    else:
                        out.append(bytes([byte]).decode("cp1252", errors="replace"))
                except ValueError:
                    pass
                i += 4
                continue

            if nxt in "\\{}":                 # escaped literal
                out.append(nxt)
                i += 2
                continue

            if nxt == "~":
                out.append("\u00a0")
                i += 2
                continue
            if nxt in "-_":
                i += 2
                continue

            match = re.match(r"\\([a-zA-Z]+)(-?\d+)? ?", rtf[i:])
            if match:
                word, param = match.group(1), match.group(2)
                if word == "u" and param is not None:
                    code = int(param)
                    if code < 0:
                        code += 65536
                    out.append(chr(code))
                    skip_unicode = 1
                elif word in ("par", "line", "pard"):
                    out.append("\n")
                elif word in ("tab",):
                    out.append("\t")
                elif word in ("emdash",):
                    out.append("\u2014")
                elif word in ("endash",):
                    out.append("\u2013")
                elif word in ("lquote",):
                    out.append("\u2018")
                elif word in ("rquote",):
                    out.append("\u2019")
                elif word in ("ldblquote",):
                    out.append("\u201c")
                elif word in ("rdblquote",):
                    out.append("\u201d")
                elif word == "uc" and param is not None:
                    skip_unicode = 0
                i += match.end()
                continue

            i += 2
            continue

        if ch in "{}":
            i += 1
            continue

        if ch in "\r\n":
            i += 1
            continue

        if skip_unicode > 0:
            skip_unicode -= 1
            i += 1
            continue

        out.append(ch)
        i += 1

    text = "".join(out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _drop_group(rtf: str, control: str) -> str:
    """Remove {\\control ...} groups, counting braces so nesting is handled."""
    marker = "{\\" + control
    out = rtf
    while True:
        start = out.find(marker)
        if start == -1:
            return out
        depth = 0
        i = start
        while i < len(out):
            if out[i] == "{" and (i == 0 or out[i - 1] != "\\"):
                depth += 1
            elif out[i] == "}" and out[i - 1] != "\\":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out = out[:start] + out[i + 1 :]


def is_compressed_rtf(data: bytes) -> bool:
    """Is this the LZFu-compressed RTF Outlook stores in PR_RTF_COMPRESSED?"""
    if len(data) < 16:
        return False
    magic = data[8:12]
    return magic in (b"LZFu", b"MELA")


def decompress_rtf(data: bytes) -> str:
    """Decompress PR_RTF_COMPRESSED (LZFu, [MS-OXRTFCP]).

    Two variants exist: 'MELA' means the RTF was stored uncompressed, and
    'LZFu' is an LZ77 scheme over a fixed dictionary of common RTF tokens.
    Raises ValueError with a specific reason when the stream is not either.
    """
    if len(data) < 16:
        raise ValueError(f"compressed RTF is only {len(data)} bytes, too short to be valid")

    import struct

    compressed_size, raw_size, magic, _crc = struct.unpack("<II4sI", data[:16])
    body = data[16:]

    if magic == b"MELA":
        return body[:raw_size].decode("cp1252", errors="replace")
    if magic != b"LZFu":
        raise ValueError(
            f"compressed RTF has an unknown marker {magic!r}; expected LZFu or MELA"
        )

    # The dictionary every LZFu stream starts from, verbatim from the spec.
    dictionary = bytearray(4096)
    preload = (
        "{\\rtf1\\ansi\\mac\\deff0\\deftab720{\\fonttbl;}{\\f0\\fnil \\froman "
        "\\fswiss \\fmodern \\fscript \\fdecor MS Sans SerifSymbolArial"
        "Times New RomanCourier{\\colortbl\\red0\\green0\\blue0\r\n\\par "
        "\\pard\\plain\\f0\\fs20\\b\\i\\u\\tab\\tx"
    ).encode("ascii")
    dictionary[: len(preload)] = preload
    write_at = len(preload)

    out = bytearray()
    pos = 0
    limit = min(len(body), compressed_size)

    while pos < limit and len(out) < raw_size:
        control = body[pos]
        pos += 1
        for bit in range(8):
            if len(out) >= raw_size or pos >= limit:
                break
            if control & (1 << bit):
                if pos + 1 >= limit:
                    break
                token = (body[pos] << 8) | body[pos + 1]
                pos += 2
                offset = token >> 4
                length = (token & 0x0F) + 2
                if offset == write_at:      # end-of-stream marker
                    return bytes(out).decode("cp1252", errors="replace")
                for k in range(length):
                    byte = dictionary[(offset + k) % 4096]
                    out.append(byte)
                    dictionary[write_at % 4096] = byte
                    write_at += 1
            else:
                byte = body[pos]
                pos += 1
                out.append(byte)
                dictionary[write_at % 4096] = byte
                write_at += 1

    return bytes(out).decode("cp1252", errors="replace")


# ---------------------------------------------------------------------------
# Subjects and signatures
# ---------------------------------------------------------------------------

#: Reply and forward prefixes in the languages this archive is likely to hold.
_REPLY_PREFIX = re.compile(
    r"^\s*(?:"
    r"re|aw|antw|sv|vs|ref|res|r|"
    r"fw|fwd|wg|tr|rv|enc|vb|doorst|"
    r"\[[^\]]{1,40}\]"          # mailing-list tags: [python-dev]
    r")\s*(?:\[\d+\])?\s*[:>]\s*",
    re.IGNORECASE,
)

_LIST_TAG = re.compile(r"^\s*\[[^\]]{1,40}\]\s*")


def normalize_subject(subject: str | None) -> str:
    """Strip RE:, FW:, list tags and repeats, for threading and dedup.

    "RE: FW: Re: [clients] Invoice 44" becomes "invoice 44". Applied
    repeatedly, because twenty years of replies pile the prefixes up.
    """
    if not subject:
        return ""
    text = unicodedata.normalize("NFC", subject).strip()
    for _ in range(12):
        stripped = _REPLY_PREFIX.sub("", text)
        stripped = _LIST_TAG.sub("", stripped)
        if stripped == text:
            break
        text = stripped
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


_SIGNATURE_MARKERS = (
    "\n-- \n",
    "\n--\n",
    "\n________________________________\n",
    "\n-----Original Message-----",
    "\n-----Ursprüngliche Nachricht-----",
    "\nOn ", # "On <date>, <person> wrote:" - handled with the regex below
)

_QUOTE_HEADER = re.compile(
    r"\n\s*(?:On .{0,120}? wrote:|From:.{0,200}?\n\s*(?:Sent|To):)",
    re.IGNORECASE | re.DOTALL,
)


def strip_signature(body: str) -> tuple[str, str | None]:
    """Split a body from its signature and quoted trail.

    Returns (body, removed) - the removed part is kept, not discarded, because
    a signature block is often the only record of somebody's job title, company
    and phone number in 1997.
    """
    if not body:
        return "", None

    cut = None
    for marker in ("\n-- \n", "\n--\n", "\n________________________________\n",
                   "\n-----Original Message-----"):
        idx = body.find(marker)
        if idx != -1 and (cut is None or idx < cut):
            cut = idx

    match = _QUOTE_HEADER.search(body)
    if match and (cut is None or match.start() < cut):
        cut = match.start()

    if cut is None:
        return body.strip(), None

    # Only refuse the split when it would leave nothing behind - a message that
    # is nothing but a signature still has to keep its text. A short message
    # above a quoted reply is perfectly ordinary and must still be split.
    kept = body[:cut].strip()
    if not kept:
        return body.strip(), None
    return kept, body[cut:].strip()


def normalize_whitespace(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def snippet(text: str | None, length: int = 240) -> str:
    """A short readable extract, cut at a word boundary."""
    if not text:
        return ""
    flat = normalize_whitespace(text)
    if len(flat) <= length:
        return flat
    cut = flat[:length]
    space = cut.rfind(" ")
    if space > length * 0.6:
        cut = cut[:space]
    return cut + "\u2026"
