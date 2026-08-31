# any-to-markdown

A single-file CLI that converts documents — a single file, a whole folder tree, or a URL — to Markdown.

It handles **every format [`markitdown`](https://github.com/microsoft/markitdown) supports** — PDF, Office documents, images, audio, web and data files, and URLs. PDFs get a dedicated engine, [`pdf-inspector`](https://github.com/firecrawl/pdf-inspector) (Rust, MIT, by Firecrawl), for structure-aware extraction; `markitdown` (backed by `pdfminer.six`) handles the other 20+ formats, and is kept as the PDF fallback when `pdf-inspector` isn't installed or can't produce anything usable. On top of that, this tool adds:

- **Structure-aware PDF extraction** — real Markdown headings and table/column detection instead of flat, unheaded text. See [PDF engine](#pdf-engine) below.
- **Selective per-page OCR** — on a PDF with some scanned pages mixed into a text document, only the flagged pages are rasterized and OCR'd; the pages that already have good text are left alone.
- **Batch + recursion** — point it at a folder and it converts every supported file under it, mirroring the source subfolder structure into an output tree.
- **Idempotent re-runs** — files whose `.md` already exists are skipped, so re-running a large folder only picks up what's new.
- **Live progress** — a spinner + elapsed timer per file (and `[3/25]`-style batch position), with a clean fallback to plain log lines when output isn't a TTY.
- **OCR fallback for scans** — a PDF or image with no text layer is passed to Tesseract locally, so scanned pages convert instead of being skipped. Needs two system binaries; without them the file is reported as `needs OCR` and skipped rather than failing. See [OCR](#ocr) below.
- **Ghostscript fallback** (PDF only, legacy ladder) — if a PDF's odd colour space (Separation/DeviceN/ICCBased/Lab) yields empty text, it automatically retries on a Ghostscript-normalized (RGB-flattened) copy. A single bad file never aborts the batch.
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

Everything past `markitdown` itself is optional, and each piece degrades independently rather than failing the tool:

| Package / binary | Required for | Without it |
|---|---|---|
| `markitdown` | every non-PDF format, plus the PDF fallback ladder | nothing works — this is the one hard dependency |
| `pdf-inspector` (pip, no system binary) | the structure-aware PDF engine — see [PDF engine](#pdf-engine) | every PDF silently takes the old `markitdown`/Ghostscript/whole-file-OCR ladder, exactly as it did before this engine existed |
| `gs` (Ghostscript) | the PDF colour-space retry, legacy ladder only | that retry is skipped; a bad colour space stays empty text |
| `tesseract` | [OCR](#ocr), any format | scans are reported as `needs OCR`/`partial` and skipped, naming the missing binary |
| `pdftoppm` (Poppler) | [OCR](#ocr) for PDFs specifically (rasterizes pages) | same as above, for PDFs only — Tesseract alone is enough for image files |

```bash
brew install ghostscript tesseract poppler          # macOS
# apt install ghostscript tesseract-ocr poppler-utils   # Debian/Ubuntu
```

`pdf-inspector` ships as a self-contained wheel (the extraction engine is compiled Rust) — no extra system binary, no extra setup, just `pip install pdf-inspector` (already in `requirements.txt`).

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

# Force every PDF through the old markitdown-first ladder, skipping pdf-inspector entirely
python any_to_markdown.py --legacy-pdf path/to/folder
```

A bare path with spaces works unquoted (`python any_to_markdown.py path/to/my file.pdf`) — the CLI joins all trailing arguments before parsing `--legacy-pdf` out of them.

### Output layout

- **Single file:** `<parent>/markdown/<name>.md`
- **Folder:** output lands under `<folder>/markdown/`, preserving the input's subfolder structure. The tool never descends into its own `markdown/` output tree.
- **URL:** `./markdown/<slug>.md` under the current directory.
- **Name collisions:** if two files in the same folder share a stem (e.g. `report.pdf` and `report.docx`), the first keeps `report.md` and the second gets its extension appended (`report_docx.md`) so neither is silently overwritten.

## How it works

**PDF-first branch** — every `.pdf`, unless `--legacy-pdf` is passed or `pdf-inspector` isn't installed:

1. Run `pdf-inspector`'s per-page extraction. Each page comes back flagged `needs_ocr` or not.
2. **All pages native** — join their Markdown in page order, write, done. Engine tag: `(pdf-inspector, …)`.
3. **All pages need OCR** — skip `markitdown` and the Ghostscript retry entirely (both are known to pay full-document latency to extract nothing from a pure scan) and go straight to whole-file Tesseract OCR. Engine tag: `(pdf-inspector + OCR, …)`.
4. **Some pages need OCR** (the mixed case) — keep the native pages' Markdown as-is, rasterize and OCR *only* the flagged pages (one `pdftoppm` render per contiguous run of pages, not the whole file), and splice the OCR text back in at the right page position. Engine tag: `(pdf-inspector + OCR, …)`.
5. If this branch can't produce anything usable at all — `pdf-inspector` isn't installed, the call fails outright, or the PDF has zero pages — fall through to the legacy ladder below, unchanged. A zero-page/degenerate PDF is never written out as an empty "converted" file.

**Legacy ladder** — every non-PDF format, always; PDFs too, whenever step 5 above falls through:

6. Route the input by extension (or treat it as a URL) and run `markitdown` on it. Engine tag: `(markitdown, …)`.
7. **PDF only:** if extraction errors or returns empty text and Ghostscript is available, re-render to a temporary RGB-flattened copy and retry.
8. **PDFs and images only:** if there's still no text, OCR the whole file with Tesseract. Engine tag: `(OCR, …)`.
9. Classify the result and print a per-file line naming the engine that actually produced the text. A batch prints a summary counting each status:
   - `converted` — text extracted and written.
   - `partial` — **PDF only.** The native pages `pdf-inspector` found came out fine and were written, but some pages still need OCR and the OCR binaries aren't installed (or OCR itself failed on just those pages). See [PDF engine](#pdf-engine) below.
   - `scanned` — needs OCR, but the OCR binaries are missing.
   - `failed` — unreadable, or genuinely empty after every attempt.

An empty result is never written out as a successful conversion — a file that yields no usable text is always reported as `partial`, `scanned`, or `failed`, so the summary count matches what's actually on disk.

## PDF engine

PDFs are extracted with [`pdf-inspector`](https://github.com/firecrawl/pdf-inspector) first: a Rust engine (MIT licence, by Firecrawl) that returns real per-page Markdown plus a per-page `needs_ocr` flag, instead of one flat text blob.

**Why not just `markitdown` for PDFs too?** Its `pdfminer.six` backend doesn't reconstruct heading structure. [`pdf-inspector`'s own published benchmark](https://github.com/firecrawl/pdf-inspector) (`opendataloader-bench`, 200 PDFs) scores:

| Engine | Overall | Tables | Headings |
|---|---|---|---|
| `markitdown` (pdfminer.six) | 0.589 | 0.273 | **0.000** |
| `pdf-inspector` | 0.875 | 0.814 | 0.788 |

Those are the vendor's numbers, cited here for context. Separately, on a local sample of mixed real-world PDFs (slide decks, technical manuals, reference books) run through both engines for this project, `pdf-inspector` produced real Markdown headings on every text-bearing one (a diagram-only export yielded none, correctly); `markitdown` produced zero headings across the whole sample, and on some files fabricated spurious pipe-tables out of what were actually bullet lists or tables of contents.

**Selective OCR.** When `pdf-inspector` flags some pages of an otherwise-text PDF as scanned (a photographed cover page, a scanned insert), only those pages are rasterized and OCR'd — the pages that already have good text are never re-rendered. When *every* page needs OCR, the tool skips `markitdown`/Ghostscript entirely and goes straight to whole-file OCR, since both are known to spend full-document time extracting nothing from a pure scan.

**Partial conversions.** A mixed PDF whose flagged pages can't be OCR'd (Tesseract/Poppler not installed, or OCR itself errors on just those pages) is written anyway with the native pages present, and the file's status is `partial` rather than `failed` — the text that was extracted cleanly isn't held back for want of the rest. A batch summary lists which files are `partial` and which 1-indexed pages are still missing.

Partial files are **not** automatically retried on a later run — the tool's idempotent-skip logic (see [Output layout](#output-layout)) sees the `.md` already exists and leaves it alone. To pick up the missing pages after installing the OCR binaries, **delete the partial `.md` file(s)** and re-run; that file will then go through the full pipeline again.

**Escape hatch.** Pass `--legacy-pdf` to force every PDF through the old `markitdown`-first ladder, skipping `pdf-inspector` entirely — useful for A/B-comparing the two engines, or working around a `pdf-inspector` regression without uninstalling it.

## OCR

When a PDF or image yields no text, it is almost always a scan: a photograph of a page, with no text layer to extract. Those files (or, for a mixed PDF, just the flagged pages) are passed to [Tesseract](https://github.com/tesseract-ocr/tesseract), which runs locally — nothing is sent to a cloud service or an LLM. PDF pages are rasterized at 300 dpi (Tesseract's recommended input resolution) via Poppler, so a long PDF doesn't have to fit in memory: one render per contiguous run of pages needed, not one render for the whole document.

OCR applies to every image extension in the table above plus `.pdf`, and it also covers formats `markitdown` can't open at all (`.tiff`, `.bmp`), since Tesseract reads those directly.

Install the [binaries](#install) to enable it. Without them, scans are reported as `needs OCR`/`partial` and listed at the end of a batch, naming the missing binary.

Accuracy depends on the scan: clean 300 dpi text is near-perfect, while low-resolution, skewed, or handwritten pages degrade. Spot-check output on a sample before trusting a large batch. For cloud alternatives with different tradeoffs, see [`markitdown-ocr`](https://pypi.org/project/markitdown-ocr/) (LLM vision, needs an API key) or Azure Document Intelligence (`markitdown[az-doc-intel]`).

## Credits

PDF extraction is powered by [firecrawl/pdf-inspector](https://github.com/firecrawl/pdf-inspector); extraction for every other format by [microsoft/markitdown](https://github.com/microsoft/markitdown) (also the PDF fallback); OCR by [tesseract-ocr](https://github.com/tesseract-ocr/tesseract) (via [`pytesseract`](https://github.com/madmaze/pytesseract) and [`pdf2image`](https://github.com/Belval/pdf2image)). This project wraps them with batch handling, idempotent runs, progress output, format routing, per-page OCR routing, and the Ghostscript and OCR fallbacks.

## License

[MIT](LICENSE)
