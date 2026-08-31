import logging
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from markitdown import MarkItDown

# pdfminer.six (used by MarkItDown for PDFs) logs noisy, non-fatal warnings such as
#   "Cannot set non-stroke color: 2 components specified, but only 1 (grayscale),
#    3 (RGB), and 4 (CMYK) are supported"
# for PDFs that use uncommon colour spaces (Separation/DeviceN/ICCBased/Lab). Text
# still extracts fine, so quiet the warnings to keep the output readable.
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfplumber").setLevel(logging.ERROR)

_GS = shutil.which("gs")

# OCR needs two system binaries, neither installable via pip: Tesseract for the
# recognition itself, and Poppler's pdftoppm (used by pdf2image) to rasterize PDF
# pages. Resolved once at import so a missing binary degrades to the old
# "needs OCR, skipped" report instead of failing per file.
_TESSERACT = shutil.which("tesseract")
_PDFTOPPM = shutil.which("pdftoppm")

# pdf-inspector gives accurate per-page Markdown plus per-page OCR routing for
# PDFs, at far higher heading-extraction quality than MarkItDown (measured: it
# produces real ATX headings, MarkItDown produces none and fabricates spurious
# pipe-tables from bullet lists). It's an optional accelerator, not a hard
# dependency — when unavailable, every PDF takes the legacy MarkItDown /
# Ghostscript / whole-file-OCR ladder below, unchanged.
try:
    import pdf_inspector as _PDF_INSPECTOR
except ImportError:
    _PDF_INSPECTOR = None

# File types MarkItDown can convert. Discovery in a folder is limited to these;
# a single-file argument is checked against this set before conversion.
SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx", ".pptx", ".xlsx", ".xls",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp",
    ".mp3", ".wav", ".m4a", ".flac",
    ".html", ".htm",
    ".csv", ".json", ".xml",
    ".zip", ".epub", ".msg",
}

# Formats where empty output most likely means "no text layer / OCR needed"
# rather than a genuinely empty or broken file. (Audio that transcribes to
# nothing, or an empty spreadsheet, is a failure — not an OCR candidate.)
_OCR_CANDIDATE_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp",
}


def _extract(src) -> str:
    """Run MarkItDown on a file path or URL and return its text content."""
    md = MarkItDown()
    return md.convert(str(src)).text_content


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _run_with_progress(fn, label: str):
    """Run a blocking callable while showing a live spinner + elapsed timer.

    MarkItDown's convert() is a single opaque call, so per-file progress is
    shown as liveness (spinner + seconds) rather than a sub-file percentage.
    Falls back to plain start/end lines when stdout isn't a TTY (piped/logged).

    Returns (result, elapsed_seconds). Any exception from fn is re-raised.
    """
    box: dict = {}

    def _worker():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 — surface on the caller thread
            box["error"] = exc

    start = time.monotonic()
    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    interactive = sys.stdout.isatty()
    if not interactive:
        print(f"  … {label}", flush=True)

    i = 0
    while t.is_alive():
        if interactive:
            elapsed = time.monotonic() - start
            frame = _SPINNER[i % len(_SPINNER)]
            line = f"\r  {frame} {label} — {elapsed:4.1f}s"
            sys.stdout.write(line[: shutil.get_terminal_size().columns - 1])
            sys.stdout.flush()
            i += 1
        t.join(timeout=0.1)

    elapsed = time.monotonic() - start
    if interactive:
        # Clear the spinner line so the caller's final status starts clean.
        sys.stdout.write("\r" + " " * (shutil.get_terminal_size().columns - 1) + "\r")
        sys.stdout.flush()

    if "error" in box:
        raise box["error"]
    return box.get("result", ""), elapsed


