"""Streaming scoped XML conversion with independently replayable evidence.

The converter never holds a whole record (or whole comment, processing
instruction, CDATA section or text run) in memory.  It is a hand written XML
1.0 tokenizer because pyexpat delivers comments and processing instructions
as single completed strings, which would let working memory grow with their
length.  Token chunks are at most one input block wide; the only state that
grows beyond block scale is the element depth stack (start tags and scopes),
exactly as SCOPED.md permits.
"""
from bisect import bisect_right
import codecs
import os
import re
from urllib.parse import urljoin, urlsplit

MAX_CHUNK = 65536
XML_NS = "http://www.w3.org/XML/1998/namespace"
REF_LIMIT = MAX_CHUNK

# XML 1.0 Char production, used as a fast C-level well-formedness probe.
_BAD_CHAR = re.compile(
    "[^\t\n\r  -퟿-�\U00010000-\U0010FFFF]"
)

# XML 1.0 Name / NameStartChar ranges above the ASCII plane (flat pairs).
_NAME_START_PAIRS = (
    (0x00C0, 0x00D6), (0x00D8, 0x00F6), (0x00F8, 0x02FF),
    (0x0370, 0x037D), (0x037F, 0x1FFF), (0x200C, 0x200D),
    (0x2070, 0x218F), (0x2C00, 0x2FEF), (0x3001, 0xD7FF),
    (0xF900, 0xFDCF), (0xFDF0, 0xFFFD), (0x10000, 0xEFFFF),
)
_NAME_EXTRA_PAIRS = ((0x00B7, 0x00B7), (0x0300, 0x036F), (0x203F, 0x2040))
_NAME_STARTS = [v for pair in _NAME_START_PAIRS for v in pair]
_NAME_CHARS = sorted(v for pair in _NAME_START_PAIRS + _NAME_EXTRA_PAIRS
                     for v in pair)

_PREDEFINED = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}


def _in_pairs(code, pairs):
    idx = bisect_right(pairs[1::2], code)
    return idx < len(pairs) // 2 and pairs[idx * 2] <= code


def _is_name_start_char(char):
    code = ord(char)
    if code < 0x80:
        return char.isalpha() or char in "_:"
    return _in_pairs(code, _NAME_STARTS)


def _is_name_char(char, allow_colon):
    code = ord(char)
    if code < 0x80:
        return char.isalnum() or char in "._-" or (allow_colon and char == ":")
    return _in_pairs(code, _NAME_CHARS)


def is_ncname(value):
    """XML 1.0 NCName check (colon forbidden)."""
    if not value:
        return False
    if not _is_name_start_char(value[0]) or value[0] == ":":
        return False
    return all(_is_name_char(char, False) for char in value[1:])


def is_name(value):
    """XML 1.0 Name check (colon allowed as a name character)."""
    if not value or not _is_name_start_char(value[0]):
        return False
    return all(_is_name_char(char, True) for char in value[1:])


def split_qname(text):
    """Split a literal QName; no whitespace trimming beyond QName legality."""
    if ":" in text:
        prefix, local = text.split(":", 1)
        if not prefix or not is_ncname(prefix) or ":" in local:
            raise XmlError("kind must be a legal QName")
    else:
        prefix, local = "", text
    if not is_ncname(local):
        raise XmlError("kind must be a legal QName")
    return prefix, local


def validate_source_base(value):
    if (not isinstance(value, str) or not value
            or any(c.isspace() for c in value)):
        raise ValueError(
            "source_base must be an absolute http(s) URI without whitespace")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ValueError("invalid source_base") from exc
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError("source_base must be an absolute http(s) URI")
    return value


def validate_record_name(value):
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError("record_name must be (namespace_uri, local_name)")
    uri, local = value
    if not isinstance(uri, str) or not isinstance(local, str):
        raise ValueError("record_name members must be strings")
    try:
        uri.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("namespace_uri must be valid Unicode") from exc
    if not is_ncname(local):
        raise ValueError("record_name local_name must be an NCName")
    return uri, local


def validate_absolute_href(value):
    try:
        scheme = urlsplit(value).scheme
    except ValueError as exc:
        raise ValueError("href must be an absolute URI without whitespace") from exc
    if not scheme or any(c.isspace() for c in value):
        raise ValueError("href must be an absolute URI without whitespace")
    return value


