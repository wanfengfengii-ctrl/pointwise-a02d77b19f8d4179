"""Transactional flat XML rows and independent streamed binary attachments."""
from collections import deque
from contextlib import contextmanager
import codecs
import hashlib
import io
import os
import re
import sqlite3
from urllib.parse import urlsplit
import weakref
from xml.parsers import expat

MAX_CHUNK = 65536
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
SCHEMA = """
CREATE TABLE IF NOT EXISTS records(
    id TEXT PRIMARY KEY NOT NULL,
    kind_uri TEXT NOT NULL,
    kind_local TEXT NOT NULL,
    href TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attachments(
    name TEXT PRIMARY KEY NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attachment_chunks(
    name TEXT NOT NULL REFERENCES attachments(name) ON DELETE CASCADE,
    part INTEGER NOT NULL CHECK(part >= 0),
    data BLOB NOT NULL,
    PRIMARY KEY(name, part)
);
"""


def _text(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(label + " must be a nonempty string")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(label + " must be valid Unicode") from exc
    return value


def _kind(value):
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError("kind must be (namespace_uri, local_name)")
    uri, local = value
    if not isinstance(uri, str):
        raise ValueError("namespace_uri must be a string")
    try:
        uri.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("namespace_uri must be valid Unicode") from exc
    _text(local, "local_name")
    return uri, local


def _href(value):
    _text(value, "href")
    try:
        scheme = urlsplit(value).scheme
    except ValueError as exc:
        raise ValueError("invalid href") from exc
    if not scheme or any(c.isspace() for c in value):
        raise ValueError("href must be an absolute URI without whitespace")
    return value


def byte_chunks(chunks):
    """Validate a single-pass stream without replacing source exceptions."""
    for block in chunks:
        if not isinstance(block, bytes) or len(block) > MAX_CHUNK:
            raise ValueError("chunks must contain bytes of at most 65536 bytes")
        yield block


def read_flat(chunks, record_name="r"):
    """Yield (id, (empty_namespace, kind), absolute_href); EOF validates XML."""
    if not isinstance(record_name, str) or not _NAME.fullmatch(record_name) or record_name == "feed":
        raise ValueError("invalid record_name")
    parser = expat.ParserCreate()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    pending = deque()
    depth = 0

    def start(name, attrs):
        nonlocal depth
        if ":" in name or any(":" in k or k == "xmlns" for k in attrs):
            raise ValueError("namespaces are not supported by flat import")
        if depth == 0:
            if name != "feed" or attrs:
                raise ValueError("expected an attribute-free feed root")
        elif depth == 1:
            if name != record_name or set(attrs) != {"id", "kind", "href"}:
                raise ValueError("expected a flat record with id, kind and href")
            ident = _text(attrs["id"], "id")
            kind = attrs["kind"]
            if not _NAME.fullmatch(kind):
                raise ValueError("flat kind must be an ASCII NCName")
            pending.append((ident, ("", kind), _href(attrs["href"])))
        else:
            raise ValueError("nested elements are not supported by flat import")
        depth += 1

    def end(name):
        nonlocal depth
        depth -= 1

    def text(data):
        if depth != 2 and any(c not in " \t\r\n" for c in data):
            raise ValueError("text outside a record")

    def declaration(version, encoding, standalone):
        if version != "1.0" or (encoding and encoding.lower() not in ("utf-8", "utf8")):
            raise ValueError("expected UTF-8 XML 1.0")

    def forbidden(*args):
        raise ValueError("DTD and external entities are forbidden")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    parser.XmlDeclHandler = declaration
    parser.StartDoctypeDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def parse(block, final):
        try:
            parser.Parse(decoder.decode(block, final=final), final)
        except (UnicodeError, expat.ExpatError) as exc:
            raise ValueError("invalid UTF-8 XML") from exc

    for block in byte_chunks(chunks):
        parse(block, False)
        while pending:
            yield pending.popleft()
    parse(b"", True)
    while pending:
        yield pending.popleft()


class _ChunkReader(io.RawIOBase):
    def __init__(self, path, name):
        super().__init__()
        self._conn = None
        self._cursor = None
        self._pending = memoryview(b"")
        self._conn = sqlite3.connect(path, isolation_level=None)
        try:
            self._conn.execute("PRAGMA query_only=ON")
            self._conn.execute("PRAGMA cache_size=-512")
            self._cursor = self._conn.execute(
                "SELECT data FROM attachment_chunks WHERE name=? ORDER BY part", (name,))
        except BaseException:
            self._conn.close()
            raise
        self._pending = memoryview(b"")

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.closed:
            raise ValueError("read from closed attachment")
        target = memoryview(buffer).cast("B")
        filled = 0
        while filled < len(target):
            if not self._pending:
                row = self._cursor.fetchone()
                if row is None:
                    break
                self._pending = memoryview(row[0])
                if not self._pending:
                    continue
            size = min(len(target) - filled, len(self._pending))
            target[filled:filled + size] = self._pending[:size]
            self._pending = self._pending[size:]
            filled += size
        return filled

    def close(self):
        if not self.closed:
            try:
                if self._cursor is not None:
                    self._cursor.close()
                if self._conn is not None:
                    self._conn.close()
                self._pending = memoryview(b"")
            finally:
                super().close()


class Batch:
    """A transaction-scoped append port. Any failed write poisons the batch."""
    def __init__(self, archive):
        self._archive = archive
        self._active = True
        self._failed = False

    @contextmanager
    def _writing(self):
        if not self._active or self._failed:
            raise RuntimeError("batch is inactive or failed")
        try:
            yield self._archive._conn
        except BaseException:
            self._failed = True
            raise

    def add_row(self, ident, kind, href):
        with self._writing() as conn:
            _text(ident, "id")
            uri, local = _kind(kind)
            _href(href)
            try:
                conn.execute("INSERT INTO records VALUES(?,?,?,?)", (ident, uri, local, href))
            except sqlite3.IntegrityError as exc:
                raise ValueError("duplicate record id") from exc

    def put_attachment(self, name, chunks):
        with self._writing() as conn:
            _text(name, "attachment name")
            try:
                conn.execute("INSERT INTO attachments VALUES(?,0,'')", (name,))
            except sqlite3.IntegrityError as exc:
                raise ValueError("duplicate attachment name") from exc
            digest = hashlib.sha256()
            size = 0
            part = 0
            for block in byte_chunks(chunks):
                if not block:
                    continue
                conn.execute("INSERT INTO attachment_chunks VALUES(?,?,?)", (name, part, block))
                digest.update(block)
                size += len(block)
                part += 1
            conn.execute("UPDATE attachments SET size=?, sha256=? WHERE name=?",
                         (size, digest.hexdigest(), name))


class Archive:
    """One writer, append-only rows and separately named binary attachments."""
    def __init__(self, path):
        self._path = os.fspath(path)
        if not isinstance(self._path, str):
            raise ValueError("path must be text")
        if not self._path or self._path == ":memory:":
            raise ValueError("a filesystem database path is required")
        self._conn = sqlite3.connect(self._path, isolation_level=None)
        self._closed = False
        self._busy = False
        self._readers = weakref.WeakSet()
        try:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA cache_size=-512")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported archive version")
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA user_version=1")
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    def _idle(self):
        if self._closed:
            raise RuntimeError("archive is closed")
        if self._busy:
            raise RuntimeError("archive queries and nested transactions are forbidden during a batch")

    @contextmanager
    def transaction(self):
        self._idle()
        self._conn.execute("BEGIN IMMEDIATE")
        self._busy = True
        batch = Batch(self)
        try:
            yield batch
            if batch._failed:
                raise ValueError("batch contains a failed write")
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        finally:
            batch._active = False
            self._busy = False

    def ingest(self, chunks, record_name="r"):
        count = 0
        with self.transaction() as batch:
            for ident, kind, href in read_flat(chunks, record_name):
                batch.add_row(ident, kind, href)
                count += 1
        return count

    def row(self, ident):
        self._idle()
        _text(ident, "id")
        row = self._conn.execute(
            "SELECT kind_uri,kind_local,href FROM records WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        return ((row[0], row[1]), row[2])

    def ids(self):
        self._idle()
        return tuple(row[0] for row in self._conn.execute("SELECT id FROM records ORDER BY id"))

    def attachment_info(self, name):
        self._idle()
        _text(name, "attachment name")
        row = self._conn.execute(
            "SELECT size,sha256 FROM attachments WHERE name=?", (name,)).fetchone()
        if row is None:
            raise KeyError(name)
        return row

    def attachment(self, name):
        self.attachment_info(name)
        reader = io.BufferedReader(_ChunkReader(self._path, name), buffer_size=MAX_CHUNK)
        self._readers.add(reader)
        return reader

    def close(self):
        if self._closed:
            return
        if self._busy:
            raise RuntimeError("cannot close during a batch")
        for reader in list(self._readers):
            reader.close()
        self._conn.close()
        self._closed = True

    def __enter__(self):
        self._idle()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
