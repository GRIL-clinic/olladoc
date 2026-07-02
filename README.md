# olladoc

Local `.pdf` / `.docx` translation via [Ollama](https://ollama.com), with an LLM-built terminology glossary.

## Setup

```
ollama pull translategemma
pip install -r requirements.txt
```

## Pipeline

Two phases run by default:

1. build_glossary —  reviews the source document with the LLM and writes a glossary file (`{output}_glossary.txt`) next to the output path.
2. translate — translates the document using that glossary, enforcing terminology rules and triggering targeted retries on violations.

The glossary file is retained after Phase 2 so you can see which terminology rules were applied. To inspect or edit it between phases, run Phase 1 alone, edit the file, then run Phase 2. Pass `keep_glossary=False` (or `--delete-glossary` on the CLI) if you don't want it kept; pass a path to archive a dated copy.

## Usage

Single file — extension-dispatched (`.pdf` or `.docx`), output is always `.docx`:

```
python translate.py INPUT OUTPUT.docx [--source-lang X] [--target-lang Y] [--model M]
```

Flags for two-phase workflow:

- `--glossary-only` — run Phase 1 only, then stop (lets you edit the glossary)
- `--translate-only` — skip Phase 1, reuse an existing glossary file
- `--force-rebuild` — delete any existing glossary file before Phase 1
- `--no-glossary` — skip the glossary entirely (raw translation)
- `--seed N` — ollama generation seed (default 42)
- `--archive-glossary PATH` — copy the glossary to PATH after Phase 2 (e.g. `archive/glossary_2026-06-21.txt`)
- `--timestamp` — insert the current timestamp into output filenames so each run produces distinct (docx, glossary) pairs
- `--phases build_glossary translate` — explicit form

Every run appends one JSON line to `<output_dir>/translation_log.jsonl` recording timestamp, input, output, glossary, phases, and model.

**Batch a folder:**

```
python batch_translate.py INPUT_DIR OUTPUT_DIR [--source-lang X] [--target-lang Y] [--model M]
```

**Web UI:**

```
streamlit run app.py
```

> Note: `app.py` does not yet support the two-phase pipeline.

Defaults: Spanish → English, model `translategemma`.

## Output

Each input produces up to four sibling docx files — `_tables`, `_footnotes`, `_comments` — written only when the source has content of that type.

## Modules

- [`blocks.py`](blocks.py) — shared `Run` / `Block` data model
- [`pdf_extract.py`](pdf_extract.py) — `PdfExtractor` (PyMuPDF)
- [`docx_extract.py`](docx_extract.py) — `DocxExtractor` (python-docx)
- [`prompts.py`](prompts.py) — all LLM prompt templates
- [`glossary.py`](glossary.py) — `DomainGlossary` data model + violation checker
- [`entity_extract.py`](entity_extract.py) — `DocumentReviewer` (Phase 1)
- [`translate.py`](translate.py) — `Translator` + `DocumentTranslator` + `translate_document` entry point (Phase 2)
- [`batch_translate.py`](batch_translate.py) — folder batcher
- [`sanity_check.py`](sanity_check.py) — post-translation structural diff
- [`app.py`](app.py) — Streamlit UI

## Testing and iteration

[`test_translate.ipynb`](test_translate.ipynb) has unit tests (Section 1, no Ollama needed) and live integration tests (Sections 2–8). See `prompts.py` for the "How to iterate on a prompt" guide.

Pass `dump_dir=...` to `translate_document` (or `DocumentReviewer`) to capture every Phase 1 intermediate artifact (per-segment prompts, raw LLM responses, parsed groups, merged state, final entries) for offline debugging.
