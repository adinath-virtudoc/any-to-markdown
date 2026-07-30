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


def _extract_with_ocr(src: Path) -> str:
    """OCR an image or image-only PDF with Tesseract and return its text.

    Imports pytesseract/pdf2image lazily so the tool still converts docx, html,
    audio and friends when the OCR extras aren't installed.
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
        pages = convert_from_path(src, dpi=300, output_folder=tmp)
        return "\n\n".join(pytesseract.image_to_string(page) for page in pages)


def convert_file(src: Path, out_file: Path, label: str | None = None) -> str:
    """Convert a single supported file to Markdown, writing to ``out_file``.

    Returns one of:
      - ``"converted"`` — text extracted and written to ``out_file``.
      - ``"scanned"``   — an image-only PDF/image that needs OCR, but the OCR
                          binaries (Tesseract / Poppler) aren't installed.
      - ``"failed"``    — the file could not be read, or produced no text and is
                          not an OCR candidate.

    ``label`` is the name shown in the live progress line (defaults to the
    file name); pass e.g. ``"[3/25] chapter.pdf"`` for batch position.
    """
    out_file.parent.mkdir(parents=True, exist_ok=True)
    label = label or src.name
    ext = src.suffix.lower()

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
    print(f"  ✓ {label} → {out_file}  ({elapsed:.1f}s)")
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


def convert_path(input_path: str) -> None:
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
        status = convert_file(target, out_file)
        sys.exit(0 if status == "converted" else 1)

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
    for i, (f, out_file) in enumerate(pending, start=1):
        rel = f.relative_to(target)
        label = f"[{i}/{total}] {rel}"
        status = convert_file(f, out_file, label=label)
        if status == "converted":
            succeeded += 1
        elif status == "scanned":
            scanned.append(str(rel))
        else:
            failed.append(str(rel))

    print(
        f"\nDone: {succeeded} converted, {len(scanned)} need OCR, "
        f"{len(failed)} failed."
    )
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
    if len(sys.argv) > 1:
        path = " ".join(sys.argv[1:]).strip().strip("'\"")
    else:
        path = input("Enter a file, folder, or URL: ").strip().strip("'\"")
    convert_path(path)


if __name__ == "__main__":
    main()
