"""Resource boundary probe for scoped imports.

Grows generated text, comments, processing instructions and nested records
along two axes independently (content length and element depth), records the
input byte size, tracemalloc peak and the row/evidence reconciliation.  The
generator yields blocks without ever materializing the full input or output,
matching the SCOPED.md measurement rules.
"""
from pathlib import Path
import tempfile
import tracemalloc

from archive import Archive
from scoped import MAX_CHUNK

RECORD = ("urn:record", "item")
BASE = "https://e.test/root/"


def length_source(repeats):
    block = b"x" * MAX_CHUNK
    yield (b'<feed xmlns="urn:feed" xmlns:r="urn:record" '
           b'xmlns:k="urn:outer">')
    yield b'<r:item id="big" kind="k:Big" href="big">'
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


def depth_source(depth):
    yield (b'<feed xmlns="urn:feed" xmlns:r="urn:record" '
           b'xmlns:k="urn:outer">')
    for level in range(depth):
        yield ("<leaf%d>" % level).encode()
    yield b'<r:item id="deep" kind="k:Deep" href="deep"/>'
    for level in range(depth - 1, -1, -1):
        yield ("</leaf%d>" % level).encode()
    yield b"</feed>"


def count_source(total):
    yield (b'<feed xmlns="urn:feed" xmlns:r="urn:record" '
           b'xmlns:k="urn:outer">')
    for index in range(total):
        yield ('<r:item id="id%05d" kind="k:T" href="u%d"/>'
               % (index, index)).encode()
    yield b"</feed>"


def measure(path, source_factory, axis, value):
    tracemalloc.start()
    input_bytes = 0
    try:
        with Archive(path) as archive:
            def counting():
                nonlocal input_bytes
                for block in source_factory(value):
                    input_bytes += len(block)
                    yield block
            count = archive.ingest_scoped(counting(), RECORD, BASE)
            # Bounded-read reconciliation of every persisted evidence.
            checked = 0
            for ident in archive.ids():
                with archive.evidence(ident) as reader:
                    size = 0
                    digest_acc = 0
                    while piece := reader.read(8192):
                        size += len(piece)
                        digest_acc += len(piece)
                self_info = archive.evidence_info(ident)
                assert size == self_info[0]
                checked += 1
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print("%-8s value=%-5d input_bytes=%-9d records=%-4d "
          "evidence_checked=%-4d python_peak=%d"
          % (axis, value, input_bytes, count, checked, peak))
    return peak


def main():
    with tempfile.TemporaryDirectory(prefix="scoped-resource-probe-") as d:
        root = Path(d)
        print("== growing content length (fixed depth, one record) ==")
        length_peaks = []
        for repeats in (8, 32, 128):
            peak = measure(root / ("len%d.sqlite" % repeats),
                           length_source, "length", repeats)
            length_peaks.append(peak)
        print("== growing element depth (small bodies) ==")
        depth_peaks = []
        for depth in (64, 256, 1024):
            peak = measure(root / ("depth%d.sqlite" % depth),
                           depth_source, "depth", depth)
            depth_peaks.append(peak)
        print("== growing record count (small fixed records) ==")
        count_peaks = []
        for total in (100, 400):
            peak = measure(root / ("count%d.sqlite" % total),
                           count_source, "count", total)
            count_peaks.append(peak)
        length_growth = max(length_peaks) / min(length_peaks)
        print("length-axis peak ratio (16x content): %.2f" % length_growth)
        assert length_growth < 2.0, "working memory scaled with body length"
        print("OK: peak working memory stays bounded while input and "
              "evidence grow on disk")


if __name__ == "__main__":
    main()
