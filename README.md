# olladoc

Local `.pdf` / `.docx` translation via [Ollama](https://ollama.com), with an LLM-built terminology glossary.

## Contents

- [Setup](#setup)
- [Run](#run)
- [How it works](#how-it-works)
- [Using the web app](#using-the-web-app)
- [Using the CLI](#using-the-cli)
- [Using the notebook](#using-the-notebook)
- [Output](#output)
- [Modules](#modules)

## Setup

Install Ollama:
- macOS: `brew install ollama`
- Other: download from https://ollama.com/download

Then:

```
ollama serve                    # or launch the menubar app
ollama pull translategemma
pip install -r requirements.txt
```

Default model is Google's [TranslateGemma](https://blog.google/innovation-and-ai/technology/developers-tools/translategemma/) but any Ollama-compatible model can work.

## Run

Web UI:

```
python app_flask.py
```

Open http://localhost:5001. Supports one-shot and two-phase workflows (with an in-app glossary editor between phases), plus Ollama controls: start/stop the server, pick or pull a model, and view logs.

CLI:

```
python translate.py INPUT OUTPUT.docx
```

See [Using the CLI](#using-the-cli) for flags, batch mode, and two-phase workflow.

## How it works

### 1. Every document becomes a list of `Block`s

Format-specific extractors (PDFs via PyMuPDF, DOCX via python-docx) parse the source into a shared, format-agnostic sequence of typed `Block`s: `Heading`, `BodyPara`, `ListItem`, `Footnote`, `Comment`, `ImageBlock`, `TablePlaceholder`, `Separator`. Each block holds one or more `Run`s that carry the actual text plus inline formatting. Downstream stages (glossary building, translation, sanity checks, docx rendering) operate on `Block`s only.

### 2. Translation runs in two phases, mediated by a glossary file

```
             build_glossary                       translate
   source  ────────────────►  glossary.txt  ────────────────►  translated.docx
                                     ▲
                                     └── (optional) human edit
```

- Phase 1, `build_glossary`: `DocumentReviewer` (in `entity_extract.py`) reviews the source in two LLM steps. Step 1a identifies terms per segment (KEEP verbatim vs. TERM to translate). Step 1b batch-translates the canonical terms. The result is a plain-text `{output}_glossary.txt` next to the output path.
- Phase 2, `translate`: `DocumentTranslator` (in `translate.py`) walks the blocks, injects only the glossary entries relevant to each chunk, translates via the LLM, and checks the output against the glossary. On violation it retries with a correction hint. Specialized translators handle tables, footnotes, and comments into sibling docx files.

The glossary file is plain text and can be edited between Phase 1 and Phase 2.

### 3. The web UI, CLI, and notebook all call the same entry point

`translate_document(input, output, ...)` in `translate.py` is the single public entry point. `phases=("build_glossary", "translate")` is the default; pass a subset to run one phase at a time.

```
     app_flask.py ─┐
     translate.py ─┼──►  translate_document(...)  ──►  DocumentReviewer + DocumentTranslator
  test_translate  ─┘
      .ipynb
```

The web UI is a Flask + JS shell around `translate_document`.

## Using the web app

![olladoc web app](olladoc-screenshot.png)

**Ollama status bar (top).** Shows whether `http://localhost:11434` is reachable. Start / Stop control an Ollama process olladoc manages itself; Stop is only enabled for processes it launched. "View logs" tails either olladoc's log or `~/.ollama/logs/server.log`. "Pull a new model" downloads from [ollama.com/library](https://ollama.com/library) with a live log and cancel button.

**Upload and settings.** Drag-and-drop or browse (200 MB per file). Source and target language pickers (default Spanish to English). Model dropdown lists installed Ollama models.

**Workflow modes.**
- One-shot: Phase 1 into Phase 2 without stopping.
- Two-phase: Stop after Phase 1, edit glossary in the browser, then continue to Phase 2.

**Advanced options.** Keep-glossary toggle (on by default) and timestamp-outputs toggle (adds `_YYYY-MM-DD_HHMM` to filenames).

**Output folder.** Where translated `.docx` files land. Defaults to `./translated`.

**Progress.** After clicking Translate, the UI polls `/api/status/<job_id>` every second and streams a log tail: extraction counts, chunk translations, glossary violations, sanity-check warnings.

**Glossary review (two-phase only).** Phase 1 output renders as one editable textarea per document. The format is documented in the file header:

```
TRANSLATE: source → target       (enforced; triggers retry on violation)
KEEP: term                       (kept verbatim, never translated)
PREFER: source → target          (soft hint included in the prompt)
```

Multiple source variants of the same entity go on one line separated by `|`, e.g.:

```
TRANSLATE: Comisión Interamericana | CIDH | la Comisión → Inter-American Commission
```

<!-- SCREENSHOT: glossary review panel. -->

**Results.** Per-file success/failure banner, character and block counts, and a download link per output file.

## Using the CLI

Single file, extension-dispatched (`.pdf` or `.docx`), output is always `.docx`:

```
python translate.py INPUT OUTPUT.docx [--source-lang X] [--target-lang Y] [--model M]
```

Flags for two-phase workflow:

- `--glossary-only`: run Phase 1 only, then stop (lets you edit the glossary)
- `--translate-only`: skip Phase 1, reuse an existing glossary file
- `--force-rebuild`: delete any existing glossary file before Phase 1
- `--no-glossary`: skip the glossary entirely (raw translation)
- `--seed N`: Ollama generation seed (default 42)
- `--archive-glossary PATH`: copy the glossary to PATH after Phase 2 (e.g. `archive/glossary_2026-06-21.txt`)
- `--timestamp`: insert the current timestamp into output filenames so each run produces distinct (docx, glossary) pairs
- `--phases build_glossary translate`: explicit form

Every run appends one JSON line to `<output_dir>/translation_log.jsonl` recording timestamp, input, output, glossary, phases, and model.

**Batch a folder:**

```
python batch_translate.py INPUT_DIR OUTPUT_DIR [--source-lang X] [--target-lang Y] [--model M]
```

Defaults: Spanish to English; model `translategemma`.

## Using the notebook

`test_translate.ipynb` is the development notebook, organized from unit tests to full end-to-end runs:

1. Glossary unit tests
2. Live Ollama sanity checks
3. Debug and inspection tools (single-segment LLM calls, snapshot dumps, extractor output)
4. End-to-end translation (one-shot, two-phase, with snapshots, timestamped)
5. Batch a directory

Sections 2+ require `ollama serve` with a translation model pulled.

## Output

Each input produces up to four sibling docx files (`_tables`, `_footnotes`, `_comments`), written only when the source has content of that type.

## Modules

- [`blocks.py`](blocks.py): shared `Run` / `Block` data model
- [`pdf_extract.py`](pdf_extract.py): `PdfExtractor`
- [`docx_extract.py`](docx_extract.py): `DocxExtractor`
- [`prompts.py`](prompts.py): all LLM prompt templates
- [`glossary.py`](glossary.py): `DomainGlossary` data model + violation checker
- [`entity_extract.py`](entity_extract.py): `DocumentReviewer` (Phase 1)
- [`translate.py`](translate.py): `Translator` + `DocumentTranslator` + `translate_document` entry point (Phase 2)
- [`batch_translate.py`](batch_translate.py): folder batcher
- [`sanity_check.py`](sanity_check.py): post-translation structural diff
- [`app_flask.py`](app_flask.py): Flask web UI backend (paired with [`templates/`](templates/) and [`static/`](static/))
