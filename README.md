# olladoc

**Document translation that runs entirely on your own machine.**

olladoc translates PDF and Word documents using large language models that run right on your computer, via [Ollama](https://ollama.com). Your files are never uploaded anywhere, so even sensitive documents are safe to translate. There are no cloud APIs, accounts, or per-page costs.

Beyond privacy, two other things set it apart from pasting text into an online translator:

- **Terminology stays consistent.** olladoc first reads the whole document and builds a glossary of its names, institutions, and technical terms: which to translate (and how), which to keep verbatim. You can review and edit the glossary before it's applied, and every translated chunk is checked against it.
- **Structure is preserved.** The translation keeps as much of the original structure as it can, including headings, lists, and text formatting. Tables, footnotes, and comments are translated separately and saved as their own files.

Use it from a point-and-click [web UI](#using-the-web-app) or the [command line](#using-the-cli). Developed at the Global Rights Innovation Lab, UC Berkeley.

## Contents

- [Setup](#setup)
- [Run](#run)
- [Using the web app](#using-the-web-app)
- [Using the CLI](#using-the-cli)
- [Using the notebook](#using-the-notebook)
- [Output](#output)
- [Limitations](#limitations)
- [How it works](#how-it-works)
- [Modules](#modules)
- [License](#license)

## Setup

**1. Install Ollama**

- macOS: `brew install ollama`
- Other: download from https://ollama.com/download

**2. Create a Python environment (Python 3.10+)**

```
# venv
python3 -m venv .venv
source .venv/bin/activate
```

```
# conda
conda create -n olladoc python=3.12   # any version >= 3.10 works
conda activate olladoc
```

**3. Install dependencies**

```
pip3 install -r requirements.txt
```

## Run

[Setup](#setup) is one-time. After that, every session takes the same three steps, plus a one-time model download on your very first run.

**1. Activate your environment**

```
source .venv/bin/activate       # or: conda activate olladoc
```

**2. Make sure Ollama is running.** Any one of these works:

- **Desktop app:** likely already running (it auto-starts at login; look for the menubar icon).
- **olladoc itself:** the web UI's status bar has a Start button.
- **Terminal:** run `ollama serve`. It stays in the foreground, so leave that window open.

**First run only:** download the default translation model, Google's [TranslateGemma](https://blog.google/innovation-and-ai/technology/developers-tools/translategemma/). In the web UI, click the Download translategemma button when it appears; from a terminal, run `ollama pull translategemma`. The model is several GB, so this takes a while. Any Ollama-compatible model can also work.

**3. Use the app.** Pick one:

**Option A: web UI**

```
python3 app_flask.py
```

The terminal will print `WARNING: This is a development server`. That's expected for a local app. Open http://localhost:5001. See [Using the web app](#using-the-web-app) for a tour of the interface.

**Option B: CLI**

```
python3 translate.py INPUT OUTPUT.docx
```

See [Using the CLI](#using-the-cli) for flags, batch mode, and two-phase workflow.

## Using the web app

![olladoc web app](olladoc-screenshot.png)

**Ollama status bar (top).** Shows whether `http://localhost:11434` is reachable. Start / Stop control an Ollama process olladoc manages itself; Stop is only enabled for processes it launched. "View logs" tails either olladoc's log or `~/.ollama/logs/server.log`. "Pull a new model" downloads from [ollama.com/library](https://ollama.com/library) with a live log and cancel button.

**Upload and settings.** Drag-and-drop or browse, one file at a time (up to 200 MB). Source and target language pickers (default Spanish to English). Model dropdown lists installed Ollama models.

**Workflow modes.**
- One-shot: Phase 1 (glossary building) straight into Phase 2 (translation) without stopping.
- Two-phase: Stop after Phase 1, edit the glossary in the browser, then continue to Phase 2.

**Advanced options.**
- Translator persona: what kind of material the model is told it is translating (human rights / legal by default, general, or custom), with a preview of the exact prompt.
- Glossary sources: fold in your global glossary (a personal term list stored at `~/.olladoc/global_glossary.txt`, viewable in place) and/or upload a base glossary file for the run. When the same term appears in more than one source, user-provided entries take precedence over automated ones, and the base glossary over the global.
- Timestamp outputs: adds `_YYYY-MM-DD_HHMM` to filenames so repeat runs never overwrite earlier ones.
- Debug snapshots: saves every glossary-building prompt and model response under the output folder.

**Output folder.** Where translated `.docx` files land. Defaults to `./translated`.

**Glossary review (two-phase only).** Phase 1 pauses with the glossary in an editable text box. The format is documented in the file header:

```
TRANSLATE: source → target       (enforced; triggers retry on violation)
KEEP: term                       (kept verbatim, never translated)
PREFER: source → target          (soft hint included in the prompt)
```

Multiple source variants of the same entity go on one line separated by `|`, e.g.:

```
TRANSLATE: Comisión Interamericana | CIDH | la Comisión → Inter-American Commission
```

Variants on one line share a single target. When two forms of the same entity need different renderings (e.g. `CIDH → IACHR` but the full name to the full English name), give each its own line instead. The target side can also list alternates, e.g. `TRANSLATE: la Comisión → the Inter-American Commission | IACHR`: any listed form satisfies the check, and the first is preferred.

If you folded in a global or base glossary, the document's new terms appear first, above entries you already approved, and a notes card points out anything that needs a decision. For example, if the document suggests a different translation for a term than one you provided, the note shows both versions so you can choose which to use. "Add to global glossary" saves reviewed terms for future runs; if any clash with the global's existing entries, a dialog asks which version to keep, term by term.

<!-- SCREENSHOT: glossary review panel. -->

**Results.** Per-file success/failure banner, character and block counts, and a download link per output file.

## Using the CLI

`translate.py` takes exactly one input file (`.pdf` or `.docx`); output is always `.docx`. To translate a whole folder, use [`batch_translate.py`](#batch-a-folder) instead.

```
python3 translate.py INPUT OUTPUT.docx [--source-lang X] [--target-lang Y] [--model M]
```

For example, to translate an English document into Spanish:

```
python3 translate.py doc1_English.docx doc1_Spanish.docx --source-lang English --target-lang Spanish
```

Note that one run can produce **several files**, not just OUTPUT.docx: the glossary (e.g. `doc1_Spanish_glossary.txt`) plus separate docx files for tables, footnotes, and comments if the source has them (see [Output](#output)). They are all written next to OUTPUT.docx.

**Two-phase workflow** (build the glossary, edit it by hand, then translate with it):

```
python3 translate.py informe.docx informe_en.docx --glossary-only
# → writes informe_en_glossary.txt and stops; open it in any text editor
python3 translate.py informe.docx informe_en.docx --translate-only
# → same paths again; picks up your edited informe_en_glossary.txt
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

Other flags:

- `--base-glossary PATH`: fold in an existing glossary as a human-reviewed base; its entries take precedence over automatically extracted ones. This also works for the web app's global glossary: pass `--base-glossary ~/.olladoc/global_glossary.txt`. (Adding entries to the global glossary is done from the web app's review screen.)
- `--domain TEXT`: subject-matter persona for the translation prompt (default "human rights and public law"; pass "" for a general translator)
- `--review-model M`: use a different model for the glossary-building pass (defaults to `--model`)
- `--dump-dir PATH`: save Phase 1 debug snapshots (per-segment prompts, raw responses, parsed results)

Every run appends one JSON line to `<output_dir>/translation_log.jsonl` recording timestamp, input, output, glossary, phases, and model.

**Batch a folder:**

```
python3 batch_translate.py INPUT_DIR OUTPUT_DIR [--source-lang X] [--target-lang Y] [--model M]
```

Defaults: Spanish to English; model `translategemma`.

NOTE: translation keeps the model and app busy for the whole run, and a batch of documents can take hours of sustained CPU/GPU load. Consider running overnight or splitting large batches.

## Using the notebook

`test_translate.ipynb` is the development notebook, organized from unit tests to full end-to-end runs:

1. Glossary unit tests
2. Live Ollama sanity checks
3. Debug and inspection tools (single-segment LLM calls, snapshot dumps, extractor output)
4. End-to-end translation (one-shot, two-phase, with snapshots, timestamped)
5. Batch a directory

## Output

Each input produces up to four sibling docx files (`_tables`, `_footnotes`, `_comments`), written only when the source has content of that type, plus the `_glossary.txt`. Everything lands in the same folder as the output path you specified (web UI default: `./translated`).

## Limitations

- **Translation quality has limits.** olladoc uses small local models for all its tasks. Results are decent for an on-device tool, but not professional-translator quality. Review anything high-stakes before relying on it.
- **One model does two different jobs.** By default, the same translation model both builds the glossary and translates. TranslateGemma (olladoc's default model) was designed purely to translate text, so the glossary-building step asks more of it than it was built for. olladoc compensates with safeguards: terms that never appear in the document are dropped, and broken rules trigger a retry. From the CLI, `--review-model` can hand glossary building to a stronger general-purpose model.
- **Glossary rules are enforced, not guaranteed.** The model is asked to follow the glossary and every chunk is checked afterwards, with a retry on violations.
- **Abbreviations may stay untranslated.** When the model is not sure how an abbreviation is written in the target language (for example, that CIDH becomes IACHR in English), it keeps the abbreviation unchanged rather than guessing. Use the two-phase workflow to supply the correct form yourself.
- **Language pairs vary in quality.** The language list only includes pairs the model was formally tested on, but that testing skews toward English: pairs involving English are the most reliable, and Spanish to English is the pair this tool itself has tested most.
- **Layout preservation is best effort.** Headings, lists, and inline formatting usually carry over. Tables, footnotes, and comments are translated into separate files rather than merged back in, and complex PDF layouts can lose structure.
- **Wrong language settings are not detected.** olladoc trusts your language picks. If they are wrong (for example, an English document with English as the target), it runs anyway rather than warning you.
- **Translation is resource-intensive.** Long documents and batch runs mean sustained CPU/GPU load.

## How it works

At a high level: olladoc reads your document and translates it in two passes. First it scans the whole document and builds a glossary: the names, institutions, and technical terms that must be translated consistently (or kept verbatim). Then it translates chunk by chunk, feeding each chunk the relevant glossary entries and checking that the translation respected them. You can pause between the two passes to review and edit the glossary yourself. Finally, everything is reassembled into a `.docx` that mirrors the original's structure. The rest of this section explains the pipeline in more detail.

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

The glossary file is plain text and can be edited between Phase 1 and Phase 2. If a global or base glossary is supplied, Phase 1 merges it with the terms it finds in the document; user-provided entries take precedence over automated ones, and the saved file lists the newly found terms first so it's clear what still needs checking. Whatever the file contains when Phase 2 starts is what gets enforced.

### 3. The web UI, CLI, and notebook all call the same entry point

`translate_document(input, output, ...)` in `translate.py` is the single public entry point. `phases=("build_glossary", "translate")` is the default; pass a subset to run one phase at a time.

```
     app_flask.py ─┐
     translate.py ─┼──►  translate_document(...)  ──►  DocumentReviewer + DocumentTranslator
  test_translate  ─┘
      .ipynb
```

The web UI is a Flask + JS shell around `translate_document`.

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

## License

[BSD 3-Clause](LICENSE). © 2026 Global Rights Innovation Lab, UC Berkeley.
