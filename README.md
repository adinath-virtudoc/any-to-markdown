# pdf-to-markdown

A single-file CLI that converts a PDF — or a whole folder tree of PDFs — to Markdown.

It's a thin, batch-friendly wrapper around Microsoft's
[`markitdown`](https://github.com/microsoft/markitdown) (which does the actual
PDF→Markdown extraction, backed by `pdfminer.six`). On top of that, this tool adds:

- **Batch + recursion** — point it at a folder and it converts every `*.pdf` under it, mirroring the source subfolder structure into an output tree.
- **Idempotent re-runs** — files whose `.md` already exists are skipped, so re-running a large folder only picks up what's new.
- **Live progress** — a spinner + elapsed timer per file (and `[3/25]`-style batch position), with a clean fallback to plain log lines when output isn't a TTY.
- **Ghostscript fallback** — if a PDF's odd colour space (Separation/DeviceN/ICCBased/Lab) yields empty text, it automatically retries on a Ghostscript-normalized (RGB-flattened) copy before giving up. A single bad file never aborts the batch.
- **Quiet output** — silences the noisy-but-harmless `pdfminer`/`pdfplumber` colour-space warnings.

## Install

```bash
pip install markitdown
```

Optional but recommended for the colour-space fallback: [Ghostscript](https://www.ghostscript.com/) (`gs`) on your `PATH`.

```bash
brew install ghostscript   # macOS
# apt install ghostscript  # Debian/Ubuntu
```

## Usage

```bash
# Single file → writes <parent>/markdown/<name>.md
python pdf_to_markdown.py path/to/file.pdf

# Folder → recurses **/*.pdf, writes to <folder>/markdown/... mirroring subfolders
python pdf_to_markdown.py path/to/folder

# No args → prompts interactively for a path
python pdf_to_markdown.py
```

### Output layout

- **Single file:** `<parent>/markdown/<name>.md`
- **Folder:** output lands under `<folder>/markdown/`, preserving the input's subfolder structure. The tool never descends into its own `markdown/` output tree.

## How it works

1. Run `markitdown` on the PDF to extract text/Markdown.
2. If extraction errors or returns empty text, and Ghostscript is available, re-render the PDF to a temporary RGB-flattened copy and retry.
3. If there's still no text, skip that file (logged) and continue the batch.

## Credits

Extraction is powered by [microsoft/markitdown](https://github.com/microsoft/markitdown). This project just wraps it with batch handling, idempotent runs, progress output, and the Ghostscript fallback.

## License

[MIT](LICENSE)
