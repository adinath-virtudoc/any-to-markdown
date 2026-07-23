import logging
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


def _extract(pdf: Path) -> str:
    """Run MarkItDown on a PDF and return its text content."""
    md = MarkItDown()
    return md.convert(str(pdf)).text_content


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


def convert_pdf(pdf: Path, output_dir: Path, label: str | None = None) -> str:
    """Convert a single PDF to Markdown.

    Returns one of:
      - ``"converted"`` — text extracted and written to ``output_dir``.
      - ``"scanned"``   — the PDF opened fine but has no text layer (image-only
                          / scanned); it needs OCR, which this tool does not do.
      - ``"failed"``    — the PDF could not be read at all (corrupt/unsupported).

    ``label`` is the name shown in the live progress line (defaults to the
    file name); pass e.g. ``"[3/25] chapter.pdf"`` for batch position.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / (pdf.stem + ".md")
    label = label or pdf.name

    text = ""
    elapsed = 0.0
    extraction_errored = False
    try:
        text, elapsed = _run_with_progress(lambda: _extract(pdf), label)
    except Exception as exc:  # noqa: BLE001 — keep a batch alive on any single bad file
        extraction_errored = True
        print(f"  ! Direct conversion failed for {pdf.name}: {exc}")

    # If extraction errored or produced effectively nothing, retry on a
    # Ghostscript-normalized copy that flattens problematic colour spaces.
    if not text.strip():
        cleaned = _normalize_with_ghostscript(pdf)
        if cleaned is not None:
            try:
                text, retry_elapsed = _run_with_progress(
                    lambda: _extract(cleaned), f"{label} (ghostscript retry)"
                )
                elapsed += retry_elapsed
                extraction_errored = False  # retry read the file successfully
            except Exception as exc:  # noqa: BLE001
                extraction_errored = True
                print(f"  ! Ghostscript retry failed for {pdf.name}: {exc}")
            finally:
                shutil.rmtree(cleaned.parent, ignore_errors=True)

    if not text.strip():
        # No text after every attempt. If the file opened without error it has
        # no text layer — a scanned / image-only PDF that needs OCR (this tool
        # extracts existing text only). If extraction errored, the file itself
        # is unreadable.
        if extraction_errored:
            print(f"  ✗ {label} — unreadable PDF, skipped ({elapsed:.1f}s)")
            return "failed"
        print(
            f"  ⚠ {label} — no text layer (likely scanned/image-only); "
            f"needs OCR, skipped ({elapsed:.1f}s)"
        )
        return "scanned"

    output_file.write_text(text, encoding="utf-8")
    print(f"  ✓ {label} → {output_file}  ({elapsed:.1f}s)")
    return "converted"


def convert_folder(folder_path: str) -> None:
    folder = Path(folder_path).expanduser().resolve()

    if not folder.exists():
        print(f"Error: path not found — {folder}")
        sys.exit(1)

    if folder.is_file():
        if folder.suffix.lower() != ".pdf":
            print(f"Error: expected a .pdf file, got '{folder.suffix}'")
            sys.exit(1)
        output_dir = folder.parent / "markdown"
        status = convert_pdf(folder, output_dir)
        sys.exit(0 if status == "converted" else 1)

    output_root = folder / "markdown"

    # Recurse into nested folders, but never descend into our own output tree.
    pdfs = sorted(
        pdf
        for pdf in folder.rglob("*.pdf")
        if output_root not in pdf.parents
    )
    if not pdfs:
        print(f"No PDF files found in {folder}")
        sys.exit(1)

    pending = []
    skipped = 0
    for pdf in pdfs:
        # Mirror the source folder structure under the output root.
        out_dir = output_root / pdf.parent.relative_to(folder)
        if (out_dir / (pdf.stem + ".md")).exists():
            skipped += 1
        else:
            pending.append((pdf, out_dir))

    if skipped:
        print(f"Skipping {skipped} already-converted PDF(s).")
    if not pending:
        print("All PDFs already converted.")
        return

    total = len(pending)
    print(f"Converting {total} PDF(s) → {output_root}")
    succeeded = 0
    failed = []
    scanned = []
    for i, (pdf, out_dir) in enumerate(pending, start=1):
        rel = pdf.relative_to(folder)
        label = f"[{i}/{total}] {rel}"
        status = convert_pdf(pdf, out_dir, label=label)
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
        print("Likely scanned/image-only (need OCR — e.g. markitdown-ocr "
              "or Azure Document Intelligence):")
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
        path = input("Enter PDF file or folder path: ").strip().strip("'\"")
    convert_folder(path)


if __name__ == "__main__":
    main()
