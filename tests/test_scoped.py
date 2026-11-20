"""Scoped import and independent evidence regression suite.

The reference oracle in this module is intentionally written against a
different parser (pyexpat in namespace mode) than the production streaming
tokenizer in scoped.py.  It independently explains both the source document
and every replayed evidence document and reconciles the results against the
persistent rows.
"""
import io
from pathlib import Path
import sqlite3
import tempfile
import tracemalloc
import unittest
from urllib.parse import urljoin
from xml.parsers import expat

from archive import Archive, MAX_CHUNK
from scoped import is_ncname

ROOT = Path(__file__).resolve().parents[1]
XML_NS = "http://www.w3.org/XML/1998/namespace"


def chunks(data, size):
    for offset in range(0, len(data), size):
        yield data[offset:offset + size]


# --------------------------------------------------------------------------
# Independent reference oracle
# --------------------------------------------------------------------------

class _RefFrame:
    def __init__(self, name, parent):
        self.name = name                     # (uri, local)
        self.parent = parent
        self.prefixes = dict(parent.prefixes) if parent else {"xml": XML_NS}
        self.default = parent.default if parent else ""
        self.base = parent.base if parent else None
        self.record_meta = None              # (id, (uri, local), href)
        self.canon = ["E", name, []]         # [tag, attrs(list), nodes...]
        self.children = self.canon


class ReferenceOracle:
    """Full-memory, independent explanation of one XML 1.0 document."""

    def __init__(self, data, record_name, base):
        self._record_name = record_name
        self._base = base
        self._stack = []
        self._records = []                   # (meta, root canon)
        self._root_nodes = []
        self._parser = expat.ParserCreate(namespace_separator="|")
        self._parser.StartElementHandler = self._start
        self._parser.EndElementHandler = self._end
        self._parser.CharacterDataHandler = self._text
        self._parser.CommentHandler = self._comment
        self._parser.ProcessingInstructionHandler = self._pi
        self._parser.StartNamespaceDeclHandler = self._ns_start
        self._parser.EndNamespaceDeclHandler = lambda prefix: None
        self._pending_decls = []
        self._parser.StartCdataSectionHandler = self._cdata_start
        self._parser.EndCdataSectionHandler = self._cdata_end
        self._in_cdata = False
        self._parser.Parse(data, True)
        if self._stack or len(self._root_nodes) != 1:
            raise ValueError("reference: not a single-root document")

    def _roots(self):
        return getattr(self, "_root_nodes", [])

    def _ns_start(self, prefix, uri):
        # expat reports all in-scope declarations before the element start.
        self._pending_decls.append((prefix or "", uri))

    def _top_canon(self):
        return self._stack[-1].canon if self._stack else self._root_nodes

    def _start(self, name, attrs):
        if "|" in name:
            uri, local = name.rsplit("|", 1)
        else:
            uri, local = "", name
        parent = self._stack[-1] if self._stack else None
        frame = _RefFrame(
            (uri, local), parent if parent else None)
        frame.base = parent.base if parent else self._base
        canon_attrs = []
        business = {}
        for key, value in attrs.items():
            if "|" in key:
                attr_uri, attr_local = key.rsplit("|", 1)
            else:
                attr_uri, attr_local = "", key
            canon_attrs.append(((attr_uri, attr_local), value))
            business[(attr_uri, attr_local)] = value
            if attr_uri == XML_NS and attr_local == "base":
                frame.base = urljoin(frame.base, value)
        frame.canon[2] = canon_attrs
        if parent is None:
            self._root_nodes.append(frame.canon)
        else:
            parent.canon.append(frame.canon)
        # Namespace declarations arrive via StartNamespaceDeclHandler and
        # must be applied before QName kind resolution below.
        for prefix, ns_uri in self._pending_decls:
            frame.prefixes[prefix] = ns_uri
            if prefix == "":
                frame.default = ns_uri
        self._pending_decls = []
        if (uri, local) == self._record_name:
            ident = business.get(("", "id"))
            kind = business.get(("", "kind"))
            href = business.get(("", "href"))
            if ident is None or kind is None or href is None:
                raise ValueError("reference: record missing id/kind/href")
            kind_uri, kind_local = self._resolve_qname(kind, frame)
            absolute = urljoin(frame.base, href)
            frame.record_meta = (ident, (kind_uri, kind_local), absolute)
        self._stack.append(frame)

    def _end(self, name):
        frame = self._stack.pop()
        if frame.record_meta is not None:
            self._records.append((frame.record_meta, frame.canon))

    def _text(self, value):
        self._top_canon().append(["C" if self._in_cdata else "T", value])

    def _cdata_start(self):
        self._in_cdata = True

    def _cdata_end(self):
        self._in_cdata = False

    def _comment(self, value):
        self._top_canon().append(["M", "comment", value])

    def _pi(self, target, data):
        self._top_canon().append(["M", "pi", target, data])

    def _resolve_qname(self, text, frame):
        if ":" in text:
            prefix, local = text.split(":", 1)
            if prefix not in frame.prefixes:
                raise ValueError("reference: unbound prefix " + prefix)
            return frame.prefixes[prefix], local
        return frame.default or "", text

    def records(self):
        return list(self._records)

    def canon_for(self, ident):
        for meta, canon in self._records:
            if meta[0] == ident:
                return canon
        raise KeyError(ident)

    def rows(self):
        return [(meta[0], meta[1], meta[2]) for meta, _ in self._records]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def canonical(node):
    """Return a structure safe for deep equality comparisons.

    CDATA sections may be serialized as ordinary character data, so the two
    text markers are treated as equivalent; adjacent text runs (an artifact
    of chunked emission) are coalesced.  The evidence root's carrier xml:base
    is an explicitly permitted scope-closure adjustment (SCOPED.md), so it is
    excluded from the comparison.
    """
    if not isinstance(node, list):
        return node
    head = "T" if node and node[0] in ("T", "C") else node[0]
    if head == "E" and len(node) >= 3 and isinstance(node[2], list):
        attrs = tuple(item for item in node[2]
                      if item[0] != (XML_NS, "base"))
        children = _coalesce_text([canonical(item) for item in node[3:]])
        return (head, canonical(node[1]),
                tuple(canonical(item) for item in attrs)) + tuple(children)
    return (head,) + tuple(canonical(item) for item in node[1:])


