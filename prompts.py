"""All LLM prompt templates used by the pipeline.

PHASE 1 (glossary build, in entity_extract.py):
  IDENTIFY_PROMPT        - per-segment term identification
  TRANSLATE_TERMS_PROMPT - batched canonical-term translation

PHASE 2 (document translation, in translate.py):
  TRANSLATEGEMMA_PROMPT  - per-chunk translation prompt (translategemma)
  DEFAULT_PROMPT         - per-chunk translation prompt (other models)
  RETRY_VIOLATION_PROMPT - wraps a base prompt with a violation hint

Format variables:
    {source_lang}      - e.g. "Spanish"
    {target_lang}      - e.g. "English"
    {src_code}         - e.g. "es"  (translategemma only)
    {tgt_code}         - e.g. "en"  (translategemma only)
    {glossary_section} - from DomainGlossary.prompt_section() (may be empty)
    {text}             - the text to translate (Phase 2) or to scan (Phase 1a)
    {terms}            - newline-separated "- term" list (Phase 1b only)


How to iterate on a prompt
--------------------------
  1. Edit the template in this file.
  2. Notebook Section 2.4 — live LLM on a single segment. Use this to see what the model emits for your new prompt.
  3. Notebook Section 1.5 — parse a canned LLM response (NO Ollama). Use this to verify the parser still handles your new output format.
  4. Notebook Section 2.5 or 8.1 — full snapshot dump (`dump_dir=`). Use this to diff every per-segment input, prompt, raw response, and parsed result against a previous run.

Each prompt below has a "Parsed by" / "Echo-guarded by" comment listing the downstream code that depends on its output format or distinctive phrasing.
If you change those, the linked code needs updating in the same commit.
"""


# ---- Phase 1 (glossary build) ----------------------------------------------

# Parsed by: entity_extract.py DocumentReviewer._parse_keep_term_lines
# Format: each output line must start with one of
#   KEEP:   <term>                 → single-variant verbatim entry
#   TERM:   <term>                 → single-variant translation candidate
# Multiple source variants on one line are separated by " | " (canonical first).
# If you change these prefixes or the variant separator, update the parser.
IDENTIFY_PROMPT = """\
You are preparing a {source_lang}-to-{target_lang} translation glossary.

Read the {source_lang} passage below and identify specialized terms a translator should track for consistency — \
abbreviations, named organizations, courts, programs, documents, or specialized terms of art.

DO NOT TRANSLATE anything. Output only {source_lang} terms.

For each term found, output exactly ONE line:
  TERM: <term>
  KEEP: <term>

- TERM = needs a consistent {target_lang} translation in the final document
- KEEP = preserve verbatim in the {target_lang} output (personal names, brand names, code identifiers, URLs, LEGAL CASE NAMES)
- LEGAL CASE NAMES (anything formatted as "Caso X", "X vs. Y", "X v. Y", "Caso X vs. Y") ALWAYS go in KEEP, never TERM.
  Example: "Caso Fulano vs. Mengano" → KEEP: Caso Fulano vs. Mengano (do NOT translate)
- One term per line. No explanation. If nothing applies, output nothing.
- Skip common words and unspecialized vocabulary.

Important pairing behavior:

Whenever the passage introduces an abbreviation with its expansion — commonly as "<expansion> (<abbreviation>)" — \
you MUST list BOTH on the SAME line, separated by | , with the FULLEST form first. \
NEVER emit a bare abbreviation alone if its expansion is present in the passage.

Example — if the passage contains:
  "red de observadores comunitarios (ROC)"
You MUST output:
  TERM: red de observadores comunitarios | ROC
NOT:
  TERM: ROC                               (wrong — expansion dropped)
  TERM: red de observadores comunitarios  (wrong — abbreviation dropped)

Same applies to multi-form references — list all known forms of the same entity on one line, fullest first:
  TERM: Ministerio de Recursos de Ruritania | MRR | el Ministerio
  TERM: Tribunal Superior de Ruritania | TSR | el Tribunal

ALL example terms above are invented illustrations. NEVER output an example term unless it actually appears in the passage below.

Passage:
---
{text}
---

Terms:"""


