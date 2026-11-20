import hashlib
import io
from pathlib import Path
import random
import sqlite3
import tempfile
import tracemalloc
import unittest
from xml.sax.saxutils import escape, quoteattr

from archive import Archive, MAX_CHUNK, read_flat

ROOT = Path(__file__).resolve().parents[1]


def chunks(data, size):
    for offset in range(0, len(data), size):
        yield data[offset:offset + size]


def xml_record(ident="a", kind="Sale", href="https://e.test/a", body=""):
    return ("<feed><r id=" + quoteattr(ident) + " kind=" + quoteattr(kind)
            + " href=" + quoteattr(href) + ">" + body + "</r></feed>").encode()


class ParserTests(unittest.TestCase):
    def test_sample_all_small_splits(self):
        data = (ROOT / "samples" / "feed.xml").read_bytes()
        expected = [
            ("sale-1", ("", "Sale"), "https://vendor.example/daily/1"),
            ("退货-2", ("", "Return"), "https://vendor.example/daily/2"),
        ]
        for size in (1, 2, 3, 7, 17, MAX_CHUNK):
            self.assertEqual(list(read_flat(chunks(data, size))), expected)

    def test_every_two_way_boundary(self):
        data = xml_record("中", body="A&#x42;<![CDATA[<中>]]><?ok here?><!--x-->&amp;")
        expected = [("中", ("", "Sale"), "https://e.test/a")]
        for split in range(len(data) + 1):
            self.assertEqual(list(read_flat([data[:split], b"", data[split:]])), expected)

    def test_declared_encoding_and_version(self):
        for declaration in (b'<?xml version="1.1"?>',
                            b'<?xml version="1.0" encoding="ISO-8859-1"?>'):
            with self.assertRaises(ValueError):
                list(read_flat([declaration + b"<feed/>"]))

    def test_reject_unsupported_or_malformed(self):
        bad = [
            b"", b"<feed>", b"<feed/><feed/>", b"<feed>x</feed>",
            b"<other/>", b'<feed id="x"/>', b"<feed><s/></feed>",
            b"<feed><r/></feed>",
            xml_record(kind="p:T"),
            xml_record(kind=""),
            xml_record(ident=""),
            xml_record(href="relative/path"),
            xml_record(href="https://e.test/a b"),
            xml_record(body="<b/>"),
            b'<feed xmlns="urn:x"/>',
            b'<feed xmlns:p="urn:p"/>',
            b'<feed xml:base="https://e.test/"/>',
            b'<!DOCTYPE feed><feed/>',
            b'<!DOCTYPE feed [<!ENTITY x "value">]><feed/>',
            b'<feed>\xff</feed>',
            xml_record(body="&#0;"),
            b'<!DOCTYPE feed SYSTEM "file:///must-not-open"><feed/>',
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError):
                list(read_flat(chunks(data, 1)))

    def test_chunk_and_record_name_validation(self):
        for source in ([bytearray(b"<feed/>")], ["<feed/>"], [b"x" * (MAX_CHUNK + 1)]):
            with self.assertRaises(ValueError):
                list(read_flat(source))
        for name in ("", "feed", "p:r", 7):
            with self.assertRaises(ValueError):
                list(read_flat([b"<feed/>"], name))
        data = b'<feed><item id="a" kind="T" href="urn:a"/></feed>'
        self.assertEqual(list(read_flat([data], "item")), [("a", ("", "T"), "urn:a")])

    def test_independent_generated_expectations(self):
        rng = random.Random(721)
        rows = []
        pieces = ["<feed>"]
        for index in range(120):
            ident = "r" + str(index) + rng.choice(["中", "&", '"'])
            kind = rng.choice(["Sale", "Return", "_x-2"])
            href = "https://e.test/" + str(index) + "?a=1&b=2"
            rows.append((ident, ("", kind), href))
            pieces.append('<r id=' + quoteattr(ident) + ' kind=' + quoteattr(kind)
                          + ' href=' + quoteattr(href) + '>' + escape("body中<&") + "</r>")
        pieces.append("</feed>")
        data = "".join(pieces).encode()
        self.assertEqual(list(read_flat(chunks(data, 11))), rows)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flat-archive-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "archive.sqlite"
        self.archive = Archive(self.path)
        self.addCleanup(self.archive.close)

    def seed(self):
        self.archive.ingest([xml_record("old")])
        with self.archive.transaction() as batch:
            batch.put_attachment("old-note", [b"keep", b"\x00unchanged"])

    def assert_seed(self):
        self.assertEqual(self.archive.ids(), ("old",))
        self.assertEqual(self.archive.row("old"), (("", "Sale"), "https://e.test/a"))
        with self.archive.attachment("old-note") as reader:
            self.assertEqual(reader.read(), b"keep\x00unchanged")
        self.assertEqual(self.archive.attachment_info("old-note"),
                         (14, hashlib.sha256(b"keep\x00unchanged").hexdigest()))

    def reopen(self):
        self.archive.close()
        self.archive = Archive(self.path)
        self.addCleanup(self.archive.close)

    def test_persistence_and_missing_names(self):
        self.seed()
        self.reopen()
        self.assert_seed()
        with self.assertRaises(KeyError):
            self.archive.row("missing")
        with self.assertRaises(KeyError):
            self.archive.attachment("missing")
        with self.assertRaises(KeyError):
            self.archive.attachment_info("missing")

    def test_empty_import_and_attachment(self):
        self.assertEqual(self.archive.ingest([b"", b"<feed/>"]), 0)
        with self.archive.transaction() as batch:
            batch.put_attachment("empty", [b"", b""])
        self.reopen()
        with self.archive.attachment("empty") as reader:
            self.assertEqual(reader.read(), b"")
        self.assertEqual(self.archive.attachment_info("empty"),
                         (0, hashlib.sha256(b"").hexdigest()))

    def test_late_truncation_and_identity_conflicts_rollback(self):
        self.seed()
        data = b'<feed><r id="new" kind="T" href="urn:x"/>'
        duplicate = b'<feed><r id="new" kind="T" href="urn:x"/><r id="new" kind="T" href="urn:y"/></feed>'
        existing = b'<feed><r id="new" kind="T" href="urn:x"/><r id="old" kind="T" href="urn:y"/></feed>'
        for document in (data, duplicate, existing):
            with self.assertRaises(ValueError):
                self.archive.ingest(chunks(document, 5))
            self.assert_seed()
        self.reopen()
        self.assert_seed()

    def test_source_exceptions_propagate_by_identity(self):
        self.seed()
        for error in (OSError("source failed"), ValueError("source failed"),
                      sqlite3.IntegrityError("source failed")):
            def source():
                yield b'<feed><r id="new" kind="T" href="urn:x"/>'
                raise error
            with self.assertRaises(type(error)) as caught:
                self.archive.ingest(source())
            self.assertIs(caught.exception, error)
            self.assert_seed()
        self.reopen()
        self.assert_seed()

    def test_joint_row_and_attachment_rollback(self):
        self.seed()
        error = OSError("late attachment source error")

        def source():
            yield b"unpublished"
            raise error

        with self.assertRaises(OSError) as caught:
            with self.archive.transaction() as batch:
                batch.add_row("new", ("", "T"), "urn:x")
                batch.put_attachment("new-note", source())
        self.assertIs(caught.exception, error)
        self.assert_seed()
        with self.assertRaises(KeyError):
            self.archive.attachment("new-note")
        self.reopen()
        self.assert_seed()

    def test_caught_write_error_still_aborts_batch(self):
        self.seed()
        with self.assertRaises(ValueError):
            with self.archive.transaction() as batch:
                batch.add_row("new", ("", "T"), "urn:x")
                try:
                    batch.put_attachment("bad", [b"partial", "not bytes"])
                except ValueError:
                    pass
        self.assert_seed()
        with self.assertRaises(KeyError):
            self.archive.attachment_info("bad")
        with self.archive.transaction() as batch:
            batch.put_attachment("bad", [b"now valid"])

    def test_duplicate_attachment_cannot_replace_existing(self):
        self.seed()
        with self.assertRaises(ValueError):
            with self.archive.transaction() as batch:
                batch.add_row("new", ("", "T"), "urn:x")
                batch.put_attachment("old-note", [b"replacement"])
        self.assert_seed()

    def test_invalid_row_and_blob_inputs_are_atomic(self):
        invalid = [
            ("", ("", "T"), "urn:x"),
            ("new", "T", "urn:x"),
            ("new", ("", ""), "urn:x"),
            ("new", ("", "T"), "/relative"),
        ]
        for args in invalid:
            with self.assertRaises(ValueError):
                with self.archive.transaction() as batch:
                    batch.add_row("valid", ("", "T"), "urn:v")
                    batch.add_row(*args)
            self.assertEqual(self.archive.ids(), ())
        with self.assertRaises(ValueError):
            with self.archive.transaction() as batch:
                batch.put_attachment("large-chunk", [b"x" * (MAX_CHUNK + 1)])
        with self.assertRaises(KeyError):
            self.archive.attachment_info("large-chunk")

    def test_batch_lifecycle_and_query_boundary(self):
        with self.archive.transaction() as batch:
            batch.add_row("a", ("urn:manual", "T"), "urn:a")
            with self.assertRaises(RuntimeError):
                self.archive.row("a")
            with self.assertRaises(RuntimeError):
                with self.archive.transaction():
                    pass
            with self.assertRaises(RuntimeError):
                self.archive.close()
        self.assertEqual(self.archive.row("a"), (("urn:manual", "T"), "urn:a"))
        with self.assertRaises(RuntimeError):
            batch.add_row("b", ("", "T"), "urn:b")
        self.archive.close()
        self.archive.close()
        with self.assertRaises(RuntimeError):
            self.archive.ids()

    def test_readers_chunk_boundaries_and_archive_close(self):
        payload = bytes(range(256)) * 600
        with self.archive.transaction() as batch:
            batch.put_attachment("binary", chunks(payload, 101))
        first = self.archive.attachment("binary")
        second = self.archive.attachment("binary")
        self.assertEqual(first.read(7), payload[:7])
        target = bytearray(70000)
        self.assertEqual(first.readinto(target), len(target))
        self.assertEqual(target, payload[7:70007])
        self.assertEqual(second.read(), payload)
        with self.assertRaises(io.UnsupportedOperation):
            first.seek(0)
        first.close()
        first.close()
        self.assertTrue(first.closed)
        self.archive.close()
        self.assertTrue(second.closed)
        with self.assertRaises(ValueError):
            second.read(1)

    def test_reader_survives_successful_and_failed_append(self):
        self.seed()
        with self.archive.attachment("old-note") as reader:
            self.assertEqual(reader.read(2), b"ke")
            self.archive.ingest([xml_record("new")])
            with self.assertRaises(ValueError):
                self.archive.ingest([b"<feed>"])
            self.assertEqual(reader.read(), b"ep\x00unchanged")

    def test_real_version_one_fixture(self):
        self.archive.close()
        fixture_path = Path(self.temp.name) / "fixture.sqlite"
        with sqlite3.connect(fixture_path) as connection:
            connection.executescript((ROOT / "samples" / "archive-v1.sql").read_text())
        with Archive(fixture_path) as archive:
            self.assertEqual(archive.ids(), ("historical-1",))
            self.assertEqual(archive.row("historical-1"),
                             (("", "Sale"), "https://vendor.example/archive/1"))
            with archive.attachment("supplier-note") as reader:
                self.assertEqual(reader.read(), b"vendor-note\n")
            self.assertEqual(archive.attachment_info("supplier-note"),
                             (12, hashlib.sha256(b"vendor-note\n").hexdigest()))
            self.assertEqual(archive.ingest([xml_record("additional")]), 1)

    def test_streaming_large_body_and_attachment(self):
        block = b"x" * MAX_CHUNK
        repeats = 128
        total = repeats * len(block)

        def document():
            yield b'<feed><r id="big" kind="T" href="urn:big">'
            for _ in range(repeats):
                yield block
            yield b"</r></feed>"

        def binary():
            for _ in range(repeats):
                yield block

        tracemalloc.start()
        try:
            self.assertEqual(self.archive.ingest(document()), 1)
            with self.archive.transaction() as batch:
                batch.put_attachment("big", binary())
            digest = hashlib.sha256()
            read_size = 0
            with self.archive.attachment("big") as reader:
                while piece := reader.read(8191):
                    digest.update(piece)
                    read_size += len(piece)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        expected = hashlib.sha256()
        for _ in range(repeats):
            expected.update(block)
        self.assertEqual(read_size, total)
        self.assertEqual(self.archive.attachment_info("big"), (total, expected.hexdigest()))
        self.assertEqual(digest.hexdigest(), expected.hexdigest())
        self.assertLess(peak, 4 * 1024 * 1024)
        print("streaming probe: bytes=", total, "python_peak=", peak)


if __name__ == "__main__":
    unittest.main()
