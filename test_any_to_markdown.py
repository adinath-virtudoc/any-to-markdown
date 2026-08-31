"""Tests for any_to_markdown.py's pdf-inspector integration.

All PDF fixtures are built at runtime by the minimal hand-rolled writer below
(no reportlab/fpdf/fitz available, and no committed binaries) — synthetic
placeholder text only, never a real document.

pdf-inspector's own page-level OCR classification is a real (Rust-backed)
library we don't control, so the branching/reassembly tests below monkeypatch
`any_to_markdown._PDF_INSPECTOR` with a small fake module that returns
hand-built PagesExtractionResult-shaped objects. That isolates what this
change is actually responsible for — convert_file's branching, index
conversion, and page reassembly — from pdf-inspector's own heuristics.
"""

import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import any_to_markdown as atm


def _esc(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _make_pdf(page_texts: list[str], page_size=(612, 792)) -> bytes:
    """Tiny hand-rolled PDF writer. Builds a syntactically valid multi-page PDF
    with real extractable text content streams and a correct xref table, so
    both pdf-inspector and MarkItDown can genuinely parse it.
    """
    w, h = page_size
    objects = {}
    next_id = 1

    def alloc():
        nonlocal next_id
        i = next_id
        next_id += 1
        return i

    font_id = alloc()
    objects[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    pages_id = alloc()
    content_ids = []
    page_ids = []
    for text in page_texts:
        lines = text.split("\n")
        stream_parts = [b"BT /F1 14 Tf 72 %d Td 16 TL" % (h - 72)]
        first = True
        for line in lines:
            if not first:
                stream_parts.append(b"T*")
            stream_parts.append(("(%s) Tj" % _esc(line)).encode("latin-1", "replace"))
            first = False
        stream_parts.append(b"ET")
        stream = b"\n".join(stream_parts)
        cid = alloc()
        objects[cid] = (
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )
        content_ids.append(cid)

        pid = alloc()
        page_ids.append(pid)
        objects[pid] = (
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, w, h, font_id, cid)
        )

    kids_str = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[pages_id] = (
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids_str.encode(), len(page_ids))
    )

    catalog_id = alloc()
    objects[catalog_id] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id

    max_id = next_id - 1
    out = bytearray()
    out += b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = [0] * (max_id + 1)
    for oid in range(1, max_id + 1):
        offsets[oid] = len(out)
        body = objects.get(oid, b"<< >>")
        out += b"%d 0 obj\n" % oid + body + b"\nendobj\n"

    xref_offset = len(out)
    out += b"xref\n0 %d\n" % (max_id + 1)
    out += b"0000000000 65535 f \n"
    for oid in range(1, max_id + 1):
        out += b"%010d 00000 n \n" % offsets[oid]
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\n" % (max_id + 1, catalog_id)
    out += b"startxref\n%d\n%%%%EOF" % xref_offset
    return bytes(out)


# Fakes shaped exactly like pdf_inspector's real types (verified via dir() on
# the live library) so the branching logic under test sees the same attribute
# surface it would in production.
@dataclass
class _FakePageMarkdown:
    page: int
    markdown: str
    needs_ocr: bool
    ocr_reason: str | None = None


@dataclass
class _FakePagesExtractionResult:
    pages: list = field(default_factory=list)
    pages_needing_ocr: list = field(default_factory=list)
    pages_with_tables: list = field(default_factory=list)
    pages_with_columns: list = field(default_factory=list)
    is_complex: bool = False
    ocr_reasons_by_page: dict = field(default_factory=dict)


class _FakePdfInspector:
    """Stand-in for the pdf_inspector module, wired to return a canned result
    (or raise a canned exception) regardless of what path it's given."""

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def extract_pages_markdown(self, path):
        if self._error is not None:
            raise self._error
        return self._result


class IndexHelperTests(unittest.TestCase):
    def test_round_trip(self):
        for n in range(0, 25):
            self.assertEqual(atm._pdf_page_to_pi_page(atm._pi_page_to_pdf_page(n)), n)

    def test_known_values(self):
        # PageMarkdown.page is 0-indexed; pdf2image/human pages are 1-indexed.
        self.assertEqual(atm._pi_page_to_pdf_page(0), 1)
        self.assertEqual(atm._pi_page_to_pdf_page(2), 3)
        self.assertEqual(atm._pdf_page_to_pi_page(1), 0)
        self.assertEqual(atm._pdf_page_to_pi_page(3), 2)


