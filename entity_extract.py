"""
Document terminology review:
builds a translation glossary from the source document before translation.

Flow:
  1. Split the full document text into segments.
  2. Step 1a — per segment, ask the LLM to identify {source_lang} terms (KEEP / TERM classification).
  Multiple source forms of the same entity (full name + abbreviation + short reference) are grouped onto one line as variants.
  3. Merge groups across segments by canonical (first variant, lowercased).
  4. Step 1b — one batched LLM call translates every canonical TERM to {target_lang}. All variants in the group share that one target.
  Identity translations are demoted to KEEP so they don't trigger spurious 'require' violations later.
  5. Supplement with fast symbolic passes (snake_case identifiers, URLs).
  6. Append any user-supplied glossary at the end, with user entries winning on collision.
  7. Return a DomainGlossary the Translator uses for every chunk.
"""

import json
import ollama
import re
import unicodedata

from pathlib import Path

from blocks import Block, Heading, BodyPara, ListItem, Footnote, Comment
from glossary import DomainGlossary, GlossaryEntry
from prompts import IDENTIFY_PROMPT, TRANSLATE_TERMS_PROMPT


# ---- Snapshot writer (offline debug artifacts) -----------------------------

class _SnapshotWriter:
    """Persists Phase 1 intermediate artifacts to disk for offline debugging.

    Captures both Step 1a (per-segment identification) and Step 1b (batched canonical translation) inputs, prompts, raw LLM responses, parsed results, and the merged state. 
    Disabled (no-op) when dump_dir is None.
    """

    def __init__(self, dump_dir: str | Path | None):
        self.dump_dir = Path(dump_dir) if dump_dir else None
        if self.dump_dir:
            self.dump_dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.dump_dir is not None

    def _write_text(self, name: str, content: str) -> None:
        if self.enabled:
            (self.dump_dir / name).write_text(content, encoding="utf-8")

    def _write_json(self, name: str, obj) -> None:
        if self.enabled:
            (self.dump_dir / name).write_text(
                json.dumps(obj, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def step1a_segment(self, segment_index: int, segment: str, prompt: str,
                       raw_response: str, keep: set[str],
                       translate_groups: list[tuple[str, ...]]) -> None:
        if not self.enabled:
            return
        tag = f"segment_{segment_index:03d}"
        self._write_text(f"{tag}_input.txt", segment)
        self._write_text(f"{tag}_step1a_prompt.txt", prompt)
        self._write_text(f"{tag}_step1a_raw.txt", raw_response)
        self._write_json(f"{tag}_step1a_parsed.json", {
            "keep": sorted(keep),
            "translate_groups": [list(g) for g in translate_groups],
        })

    def step1b_input(self, terms: list[str]) -> None:
        self._write_json("step1b_input.json", terms)

    def step1b_batch(self, batch_idx: int, prompt: str, raw_response: str) -> None:
        if not self.enabled:
            return
        tag = f"step1b_batch_{batch_idx:02d}"
        self._write_text(f"{tag}_prompt.txt", prompt)
        self._write_text(f"{tag}_raw.txt", raw_response)

    def step1b_result(self, translations: dict[str, str]) -> None:
        self._write_json("step1b_parsed.json", translations)

    def step1_merged(self, keep_terms: set[str],
                     groups: dict[str, list[str]]) -> None:
        self._write_json("step1_merged.json", {
            "keep_terms": sorted(keep_terms),
            "groups": dict(groups),
        })

    def review_entries(self, entries: list[GlossaryEntry]) -> None:
        self._write_json("review_entries.json", [
            {"source_terms": e.source_terms, "target": e.target,
             "target_alts": e.target_alts, "kind": e.kind, "note": e.note}
            for e in entries
        ])


# ---- DocumentReviewer ------------------------------------------------------

class DocumentReviewer:
    """Reviews a document for specialized terminology before translation.

    Splits the full document into segments, sends each to the LLM asking it to identify {source_lang} terms,
    then batches one LLM call to translate the canonical of each TERM group to {target_lang}.
    Variant source forms of the same entity are merged across segments and share one target translation.
    """

    # ---- Regex constants ----

    # Matches snake_case identifiers: two or more lowercase/digit words joined by underscores.
    _VAR_RE = re.compile(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b')
    # Matches URLs and common domain-suffix bare hostnames.
    _URL_RE = re.compile(r'https?://\S+|\b\w+\.(org|eu|int|un|gov|net)\b')
    # Trailing parenthetical the LLM emits as part of a source term — the common
    # "Full Name (Abbrev)" definition pattern. Captures (full, abbrev) so the
    # parser can record them as variants of the same entity.
    _TRAILING_PAREN_RE = re.compile(r'^(.+?)\s*\(([^()]{1,40})\)\s*$')
    # Matches Step 1b output lines of the form "source term → target term".
    _TERM_LINE_RE = re.compile(r'^\s*(.+?)\s*→\s*(.+?)\s*$')

    # ---- Static helpers (pure functions used by the class methods below) ----

    @staticmethod
    def _split_parenthetical_variants(term: str) -> tuple[str, ...]:
        """Split 'Full Name (Abbrev)' into ('Full Name', 'Abbrev').

        Returns a one-element tuple if no trailing parenthetical, otherwise both forms as variants. 
        Pure parser cleanup of LLM output — no semantic extraction from source text.
        """
        m = DocumentReviewer._TRAILING_PAREN_RE.match(term.strip())
        if not m:
            return (term.strip(),)
        full = m.group(1).strip()
        abbrev = m.group(2).strip()
        if not full or not abbrev:
            return (term.strip(),)
        return (full, abbrev)

    @staticmethod
    def _fold_key(s: str) -> str:
        """Lowercase + accent-fold for cross-segment matching.
        Used as the canonical-merge key; original-cased variant is preserved as the display form.
        """
        return ''.join(c for c in unicodedata.normalize('NFKD', s.lower())
                       if not unicodedata.combining(c))

    # Connectives kept lowercase when title-casing all-caps phrases.
    _TITLECASE_LOWERCASE_WORDS = {
        "de", "del", "la", "las", "los", "el", "y", "para", "en", "por", "a",
        "o", "u", "con", "sin", "sobre", "al",
        "of", "the", "and", "or", "for", "in", "on", "to", "an", "by",
    }

    @staticmethod
    def _is_all_caps_multiword(s: str) -> bool:
        """True if s is a multi-word phrase with every word all-uppercase."""
        words = s.split()
        alpha_words = [w for w in words if any(c.isalpha() for c in w)]
        return len(alpha_words) >= 2 and all(w == w.upper() for w in alpha_words)

    @staticmethod
    def _title_case_phrase(s: str) -> str:
        """Title-case an all-caps phrase, keeping connectives lowercase and short embedded acronyms (≤4 letters) as-is."""
        out = []
        for i, w in enumerate(s.split()):
            wlower = w.lower()
            if i > 0 and wlower in DocumentReviewer._TITLECASE_LOWERCASE_WORDS:
                out.append(wlower)
            elif sum(c.isalpha() for c in w) <= 4:
                # Likely an embedded acronym (CIDH, IDH, ONU, OEA, etc.)
                out.append(w)
            else:
                out.append(w[0].upper() + w[1:].lower())
        return " ".join(out)

    @staticmethod
    def _normalize_group_case(variants: list[str]) -> list[str]:
        """Normalize ALL-CAPS variants using sibling case as a signal; 
        title-case if any sibling is mixed-case (proper noun), else lowercase.
        Non-all-caps variants pass through unchanged."""
        has_titlecased_sibling = any(
            (not DocumentReviewer._is_all_caps_multiword(v)
             and any(c.isupper() for c in v) and any(c.islower() for c in v))
            for v in variants
        )
        out = []
        for v in variants:
            if not DocumentReviewer._is_all_caps_multiword(v):
                out.append(v)
            elif has_titlecased_sibling:
                out.append(DocumentReviewer._title_case_phrase(v))
            else:
                out.append(v.lower())
        return out

    @staticmethod
    def _looks_like_real_keep(term: str) -> bool:
        """Heuristic: would this term plausibly appear unchanged in target-lang text?

        Used to catch LLM misclassifications where common phrases like 'medios de comunicación' end up in KEEP. 
        Conservative — only filters out things that are clearly ordinary source-language phrases.
        """
        t = term.strip()
        if not t:
            return False
        # URLs, domains, paths — definitely keep
        if "/" in t or "://" in t or DocumentReviewer._URL_RE.search(t):
            return True
        # Code identifiers
        if "_" in t or any(c.isdigit() for c in t):
            return True
        # All-uppercase abbreviations (e.g. "URL", "PDF")
        if t.replace(".", "").isupper() and len(t) <= 6:
            return True
        words = t.split()
        # Single capitalized word — probably a proper name
        if len(words) == 1 and words[0][0].isupper():
            return True
        # Multi-word phrase: at least one word must start uppercase
        # (proper noun phrase like "Inter-American Court")
        if any(w[0].isupper() for w in words):
            return True
        # Otherwise: all-lowercase multi-word phrase — likely an ordinary
        # source-language phrase the LLM wrongly classified
        return False

    @staticmethod
    def _all_text_blocks(blocks: list[Block],
                        include_footnotes_comments: bool = True,
                        include_headings: bool = True) -> list[str]:
        """Flatten a Block list into a list of textual strings.

        `include_footnotes_comments=False` excludes Footnote and Comment blocks from the output.
        `include_headings=False` excludes Heading blocks.
        """
        parts = []
        for b in blocks:
            if isinstance(b, Heading) and include_headings:
                parts.append(b.text)
            elif isinstance(b, BodyPara):
                parts.append(b.text)
            elif isinstance(b, ListItem):
                parts.append(b.title + " " + b.body_text)
            elif isinstance(b, (Footnote, Comment)) and include_footnotes_comments:
                parts.append(b.text)
        return parts

    @staticmethod
    def _symbolic_entries(blocks: list[Block]) -> list[GlossaryEntry]:
        """Extract snake_case variables and URLs as verbatim entries.

        URLs are extracted first and masked out before snake_case scanning so path segments like `/historias_destacadas/` aren't mistaken for data identifiers. 
        URL extraction strips trailing sentence punctuation (.,;:!?)) that the source text often leaves attached, and dedupes case-insensitively so the same URL with different path casing only produces one entry.
        """
        full = "\n".join(DocumentReviewer._all_text_blocks(blocks))
        seen: set[str] = set()
        url_seen_lower: set[str] = set()
        entries: list[GlossaryEntry] = []
        # URLs first — collect, strip trailing punctuation, dedupe case-insensitively
        url_spans: list[tuple[int, int]] = []
        for m in DocumentReviewer._URL_RE.finditer(full):
            url = m.group(0).rstrip('.,;:!?)')
            url_spans.append((m.start(), m.start() + len(url)))
            if not url:
                continue
            low = url.lower()
            if low in url_seen_lower:
                continue
            url_seen_lower.add(low)
            seen.add(url)
            entries.append(GlossaryEntry([url], url, kind="verbatim"))
        # Mask URL spans before snake_case scan so URL path segments don't get captured as data identifiers
        masked_chars = list(full)
        for start, end in url_spans:
            for i in range(start, min(end, len(masked_chars))):
                masked_chars[i] = ' '
        masked = ''.join(masked_chars)
        for m in DocumentReviewer._VAR_RE.finditer(masked):
            var = m.group(1)
            if var not in seen:
                seen.add(var)
                entries.append(GlossaryEntry([var], var, kind="verbatim"))
        return entries

    @staticmethod
    def _find_context_for_term(term: str, source_text: str,
                               max_chars: int = 200) -> str | None:
        """Find `term` in `source_text` and return surrounding context.

        Returns up to `max_chars` characters of the source containing the first case-insensitive occurrence of the term, trimmed to sentence boundaries where possible. 
        Returns None if the term isn't found.

        Used by Step 1b to ground abbreviation translations in the document's actual usage.
        """
        if not term or not source_text:
            return None
        lower_source = source_text.lower()
        lower_term = term.lower()
        idx = lower_source.find(lower_term)
        if idx == -1:
            return None

        # Trim back to a sentence start (or paragraph boundary). 
        # Fall back to a word boundary so we don't start the context mid-word.
        half = max_chars // 2
        start = max(0, idx - half)
        found_boundary = False
        for boundary in (". ", "! ", "? ", "\n"):
            pos = source_text.rfind(boundary, start, idx)
            if pos != -1:
                start = pos + len(boundary)
                found_boundary = True
                break
        if not found_boundary and start > 0:
            # Move forward to the FIRST space (preserve max context; just avoid starting mid-word).
            pos = source_text.find(" ", start, idx)
            if pos != -1:
                start = pos + 1

        # Trim forward to a sentence end (with same word-boundary fallback)
        end = min(len(source_text), idx + len(term) + half)
        found_boundary = False
        for boundary in (". ", "! ", "? ", "\n"):
            pos = source_text.find(boundary, idx + len(term), end)
            if pos != -1:
                end = pos + 1
                found_boundary = True
                break
        if not found_boundary and end < len(source_text):
            pos = source_text.rfind(" ", idx + len(term), end)
            if pos != -1:
                end = pos

        context = source_text[start:end].strip().replace("\n", " ")
        if len(context) > max_chars:
            context = context[:max_chars - 3] + "..."
        # Quotes inside context would confuse the prompt format
        # replace double-quotes with single quotes since we wrap in double-quotes
        return context.replace('"', "'")


    def __init__(self, model: str, source_lang: str, target_lang: str,
                 segment_chars: int = 6_000,
                 term_batch_size: int = 40,
                 dump_dir: str | Path | None = None,
                 seed: int | None = 42):
        self.model = model
        self.source_lang = source_lang
        self.target_lang = target_lang
        # Step 1a sends this many chars per LLM call (one call per segment).
        self.segment_chars = segment_chars
        # Step 1b sends this many TERM canonicals per LLM call.
        self.term_batch_size = term_batch_size
        # When dump_dir is set, intermediate artifacts are written to disk for offline debugging.
        # See _SnapshotWriter for the layout.
        self._snap = _SnapshotWriter(dump_dir)
        # When set, passed to ollama as `options.seed` so Step 1a/1b output is reproducible across runs
        self.seed = seed

    @property
    def dump_dir(self) -> Path | None:
        """Read-only view of where snapshots are being written (or None)."""
        return self._snap.dump_dir

    # ---- segmentation ----

    # Inline footnote / comment placeholder markers (e.g. ‹FN67›, ‹C2›) to omit from the glossary
    _INLINE_REF_MARKER_RE = re.compile(r'‹(?:FN|C)\d+›')

    def _segment_text(self, blocks: list[Block]) -> list[str]:
        """Split document text into segments, breaking on paragraph boundaries.

        Step 1a sees only body paragraphs / list items.
        Headings, footnotes, and comments are excluded;
        headings tend to be all-caps section labels with generic vocabulary that produce noisy glossary entries; 
        real entity names also appear in body text with better context for Step 1b.
        Inline ‹FN…› / ‹C…› placeholder markers are stripped so the LLM doesn't pick them up as terms.
        """
        parts = [self._INLINE_REF_MARKER_RE.sub('', p)
                 for p in self._all_text_blocks(blocks,
                                                include_footnotes_comments=False,
                                                include_headings=False)
                 if p.strip()]
        segments: list[str] = []
        current: list[str] = []
        current_len = 0
        for part in parts:
            if current and current_len + len(part) + 1 > self.segment_chars:
                segments.append("\n".join(current))
                current = []
                current_len = 0
            current.append(part)
            current_len += len(part) + 1
        if current:
            segments.append("\n".join(current))
        return segments

    # ---- Step 1a (per-segment identification) ----

    def _identify_segment(self, segment: str, segment_index: int | None = None) -> tuple[set[str], list[tuple[str, ...]]]:
        """LLM step 1a. Returns (keep_terms, translate_groups); each group tuples variants of one entity, canonical first."""
        _, keep, groups = self._identify_segment_with_raw(segment, segment_index)
        return keep, groups

    def identify_segment_debug(self, segment: str) -> tuple[str, set[str], list[tuple[str, ...]]]:
        """Same as `_identify_segment` but also returns the raw LLM text. For notebook prompt iteration."""
        return self._identify_segment_with_raw(segment, segment_index=None)

    def _identify_segment_with_raw(self, segment: str, segment_index: int | None) -> tuple[str, set[str], list[tuple[str, ...]]]:
        prompt = IDENTIFY_PROMPT.format(source_lang=self.source_lang, target_lang=self.target_lang, text=segment)
        options = {"temperature": 0.1, "num_predict": 512}
        if self.seed is not None:
            options["seed"] = self.seed
        resp = ollama.chat(model=self.model, messages=[{"role": "user", "content": prompt}], options=options)
        text = resp["message"]["content"].strip()

        keep, groups = self._parse_keep_term_lines(text)
        if segment_index is not None:
            self._snap.step1a_segment(segment_index, segment, prompt, text, keep, groups)

        if not keep and not groups:
            preview = text if len(text) <= 400 else text[:400] + "...[truncated]"
            print(f"  [entity_extract] WARN: segment produced 0 parsed terms. Raw LLM response:\n---\n{preview}\n---")

        return text, keep, groups

    # Common abbreviations whose trailing "." is NOT a sentence boundary.
    # Stripped before the "intra-sentence period" prose check so legal citations like "Cajar vs. Colombia" survive.
    _SAFE_ABBREV_RE = re.compile(
        r'\b(?:vs|v|cf|etc|i\.e|e\.g|Sr|Sra|Sres|Sras|Dr|Dra|Mr|Mrs|Ms|No|Art|Inc|Ltd|St)\.',
        re.IGNORECASE,
    )

    @staticmethod
    def _looks_like_term_candidate(line: str) -> bool:
        """Heuristic: does this bare (no-prefix) line look like a term?

        Used as a fallback when the LLM emits terms without the KEEP:/TERM: prefix
        Conservative: only accepts lines that look like proper nouns, abbreviations, named entities, or identifiers
        """
        s = line.strip()
        if not s or len(s) < 2:
            return False
        # Sentence-ending punctuation suggests prose not a term
        # Strip trailing brackets/quotes so wrapped forms like "(...sentence.)" or "...sentence.\"" are still caught.
        if s.rstrip(")]}»›\"'").endswith(("?", "!", ".")):
            return False
        # A ". " in the middle suggests multi-sentence prose 
        # But strip common abbreviations first so "Cajar vs. Colombia" doesn't trigger it
        s_check = DocumentReviewer._SAFE_ABBREV_RE.sub('', s)
        if ". " in s_check:
            return False
        # Must have SOMETHING distinguishing it from random lowercase words —
        # a capital letter (proper noun), underscore (code identifier), or internal period (URL-like).
        if not any(c.isupper() for c in s) and "_" not in s and "." not in s:
            return False
        return True

    @staticmethod
    def _clean_kt_content(content: str) -> list[str]:
        """Clean a KEEP/TERM content string into a list of variant strings.

        Handles four LLM output issues:
        - Trailing or leading '|' (strip).
        - '→ target' suffix that snuck in despite the "no translation" rule in Step 1a.
        - 'X: Y' definition syntax (the LLM sometimes writes 'KEEP: X: Y'
          to mean "X has the definition Y" — we treat as two variants).
        - Within-group duplicates (case-insensitive dedup).
        """
        s = content.strip().strip("|").strip()
        if not s:
            return []
        # Defensive: if the LLM still emitted a "→ target", drop the target —
        # Step 1a is identification only. Step 1b handles translation.
        if "→" in s:
            s = s.split("→", 1)[0].strip().strip("|").strip()
        if not s:
            return []
        # "X: Y" acts as a variant separator when there's no | on the line.
        # "X: X" (self-annotation like "BLUEPRINT: blueprint") always collapses to the right side
        def _collapse_self_annotation(v: str) -> str:
            if ": " in v:
                left, _, right = v.partition(": ")
                if left.strip().lower() == right.strip().lower():
                    return right.strip()
            return v

        if "|" not in s and ": " in s and _collapse_self_annotation(s) == s:
            s = s.replace(": ", " | ", 1)
        raw_variants = [_collapse_self_annotation(v.strip())
                        for v in s.split("|") if v.strip()]
        out: list[str] = []
        seen_lower: set[str] = set()
        # Reserved prompt tokens the LLM sometimes echoes as variants ("KEEP:", etc.)
        reserved = {"keep", "term", "translate", "prefer", "pairing rule", "important pairing behavior"}
        for rv in raw_variants:
            for sub in DocumentReviewer._split_parenthetical_variants(rv):
                sub = sub.strip().strip('"').strip("'").strip().rstrip(":.,;")
                if not sub:
                    continue
                # Strip a leading reserved prefix: "PAIRING RULE: Corte..." → "Corte..."
                if ":" in sub:
                    prefix, _, rest = sub.partition(":")
                    if prefix.strip().lower() in reserved and rest.strip():
                        sub = rest.strip()
                low = sub.lower()
                if low in reserved or low in seen_lower:
                    continue
                if len(sub.split()) > 18:  # too long; looks like prose
                    continue
                out.append(sub)
                seen_lower.add(low)
        return out

    # Common Spanish + English stopwords ignored when computing pairwise word overlap for the list-detection heuristic.
    _OVERLAP_STOPWORDS = {
        "de", "del", "la", "las", "los", "el", "y", "para", "en", "por", "a", "o", "u", "con", "sin", "sobre", "al",
        "of", "the", "and", "or", "for", "in", "on", "to", "a", "an", "by",
    }

    @staticmethod
    def _looks_like_acronym(s: str) -> bool:
        """True if s is a short, all-uppercase, no-spaces token (e.g. CIDH, OG)."""
        s = s.strip()
        if " " in s or "\t" in s:
            return False
        letters = [c for c in s if c.isalpha()]
        return bool(letters) and 2 <= len(letters) <= 7 and all(c.isupper() for c in letters)

    @staticmethod
    def _looks_like_entity_list(variants: list[str]) -> bool:
        """Heuristic: does this group look like a `|`-separated LIST of distinct entities rather than VARIANTS of one entity?

        Triggers when:
        - 3+ variants, AND
        - no variant is a short all-caps acronym (acronym presence is a strong signal of "abbreviation + expansion + short-form"), AND
        - no pair of variants shares any non-stopword (real variants of one entity typically share at least one significant word).
        """
        if len(variants) < 3:
            return False
        if any(DocumentReviewer._looks_like_acronym(v) for v in variants):
            return False
        def words(v: str) -> set[str]:
            tokens = re.findall(r'\w+', v.lower())
            return {t for t in tokens if t not in DocumentReviewer._OVERLAP_STOPWORDS and len(t) > 1}
        word_sets = [words(v) for v in variants]
        for i in range(len(word_sets)):
            for j in range(i + 1, len(word_sets)):
                if word_sets[i] & word_sets[j]:
                    return False
        return True

    @staticmethod
    def _append_or_split(translate_groups: list[tuple[str, ...]],
                         variants: list[str]) -> None:
        """Append `variants` to translate_groups, splitting into singletons if it looks like a list of distinct entities rather than variants."""
        if DocumentReviewer._looks_like_entity_list(variants):
            for v in variants:
                translate_groups.append((v,))
        else:
            translate_groups.append(tuple(variants))

    @staticmethod
    def _parse_keep_term_lines(text: str) -> tuple[set[str], list[tuple[str, ...]]]:
        """Parse the raw KEEP/TERM/TRANSLATE lines, plus bare-line fallback."""
        keep: set[str] = set()
        translate_groups: list[tuple[str, ...]] = []

        def already_known(variant_lower: str) -> bool:
            """True if a variant is already in any KEEP or TERM group (case-insensitive)."""
            if any(variant_lower == k.lower() for k in keep):
                return True
            return any(variant_lower == v.lower()
                       for g in translate_groups for v in g)

        for line in text.splitlines():
            line = line.strip().lstrip("-* ").strip()
            if not line or line.startswith("#"):
                continue
            upper = line.upper()
            if upper.startswith("KEEP:"):
                content = line.split(":", 1)[1].strip()
                for variant in DocumentReviewer._clean_kt_content(content):
                    keep.add(variant)
            elif upper.startswith("TERM:") or upper.startswith("TRANSLATE:"):
                content = line.split(":", 1)[1].strip()
                variants = DocumentReviewer._clean_kt_content(content)
                # Drop self-referential groups (e.g. "PDDH | PDDH")
                if variants:
                    DocumentReviewer._append_or_split(translate_groups, variants)
            elif DocumentReviewer._looks_like_term_candidate(line):
                # Bare-line fallback: model ignored the KEEP:/TERM: format but emitted a string that looks like a term.
                # Route through _clean_kt_content so the same cleanups apply.
                # All variants from one line are kept as a SINGLE multi-variant group.
                variants = DocumentReviewer._clean_kt_content(line)
                new = [v for v in variants if not already_known(v.lower())]
                if new:
                    DocumentReviewer._append_or_split(translate_groups, new)

        return keep, translate_groups

    # ---- Step 1b (batched canonical translation) ----

    def _translate_terms(self, terms: list[str],
                         source_text: str | None = None) -> dict[str, str]:
        """Translate source-language terms to target language in batches.

        When `source_text` is provided, each term is annotated with the sentence around its first occurrence in the source.
        This grounds the translation in the document's actual usage.

        Returns {source_term: target_term}.
        Terms the LLM fails to translate (or returns unchanged) are omitted.
        """
        if not terms:
            return {}
        result: dict[str, str] = {}
        self._snap.step1b_input(terms)

        for batch_idx, start in enumerate(range(0, len(terms), self.term_batch_size)):
            batch = terms[start:start + self.term_batch_size]
            # If we have source text, annotate each term with a short context snippet from where it first appears
            lines = []
            for t in batch:
                ctx = (self._find_context_for_term(t, source_text)
                       if source_text else None)
                if ctx:
                    lines.append(f'- {t} [context: "{ctx}"]')
                else:
                    lines.append(f"- {t}")
            terms_block = "\n".join(lines)
            prompt = TRANSLATE_TERMS_PROMPT.format(
                source_lang=self.source_lang,
                target_lang=self.target_lang,
                terms=terms_block,
            )
            try:
                options = {"temperature": 0.1, "num_predict": 1024}
                if self.seed is not None:
                    options["seed"] = self.seed
                resp = ollama.chat(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    options=options,
                )
            except Exception as e:
                print(f"  [entity_extract] term-translation batch failed: {e}")
                continue

            text = resp["message"]["content"].strip()
            self._snap.step1b_batch(batch_idx, prompt, text)

            # Map by lowercased source for tolerant matching
            wanted = {t.lower(): t for t in batch}
            for line in text.splitlines():
                line = line.strip().lstrip("-* ").strip()
                m = self._TERM_LINE_RE.match(line)
                if not m:
                    continue
                src_out, tgt_out = m.group(1).strip(), m.group(2).strip()
                key = src_out.lower()
                if key in wanted and tgt_out:
                    result[wanted[key]] = tgt_out

        self._snap.step1b_result(result)
        return result

    # ---- review() helpers ----

    def _identify_terms_in_segments(self, segments: list[str]
                        ) -> tuple[set[str], dict[str, list[str]], dict[tuple[str, str], int]]:
        """Run Step 1a per segment and union the results.

        Returns (keep_terms, group_by_canonical, variant_support), where:
        - group_by_canonical maps lowercased-canonical → list of source variants for that entity.
        - variant_support maps (canonical_lower, variant_lower) → number of segments that paired them.
          Used by the consolidator to detect LLM grouping errors when one segment places a variant under a different canonical than the majority do.
        """
        keep_terms: set[str] = set()
        group_by_canonical: dict[str, list[str]] = {}
        variant_support: dict[tuple[str, str], int] = {}
        for i, segment in enumerate(segments):
            try:
                k, segment_groups = self._identify_segment(segment, segment_index=i)
            except Exception as e:
                print(f"  [entity_extract] segment {i+1} failed: {e} — skipping")
                continue
            keep_terms |= k
            for variants in segment_groups:
                canonical_key = self._fold_key(variants[0])
                existing = group_by_canonical.setdefault(canonical_key, [])
                seen_variants = {self._fold_key(v) for v in existing}
                for v in variants:
                    vkey = self._fold_key(v)
                    variant_support[(canonical_key, vkey)] = \
                        variant_support.get((canonical_key, vkey), 0) + 1
                    if vkey not in seen_variants:
                        existing.append(v)
                        seen_variants.add(vkey)
        return keep_terms, group_by_canonical, variant_support

    @staticmethod
    def _reclassify_misclassified_keeps(
            keep_terms: set[str],
            groups: dict[str, list[str]],) -> tuple[set[str], dict[str, list[str]]]:
        """Demote KEEPs that look like ordinary source-language phrases.

        Otherwise the translator's correct rendering will trip the verbatim violation check. 
        Moved entries become standalone TERM groups; if a group already exists for that canonical, the misclassified term is silently absorbed (the existing group "wins").
        """
        misclassified = {t for t in keep_terms if not DocumentReviewer._looks_like_real_keep(t)}
        if not misclassified:
            return keep_terms, groups
        keep_terms = keep_terms - misclassified
        for t in misclassified:
            key = t.lower()
            if key not in groups:
                groups[key] = [t]
        print(f"  [entity_extract] reclassified {len(misclassified)} KEEP → TERM (ordinary phrases): {sorted(misclassified)}")
        return keep_terms, groups

    @staticmethod
    def _resolve_keep_translate_conflicts(
            keep_terms: set[str],
            groups: dict[str, list[str]],) -> tuple[set[str], dict[str, list[str]]]:
        """Make KEEPs and TRANSLATE groups disjoint. On conflict, the TRANSLATE group always wins.
        Its variants stay, translation will be produced by Step 1b, and the conflicting KEEP is dropped.
        The TRANSLATE group carries more information (it has the entity's variant set linked together), so we let it win and drop the redundant KEEP.
        """
        variant_lowers = {v.lower() for variants in groups.values() for v in variants}
        absorbed = {kt for kt in keep_terms if kt.lower() in variant_lowers}
        if absorbed:
            keep_terms -= absorbed
            print(f"  [entity_extract] absorbed {len(absorbed)} KEEP(s) into existing TRANSLATE groups: {sorted(absorbed)}")
        return keep_terms, groups

    @staticmethod
    def _consolidate_groups_by_shared_variants(
            groups: dict[str, list[str]],
            variant_support: dict[tuple[str, str], int] | None = None,
            ) -> dict[str, list[str]]:
        """Merge groups whose variant sets overlap (case-insensitive).

        Across many segments, the LLM emits the same entity with different canonical-first variants
        (e.g. "Corte Interamericana de Derechos Humanos | Corte IDH" in one segment, "Corte IDH | Corte" in another, "CORTES INTERAMERICANAS..." in a heading-derived segment)
        Each distinct first-variant-lowercased becomes its own dict key, so the cross-segment merger doesn't unify them.

        This pass walks the groups and union-merges any two whose variant lists share at least one element (case-insensitive).
        The merged group's canonical is the LONGEST variant (most context for Step 1b).

        Cross-segment voting (when variant_support is supplied):
        Before unioning, check per-segment support for each shared variant.
        If one group has only 1 segment of support and another has >=3 segments, the singleton is treated as an LLM grouping error: 
        drop the variant from the minority group and don't union via it.
        Example: 5 segments grouped CIDH with Comisión but 1 segment grouped it with Corte. Voting keeps CIDH with Comisión and drops it from Corte.
        """
        # Build canonical → group items list
        items = list(groups.items())  # list of (canonical_key, variants)
        # Union-find by index
        parent = list(range(len(items)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        # Map each variant (accent/case-folded) to all group indices that contain it.
        variant_to_groups: dict[str, list[int]] = {}
        for i, (_key, variants) in enumerate(items):
            for v in variants:
                variant_to_groups.setdefault(DocumentReviewer._fold_key(v), []).append(i)

        # Cross-segment voting: drop singleton variant assignments that contradict a >=3-segment majority.
        # Conservative threshold - we only overrule a grouping when the alternative has strong consensus.
        dropped: dict[int, set[str]] = {}  # group_idx -> variant_lowers to drop
        if variant_support is not None:
            for v_lower, indices in variant_to_groups.items():
                unique_groups = set(indices)
                if len(unique_groups) < 2:
                    continue
                supports = {idx: variant_support.get((items[idx][0], v_lower), 0)
                            for idx in unique_groups}
                max_support = max(supports.values())
                if max_support < 3:
                    continue
                for idx, count in supports.items():
                    if count == 1 and count < max_support:
                        dropped.setdefault(idx, set()).add(v_lower)
            if dropped:
                total = sum(len(s) for s in dropped.values())
                print(f"  [entity_extract] cross-segment voting dropped {total} singleton variant assignment(s) contradicting >=3-segment majority")
                # Rebuild variant_to_groups without the dropped (idx, variant) pairs
                new_v2g: dict[str, list[int]] = {}
                for v_lower, indices in variant_to_groups.items():
                    kept = [i for i in indices if v_lower not in dropped.get(i, set())]
                    if kept:
                        new_v2g[v_lower] = kept
                variant_to_groups = new_v2g

        # For any variant that still appears in multiple groups, union those groups.
        for indices in variant_to_groups.values():
            if len(set(indices)) > 1:
                base = indices[0]
                for idx in indices[1:]:
                    union(base, idx)

        # Collect merged groups by root, skipping variants dropped by voting.
        # Dedupe by accent/case-folded key
        merged: dict[int, list[str]] = {}
        for i, (_key, variants) in enumerate(items):
            root = find(i)
            target = merged.setdefault(root, [])
            seen = {DocumentReviewer._fold_key(v) for v in target}
            for v in variants:
                vkey = DocumentReviewer._fold_key(v)
                if vkey in dropped.get(i, set()):
                    continue
                if vkey not in seen:
                    target.append(v)
                    seen.add(vkey)

        # For each merged set, pick the longest variant as the new canonical
        # (it has the most context for Step 1b) and rebuild the dict keyed by that canonical (accent/case-folded).
        # Skip empty merged groups (every variant was dropped by voting).
        out: dict[str, list[str]] = {}
        for variants in merged.values():
            if not variants:
                continue
            # Sort by length descending, but preserve order otherwise
            canonical = max(variants, key=len)
            # Put canonical first, the rest in their original order
            ordered = [canonical] + [v for v in variants if v != canonical]
            out[DocumentReviewer._fold_key(canonical)] = ordered

        if len(out) < len(items):
            print(f"  [entity_extract] consolidated {len(items)} groups "
                  f"into {len(out)} by shared variants")
        return out

    @staticmethod
    def _build_entries(keep_terms: set[str],
                       groups: dict[str, list[str]],
                       translations: dict[str, str]) -> tuple[list[GlossaryEntry], int]:
        """Build GlossaryEntry objects from the merged Step 1a/1b state.

        Returns (entries, num_demoted) where num_demoted counts identity translations that were rewritten to verbatim entries.
        """
        entries: list[GlossaryEntry] = []
        seen: set[str] = set()

        for term in sorted(keep_terms):
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            entries.append(GlossaryEntry([term], term, kind="verbatim"))

        demoted = 0
        dropped_to_prefer = 0
        for canonical_key, variants in groups.items():
            canonical = variants[0]
            if canonical_key in seen:
                continue
            seen.add(canonical_key)
            target = translations.get(canonical)
            if target and target.lower() != canonical.lower():
                entries.append(GlossaryEntry(list(variants), target, kind="require"))
            elif target and target.lower() == canonical.lower():
                # Identity translation - the term doesn't change between languages (proper noun, acronym kept verbatim)
                entries.append(GlossaryEntry(list(variants), canonical, kind="verbatim"))
                demoted += 1
            else:
                entries.append(GlossaryEntry(list(variants), canonical, kind="prefer"))
                dropped_to_prefer += 1
        if dropped_to_prefer:
            print(f"  [entity_extract] {dropped_to_prefer} term(s) untranslated by step 1b "
                  f"— marked as PREFER (soft guidance, no enforcement)")
        return entries, demoted

    # ---- review() orchestrator ----

    def extract_terms(self, blocks: list[Block]) -> list[GlossaryEntry]:
        """Run Step 1a (per-segment identification) and Step 1b (batched translation of canonicals) and return the resulting GlossaryEntry list."""
        segments = self._segment_text(blocks)
        if not segments:
            return []

        print(f"  [entity_extract] identifying terms in {len(segments)} segment(s) ...")

        keep_terms, groups, variant_support = self._identify_terms_in_segments(segments)
        keep_terms, groups = self._reclassify_misclassified_keeps(keep_terms, groups)
        keep_terms, groups = self._resolve_keep_translate_conflicts(keep_terms, groups)
        # Collapse duplicate entities the cross-segment merge missed (e.g. same entity emitted with different canonical-first variants per segment).
        groups = self._consolidate_groups_by_shared_variants(groups, variant_support)
        # Normalize ALL-CAPS variants: title-case if any sibling is title-cased (proper noun signal), else lowercase.
        groups = {k: self._normalize_group_case(variants)
                  for k, variants in groups.items()}

        print(f"  [entity_extract] identification complete: {len(keep_terms)} KEEP, {len(groups)} TERM group(s)")
        self._snap.step1_merged(keep_terms, groups)

        canonicals = [variants[0] for variants in groups.values()]
        if canonicals:
            print(f"  [entity_extract] translating {len(canonicals)} canonical term(s) to {self.target_lang} ...")
            # Concatenate all segments so Step 1b can find context for each term
            full_source_text = "\n".join(segments)
            translations = self._translate_terms(canonicals, source_text=full_source_text)
        else:
            translations = {}

        entries, demoted = self._build_entries(keep_terms, groups, translations)
        if demoted:
            print(f"  [entity_extract] demoted {demoted} identity translation(s) to KEEP")
        print(f"  [entity_extract] {len(entries)} total entries")
        self._snap.review_entries(entries)
        return entries

    # ---- final glossary merging ----

    @staticmethod
    def _merge_into_final_glossary(
            primary_entries: list[GlossaryEntry],
            symbolic_entries: list[GlossaryEntry],
            user_glossary: DomainGlossary | None,
            log_label: str,) -> DomainGlossary:
        """Merge LLM/loaded entries + symbolic supplement + user glossary.

        Order in the final glossary: primary entries first, then any symbolic entries that don't collide, then user-provided entries at the end.
        """
        user_sources: set[str] = set()
        if user_glossary:
            for entry in user_glossary._entries:
                for t in entry.source_terms:
                    user_sources.add(t.lower())

        kept: list[GlossaryEntry] = []
        seen_sources: set[str] = set(user_sources)
        for entry in primary_entries + symbolic_entries:
            if any(t.lower() in seen_sources for t in entry.source_terms):
                continue
            kept.append(entry)
            for t in entry.source_terms:
                seen_sources.add(t.lower())

        user_entries = user_glossary._entries if user_glossary else []
        combined = DomainGlossary(kept + user_entries)
        print(f"  [entity_extract] {log_label}: {len(kept)} primary + {len(user_entries)} user-provided = {len(combined._entries)} total entries")
        return combined

    # ---- build_glossary (the public entry point) ----

    def build_glossary(self, blocks: list[Block],
                       user_glossary: DomainGlossary | None = None,
                       glossary_path: str | Path | None = None) -> DomainGlossary:
        """Build a document-specific glossary from `blocks`.

        This is the public entry point for Phase 1 of the pipeline (called when `translate_document(..., phases=...)` includes "build_glossary").

        Two paths, depending on whether the glossary file already exists:

        - File exists: load it (preserving any user edits to the file itself) instead of re-running the LLM review. 
          New symbolic entries for snake_case identifiers and URLs in the document are added if they don't already appear in the file.

        - File doesn't exist (or no path given): run `extract_terms()` to produce LLM-derived entries (Step 1a + Step 1b), then add the symbolic supplement.

        In both paths the result is then merged with `user_glossary`:
          - User-provided entries are appended at the end of the final list
          - On collision (same source term), the user-provided entry wins

        If `glossary_path` is provided, the result is written there. 
        To skip the auto-load behavior on a re-run, delete the file first or pass `force_rebuild=True` to the top-level `translate_document`.
        """
        sym_entries = self._symbolic_entries(blocks)

        if glossary_path and Path(glossary_path).exists():
            print(f"  [entity_extract] loading existing glossary from {glossary_path}")
            loaded = DomainGlossary.load(glossary_path)
            combined = self._merge_into_final_glossary(
                loaded._entries, sym_entries, user_glossary,
                log_label="loaded glossary",
            )
            return combined

        llm_entries = self.extract_terms(blocks)
        combined = self._merge_into_final_glossary(
            llm_entries, sym_entries, user_glossary,
            log_label="glossary",
        )

        if glossary_path:
            combined.save(glossary_path)
        return combined
