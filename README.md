# any-to-markdown

A single-file CLI that converts documents — a single file, a whole folder tree, or a URL — to Markdown.

It handles **every format [`markitdown`](https://github.com/microsoft/markitdown) supports** — PDF, Office documents, images, audio, web and data files, and URLs. It's a thin, batch-friendly wrapper around Microsoft's `markitdown` (which does the actual extraction, backed by `pdfminer.six` for PDFs). On top of that, this tool adds:

- **Batch + recursion** — point it at a folder and it converts every supported file under it, mirroring the source subfolder structure into an output tree.
- **Idempotent re-runs** — files whose `.md` already exists are skipped, so re-running a large folder only picks up what's new.
- **Live progress** — a spinner + elapsed timer per file (and `[3/25]`-style batch position), with a clean fallback to plain log lines when output isn't a TTY.
- **Scanned-PDF / image detection** — a PDF or image that opens fine but has no text layer is flagged as `needs OCR` and reported separately, rather than being lumped into "failed". (This tool extracts existing text only; it does not OCR — see [OCR](#ocr) below.)
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
pip install 'markitdown[all]'
```

`[all]` pulls in every format's dependencies. To keep it lean, install only what you need — e.g. `pip install 'markitdown[pdf, docx, pptx]'`.

Optional but recommended for the PDF colour-space fallback: [Ghostscript](https://www.ghostscript.com/) (`gs`) on your `PATH`.

```bash
brew install ghostscript   # macOS
# apt install ghostscript  # Debian/Ubuntu
```

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
3. Classify the result: `converted`, `scanned` (PDF/image with no text layer — needs OCR), or `failed` (unreadable, or empty and not an OCR candidate). A batch prints a summary counting each.

## OCR

This tool extracts **existing** text. It does not OCR scanned/image-only PDFs or images — those are flagged `needs OCR` and skipped. To add OCR, use one of:

- [`markitdown-ocr`](https://pypi.org/project/markitdown-ocr/) — third-party plugin using an LLM vision model (needs an API key; sends content to an external LLM).
- Azure Document Intelligence (`markitdown[az-doc-intel]`) — Microsoft's cloud OCR, runnable inside your own Azure tenant.

## Credits

Extraction is powered by [microsoft/markitdown](https://github.com/microsoft/markitdown). This project just wraps it with batch handling, idempotent runs, progress output, format routing, and the Ghostscript fallback.

## License

[MIT](LICENSE)