class ContiguousPageRunsTests(unittest.TestCase):
    def test_collapses_consecutive_pages(self):
        self.assertEqual(atm._contiguous_page_runs([2, 3, 4, 7, 8, 10]),
                          [(2, 4), (7, 8), (10, 10)])

    def test_empty(self):
        self.assertEqual(atm._contiguous_page_runs([]), [])


class ExtractPdfNativeGuardTests(unittest.TestCase):
    def test_zero_pages_returns_none(self):
        # Degenerate PDF: pdf-inspector parsed it but found no pages at all.
        # Must never be treated as "converted with empty content".
        fake = _FakePdfInspector(result=_FakePagesExtractionResult(pages=[]))
        with _patched_inspector(fake):
            self.assertIsNone(atm._extract_pdf_native(Path("unused.pdf")))

    def test_broken_pdf_raises_valueerror_falls_through(self):
        # Ground-truth landmine: broken/encrypted PDFs raise a plain ValueError
        # with a distinct message. _extract_pdf_native must swallow it.
        fake = _FakePdfInspector(error=ValueError("PDF is encrypted"))
        with _patched_inspector(fake):
            self.assertIsNone(atm._extract_pdf_native(Path("unused.pdf")))

    def test_overflow_error_falls_through(self):
        # Ground-truth landmine: a negative/out-of-range page index raises
        # OverflowError, a different type leaking from PyO3. Must be caught too.
        fake = _FakePdfInspector(error=OverflowError("bad page index"))
        with _patched_inspector(fake):
            self.assertIsNone(atm._extract_pdf_native(Path("unused.pdf")))

    def test_no_pdf_inspector_returns_none(self):
        with _patched_inspector(None):
            self.assertIsNone(atm._extract_pdf_native(Path("unused.pdf")))

    def test_partitions_native_and_ocr_pages(self):
        pages = [
            _FakePageMarkdown(page=0, markdown="# A", needs_ocr=False),
            _FakePageMarkdown(page=1, markdown="", needs_ocr=False),  # blank -> ocr
            _FakePageMarkdown(page=2, markdown="# C", needs_ocr=True),  # flagged -> ocr
        ]
        fake = _FakePdfInspector(result=_FakePagesExtractionResult(pages=pages))
        with _patched_inspector(fake):
            native, ocr, result = atm._extract_pdf_native(Path("unused.pdf"))
        self.assertEqual([p.page for p in native], [0])
        self.assertEqual([p.page for p in ocr], [1, 2])


def _patched_inspector(value):
    class _Ctx:
        def __enter__(self_ctx):
            self_ctx.old = atm._PDF_INSPECTOR
            atm._PDF_INSPECTOR = value
            return value

        def __exit__(self_ctx, *exc):
            atm._PDF_INSPECTOR = self_ctx.old
            return False

    return _Ctx()


class ConvertFileNativePathTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.src = self.tmp / "doc.pdf"
        self.src.write_bytes(_make_pdf(["placeholder page one", "placeholder page two"]))
        self.out_file = self.tmp / "doc.md"

    def test_all_native_pages_written_in_order(self):
        pages = [
            _FakePageMarkdown(page=0, markdown="# Heading One", needs_ocr=False),
            _FakePageMarkdown(page=1, markdown="# Heading Two", needs_ocr=False),
        ]
        fake = _FakePdfInspector(result=_FakePagesExtractionResult(pages=pages))
        with _patched_inspector(fake):
            status = atm.convert_file(self.src, self.out_file)
        self.assertEqual(status, "converted")
        self.assertEqual(
            self.out_file.read_text(encoding="utf-8"), "# Heading One\n\n# Heading Two"
        )

    def test_legacy_pdf_flag_skips_pdf_inspector_branch(self):
        # Even with pdf-inspector "available" via the fake, --legacy-pdf must
        # route straight to the old ladder untouched.
        fake = _FakePdfInspector(result=_FakePagesExtractionResult(
            pages=[_FakePageMarkdown(page=0, markdown="# Should not be used", needs_ocr=False)]
        ))
        with _patched_inspector(fake):
            status = atm.convert_file(self.src, self.out_file, legacy_pdf=True)
        self.assertEqual(status, "converted")
        text = self.out_file.read_text(encoding="utf-8")
        self.assertIn("placeholder page one", text)

    def test_zero_page_guard_falls_through_to_legacy_ladder(self):
        fake = _FakePdfInspector(result=_FakePagesExtractionResult(pages=[]))
        with _patched_inspector(fake):
            status = atm.convert_file(self.src, self.out_file)
        # Legacy MarkItDown ladder picks up the real synthetic text instead.
        self.assertEqual(status, "converted")
        self.assertIn("placeholder page one", self.out_file.read_text(encoding="utf-8"))

    def test_mixed_pdf_reassembles_pages_in_order(self):
        pages = [
            _FakePageMarkdown(page=0, markdown="# Intro", needs_ocr=False),
            _FakePageMarkdown(page=1, markdown="", needs_ocr=True),  # scanned page
            _FakePageMarkdown(page=2, markdown="# Conclusion", needs_ocr=False),
        ]
        fake = _FakePdfInspector(result=_FakePagesExtractionResult(pages=pages))

        def fake_ocr(src, pages=None):
            # convert_file must ask for exactly the missing 1-indexed page (2).
            self.assertEqual(pages, [2])
            return {2: "OCR TEXT"}

        with _patched_inspector(fake), \
                _patched_attr(atm, "_ocr_unavailable_reason", lambda ext: None), \
                _patched_attr(atm, "_extract_with_ocr", fake_ocr):
            status = atm.convert_file(self.src, self.out_file)

        self.assertEqual(status, "converted")
        self.assertEqual(
            self.out_file.read_text(encoding="utf-8"),
            "# Intro\n\nOCR TEXT\n\n# Conclusion",
        )

    def test_mixed_pdf_partial_when_ocr_unavailable(self):
        pages = [
            _FakePageMarkdown(page=0, markdown="# Intro", needs_ocr=False),
            _FakePageMarkdown(page=1, markdown="", needs_ocr=True),
        ]
        fake = _FakePdfInspector(result=_FakePagesExtractionResult(pages=pages))

        with _patched_inspector(fake), \
                _patched_attr(atm, "_ocr_unavailable_reason", lambda ext: "Tesseract not on PATH"):
            status = atm.convert_file(self.src, self.out_file)

        self.assertEqual(status, "partial")
        # Native page still written even though OCR couldn't run.
        self.assertEqual(self.out_file.read_text(encoding="utf-8"), "# Intro")

    def test_all_ocr_pdf_short_circuits_when_ocr_unavailable(self):
        pages = [_FakePageMarkdown(page=0, markdown="", needs_ocr=True)]
        fake = _FakePdfInspector(result=_FakePagesExtractionResult(pages=pages))

        with _patched_inspector(fake), \
                _patched_attr(atm, "_ocr_unavailable_reason", lambda ext: "Tesseract not on PATH"):
            status = atm.convert_file(self.src, self.out_file)

        self.assertEqual(status, "scanned")
        self.assertFalse(self.out_file.exists())

    def test_none_pdf_inspector_uses_legacy_ladder(self):
        with _patched_inspector(None):
            status = atm.convert_file(self.src, self.out_file)
        self.assertEqual(status, "converted")
        self.assertIn("placeholder page one", self.out_file.read_text(encoding="utf-8"))

    def test_mixed_pdf_partial_when_ocr_call_raises(self):
        # The OCR binaries can resolve fine (_ocr_unavailable_reason is None)
        # while the actual _extract_with_ocr call still raises for some other
        # reason (e.g. a missing optional Python dependency, which only fails
        # lazily on first use per the ground truth). That must degrade to
        # partial — keeping the native pages already extracted — not destroy
        # them by returning "failed".
        pages = [
            _FakePageMarkdown(page=0, markdown="# Intro", needs_ocr=False),
            _FakePageMarkdown(page=1, markdown="", needs_ocr=True),
        ]
        fake = _FakePdfInspector(result=_FakePagesExtractionResult(pages=pages))

        def raising_ocr(src, pages=None):
            raise ImportError("No module named 'pytesseract'")

        with _patched_inspector(fake), \
                _patched_attr(atm, "_ocr_unavailable_reason", lambda ext: None), \
                _patched_attr(atm, "_extract_with_ocr", raising_ocr):
            status = atm.convert_file(self.src, self.out_file)

        self.assertEqual(status, "partial")
        self.assertEqual(self.out_file.read_text(encoding="utf-8"), "# Intro")

    def test_mixed_pdf_partial_when_ocr_result_missing_a_requested_page(self):
        # Ground truth for this finding: poppler's convert_from_path silently
        # clips out-of-range first_page/last_page requests instead of raising
        # or padding, so _extract_with_ocr's returned dict can legitimately be
        # short pages it was asked for. That must never be papered over with
        # an empty string written into the output as if nothing were missing.
        pages = [
            _FakePageMarkdown(page=0, markdown="# Page One", needs_ocr=False),
            _FakePageMarkdown(page=1, markdown="# Page Two", needs_ocr=False),
            _FakePageMarkdown(page=2, markdown="", needs_ocr=True),
        ]
        fake = _FakePdfInspector(result=_FakePagesExtractionResult(pages=pages))

        def short_ocr(src, pages=None):
            self.assertEqual(pages, [3])
            return {}  # page 3 silently dropped, exactly as poppler really does

        with _patched_inspector(fake), \
                _patched_attr(atm, "_ocr_unavailable_reason", lambda ext: None), \
                _patched_attr(atm, "_extract_with_ocr", short_ocr):
            status = atm.convert_file(self.src, self.out_file)

        self.assertEqual(status, "partial")
        self.assertEqual(
            self.out_file.read_text(encoding="utf-8"), "# Page One\n\n# Page Two"
        )