def _normalize_with_ghostscript(pdf: Path) -> Path | None:
    """Re-render a PDF through Ghostscript to flatten odd colour spaces to RGB.

    Returns the path to a temporary cleaned PDF, or None if Ghostscript is
    unavailable or the rewrite fails.
    """
    if not _GS:
        return None

    cleaned = Path(tempfile.mkdtemp(prefix="pdf2md_")) / (pdf.stem + "_clean.pdf")
    try:
        subprocess.run(
            [
                _GS,
                "-q",
                "-o",
                str(cleaned),
                "-sDEVICE=pdfwrite",
                "-dColorConversionStrategy=/RGB",
                "-dProcessColorModel=/DeviceRGB",
                str(pdf),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, OSError):
        return None

    return cleaned if cleaned.exists() and cleaned.stat().st_size > 0 else None

def _ocr_unavailable_reason(ext: str) -> str | None:
    """Return why OCR can't run for ``ext``, or None if it can."""
    if not _TESSERACT:
        return "Tesseract not on PATH"
    if ext == ".pdf" and not _PDFTOPPM:
        return "Poppler (pdftoppm) not on PATH"
    return None


def _extract_with_ocr(src: Path, pages: list[int] | None = None) -> str | dict[int, str]:
    """OCR an image or image-only PDF with Tesseract and return its text.

    Imports pytesseract/pdf2image lazily so the tool still converts docx, html,
    audio and friends when the OCR extras aren't installed.

    When ``pages`` is None (the default), OCRs the whole document and returns
    one joined string, exactly as before. When ``pages`` is given — 1-indexed
    page numbers, for the mixed-PDF case where pdf-inspector says only some
    pages need OCR — returns a ``{page_number: text}`` dict instead, so the
    caller can splice OCR text back into page order without re-rendering pages
    that already have good native text. Rendering runs once per contiguous run
    of page numbers (via pdf2image's first_page/last_page) rather than
    rasterizing the whole document.
    """
    import pytesseract  # noqa: PLC0415 — optional dependency, imported on demand

    if src.suffix.lower() != ".pdf":
        from PIL import Image  # noqa: PLC0415

        with Image.open(src) as img:
            return pytesseract.image_to_string(img)

    from pdf2image import convert_from_path  # noqa: PLC0415

    # 300 dpi is Tesseract's recommended input resolution (pdf2image defaults to
    # 200). output_folder streams pages to disk instead of holding every rendered
    # page of a long PDF in memory at once.
    with tempfile.TemporaryDirectory(prefix="a2m_ocr_") as tmp:
        if pages is None:
            rendered = convert_from_path(src, dpi=300, output_folder=tmp)
            return "\n\n".join(pytesseract.image_to_string(page) for page in rendered)

        by_page: dict[int, str] = {}
        for start, end in _contiguous_page_runs(sorted(pages)):
            rendered = convert_from_path(
                src, dpi=300, output_folder=tmp, first_page=start, last_page=end
            )
            for offset, page_img in enumerate(rendered):
                by_page[start + offset] = pytesseract.image_to_string(page_img)
        return by_page


def _contiguous_page_runs(sorted_pages: list[int]) -> list[tuple[int, int]]:
    """Collapse a sorted list of 1-indexed page numbers into (start, end) runs
    of consecutive pages, so mixed-PDF OCR renders each run with a single
    convert_from_path call instead of one call per flagged page.
    """
    runs: list[tuple[int, int]] = []
    for p in sorted_pages:
        if runs and p == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], p)
        else:
            runs.append((p, p))
    return runs


def _pi_page_to_pdf_page(n: int) -> int:
    """Convert a 0-indexed pdf-inspector ``PageMarkdown.page`` number to the
    1-indexed page number pdf2image's ``convert_from_path(first_page=,
    last_page=)`` and humans expect."""
    return n + 1


def _pdf_page_to_pi_page(n: int) -> int:
    """Convert a 1-indexed pdf2image/human page number to pdf-inspector's
    0-indexed ``PageMarkdown.page`` numbering."""
    return n - 1


def _document_notes(result) -> str | None:
    """One short optional note on layout signals pdf-inspector already computed
    for this PDF (tables, columns, overall complexity), or None when nothing
    stands out enough to mention. Reads fields already present on the same
    PagesExtractionResult from extract_pages_markdown() — no second call.
    """
    notes = []
    if result.is_complex:
        notes.append("complex layout")
    if result.pages_with_tables:
        notes.append(f"{len(result.pages_with_tables)} page(s) w/ tables")
    if result.pages_with_columns:
        notes.append(f"{len(result.pages_with_columns)} page(s) w/ columns")
    return "; ".join(notes) if notes else None