# Parsed by: entity_extract.py DocumentReviewer._translate_terms (via _TERM_LINE_RE)
# Format: each output line must be of the form
#   <source term> → <target translation>
# matching the regex r'^\s*(.+?)\s*→\s*(.+?)\s*$'. If you change the "source → target" format, update _TERM_LINE_RE accordingly.
TRANSLATE_TERMS_PROMPT = """\
Translate each {source_lang} term below to its canonical {target_lang} form.

For abbreviations, give the canonical {target_lang} abbreviation if one is widely established (e.g. ONU → UN). \
Otherwise give the standard {target_lang} rendering used in {target_lang} legal / human-rights / academic writing.

Some terms are followed by "[context: ...]" showing a sentence from the source document where the term appears. \
USE the context to ground the translation, especially for abbreviations. \
If the context shows or implies a definition for an abbreviation, translate using THAT definition, NOT a guess from your prior knowledge.

If a term does not translate (proper noun, brand, identifier), output the term unchanged.

Output ONE line per input term, in this exact format:
  <source term> → <{target_lang} translation>

Do not add explanation, numbering, or extra lines.
Translate ONLY the terms listed below. Never add terms of your own or from the examples above.

Terms:
{terms}

Translations:"""


# ---- Phase 2 (document translation) ----------------------------------------

# Echo-guarded by: translate.py Translator._PROMPT_LEAK
# If you change distinctive phrases here (e.g. "Output ONLY the ... translation"), update _PROMPT_LEAK in translate.py so the echo guard still catches model echoes. 
# The guard is a substring check — match any sentence you'd be unhappy to see echoed verbatim in the model's output.
TRANSLATEGEMMA_PROMPT = """\
You are a professional legal translator working from {source_lang} ({src_code}) to {target_lang} ({tgt_code}), specializing in human rights and public law. Produce fluent, idiomatic {target_lang} using standard terminology from international human rights and legal contexts.

Do not translate word-for-word if it produces unnatural or incorrect {target_lang} — prefer correct legal terminology over literal translation. Preserve structure: a fragment stays a fragment, not a command or full sentence. Do not omit legal qualifiers (e.g., "ex officio").

Rules:
- Preserve inline markdown emphasis exactly: *italic*, **bold**, ***bold-italic***. Do not add markdown the source lacks.
- Preserve snake_case identifiers and ⟪V0⟫ placeholders verbatim.
- Glossary entries are a *meaning* reference only — match the case and formatting of the source text in your output, NOT the case in the glossary entry. If the source says "PERSONAS DEFENSORAS" use ALL CAPS for the translation; if it says "personas defensoras" use lowercase. The glossary tells you WHAT to say, the source tells you HOW to format it.

{glossary_section}

Output ONLY the {target_lang} translation, with no explanation or commentary.

{text}
"""


# Echo-guarded by: translate.py Translator._PROMPT_LEAK
# Same caveat as TRANSLATEGEMMA_PROMPT — update _PROMPT_LEAK if you change distinctive phrasing.
DEFAULT_PROMPT = """\
Translate the {source_lang} text below into {target_lang}. Produce clear, natural, idiomatic {target_lang} using standard professional terminology.

Do not translate word-for-word if it produces unnatural phrasing. Preserve structure: a fragment stays a fragment, not a command or full sentence.

Rules:
- Preserve inline markdown emphasis exactly: *italic*, **bold**, ***bold-italic***. Do not add markdown the source lacks.
- Preserve snake_case identifiers and ⟪V0⟫ placeholders verbatim.
- Glossary entries are a *meaning* reference only — match the case and formatting of the source text in your output, NOT the case in the glossary entry. The glossary tells you WHAT to say, the source tells you HOW to format it.

{glossary_section}

Output ONLY the translation, with no explanation or commentary.

{text}
"""


# Used by: translate.py Translator.translate (the violation-retry path)
# Wraps a base translation prompt with a violation_hint produced by DomainGlossary.retry_hint_with_previous or retry_hint_minimal. 
# The hint header phrases are also in _PROMPT_LEAK.
RETRY_VIOLATION_PROMPT = """\
{original_prompt}

{violation_hint}
"""
