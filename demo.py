"""Run a real import, attachment transaction, rollback and reopen in a temp directory."""
import hashlib
from pathlib import Path
import tempfile
from archive import Archive, MAX_CHUNK

ROOT = Path(__file__).resolve().parent


def file_chunks(path):
    with path.open("rb") as source:
        while block := source.read(MAX_CHUNK):
            yield block


def main():
    with tempfile.TemporaryDirectory(prefix="flat-archive-demo-") as directory:
        path = Path(directory) / "archive.sqlite"
        with Archive(path) as archive:
            count = archive.ingest(file_chunks(ROOT / "samples" / "feed.xml"))
            with archive.transaction() as batch:
                batch.put_attachment("supplier-note", file_chunks(ROOT / "samples" / "note.txt"))
            print("imported:", count)
            for ident in archive.ids():
                print(ident, archive.row(ident))
            before = archive.ids()
            try:
                archive.ingest([b'<feed><r id="unpublished" kind="Sale" href="https://e.test/x"/>'])
            except ValueError:
                pass
            else:
                raise AssertionError("truncated document was accepted")
            assert archive.ids() == before
            print("truncated batch rolled back")
        with Archive(path) as reopened:
            with reopened.attachment("supplier-note") as reader:
                payload = reader.read()
            assert payload == (ROOT / "samples" / "note.txt").read_bytes()
            assert reopened.attachment_info("supplier-note") == (
                len(payload), hashlib.sha256(payload).hexdigest())
            assert reopened.ids() == before
            print("reopened rows:", len(before), "attachment:", payload.decode().rstrip())


if __name__ == "__main__":
    main()
