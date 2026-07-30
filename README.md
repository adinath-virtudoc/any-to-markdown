# any-to-markdown

A single-file CLI that converts documents — a single file, a whole folder tree, or a URL — to Markdown.

It handles **every format [`markitdown`](https://github.com/microsoft/markitdown) supports** — PDF, Office documents, images, audio, web and data files, and URLs. It's a thin, batch-friendly wrapper around Microsoft's `markitdown` (which does the actual extraction, backed by `pdfminer.six` for PDFs). On top of that, this tool adds:

- **Batch + recursion** — point it at a folder and it converts every supported file under it, mirroring the source subfolder structure into an output tree.
- **Idempotent re-runs** — files whose `.md` already exists are skipped, so re-running a large folder only picks up what's new.
- **Live progress** — a spinner + elapsed timer per file (and `[3/25]`-style batch position), with a clean fallback to plain log lines when output isn't a TTY.
- **OCR fallback for scans** — a PDF or image with no text layer is passed to Tesseract locally, so scanned pages convert instead of being skipped. Needs two system binaries; without them the file is reported as `needs OCR` and skipped rather than failing. See [OCR](#ocr) below.
- **Ghostscript fallback** (PDF only) — if a PDF's odd colour space (Separation/DeviceN/ICCBased/Lab) yields empty text, it automatically retries on a Ghostscript-normalized (RGB-flattened) copy. A single bad file never aborts the batch.
- **Quiet output** — silences the noisy-but-harmless `pdfminer`/`pdfplumber` colour-space warnings.

## Supported formats

| Category | Extensions |
|---|---|
| Documents | `.pdf` `.docx` `.pptx` `.xlsx` `.xls` `.epub` `.msg` |
| Images | `.jpg` `.jpeg` `.png` `.gif` `.bmp` `.tiff` `.tif` `.webp` |
| Audio | `.mp3` `.wav` `.m4a` `.flac` (speech → transcript) |
| Web / data | `.html` `.htm` `.csv` `.json` `.xml` `.zip` |
| URLs | `http(s)://…` including YouTube (pulls title + caption transcript) |

## Install

```bash
pip install 'markitdown[all]' -r requirements.txt
```

`[all]` pulls in every format's dependencies. To keep it lean, install only what you need — e.g. `pip install 'markitdown[pdf, docx, pptx]'`.

Two optional system binaries, both looked up on your `PATH` at startup:

```bash
brew install ghostscript tesseract poppler          # macOS
# apt install ghostscript tesseract-ocr poppler-utils   # Debian/Ubuntu
```

- **Ghostscript** (`gs`) — the PDF colour-space fallback.
- **Tesseract** + **Poppler** (`pdftoppm`) — [OCR](#ocr). Tesseract does the recognition; Poppler rasterizes PDF pages so there's an image to recognize. Tesseract alone is enough for image files.

Each is optional: a missing binary disables only its own fallback, and the tool reports why.

## Usage

```bash
# Single file → writes <parent>/markdown/<name>.md
python any_to_markdown.py path/to/slides.pptx

# Folder → recurses all supported files, writes to <folder>/markdown/... mirroring subfolders
python any_to_markdown.py path/to/folder

# URL (e.g. YouTube) → writes to ./markdown/<slug>.md
python any_to_markdown.py "https://www.youtube.com/watch?v=..."

# No args → prompts interactively for a file, folder, or URL
python any_to_markdown.py
```

### Output layout

- **Single file:** `<parent>/markdown/<name>.md`
- **Folder:** output lands under `<folder>/markdown/`, preserving the input's subfolder structure. The tool never descends into its own `markdown/` output tree.
- **URL:** `./markdown/<slug>.md` under the current directory.
- **Name collisions:** if two files in the same folder share a stem (e.g. `report.pdf` and `report.docx`), the first keeps `report.md` and the second gets its extension appended (`report_docx.md`) so neither is silently overwritten.

## How it works

1. Route the input by extension (or treat it as a URL) and run `markitdown` on it.
2. **PDF only:** if extraction errors or returns empty text and Ghostscript is available, re-render to a temporary RGB-flattened copy and retry.
3. **PDFs and images only:** if there's still no text, OCR the file with Tesseract.
4. Classify the result: `converted`, `scanned` (a scan needing OCR, but the OCR binaries are missing), or `failed` (unreadable, or genuinely empty). A batch prints a summary counting each.

An empty result is never written out as a successful conversion — a file that yields no text is always reported as `scanned` or `failed`, so the summary count matches what's actually on disk.

## OCR

When a PDF or image yields no text, it is almost always a scan: a photograph of a page, with no text layer to extract. Those files are passed to [Tesseract](https://github.com/tesseract-ocr/tesseract), which runs locally — nothing is sent to a cloud service or an LLM. PDF pages are rasterized at 300 dpi (Tesseract's recommended input resolution) via Poppler, one page at a time, so a long PDF doesn't have to fit in memory.

OCR applies to every image extension in the table above plus `.pdf`, and it also covers formats `markitdown` can't open at all (`.tiff`, `.bmp`), since Tesseract reads those directly.

Install the [two binaries](#install) to enable it. Without them, scans are reported as `needs OCR` and listed at the end of a batch, naming the missing binary.

Accuracy depends on the scan: clean 300 dpi text is near-perfect, while low-resolution, skewed, or handwritten pages degrade. Spot-check output on a sample before trusting a large batch. For cloud alternatives with different tradeoffs, see [`markitdown-ocr`](https://pypi.org/project/markitdown-ocr/) (LLM vision, needs an API key) or Azure Document Intelligence (`markitdown[az-doc-intel]`).

## Credits

Extraction is powered by [microsoft/markitdown](https://github.com/microsoft/markitdown), and OCR by [tesseract-ocr](https://github.com/tesseract-ocr/tesseract) (via [`pytesseract`](https://github.com/madmaze/pytesseract) and [`pdf2image`](https://github.com/Belval/pdf2image)). This project wraps them with batch handling, idempotent runs, progress output, format routing, and the Ghostscript and OCR fallbacks.

## License

[MIT](LICENSE)