def _patched_attr(obj, name, value):
    class _Ctx:
        def __enter__(self_ctx):
            self_ctx.old = getattr(obj, name)
            setattr(obj, name, value)
            return value

        def __exit__(self_ctx, *exc):
            setattr(obj, name, self_ctx.old)
            return False

    return _Ctx()


class RealImportGuardTests(unittest.TestCase):
    def test_pdf_inspector_actually_imported(self):
        # Every other test in this file monkeypatches atm._PDF_INSPECTOR, so
        # none of them independently verify the module-level try/except
        # import guard's success path. pdf_inspector is genuinely installed
        # on this machine (ground truth), so a regression that hardcodes the
        # guard to None -- or otherwise breaks the successful-import branch --
        # must fail here.
        import pdf_inspector

        self.assertIsNotNone(atm._PDF_INSPECTOR)
        self.assertIs(atm._PDF_INSPECTOR, pdf_inspector)
        self.assertTrue(hasattr(atm._PDF_INSPECTOR, "extract_pages_markdown"))


class BrokenPdfDoesNotCrashBatchTests(unittest.TestCase):
    def test_broken_pdf_falls_through_without_raising(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        tmp = Path(tmpdir.name)
        # Not a real PDF at all -- both the pdf-inspector branch and the
        # legacy MarkItDown ladder should fail gracefully, not raise.
        src = tmp / "broken.pdf"
        src.write_bytes(b"this is not a pdf file")
        out_file = tmp / "broken.md"

        fake = _FakePdfInspector(error=ValueError("Invalid PDF structure"))
        with _patched_inspector(fake):
            try:
                status = atm.convert_file(src, out_file)
            except Exception as exc:  # noqa: BLE001 -- the point of this test
                self.fail(f"convert_file raised instead of degrading: {exc}")
        # The pdf-inspector branch must have caught its ValueError and fallen
        # through to the legacy ladder rather than propagating -- whatever the
        # legacy ladder itself then does with the garbage bytes (extracts
        # something, or reports failed/scanned) is a separate concern.
        self.assertIn(status, ("converted", "failed", "scanned"))


if __name__ == "__main__":
    unittest.main()