def _extract_pdf_native(src: Path) -> tuple[list, list, object] | None:
    """Try pdf-inspector's per-page extraction on ``src``.

    Returns ``(native_pages, ocr_pages, result)`` — both lists holding the
    PageMarkdown objects from ``result.pages`` (native = has usable text and
    doesn't need OCR; ocr = needs OCR or came back blank) — or ``None`` when
    pdf-inspector is unavailable, the call fails for any reason, or the PDF has
    zero pages. A None here always means "fall through to the legacy ladder",
    never "write an empty file".
    """
    if _PDF_INSPECTOR is None:
        return None
    try:
        result = _PDF_INSPECTOR.extract_pages_markdown(str(src))
    except Exception:  # noqa: BLE001 — broad on purpose: a negative/out-of-range
        # page index raises OverflowError (a distinct type leaking from PyO3,
        # not the plain ValueError every other broken-PDF case raises), so a
        # narrower except would miss it and crash the batch.
        return None

    if not result.pages:
        # Degenerate/0-page PDF. Never write an empty file and call it converted.
        return None

    native_pages = [p for p in result.pages if not p.needs_ocr and p.markdown.strip()]
    ocr_pages = [p for p in result.pages if p.needs_ocr or not p.markdown.strip()]
    return native_pages, ocr_pages, result


