"""Real flat and scoped imports, evidence replay, rollback and reopen."""
import hashlib
from pathlib import Path
import tempfile
from urllib.parse import urljoin
from xml.parsers import expat

from archive import Archive, MAX_CHUNK

ROOT = Path(__file__).resolve().parent
RECORD_NAME = ("urn:record", "item")
SOURCE_BASE = "https://e.test/root/"
# Deliberately unrelated to the original source base.
FOREIGN_BASE = "https://replay.example/another/place/"


def file_chunks(path):
    with path.open("rb") as source:
        while block := source.read(MAX_CHUNK):
            yield block


def explain(evidence_bytes, record_name, base):
    """Independently explain one evidence document with expat."""
    pending = []
    stack = [{"prefixes": {"xml": "http://www.w3.org/XML/1998/namespace"},
              "default": "", "base": base}]
    result = {}
    parser = expat.ParserCreate(namespace_separator="|")

    def ns_start(prefix, uri):
        pending.append((prefix or "", uri))

    def start(name, attrs):
        frame = {"prefixes": dict(stack[-1]["prefixes"]),
                 "default": stack[-1]["default"],
                 "base": stack[-1]["base"]}
        for prefix, uri in pending:
            frame["prefixes"][prefix] = uri
            if prefix == "":
                frame["default"] = uri
        pending.clear()
        stack.append(frame)
        xml_base = "http://www.w3.org/XML/1998/namespace|base"
        if xml_base in attrs:
            frame["base"] = urljoin(frame["base"], attrs[xml_base])
        uri, local = (name.rsplit("|", 1) if "|" in name else ("", name))
        if (uri, local) == record_name:
            kind = attrs["kind"]
            if ":" in kind:
                prefix, kind_local = kind.split(":", 1)
                kind = (frame["prefixes"][prefix], kind_local)
            else:
                kind = (frame["default"] or "", kind)
            result[attrs["id"]] = (kind, urljoin(frame["base"], attrs["href"]))

    def end(name):
        stack.pop()

    parser.StartNamespaceDeclHandler = ns_start
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.Parse(evidence_bytes, True)
    return result


def flat_demo(path):
    with Archive(path) as archive:
        count = archive.ingest(file_chunks(ROOT / "samples" / "feed.xml"))
        with archive.transaction() as batch:
            batch.put_attachment("supplier-note",
                                 file_chunks(ROOT / "samples" / "note.txt"))
        print("flat imported:", count)
        for ident in archive.ids():
            print(" ", ident, archive.row(ident))
        before = archive.ids()
        try:
            archive.ingest(
                [b'<feed><r id="unpublished" kind="Sale" '
                 b'href="https://e.test/x"/>'])
        except ValueError:
            pass
        else:
            raise AssertionError("truncated document was accepted")
        assert archive.ids() == before
        print("truncated flat batch rolled back")
    with Archive(path) as reopened:
        with reopened.attachment("supplier-note") as reader:
            payload = reader.read()
        assert payload == (ROOT / "samples" / "note.txt").read_bytes()
        assert reopened.attachment_info("supplier-note") == (
            len(payload), hashlib.sha256(payload).hexdigest())
        assert reopened.ids() == before
        print("reopened flat rows:", len(before),
              "attachment:", payload.decode().rstrip())


def scoped_demo(path):
    print()
    with Archive(path) as archive:
        source = (ROOT / "samples" / "scoped.xml").read_bytes()
        count = archive.ingest_scoped(
            file_chunks(ROOT / "samples" / "scoped.xml"),
            RECORD_NAME, SOURCE_BASE)
        assert count == 2
        print("scoped imported:", count, "from", len(source), "source bytes")
        for ident in archive.ids():
            print(" ", ident, archive.row(ident))
        expected = {ident: archive.row(ident) for ident in archive.ids()}

        # A literal cut of the inner record loses its ancestor namespace and
        # base environment and cannot be replayed on its own.
        literal = source[source.find(b"<r:item xmlns:k="):]
        literal = literal[: literal.rfind(b"</r:item>") + len(b"</r:item>")]
        try:
            expat.ParserCreate(namespace_separator="|").Parse(literal, True)
        except expat.ExpatError as exc:
            print("literal cut alone fails to parse:", exc)

        # Every persisted evidence is self-contained and replays under a base
        # unrelated to the original source, yielding identical semantics.
        for ident in archive.ids():
            with archive.evidence(ident) as reader:
                evidence = reader.read()
            replayed = explain(evidence, RECORD_NAME, FOREIGN_BASE)
            kind, href = replayed[ident]
            assert (kind, href) == expected[ident], (ident, replayed)
            print(" evidence", ident, "replayed under foreign base ->",
                  kind, href)
            # Nested record content survives inside the outer evidence.
            if ident == "outer":
                assert b'id="inner"' in evidence
                assert b"<plain" in evidence and b"k:literal" in evidence
            if ident == "inner":
                assert b"<plain" in evidence
        # The caller's attachment key space stays separate.
        with archive.transaction() as batch:
            batch.put_attachment("outer", [b"caller attachment, not evidence"])
        with archive.attachment("outer") as reader:
            assert reader.read() == b"caller attachment, not evidence"
        print("same-named generic attachment left untouched by evidence")

        # A failing scoped import publishes nothing.
        before = archive.ids()
        try:
            archive.ingest_scoped(
                [b'<feed xmlns:r="urn:record"><r:item id="broken"'],
                RECORD_NAME, SOURCE_BASE)
        except ValueError:
            pass
        else:
            raise AssertionError("truncated scoped document was accepted")
        assert archive.ids() == before
        print("truncated scoped batch rolled back")

    with Archive(path) as reopened:
        assert reopened.ids() == before
        for ident in reopened.ids():
            with reopened.evidence(ident) as reader:
                evidence = reader.read()
            replayed = explain(evidence, RECORD_NAME, FOREIGN_BASE)
            assert replayed[ident] == expected[ident]
        print("reopen: all rows and independent evidences replay identically")


def main():
    with tempfile.TemporaryDirectory(prefix="archive-demo-") as directory:
        flat_demo(Path(directory) / "flat.sqlite")
        scoped_demo(Path(directory) / "scoped.sqlite")


if __name__ == "__main__":
    main()