def escape_text(value):
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(value):
    return (escape_text(value).replace('"', "&quot;")
            .replace("\t", "&#9;").replace("\n", "&#10;").replace("\r", "&#13;"))


class XmlError(ValueError):
    """Raised for every malformed or unsupported input construct."""


_DATA, _START, _END, _COMMENT, _PI, _CDATA, _REF = range(7)


class _Tokenizer:
    """Incremental XML 1.0 tokenizer with bounded retained tail."""

    def __init__(self, sink):
        self._sink = sink
        self._buf = ""
        self._state = _DATA
        self._cr_carry = False
        self._seen_bom = False
        self._root_seen = False
        self._depth = 0
        self._tokens = 0
        self._comment_dash = 0     # trailing '-' retained at a chunk edge
        self._cdata_brackets = 0   # trailing ']' retained inside CDATA
        self._text_brackets = 0    # trailing ']' retained in element text
        self._tag = ""             # accumulated start/end tag (tag scale)
        self._tag_quote = ""       # quote carried across chunk boundaries
        self._pi_body = ""         # buffered PI target line (bounded)
        self._pi_target = None     # resolved PI target once the line ends
        self._pi_streamed = False  # body data handed to the sink already
        self._ref = ""             # accumulated entity reference body

    # -- input plumbing ---------------------------------------------------

    def push(self, text):
        if not self._seen_bom:
            self._seen_bom = True
            if text.startswith("﻿"):
                text = text[1:]
        if self._cr_carry:
            text = "\r" + text
            self._cr_carry = False
        if text.endswith("\r"):
            self._cr_carry = True
            text = text[:-1]
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if text:
            self._buf += text
            self._drain()

    def finish(self):
        if self._cr_carry:
            self.push("")
        self._drain()
        if self._buf or self._state != _DATA or self._depth:
            raise XmlError("unexpected end of document")
        if not self._root_seen:
            raise XmlError("document must contain a root element")
    def _drain(self):
        while self._buf:
            state = self._state
            if state == _DATA:
                if not self._data_step():
                    return
            elif state == _START:
                if not self._tag_step(self._finish_start):
                    return
            elif state == _END:
                if not self._tag_step(self._finish_end):
                    return
            elif state == _COMMENT:
                if not self._comment_step():
                    return
            elif state == _PI:
                if not self._pi_step():
                    return
            elif state == _CDATA:
                if not self._cdata_step():
                    return
            else:
                if not self._ref_step():
                    return

    # -- data state -------------------------------------------------------

    def _data_step(self):
        markup = self._buf.find("<")
        boundary = len(self._buf) if markup < 0 else markup
        text = self._buf[:boundary]
        amp = text.find("&")
        if amp >= 0:
            text = text[:amp]
            boundary = amp
        if text:
            self._emit_text(text)
            self._buf = self._buf[len(text):]
        if not self._buf:
            return False
        if self._buf.startswith("&"):
            self._state = _REF
            self._ref = ""
            self._buf = self._buf[1:]
            return True
        return self._enter_markup()

    def _ref_step(self):
        semi = self._buf.find(";")
        if semi < 0:
            self._ref += self._buf
            self._buf = ""
            if len(self._ref) > REF_LIMIT:
                raise XmlError("entity reference is too long")
            return False
        self._ref += self._buf[:semi]
        self._buf = self._buf[semi + 1:]
        self._state = _DATA
        self._emit_resolved(self._resolve_ref(self._ref))
        self._ref = ""
        return True

    def _tail_brackets(self, value):
        trailing = len(value) - len(value.rstrip("]"))
        if trailing:
            # Whole segment is brackets (or ends on a run); accumulate with
            # the carry so runs split across chunks stay visible.
            return min(2, self._text_brackets + trailing)
        return 0

    def _check_text(self, value):
        if self._depth == 0:
            # Only whitespace character data is allowed in the prolog and
            # epilog (comments and PIs are handled by their own states).
            if not value.isspace():
                raise XmlError(
                    "text is not allowed outside the root element")
        else:
            probe = "]" * self._text_brackets + value
            if "]]>" in probe:
                raise XmlError("']]>' is not allowed in element content")
        self._text_brackets = self._tail_brackets(value)

    def _emit_resolved(self, value):
        self._check_text(value)
        self._sink.text(value)
    def _enter_markup(self):
        buf = self._buf
        if buf.startswith("</"):
            if len(buf) < 3:
                return False
            self._state = _END
            self._tag = ""
            self._tag_quote = ""
            self._buf = buf[2:]
            return True
        if buf.startswith("<!--"):
            self._state = _COMMENT
            self._comment_dash = 0
            self._buf = buf[4:]
            return True
        if buf.startswith("<![CDATA["):
            self._state = _CDATA
            self._cdata_brackets = 0
            self._buf = buf[9:]
            return True
        if buf.startswith("<?"):
            self._state = _PI
            self._pi_body = ""
            self._pi_target = None
            self._pi_streamed = False
            self._buf = buf[2:]
            return True
        if buf.startswith("<!"):
            cdata_prefix = "<![CDATA["
            comment_prefix = "<!--"
            if len(buf) < len(cdata_prefix) and (
                    cdata_prefix.startswith(buf)
                    or comment_prefix.startswith(buf)):
                # The preamble may still grow into a CDATA section or comment.
                return False
            raise XmlError("DTD and unsupported declarations are forbidden")
        if len(buf) < 2:
            return False
        if not _is_name_start_char(buf[1]):
            raise XmlError("malformed markup")
        self._state = _START
        self._tag = ""
        self._tag_quote = ""
        self._buf = buf[1:]
        return True

    def _emit_text(self, text):
        if _BAD_CHAR.search(text):
            raise XmlError("character outside the XML 1.0 Char production")
        if "&" in text:
            # _data_step cuts at the first '&'; any remainder is handled via
            # the explicit reference state, so this would be a state bug.
            raise XmlError("malformed entity reference")
        if text:
            self._check_text(text)
            self._sink.text(text)

    # -- tag accumulation -------------------------------------------------

    def _tag_step(self, finish):
        buf = self._buf
        quote = self._tag_quote
        for index, char in enumerate(buf):
            if quote:
                if char == quote:
                    quote = ""
            elif char in ('"', "'"):
                quote = char
            elif char == ">":
                self._tag += buf[:index]
                self._buf = buf[index + 1:]
                self._state = _DATA
                self._tag_quote = ""
                finish(self._tag)
                self._tag = ""
                return True
        self._tag += buf
        self._tag_quote = quote
        self._buf = ""
        return False

    def _finish_start(self, body):
        self._tokens += 1
        body = body.strip()
        self_closing = False
        if body.endswith("/"):
            self_closing = True
            body = body[:-1].rstrip()
        pieces = body.split(None, 1)
        if not pieces:
            raise XmlError("missing element name")
        qname = pieces[0]
        if not is_name(qname):
            raise XmlError("malformed element name")
        attrs = (self._parse_attributes(pieces[1].strip())
                 if len(pieces) == 2 else [])
        if self._depth == 0:
            if self._root_seen:
                raise XmlError("documents have exactly one root element")
            self._root_seen = True
        self._depth += 1
        self._sink.start_element(qname, attrs, self_closing)
        if self_closing:
            self._depth -= 1
            self._sink.end_element(qname)

    def _finish_end(self, body):
        self._tokens += 1
        tokens = body.split()
        if len(tokens) != 1 or not is_name(tokens[0]):
            raise XmlError("malformed end tag")
        self._depth -= 1
        self._sink.end_element(tokens[0])

    def _parse_attributes(self, text):
        attrs = []
        seen = set()
        pos = 0
        length = len(text)
        while pos < length:
            while pos < length and text[pos].isspace():
                pos += 1
            if pos == length:
                break
            start = pos
            while pos < length and not text[pos].isspace() and text[pos] != "=":
                pos += 1
            name = text[start:pos]
            if not is_name(name) or name in seen:
                raise XmlError("malformed or duplicate attribute")
            seen.add(name)
            while pos < length and text[pos].isspace():
                pos += 1
            if pos >= length or text[pos] != "=":
                raise XmlError("attribute values are required")
            pos += 1
            while pos < length and text[pos].isspace():
                pos += 1
            if pos >= length or text[pos] not in ('"', "'"):
                raise XmlError("attribute values must be quoted")
            quote = text[pos]
            pos += 1
            end = text.find(quote, pos)
            if end < 0:
                raise XmlError("unterminated attribute value")
            value = self._decode_attr_raw(text[pos:end])
            attrs.append((name, value))
            pos = end + 1
        return attrs

    def _decode_attr_raw(self, raw):
        # XML 1.0 attribute normalization: raw line ends/tabs become spaces;
        # replacement text of character references is kept verbatim.
        out = []
        pos = 0
        while pos < len(raw):
            amp = raw.find("&", pos)
            if amp < 0:
                out.append(raw[pos:].translate(
                    {9: " ", 10: " ", 13: " "}))
                break
            out.append(raw[pos:amp].translate({9: " ", 10: " ", 13: " "}))
            semi = raw.find(";", amp + 1)
            if semi < 0:
                raise XmlError("unterminated entity reference")
            if semi - amp > REF_LIMIT:
                raise XmlError("entity reference is too long")
            out.append(self._resolve_ref(raw[amp + 1:semi]))
            pos = semi + 1
        value = "".join(out)
        if "<" in value:
            raise XmlError("'<' is not allowed in attribute values")
        return value

    # -- comments ---------------------------------------------------------

    def _comment_step(self):
        end = self._buf.find("-->")
        if end >= 0:
            segment = self._buf[:end]
            self._check_comment(segment, closing=True)
            self._sink.comment(segment)
            self._buf = self._buf[end + 3:]
            self._state = _DATA
            self._comment_dash = 0
            self._tokens += 1
            return True
        keep = min(2, len(self._buf) - len(self._buf.rstrip("-")))
        segment = self._buf[:len(self._buf) - keep]
        self._check_comment(segment, closing=False)
        if segment:
            self._sink.comment(segment)
        self._buf = self._buf[len(segment):]
        self._comment_dash = len(self._buf)
        return False

    def _check_comment(self, segment, closing):
        if _BAD_CHAR.search(segment):
            raise XmlError("character outside the XML 1.0 Char production")
        probe = "-" * self._comment_dash + segment
        if closing:
            # The only legal '--' is the terminator's own pair at the end.
            body = probe[:-2] if probe.endswith("--") else probe
            if "--" in body:
                raise XmlError("'--' is not allowed inside comments")
        elif "--" in probe:
            raise XmlError("'--' is not allowed inside comments")

    # -- processing instructions -----------------------------------------

    def _pi_step(self):
        end = self._buf.find("?>")
        if end >= 0:
            segment = self._buf[:end]
            self._buf = self._buf[end + 2:]
            self._state = _DATA
            self._pi_body += segment
            self._finish_pi()
            return True
        keep = 1 if self._buf.endswith("?") else 0
        segment = self._buf[:len(self._buf) - keep]
        if _BAD_CHAR.search(segment):
            raise XmlError("character outside the XML 1.0 Char production")
        self._buf = self._buf[len(segment):]
        if self._pi_target is None:
            # Still accumulating the bounded target line.
            self._pi_body += segment
            boundary = self._pi_body.find(" ")
            if boundary >= 0:
                self._pi_target = self._pi_body[:boundary]
                self._begin_pi()
                tail = self._pi_body[boundary:]
                if self._pi_streamed and tail:
                    self._sink.pi_data(tail)
        elif self._pi_streamed:
            # Target line resolved: stream the body data as it arrives.
            self._sink.pi_data(segment)
        else:
            # xml declaration: keep the whole (short) declaration buffered.
            self._pi_body += segment
        return False

    def _begin_pi(self):
        target = self._pi_target
        if not target:
            raise XmlError("processing instruction without target")
        if target.lower() == "xml":
            # The xml declaration is parsed whole when it completes.
            self._pi_streamed = False
            return
        self._pi_streamed = True
        self._tokens += 1
        self._sink.pi_begin(target)

    def _finish_pi(self):
        body = self._pi_body
        target = self._pi_target
        if self._pi_streamed:
            self._sink.pi_end()
        else:
            self._tokens += 1
            if not body or body.isspace():
                raise XmlError("processing instruction without target")
            if target is None:
                target = body.split(None, 1)[0]
            if target.lower() == "xml":
                if self._tokens != 1 or self._root_seen or self._depth:
                    raise XmlError(
                        "the xml declaration must start the document")
                self._parse_xml_declaration(body)
            else:
                if "?>" in body:
                    raise XmlError("malformed processing instruction")
                self._sink.processing_instruction(body)
        self._pi_body = ""
        self._pi_target = None
        self._pi_streamed = False

    def _parse_xml_declaration(self, body):
        parts = body.split()
        if parts[0] != "xml":
            raise XmlError("malformed xml declaration")
        version = encoding = standalone = None
        for token in parts[1:]:
            if "=" not in token:
                raise XmlError("malformed xml declaration")
            key, value = token.split("=", 1)
            if len(value) < 2 or value[0] not in ('"', "'") or value[-1] != value[0]:
                raise XmlError("malformed xml declaration")
            value = value[1:-1]
            if key == "version":
                version = value
            elif key == "encoding":
                encoding = value
            elif key == "standalone":
                standalone = value
            else:
                raise XmlError("unknown xml declaration attribute")
        if version != "1.0":
            raise XmlError("expected XML 1.0")
        if encoding is not None and encoding.lower() not in ("utf-8", "utf8"):
            raise XmlError("expected UTF-8 XML 1.0")
        if standalone is not None and standalone not in ("yes", "no"):
            raise XmlError("malformed xml declaration")

    # -- CDATA ------------------------------------------------------------

    def _cdata_step(self):
        end = self._buf.find("]]>")
        if end >= 0:
            segment = self._buf[:end]
            self._emit_cdata(segment)
            self._buf = self._buf[end + 3:]
            self._state = _DATA
            self._cdata_brackets = 0
            self._tokens += 1
            return True
        keep = min(2, len(self._buf) - len(self._buf.rstrip("]")))
        segment = self._buf[:len(self._buf) - keep]
        self._emit_cdata(segment)
        self._buf = self._buf[len(segment):]
        self._cdata_brackets = len(self._buf)
        return False

    def _emit_cdata(self, segment):
        if _BAD_CHAR.search(segment):
            raise XmlError("character outside the XML 1.0 Char production")
        if segment:
            self._sink.cdata(segment)

    # -- references -------------------------------------------------------

    @staticmethod
    def _resolve_ref(ref):
        if not ref:
            raise XmlError("empty entity reference")
        if ref[0] == "#":
            digits = ref[1:]
            try:
                if digits[:1] in ("x", "X"):
                    code = int(digits[1:], 16)
                else:
                    code = int(digits, 10)
            except ValueError as exc:
                raise XmlError("malformed numeric character reference") from exc
            if (0 < code < 0x20 and code not in (0x9, 0xA, 0xD)
                    or code in (0xFFFE, 0xFFFF)
                    or 0xD800 <= code <= 0xDFFF or code > 0x10FFFF
                    or code == 0):
                raise XmlError(
                    "numeric character reference outside the Char production")
            return chr(code)
        if ref not in _PREDEFINED:
            raise XmlError("only predefined and numeric entities are allowed")
        return _PREDEFINED[ref]