def _convert_pdf_native(src: Path, out_file: Path, label: str) -> str | None:
    """The pdf-inspector PDF branch, tried before the legacy MarkItDown ladder.

    Returns a status string (``"converted"`` / ``"partial"`` / ``"scanned"`` /
    ``"failed"``) once this branch has written (or deliberately not written)
    ``out_file``, or ``None`` to tell the caller to fall through to the legacy
    ladder unchanged (0-page/degenerate PDF, or pdf-inspector failed outright).
    """
    try:
        native, elapsed = _run_with_progress(lambda: _extract_pdf_native(src), label)
    except Exception as exc:  # noqa: BLE001 — keep a batch alive on any single bad file
        print(f"  ! pdf-inspector failed for {src.name}: {exc}")
        return None
    if native is None:
        return None
    native_pages, ocr_pages, result = native

    note = _document_notes(result)
    note_suffix = f"  ({note})" if note else ""

    if not ocr_pages:
        # (a) every page already has usable text — no OCR needed at all.
        text = "\n\n".join(p.markdown for p in sorted(native_pages, key=lambda p: p.page))
        out_file.write_text(text, encoding="utf-8")
        print(f"  ✓ {label} → {out_file}  (pdf-inspector, {elapsed:.1f}s){note_suffix}")
        return "converted"

    missing_pdf_pages = sorted(_pi_page_to_pdf_page(p.page) for p in ocr_pages)
    unavailable = _ocr_unavailable_reason(".pdf")

    if not native_pages:
        # (b) every page needs OCR. Terminal short-circuit: skip MarkItDown and
        # the Ghostscript retry entirely — both are known to pay full-document
        # latency to extract nothing from a pure scan.
        if unavailable:
            print(
                f"  ⚠ {label} — scanned/image-only (pdf-inspector); "
                f"OCR unavailable ({unavailable}), skipped ({elapsed:.1f}s)"
            )
            return "scanned"
        try:
            ocr_text, ocr_elapsed = _run_with_progress(
                lambda: _extract_with_ocr(src), f"{label} (OCR)"
            )
        except Exception as exc:  # noqa: BLE001 — keep a batch alive
            print(f"  ✗ {label} — OCR failed: {exc}")
            return "failed"
        if not ocr_text.strip():
            print(f"  ✗ {label} — OCR found no text, skipped ({elapsed + ocr_elapsed:.1f}s)")
            return "failed"
        out_file.write_text(ocr_text, encoding="utf-8")
        print(
            f"  ✓ {label} → {out_file}  (pdf-inspector + OCR, {elapsed + ocr_elapsed:.1f}s)"
        )
        return "converted"

    # (c) mixed: keep the native pages' markdown, OCR only the flagged pages.
    if unavailable:
        ordered = [p.markdown for p in sorted(native_pages, key=lambda p: p.page)]
        out_file.write_text("\n\n".join(ordered), encoding="utf-8")
        pages_str = ", ".join(str(p) for p in missing_pdf_pages)
        print(
            f"  ⚠ {label} → {out_file}  (pdf-inspector, partial, {elapsed:.1f}s) — "
            f"page(s) {pages_str} need OCR; {unavailable}, skipped"
        )
        return "partial"

    try:
        ocr_by_pdf_page, ocr_elapsed = _run_with_progress(
            lambda: _extract_with_ocr(src, pages=missing_pdf_pages),
            f"{label} (OCR pages {', '.join(str(p) for p in missing_pdf_pages)})",
        )
    except Exception as exc:  # noqa: BLE001 — an OCR-layer failure (e.g. a
        # missing optional dependency raised lazily) must not throw away the
        # native pages already in hand: degrade to partial instead of failed,
        # the same way an absent OCR binary does just above.
        ordered = [p.markdown for p in sorted(native_pages, key=lambda p: p.page)]
        out_file.write_text("\n\n".join(ordered), encoding="utf-8")
        pages_str = ", ".join(str(p) for p in missing_pdf_pages)
        print(
            f"  ⚠ {label} → {out_file}  (pdf-inspector, partial, {elapsed:.1f}s) — "
            f"page(s) {pages_str} need OCR; OCR failed: {exc}"
        )
        return "partial"

    # Poppler silently clips out-of-range page requests instead of raising or
    # padding (confirmed: convert_from_path(first_page=, last_page=) beyond the
    # real page count returns fewer images than asked for) — never assume
    # ocr_by_pdf_page has an entry for every page we requested.
    still_missing = [p for p in missing_pdf_pages if p not in ocr_by_pdf_page]

    ocr_page_ids = {p.page for p in ocr_pages}
    parts = []
    for p in sorted(result.pages, key=lambda p: p.page):
        if p.page in ocr_page_ids:
            pdf_page = _pi_page_to_pdf_page(p.page)
            if pdf_page in ocr_by_pdf_page:
                parts.append(ocr_by_pdf_page[pdf_page])
            # else: no OCR text came back for this page — omit its slot rather
            # than silently writing an empty string, and report it below.
        else:
            parts.append(p.markdown)
    out_file.write_text("\n\n".join(parts), encoding="utf-8")

    if still_missing:
        pages_str = ", ".join(str(p) for p in still_missing)
        print(
            f"  ⚠ {label} → {out_file}  (pdf-inspector + OCR, partial, "
            f"{elapsed + ocr_elapsed:.1f}s) — page(s) {pages_str} still missing "
            f"after OCR{note_suffix}"
        )
        return "partial"

    print(
        f"  ✓ {label} → {out_file}  (pdf-inspector + OCR, {elapsed + ocr_elapsed:.1f}s)"
        f"{note_suffix}"
    )
    return "converted"