def _coalesce_text(items):
    merged = []
    for item in items:
        if (item and item[0] == "T" and merged and merged[-1][0] == "T"):
            merged[-1] = ("T", merged[-1][1] + item[1])
        else:
            merged.append(item)
    return merged


def make_doc(body, decl=b''):
    return decl + b'<feed xmlns="urn:feed" xmlns:r="urn:record" ' \
                  b'xmlns:k="urn:outer" xml:base="daily/">' + body + b'</feed>'


RECORD = ("urn:record", "item")
BASE = "https://e.test/root/"


class ScopedImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scoped-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "archive.sqlite"
        self.archive = Archive(self.path)
        self.addCleanup(self.archive.close)

    def import_data(self, data, record_name=RECORD, base=BASE, size=13):
        return self.archive.ingest_scoped(chunks(data, size), record_name, base)

    def test_provided_nested_sample_matches_contract(self):
        data = (ROOT / "samples" / "scoped.xml").read_bytes()
        count = self.import_data(data)
        self.assertEqual(count, 2)
        self.assertEqual(self.archive.ids(), ("inner", "outer"))
        self.assertEqual(
            self.archive.row("outer"),
            (("urn:outer", "Batch"), "https://e.test/root/daily/a.xml"))
        self.assertEqual(
            self.archive.row("inner"),
            (("urn:inner", "Part"), "https://e.test/root/detail/b.xml"))
        for ident in ("inner", "outer"):
            with self.archive.evidence(ident) as reader:
                payload = reader.read()
            # Every evidence is an independently parseable XML document.
            expat.ParserCreate().Parse(payload, True)

    def test_every_split_point_of_sample(self):
        data = (ROOT / "samples" / "scoped.xml").read_bytes()
        expected = {
            "outer": (("urn:outer", "Batch"),
                      "https://e.test/root/daily/a.xml"),
            "inner": (("urn:inner", "Part"),
                      "https://e.test/root/detail/b.xml"),
        }
        for size in (1, 2, 3, 5, 7, 31, 64, 257, MAX_CHUNK):
            with Archive(Path(self.temp.name) / ("s%d.sqlite" % size)) as arc:
                self.assertEqual(arc.ingest_scoped(chunks(data, size),
                                                   RECORD, BASE), 2)
                self.assertEqual(dict((i, arc.row(i)) for i in arc.ids()),
                                 expected)

    def test_default_namespace_cancel_and_local_rebind(self):
        body = (b'<r:item id="a" kind="k:T" href="a">'
                b'<child xmlns="" k="literal" xmlns:k="urn:rebound">'
                b'<r:item id="b" kind="k:Local" href="b"/>'
                b'</child></r:item>')
        self.assertEqual(self.import_data(make_doc(body)), 2)
        self.assertEqual(self.archive.row("a"),
                         (("urn:outer", "T"), "https://e.test/root/daily/a"))
        self.assertEqual(self.archive.row("b"),
                         (("urn:rebound", "Local"),
                          "https://e.test/root/daily/b"))
        oracle = ReferenceOracle(make_doc(body), RECORD, BASE)
        self._reconcile(oracle)

    def test_qname_through_character_reference(self):
        # 'k:Part' split by a numeric character reference inside kind.
        body = b'<r:item id="q" kind="k:Par&#116;" href="a?x=1&amp;y=2"/>'
        self.assertEqual(self.import_data(make_doc(body)), 1)
        self.assertEqual(self.archive.row("q"),
                         (("urn:outer", "Part"),
                          "https://e.test/root/daily/a?x=1&y=2"))

    def test_empty_elements_mixed_text_and_cross_chunk_utf8(self):
        body = ("<r:item id=\"m\" kind=\"k:Mix\" href=\"m/\">"
                "te<b/>xt<empty></empty>中&#x42;<![CDATA[<z>&]]></r:item>").encode()
        for size in (1, 2, 4, 9, MAX_CHUNK):
            path = Path(self.temp.name) / ("m%d.sqlite" % size)
            with Archive(path) as arc:
                self.assertEqual(
                    arc.ingest_scoped(chunks(make_doc(body), size),
                                      RECORD, BASE), 1)
                self.assertEqual(
                    arc.row("m"),
                    (("urn:outer", "Mix"), "https://e.test/root/daily/m/"))

    def test_root_element_can_be_a_record(self):
        data = ('<r:item xmlns:r="urn:record" xmlns:k="urn:outer" '
                'id="root" kind="k:R" href="https://e.test/x"/>').encode()
        self.assertEqual(self.import_data(data), 1)
        self.assertEqual(self.archive.row("root"),
                         (("urn:outer", "R"), "https://e.test/x"))
        with self.archive.evidence("root") as reader:
            expat.ParserCreate().Parse(reader.read(), True)

    def test_no_selected_records_publishes_nothing_but_validates(self):
        good = b'<feed xmlns="urn:feed"><other/></feed>'
        self.assertEqual(self.import_data(good, RECORD, BASE), 0)
        self.assertEqual(self.archive.ids(), ())
        with self.assertRaises(ValueError):
            self.import_data(b'<feed xmlns="urn:feed"><other>', RECORD, BASE)

    def test_empty_href_means_current_base(self):
        body = b'<r:item id="e" kind="k:T" href=""/>'
        self.assertEqual(self.import_data(make_doc(body)), 1)
        self.assertEqual(self.archive.row("e")[1],
                         "https://e.test/root/daily/")

    def test_kind_without_prefix_uses_default_namespace(self):
        body = b'<r:item id="d" kind="Plain" href="d"/>'
        self.assertEqual(self.import_data(make_doc(body)), 1)
        self.assertEqual(self.archive.row("d"),
                         (("urn:feed", "Plain"), "https://e.test/root/daily/d"))
        body_cancel = (b'<r:item xmlns="" id="z" kind="Bare" '
                       b'href="z"/>')
        self.assertEqual(self.import_data(make_doc(body_cancel)), 1)
        self.assertEqual(self.archive.row("z"),
                         (("", "Bare"), "https://e.test/root/daily/z"))

    def test_attribute_text_is_normalized_but_not_stripped(self):
        body = b'<r:item id=" spaced " kind="k:T" href="a"/>'
        self.assertEqual(self.import_data(make_doc(body)), 1)
        self.assertIn(" spaced ", self.archive.ids())
        body_tab = ('<r:item id="a\tb" kind="k:T" href="a"/>').encode()
        with Archive(Path(self.temp.name) / "tab.sqlite") as arc:
            arc.ingest_scoped(chunks(make_doc(body_tab), 3), RECORD, BASE)
            self.assertEqual(arc.ids(), ("a b",))

    def test_parameter_validation(self):
        good = b'<feed xmlns="urn:feed"/>'
        for bad_name in (("urn:record", "bad:name"), ("urn:record", ""),
                         ("urn:record", 7), "item", (1, "item")):
            with self.assertRaises(ValueError):
                self.archive.ingest_scoped(chunks(good, 4), bad_name, BASE)
        for bad_base in ("relative/path", "ftp://e.test/x",
                         "https://e.test/a b", "", "https://", 7):
            with self.assertRaises(ValueError):
                self.archive.ingest_scoped(chunks(good, 4), RECORD, bad_base)
        for source in ([bytearray(good)], [good.decode()],
                       [b"x" * (MAX_CHUNK + 1)]):
            with self.assertRaises(ValueError):
                self.archive.ingest_scoped(source, RECORD, BASE)
        self.assertTrue(is_ncname("item"))

    def test_xml_lang_and_space_are_rejected(self):
        for attr in ("xml:lang", "xml:space"):
            data = ('<feed xmlns="urn:feed" %s="en"/>' % attr).encode()
            with self.assertRaises(ValueError):
                self.import_data(data)

    def test_dtd_illegal_xml_and_unbound_prefixes_rejected(self):
        bad = [
            b"", b"<feed>", b"<feed/><feed/>",
            b"<!DOCTYPE feed><feed/>",
            b'<!DOCTYPE feed [<!ENTITY x "v">]><feed/>',
            b'<feed xmlns="urn:feed"><a ::="1"/></feed>',
            b'<feed xmlns="urn:feed"><a></b></feed>',
            b'<feed xmlns="urn:feed">&undeclared;</feed>',
            b'<feed xmlns="urn:feed"><![CDATA[',
            b'<feed xmlns="urn:feed"><!-- not closed',
            b'<feed xmlns="urn:feed"><?pi not closed',
            b'<feed xmlns="urn:feed"><!--a--b--></feed>',
            b'<feed xmlns="urn:feed">&#0;</feed>',
            b'<feed xmlns="urn:feed">&#xD800;</feed>',
            b'<feed xmlns="urn:feed">\xff</feed>',
            b'<?xml version="1.1"?><feed xmlns="urn:feed"/>',
            b'<?xml version="1.0" encoding="ISO-8859-1"?><feed/>',
            b'<feed xmlns="urn:feed"><a p:x="1"/></feed>',
            b'<feed xmlns="urn:feed"><a b="1" b="2"/></feed>',
            b'<feed xmlns="urn:feed"><a/></feed><sibling/>',
        ]
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    self.import_data(data, size=1)

    def test_text_outside_root_and_cdata_brackets_rejected(self):
        bad = [
            b"junk<feed xmlns='urn:feed'/>",
            b"<feed xmlns='urn:feed'/>trail",
            b"<feed xmlns='urn:feed'>a]]>b</feed>",
            b"<feed xmlns='urn:feed'>ab]]></feed>",
            b'<feed xmlns="urn:feed" xmlns:xml="urn:bad"/>',
            b'<feed xmlns="urn:feed" xmlns:p=""/>',
            b"<feed xmlns='urn:feed'/>x<feed xmlns='urn:feed'/>",
        ]
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    self.import_data(data, size=1)
        # Whitespace in the prolog and epilog is legal XML.
        for data in (b"  \n<feed xmlns='urn:feed'/>",
                     b"<?xml version='1.0'?>\n<feed xmlns='urn:feed'/>\t\n"):
            self.assertEqual(self.import_data(data, size=1), 0)

    def test_cdata_may_contain_bracket_sequence(self):
        # ']]' sequences inside a CDATA section are legal character data;
        # production rewrites CDATA as escaped character data in evidence.
        with_cdata = (
            b'<feed xmlns:r="urn:record" xmlns:k="urn:outer">'
            b'<r:item id="c" kind="k:T" href="c">'
            b"<![CDATA[x]]y]]z]]></r:item></feed>")
        # Split every byte to exercise the CDATA terminator carry handling.
        self.assertEqual(self.import_data(with_cdata, size=1), 1)
        with self.archive.evidence("c") as reader:
            evidence = reader.read()
        expat.ParserCreate().Parse(evidence, True)
        self.assertIn(b"x]]y]]z", evidence)

    def test_record_requires_its_three_attributes(self):
        for body in (b'<r:item id="x" kind="k:T"/>',
                     b'<r:item kind="k:T" href="a"/>',
                     b'<r:item id="" kind="k:T" href="a"/>',
                     b'<r:item id="x" kind=":T" href="a"/>',
                     b'<r:item id="x" kind="k:T" href="a b"/>'):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    self.import_data(make_doc(body))

    def _reconcile(self, oracle, replay_base="https://replay.example/elsewhere/"):
        source_rows = {ident: (kind, href)
                       for ident, kind, href in oracle.rows()}
        for ident, kind, href in oracle.rows():
            self.assertEqual(self.archive.row(ident), (kind, href))
            with self.archive.evidence(ident) as reader:
                evidence_bytes = reader.read()
            replayed = ReferenceOracle(evidence_bytes, RECORD, replay_base)
            # The replayed evidence must explain the same record without any
            # help from the original source base.
            rows = {meta[0]: (meta[1], meta[2])
                    for meta, _ in replayed.records()}
            self.assertIn(ident, rows)
            self.assertEqual(rows[ident], source_rows[ident])
            self.assertEqual(canonical(replayed.canon_for(ident)),
                             canonical(oracle.canon_for(ident)))


class ScopedReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scoped-replay-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "archive.sqlite"

    def test_independent_replay_under_foreign_bases(self):
        body = (
            b'<r:item id="outer" kind="k:Batch" href="a.xml">'
            b"before text\n"
            b'<r:item xmlns:k="urn:inner" xml:base="../detail/" '
            b'id="inner" kind="k:Part" href="b.xml">'
            b"<![CDATA[<raw>&]]>&#20013;"
            b'<plain xmlns="" note="k:literal"/></r:item>'
            b"after</r:item>")
        data = make_doc(body)
        with Archive(self.path) as archive:
            self.assertEqual(
                archive.ingest_scoped(chunks(data, 17), RECORD, BASE), 2)
            oracle = ReferenceOracle(data, RECORD, BASE)
            expected = {ident: (kind, href)
                        for ident, kind, href in oracle.rows()}
            for replay_base in ("https://replay.test/other/place/",
                                "http://mirror.test/",
                                "https://x.test/deep/nested/dir/"):
                for ident in archive.ids():
                    with archive.evidence(ident) as reader:
                        evidence = reader.read()
                    replayed = ReferenceOracle(evidence, RECORD, replay_base)
                    rows = {meta[0]: (meta[1], meta[2])
                            for meta, _ in replayed.records()}
                    self.assertEqual(rows[ident], expected[ident])
                    self.assertEqual(
                        canonical(replayed.canon_for(ident)),
                        canonical(oracle.canon_for(ident)))

    def test_evidence_keyspace_is_separate_from_attachments(self):
        with Archive(self.path) as archive:
            with archive.transaction() as batch:
                batch.add_row("manual", ("", "T"), "https://e.test/m")
                batch.put_attachment("manual", [b"caller-owned"])
            # Old manual row has no evidence and none is fabricated.
            with self.assertRaises(KeyError):
                archive.evidence("manual")
            body = b'<r:item id="manual2" kind="k:T" href="m2"/>'
            archive.ingest_scoped(chunks(make_doc(body), 5), RECORD, BASE)
            with archive.evidence("manual2") as reader:
                evidence = reader.read()
            self.assertIn(b"<r:item", evidence)
            # The caller's same-named attachment is untouched.
            with archive.attachment("manual") as reader:
                self.assertEqual(reader.read(), b"caller-owned")
            with self.assertRaises(KeyError):
                archive.attachment("manual2")

    def test_reader_lifecycle_interleave_and_reopen(self):
        body = b'<r:item id="a" kind="k:T" href="a">x</r:item>'
        archive = Archive(self.path)
        archive.ingest_scoped(chunks(make_doc(body), 3), RECORD, BASE)
        first = archive.evidence("a")
        second = archive.evidence("a")
        self.assertEqual(first.read(5), b"<?xml")
        target = bytearray(10)
        self.assertEqual(first.readinto(target), 10)
        self.assertTrue(second.read())
        with self.assertRaises(io.UnsupportedOperation):
            first.seek(0)
        archive.close()
        self.assertTrue(first.closed and second.closed)
        with self.assertRaises(ValueError):
            first.read(1)
        with Archive(self.path) as reopened:
            with reopened.evidence("a") as reader:
                payload = reader.read()
            expat.ParserCreate().Parse(payload, True)
            with self.assertRaises(KeyError):
                reopened.evidence("missing")

    def test_evidence_survives_rollback_of_later_import(self):
        with Archive(self.path) as archive:
            archive.ingest_scoped(
                chunks(make_doc(b'<r:item id="keep" kind="k:T" href="k"/>'), 4),
                RECORD, BASE)
            with archive.evidence("keep") as reader:
                good = reader.read()
            before = archive.ids()
            with self.assertRaises(ValueError):
                archive.ingest_scoped(
                    [b'<feed xmlns="urn:feed"><r:item id="nope" '
                     b'kind="k:T" href="n">'], RECORD, BASE)
            self.assertEqual(archive.ids(), before)
            with archive.evidence("keep") as reader:
                self.assertEqual(reader.read(), good)


class ScopedAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scoped-atomic-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "archive.sqlite"
        self.archive = Archive(self.path)
        self.addCleanup(self.archive.close)

    def seed(self):
        self.archive.ingest_scoped(
            chunks(make_doc(b'<r:item id="old" kind="k:T" href="old"/>'), 6),
            RECORD, BASE)
        with self.archive.transaction() as batch:
            batch.put_attachment("old-note", [b"kept"])

    def assert_seed(self):
        self.assertEqual(self.archive.ids(), ("old",))
        with self.archive.attachment("old-note") as reader:
            self.assertEqual(reader.read(), b"kept")
        with self.archive.evidence("old") as reader:
            self.assertTrue(reader.read())

    def test_late_truncation_rolls_back_everything(self):
        self.seed()
        good_prefix = make_doc(b'<r:item id="new" kind="k:T" href="n"/>')[:-1]
        with self.assertRaises(ValueError):
            self.archive.ingest_scoped(chunks(good_prefix, 5), RECORD, BASE)
        self.assert_seed()
        with self.assertRaises(KeyError):
            self.archive.evidence("new")

    def test_duplicate_ids_inside_and_across_inputs(self):
        self.seed()
        dup = make_doc(
            b'<r:item id="new" kind="k:T" href="n"/>'
            b'<r:item id="new" kind="k:U" href="o"/>')
        with self.assertRaises(ValueError):
            self.archive.ingest_scoped(chunks(dup, 7), RECORD, BASE)
        clash = make_doc(b'<r:item id="old" kind="k:T" href="x"/>')
        with self.assertRaises(ValueError):
            self.archive.ingest_scoped(chunks(clash, 7), RECORD, BASE)
        self.assert_seed()

    def test_source_exception_identity_is_preserved(self):
        self.seed()
        for error in (OSError("boom"), ValueError("boom"),
                      sqlite3.IntegrityError("boom")):
            def source():
                yield make_doc(b'<r:item id="x" kind="k:T" href="x"/>')[:20]
                raise error
            with self.assertRaises(type(error)) as caught:
                self.archive.ingest_scoped(source(), RECORD, BASE)
            self.assertIs(caught.exception, error)
            self.assert_seed()

    def test_staging_is_cleaned_up(self):
        parent = Path(self.temp.name)
        before = set(parent.iterdir())
        self.archive.ingest_scoped(
            chunks(make_doc(b'<r:item id="c" kind="k:T" href="c"/>'), 4),
            RECORD, BASE)
        after = set(parent.iterdir())
        leftovers = [p for p in after - before if p.name.startswith("scoped-")]
        self.assertEqual(leftovers, [])
        with self.assertRaises(ValueError):
            self.archive.ingest_scoped(
                [b'<feed xmlns="urn:feed">unclosed'], RECORD, BASE)
        self.assertEqual(
            [p for p in parent.iterdir()
             if p.name.startswith("scoped-evidence-")], [])

    def test_reopen_fidelity_alongside_flat_import(self):
        flat = (b"<feed><r id='flat' kind='Sale' "
                b"href='https://e.test/f'>body</r></feed>")
        self.archive.ingest([flat])
        self.archive.ingest_scoped(
            chunks(make_doc(b'<r:item id="scoped" kind="k:T" href="s"/>'), 5),
            RECORD, BASE)
        self.archive.close()
        with Archive(self.path) as reopened:
            self.assertEqual(reopened.ids(), ("flat", "scoped"))
            self.assertEqual(reopened.row("flat"),
                             (("", "Sale"), "https://e.test/f"))
            with self.assertRaises(KeyError):
                reopened.evidence("flat")
            with reopened.evidence("scoped") as reader:
                expat.ParserCreate().Parse(reader.read(), True)


class ScopedResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scoped-res-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "archive.sqlite"

    def _run(self, source, label):
        tracemalloc.start()
        try:
            with Archive(self.path) as archive:
                count = archive.ingest_scoped(source(), RECORD, BASE)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        print("%s: records=%d python_peak=%d" % (label, count, peak))
        return count, peak

    def test_growing_text_comment_and_pi_length(self):
        block = b"x" * MAX_CHUNK
        peaks = []
        for repeats in (8, 32, 128):
            path = Path(self.temp.name) / ("text%d.sqlite" % repeats)

            def source(repeats=repeats):
                yield b'<feed xmlns="urn:feed" xmlns:r="urn:record" ' \
                      b'xmlns:k="urn:outer">'
                yield b'<r:item id="big" kind="k:T" href="big">'
                for _ in range(repeats):
                    yield block
                yield b"<!--"
                for _ in range(repeats):
                    yield block
                yield b"--><?app "
                for _ in range(repeats):
                    yield block
                yield b"?><![CDATA["
                for _ in range(repeats):
                    yield block
                yield b"]]></r:item></feed>"
            self.path = path
            count, peak = self._run(source, "text/comment/pi x%d" % repeats)
            self.assertEqual(count, 1)
            self.assertLess(peak, 6 * 1024 * 1024)
            peaks.append(peak)
        # Peak must not scale with the body length.
        self.assertLess(max(peaks) * 2, min(peaks) * 4 + 2 * 1024 * 1024)

    def test_growing_nesting_depth(self):
        peaks = []
        for depth in (64, 256, 1024):
            path = Path(self.temp.name) / ("depth%d.sqlite" % depth)

            def source(depth=depth):
                yield b'<feed xmlns="urn:feed" xmlns:r="urn:record" ' \
                      b'xmlns:k="urn:outer">'
                for level in range(depth):
                    yield ("<leaf%d>" % level).encode()
                yield b'<r:item id="deep" kind="k:T" href="d"/>'
                for level in range(depth - 1, -1, -1):
                    yield ("</leaf%d>" % level).encode()
                yield b"</feed>"
            self.path = path
            count, peak = self._run(source, "depth%d" % depth)
            self.assertEqual(count, 1)
            peaks.append(peak)
        self.assertLess(max(peaks), 8 * 1024 * 1024)

    def test_many_small_records_do_not_accumulate(self):
        total = 400

        def source():
            yield b'<feed xmlns="urn:feed" xmlns:r="urn:record" ' \
                  b'xmlns:k="urn:outer">'
            for index in range(total):
                yield ('<r:item id="id%04d" kind="k:T" href="u%d"/>'
                       % (index, index)).encode()
            yield b"</feed>"
        count, peak = self._run(source, "records%d" % total)
        self.assertEqual(count, total)
        self.assertLess(peak, 8 * 1024 * 1024)
        with Archive(self.path) as archive:
            self.assertEqual(len(archive.ids()), total)
            # Bounded read verification of every persisted evidence.
            for ident in archive.ids():
                with archive.evidence(ident) as reader:
                    size = 0
                    while piece := reader.read(4096):
                        size += len(piece)
                    self.assertGreater(size, 0)


if __name__ == "__main__":
    unittest.main()