class _Frame:
    __slots__ = ("qname", "decls", "default", "base", "record", "handle",
                 "record_meta", "self_closed", "path")

    def __init__(self, qname, default, base):
        self.qname = qname
        self.decls = {}
        self.default = default
        self.base = base
        self.record = False
        self.handle = None
        self.record_meta = None
        self.self_closed = False
        self.path = None


class ScopedConverter:
    """Token sink that builds rows and streams evidence sidecars."""

    def __init__(self, staging_dir, record_name, source_base):
        self._dir = staging_dir
        self._record_uri, self._record_local = record_name
        self._source_base = source_base
        self._tokenizer = _Tokenizer(self)
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._frames = []
        self._open = []            # open evidence handles, depth bounded
        self._seq = 0

    # -- source iteration -------------------------------------------------

    def consume(self, chunks):
        for block in chunks:
            if not isinstance(block, bytes) or len(block) > MAX_CHUNK:
                raise ValueError(
                    "chunks must contain bytes of at most 65536 bytes")
            if block:
                text = self._decoder.decode(block, False)
                if text:
                    self._tokenizer.push(text)
        tail = self._decoder.decode(b"", True)
        if tail:
            self._tokenizer.push(tail)
        self._tokenizer.finish()
        return self._seq

    # -- token sink -------------------------------------------------------

    def text(self, value):
        if self._open:
            payload = escape_text(value).encode("utf-8")
            for handle in self._open:
                handle.write(payload)

    def cdata(self, value):
        self.text(value)

    def comment(self, value):
        if self._open:
            payload = ("<!--" + value + "-->").encode("utf-8")
            for handle in self._open:
                handle.write(payload)

    def processing_instruction(self, body):
        if self._open:
            payload = ("<?" + body + "?>").encode("utf-8")
            for handle in self._open:
                handle.write(payload)

    def pi_begin(self, target):
        if self._open:
            payload = ("<?" + target).encode("utf-8")
            for handle in self._open:
                handle.write(payload)

    def pi_data(self, segment):
        if self._open:
            payload = segment.encode("utf-8")
            for handle in self._open:
                handle.write(payload)

    def pi_end(self):
        if self._open:
            for handle in self._open:
                handle.write(b"?>")

    def start_element(self, qname, attrs, self_closing):
        parent = self._frames[-1] if self._frames else None
        frame = _Frame(qname, parent.default if parent else "",
                       parent.base if parent else self._source_base)
        business = []
        for name, value in attrs:
            if name == "xmlns":
                frame.decls[""] = value
                frame.default = value
            elif name.startswith("xmlns:"):
                prefix = name[6:]
                if not is_ncname(prefix) or prefix == "xmlns":
                    raise XmlError("malformed namespace declaration")
                if prefix == "xml" and value != XML_NS:
                    raise XmlError("the xml prefix has a fixed binding")
                if value == "":
                    raise XmlError("prefixed namespace bindings may not be empty")
                frame.decls[prefix] = value
            elif name in ("xml:lang", "xml:space"):
                raise ValueError(name + " is not supported")
            elif name == "xml:base":
                if any(c.isspace() for c in value):
                    raise ValueError("URI references must not contain whitespace")
                try:
                    frame.base = urljoin(frame.base, value)
                except ValueError as exc:
                    raise ValueError("invalid xml:base") from exc
                business.append((name, value))
            else:
                business.append((name, value))
        # Namespace well-formedness for every element and attribute name.
        self._expand(qname, frame)
        for name, _value in business:
            if ":" in name:
                prefix, local = split_qname(name)
                if not is_ncname(local):
                    raise XmlError("malformed attribute name")
                self._lookup_prefix(prefix, frame)
        is_record = self._is_record(qname, frame)
        if is_record:
            meta = self._record_meta(business, frame)
            handle, path = self._open_evidence(qname, frame, business)
            frame.record = True
            frame.handle = handle
            frame.record_meta = meta
            frame.path = path
            self._open.append(handle)
            # The same element is an ordinary descendant inside every
            # ancestor evidence, preserving its literal attributes.
            payload = self._plain_start(qname, attrs, self_closing)
            for ancestor_handle in self._open[:-1]:
                ancestor_handle.write(payload)
        elif self._open:
            payload = self._plain_start(qname, attrs, self_closing)
            for handle in self._open:
                handle.write(payload)
        frame.self_closed = self_closing
        self._frames.append(frame)

    def end_element(self, qname):
        frame = self._frames.pop()
        if frame.qname != qname:
            raise XmlError("mismatched element tags")
        if frame.record:
            frame.handle.write(("</" + qname + ">").encode("utf-8"))
            self._stage_meta(frame)
            frame.handle.close()
            self._open.remove(frame.handle)
            # Close the element inside ancestor evidence as well.
            if not frame.self_closed:
                payload = ("</" + qname + ">").encode("utf-8")
                for handle in self._open:
                    handle.write(payload)
        elif self._open and not frame.self_closed:
            payload = ("</" + qname + ">").encode("utf-8")
            for handle in self._open:
                handle.write(payload)

    # -- naming and scopes ------------------------------------------------

    def _is_record(self, qname, frame):
        uri, local = self._expand(qname, frame)
        return uri == self._record_uri and local == self._record_local

    def _lookup_prefix(self, prefix, frame):
        if prefix == "xml":
            return XML_NS
        if prefix in frame.decls:
            return frame.decls[prefix]
        for ancestor in reversed(self._frames):
            if prefix in ancestor.decls:
                return ancestor.decls[prefix]
        raise XmlError("unbound namespace prefix: " + prefix)

    def _expand(self, qname, frame):
        if ":" in qname:
            prefix, local = split_qname(qname)
            return self._lookup_prefix(prefix, frame), local
        return frame.default or "", qname

    def _record_meta(self, business, frame):
        values = dict(business)
        for required in ("id", "kind", "href"):
            if required not in values:
                raise ValueError(
                    "record elements require id, kind and href attributes")
        ident = values["id"]
        if not ident:
            raise ValueError("id must be a nonempty string")
        prefix, local = split_qname(values["kind"])
        if prefix:
            kind_uri = self._lookup_prefix(prefix, frame)
        else:
            kind_uri = frame.default or ""
        href_text = values["href"]
        if any(c.isspace() for c in href_text):
            raise ValueError("URI references must not contain whitespace")
        try:
            absolute = urljoin(frame.base, href_text)
        except ValueError as exc:
            raise ValueError("invalid href reference") from exc
        validate_absolute_href(absolute)
        return ident, kind_uri, local, absolute

    # -- evidence serialization ------------------------------------------

    def _open_evidence(self, qname, frame, business):
        name = "%012d" % self._seq
        self._seq += 1
        path = os.path.join(self._dir, name)
        os.mkdir(path)
        handle = open(os.path.join(path, "evidence"), "wb",
                      buffering=MAX_CHUNK)
        handle.write(b'<?xml version="1.0" encoding="UTF-8"?>')
        bindings = {}
        for ancestor in self._frames:
            bindings.update(ancestor.decls)
        bindings.update(frame.decls)
        default_now = bindings.pop("", frame.default or "")
        parts = ["<", qname]
        for prefix in sorted(bindings):
            parts.append(' xmlns:' + prefix + '="'
                         + escape_attr(bindings[prefix]) + '"')
        if default_now:
            parts.append(' xmlns="' + escape_attr(default_now) + '"')
        parts.append(' xml:base="' + escape_attr(frame.base) + '"')
        for name, value in business:
            if name == "xml:base" or name == "xmlns" or name.startswith("xmlns:"):
                continue
            parts.append(" " + name + '="' + escape_attr(value) + '"')
        parts.append(">")
        handle.write("".join(parts).encode("utf-8"))
        return handle, path

    @staticmethod
    def _plain_start(qname, attrs, self_closing):
        parts = ["<", qname]
        for name, value in attrs:
            parts.append(" " + name + '="' + escape_attr(value) + '"')
        parts.append("/>" if self_closing else ">")
        return "".join(parts).encode("utf-8")

    def _stage_meta(self, frame):
        ident, kind_uri, local, href = frame.record_meta
        with open(os.path.join(frame.path, "meta"), "w",
                  encoding="utf-8") as meta_file:
            meta_file.write("\x1f".join((ident, kind_uri, local, href)))

    # -- publishing -------------------------------------------------------

    def publish(self, batch):
        count = 0
        with os.scandir(self._dir) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                with open(os.path.join(entry.path, "meta"),
                          encoding="utf-8") as meta_file:
                    ident, kind_uri, local, href = \
                        meta_file.read().split("\x1f")
                batch.add_row(ident, (kind_uri, local), href)

                def bounded_read(path=os.path.join(entry.path, "evidence")):
                    with open(path, "rb") as evidence_file:
                        while True:
                            piece = evidence_file.read(MAX_CHUNK)
                            if not piece:
                                break
                            yield piece

                batch.put_evidence(ident, bounded_read())
                count += 1
        return count


def run_scoped(batch, chunks, staging_dir, record_name, source_base):
    converter = ScopedConverter(
        staging_dir, validate_record_name(record_name),
        validate_source_base(source_base))
    staged = converter.consume(chunks)
    published = converter.publish(batch)
    if staged != published:
        raise ValueError("scoped import lost staged records")
    return published