def convert_file(
    src: Path, out_file: Path, label: str | None = None, legacy_pdf: bool = False
) -> str:
    """Convert a single supported file to Markdown, writing to ``out_file``.

    Returns one of:
      - ``"converted"`` — text extracted and written to ``out_file``.
      - ``"partial"``   — a mixed PDF where pdf-inspector's native pages were
                          written but some pages still need OCR, which isn't
                          installed. Naming which pages are missing is the
                          caller's job (it's printed here, and callers should
                          surface it in any batch summary).
      - ``"scanned"``   — an image-only PDF/image that needs OCR, but the OCR
                          binaries (Tesseract / Poppler) aren't installed.
      - ``"failed"``    — the file could not be read, or produced no text and is
                          not an OCR candidate.

    ``label`` is the name shown in the live progress line (defaults to the
    file name); pass e.g. ``"[3/25] chapter.pdf"`` for batch position.

    ``legacy_pdf`` forces PDFs through the old MarkItDown-first ladder even
    when pdf-inspector is available (the ``--legacy-pdf`` CLI escape hatch).
    """
    out_file.parent.mkdir(parents=True, exist_ok=True)
    label = label or src.name
    ext = src.suffix.lower()

    if ext == ".pdf" and _PDF_INSPECTOR is not None and not legacy_pdf:
        status = _convert_pdf_native(src, out_file, label)
        if status is not None:
            return status
        # Native extraction yielded nothing usable (0-page/degenerate PDF, or
        # pdf-inspector itself failed) — fall through to the legacy ladder.

    text = ""
    elapsed = 0.0
    extraction_errored = False
    try:
        text, elapsed = _run_with_progress(lambda: _extract(src), label)
    except Exception as exc:  # noqa: BLE001 — keep a batch alive on any single bad file
        extraction_errored = True
        print(f"  ! Conversion failed for {src.name}: {exc}")

    # PDF-only: retry on a Ghostscript-normalized copy that flattens problematic
    # colour spaces, which sometimes unblocks text extraction.
    if not text.strip() and ext == ".pdf":
        cleaned = _normalize_with_ghostscript(src)
        if cleaned is not None:
            try:
                text, retry_elapsed = _run_with_progress(
                    lambda: _extract(cleaned), f"{label} (ghostscript retry)"
                )
                elapsed += retry_elapsed
                extraction_errored = False  # retry read the file successfully
            except Exception as exc:  # noqa: BLE001
                extraction_errored = True
                print(f"  ! Ghostscript retry failed for {src.name}: {exc}")
            finally:
                shutil.rmtree(cleaned.parent, ignore_errors=True)

    if not text.strip():
        # No text after every attempt. For PDFs and images that is the expected
        # signature of a scan, so fall back to OCR — including when MarkItDown
        # could not open the file at all, since it has no converter for .tiff or
        # .bmp while Tesseract reads both.
        if ext in _OCR_CANDIDATE_EXTENSIONS:
            unavailable = _ocr_unavailable_reason(ext)
            if unavailable:
                print(
                    f"  ⚠ {label} — no text layer (likely scanned/image-only); "
                    f"OCR unavailable ({unavailable}), skipped ({elapsed:.1f}s)"
                )
                return "scanned"

            try:
                ocr_text, ocr_elapsed = _run_with_progress(
                    lambda: _extract_with_ocr(src), f"{label} (OCR)"
                )
                elapsed += ocr_elapsed
            except Exception as exc:  # noqa: BLE001 — keep a batch alive
                print(f"  ✗ {label} — OCR failed: {exc}")
                return "failed"

            if ocr_text.strip():
                out_file.write_text(ocr_text, encoding="utf-8")
                print(f"  ✓ {label} → {out_file}  (OCR, {elapsed:.1f}s)")
                return "converted"

            print(f"  ✗ {label} — OCR found no text, skipped ({elapsed:.1f}s)")
            return "failed"

        if extraction_errored:
            print(f"  ✗ {label} — could not read file, skipped ({elapsed:.1f}s)")
            return "failed"
        print(f"  ✗ {label} — no text extracted, skipped ({elapsed:.1f}s)")
        return "failed"

    out_file.write_text(text, encoding="utf-8")
    print(f"  ✓ {label} → {out_file}  (markitdown, {elapsed:.1f}s)")
    return "converted"


def _looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def convert_url(url: str) -> None:
    """Convert a URL (e.g. a YouTube video or web page) to Markdown.

    Writes to ``./markdown/<slug>.md`` under the current directory, since a URL
    has no source folder to mirror.
    """
    output_dir = Path.cwd() / "markdown"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", url.split("://", 1)[-1]).strip("_")[:80]
    out_file = output_dir / ((slug or "url") + ".md")
    out_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        text, elapsed = _run_with_progress(lambda: _extract(url), url)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {url} — conversion failed: {exc}")
        sys.exit(1)

    if not text.strip():
        print(f"  ✗ {url} — no content extracted (no captions/transcript?)")
        sys.exit(1)

    out_file.write_text(text, encoding="utf-8")
    print(f"  ✓ {url} → {out_file}  ({elapsed:.1f}s)")


def convert_path(input_path: str, legacy_pdf: bool = False) -> None:
    if _looks_like_url(input_path):
        convert_url(input_path)
        return

    target = Path(input_path).expanduser().resolve()

    if not target.exists():
        print(f"Error: path not found — {target}")
        sys.exit(1)

    if target.is_file():
        ext = target.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            print(
                f"Error: unsupported file type '{target.suffix}'. Supported: "
                f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )
            sys.exit(1)
        out_file = target.parent / "markdown" / (target.stem + ".md")
        status = convert_file(target, out_file, legacy_pdf=legacy_pdf)
        # A "partial" PDF (native pages written, some pages still need OCR) is
        # still useful output, not a failure — treat it as success.
        sys.exit(0 if status in ("converted", "partial") else 1)

    output_root = target / "markdown"

    # Recurse into nested folders, but never descend into our own output tree.
    files = sorted(
        f
        for f in target.rglob("*")
        if f.is_file()
        and f.suffix.lower() in SUPPORTED_EXTENSIONS
        and output_root not in f.parents
    )
    if not files:
        print(f"No supported files found in {target}")
        sys.exit(1)

    pending = []
    skipped = 0
    claimed: dict[Path, Path] = {}  # output path -> source, to catch stem clashes
    for f in files:
        # Mirror the source folder structure under the output root.
        out_dir = output_root / f.parent.relative_to(target)
        out_file = out_dir / (f.stem + ".md")
        if out_file in claimed:
            # Another file in the same folder already claims <stem>.md (e.g.
            # report.pdf vs report.docx). Keep the source extension so neither
            # output silently overwrites the other: report.docx -> report_docx.md
            out_file = out_dir / (f.stem + "_" + f.suffix.lower().lstrip(".") + ".md")
        claimed[out_file] = f

        if out_file.exists():
            skipped += 1
        else:
            pending.append((f, out_file))

    if skipped:
        print(f"Skipping {skipped} already-converted file(s).")
    if not pending:
        print("All files already converted.")
        return

    total = len(pending)
    print(f"Converting {total} file(s) → {output_root}")
    succeeded = 0
    failed = []
    scanned = []
    partial = []
    for i, (f, out_file) in enumerate(pending, start=1):
        rel = f.relative_to(target)
        label = f"[{i}/{total}] {rel}"
        status = convert_file(f, out_file, label=label, legacy_pdf=legacy_pdf)
        if status == "converted":
            succeeded += 1
        elif status == "partial":
            partial.append(str(rel))
        elif status == "scanned":
            scanned.append(str(rel))
        else:
            failed.append(str(rel))

    print(
        f"\nDone: {succeeded} converted, {len(partial)} partial, "
        f"{len(scanned)} need OCR, {len(failed)} failed."
    )
    if partial:
        print("Partially converted — native pages written, but some pages "
              "still need OCR (not installed). Installing the OCR binaries "
              "(see README) and deleting these .md files will pick up the "
              "missing pages on the next run (idempotent re-runs skip "
              "existing output):")
        for name in partial:
            print(f"  - {name}")
    if scanned:
        print("Scanned/image-only, and OCR is unavailable — install the OCR "
              "binaries (see README) and re-run:")
        for name in scanned:
            print(f"  - {name}")
    if failed:
        print("Failed files:")
        for name in failed:
            print(f"  - {name}")


def main():
    argv = sys.argv[1:]
    legacy_pdf = "--legacy-pdf" in argv
    if legacy_pdf:
        argv = [a for a in argv if a != "--legacy-pdf"]

    if argv:
        path = " ".join(argv).strip().strip("'\"")
    else:
        path = input("Enter a file, folder, or URL: ").strip().strip("'\"")
    convert_path(path, legacy_pdf=legacy_pdf)


if __name__ == "__main__":
    main()
