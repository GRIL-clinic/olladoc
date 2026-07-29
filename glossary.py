"""
Domain glossary for translation verification.

Three entry kinds:
  "require"  — if any source_term appears in source text, one of (target, *target_alts) must appear in the translation.
               A targeted retry is triggered when this rule fails.
  "verbatim" — the source term must appear unchanged in the translation (org names, brand names, intentional naming choices).
  "prefer"   — soft hint included in the prompt but not verified.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


def _fold(s: str) -> str:
    """Lowercase + strip accents so matching is tolerant of case/diacritic differences ("Comisión" == "COMISIÓN" == "comision")."""
    s = unicodedata.normalize('NFKD', s.lower())
    return ''.join(c for c in s if not unicodedata.combining(c))

EntryKind = Literal["require", "verbatim", "prefer"]

_FILE_HEADER = """\
# Translation glossary — auto-generated from document review.
# You may edit this file before translation completes.
# Formats:  TRANSLATE: source → target   (enforced; triggers retry if wrong)
#           KEEP: term                   (verbatim; never translated)
#           PREFER: source → target      (soft guidance in prompt)
# Variants: list multiple source forms of the same entity separated by '|',
#           e.g.  TRANSLATE: full name | abbrev | short form → target
# Lines starting with # are notes. The tool ignores them, and it is safe to leave or delete them.
# By default this file is retained after translation as a record of the rules applied.

"""

_LINE_RE = re.compile(
    r'^(TRANSLATE|KEEP|PREFER):\s*(.+?)(?:\s*→\s*(.+))?$',
    re.IGNORECASE,
)


@dataclass
class GlossaryEntry:
    source_terms: list[str]       # all source variants (matched case-insensitively)
    target: str                   # canonical target form
    target_alts: list[str] = field(default_factory=list)  # other accepted forms
    kind: EntryKind = "require"
    note: str = ""                # extra constraint shown in prompt
    origin: str = ""              # provenance for review display: "draft" (automated, unreviewed) or "approved" (from a human glossary); "" = unknown
    source: str = ""              # human-readable provenance for messages, e.g. "your global glossary" or "the base glossary you provided"
    alt_versions: list = field(default_factory=list)  # [(source_label, target)] differing versions of this term from lower-precedence human glossaries, kept for the review note


class DomainGlossary:
    def __init__(self, entries: list[GlossaryEntry] | None = None,
                 extra: dict[str, str] | None = None):
        self._entries: list[GlossaryEntry] = list(entries or [])
        # Free-text notes written into the saved file as comments, e.g. variants the merge could not assign to an entry. Visible in the review screen; ignored by the parser.
        self.review_notes: list[str] = []
        for src, tgt in (extra or {}).items():
            self._entries.append(GlossaryEntry([src], tgt, kind="prefer"))

    # ---- constructors ------------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "DomainGlossary":
        """Wrap a plain dict as a prefer-only glossary (backward compat)."""
        return cls([GlossaryEntry([src], tgt, kind="prefer") for src, tgt in d.items()])

    @classmethod
    def coerce(cls, value) -> "DomainGlossary | None | bool":
        """Normalize a user-supplied glossary argument.

        Accepted inputs:
          None             → returns None (no user-supplied glossary; the pipeline will auto-build one if Phase 1 runs)
          False            → returns False (no glossary at all, skip Phase 1, translate without any glossary)
          dict[str, str]   → wrapped as a PREFER-only DomainGlossary
          DomainGlossary   → returned as-is
          str | Path       → loaded from that file via DomainGlossary.load

        Empty dicts return None. Any other type raises TypeError so that bad input fails loudly instead of being silently dropped.
        """
        if value is None:
            return None
        if value is False:
            return False
        if isinstance(value, DomainGlossary):
            return value
        if isinstance(value, dict):
            return cls.from_dict(value) if value else None
        if isinstance(value, (str, Path)):
            return cls.load(value)
        raise TypeError(
            f"glossary must be None, False, dict, DomainGlossary, str, or Path — got {type(value).__name__}."
        )

    # ---- file I/O ----------------------------------------------------------

    @staticmethod
    def _split_variants(s: str) -> list[str]:
        """Split a '|'-separated source variant list, stripping whitespace."""
        return [v.strip() for v in s.split("|") if v.strip()]

    @staticmethod
    def parse_lines(text: str) -> list[GlossaryEntry]:
        """Parse TRANSLATE/KEEP/PREFER lines into GlossaryEntry objects.

        The source side may list multiple variants separated by '|' — they are all treated as forms of the same entity sharing one target.
        Used for both LLM review output and glossary file loading.
        Comment lines (starting with #) and blank lines are ignored.
        """
        entries = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _LINE_RE.match(line)
            if not m:
                continue
            kind_raw = m.group(1).upper()
            source = m.group(2).strip()
            target = m.group(3)
            variants = DomainGlossary._split_variants(source) or [source]

            if kind_raw == "KEEP":
                # Each KEEP variant is its own verbatim entry.
                for v in variants:
                    entries.append(GlossaryEntry([v], v, kind="verbatim"))
            elif kind_raw == "TRANSLATE" and target:
                # The target side may also list '|'-separated alternates: the first is the canonical form shown to the model, and any listed form satisfies the violation check.
                targets = DomainGlossary._split_variants(target) or [target.strip()]
                entries.append(GlossaryEntry(variants, targets[0], target_alts=targets[1:], kind="require"))
            elif kind_raw == "PREFER":
                tgt = target.strip() if target else variants[0]
                entries.append(GlossaryEntry(variants, tgt, kind="prefer"))
        return entries

    @staticmethod
    def validate_text(text: str) -> list[dict]:
        """Check glossary text line-by-line and return issues that parse_lines would silently ignore or misread.

        Returns a list of {"line": 1-based line number, "level": "error"|"warning", "message": str}.
        "error" means the line will be dropped entirely by parse_lines; "warning" means it parses but probably not the way the author intended.
        Blank lines and # comments are fine and produce no issues.
        """
        issues = []
        examples = {"TRANSLATE": "TRANSLATE: source → target", "KEEP": "KEEP: term", "PREFER": "PREFER: source → target"}
        for n, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            prefix = re.match(r'^(TRANSLATE|KEEP|PREFER):\s*(.*)$', line, re.IGNORECASE)
            if not prefix:
                issues.append({"line": n, "level": "error", "message": "Not a recognized rule. Lines must start with TRANSLATE:, KEEP:, or PREFER:"})
                continue
            kind = prefix.group(1).upper()
            rest = prefix.group(2).strip()
            ascii_arrow = "->" in line and "→" not in line
            if not rest:
                issues.append({"line": n, "level": "error", "message": f"{kind} has nothing after the colon: {examples[kind]}"})
                continue
            if rest.startswith("→"):
                issues.append({"line": n, "level": "error", "message": "Missing source term before the arrow"})
                continue
            source, target = (s.strip() for s in rest.split("→", 1)) if "→" in rest else (rest, None)
            if kind == "TRANSLATE" and not target:
                if ascii_arrow:
                    issues.append({"line": n, "level": "error", "message": "Use the arrow character → between source and target ('->' is not recognized)"})
                else:
                    issues.append({"line": n, "level": "error", "message": "TRANSLATE needs a target: TRANSLATE: source → target"})
                continue
            if kind == "KEEP" and target:
                issues.append({"line": n, "level": "warning", "message": "KEEP lines don't take '→ target'. The term is kept verbatim and the target part is ignored"})
            if kind == "PREFER" and not target and ascii_arrow:
                issues.append({"line": n, "level": "warning", "message": "Use the arrow character → before the target ('->' is not recognized, so this whole line is treated as the source term)"})
            if not DomainGlossary._split_variants(source):
                issues.append({"line": n, "level": "error", "message": "Missing source term before the arrow"})
        return issues

    @staticmethod
    def _entry_line(entry: GlossaryEntry) -> str:
        """Format one entry as its glossary-file line."""
        src = " | ".join(entry.source_terms)
        if entry.kind == "verbatim":
            return f"KEEP: {src}"
        if entry.kind == "require":
            tgt = " | ".join([entry.target] + entry.target_alts)
            return f"TRANSLATE: {src} → {tgt}"
        canonical = entry.source_terms[0] if entry.source_terms else ""
        if entry.target and entry.target != canonical:
            return f"PREFER: {src} → {entry.target}"
        return f"PREFER: {src}"

    def save(self, path: str | Path) -> None:
        """Write the glossary to a human-readable TRANSLATE/KEEP/PREFER file.

        Entries with multiple source variants are emitted with '|' between the variants — e.g. `TRANSLATE: foo | bar | baz → result`.
        """
        lines = [_FILE_HEADER]
        drafts = [e for e in self._entries if e.origin == "draft"]
        approved = [e for e in self._entries if e.origin == "approved"]
        if drafts and approved and len(drafts) + len(approved) == len(self._entries):
            # Drafts first, approved entries below.
            lines.append("# --- New terms found in this document, not yet reviewed:")
            lines += [self._entry_line(e) for e in drafts]
            lines.append("")
            lines.append("# --- From your global or base glossary, already approved:")
            lines += [self._entry_line(e) for e in approved]
        else:
            for entry in self._entries:
                lines.append(self._entry_line(entry))
        for note in self.review_notes:
            lines.append(f"# NEEDS A DECISION: {note}")
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  [glossary] written to {path}")

    @classmethod
    def load(cls, path: str | Path,
             user_glossary: "DomainGlossary | None" = None) -> "DomainGlossary":
        """Load a glossary file, optionally appending a user-provided glossary.

        Order: file entries first, user-provided appended at end.
        """
        text = Path(path).read_text(encoding="utf-8")
        entries = cls.parse_lines(text)
        user_entries = user_glossary._entries if user_glossary else []
        return cls(entries + user_entries)

    @classmethod
    def combine(cls, *glossaries: "DomainGlossary | None") -> "DomainGlossary | None":
        """Combine glossaries into one; on a source-term collision (fold-insensitive), entries from LATER glossaries take precedence. Returns None if nothing was given.

        When a dropped entry's target differs from the kept entry's, the dropped version is recorded on the kept entry's alt_versions, so the merge step can include it in a review note.
        """
        parts = [g for g in glossaries if g is not None]
        if not parts:
            return None
        result: list[GlossaryEntry] = []
        claimed_by: dict[str, GlossaryEntry] = {}
        for g in reversed(parts):
            keep = []
            for entry in g._entries:
                keys = {_fold(t) for t in entry.source_terms}
                colliding = keys & set(claimed_by)
                if colliding:
                    winner = claimed_by[next(iter(colliding))]
                    accepted = {_fold(x) for x in [winner.target] + winner.target_alts}
                    if _fold(entry.target) not in accepted:
                        winner.alt_versions.append((entry.source or "another of your glossaries", entry.target))
                    continue
                for k in keys:
                    claimed_by[k] = entry
                keep.append(entry)
            result = keep + result
        return cls(result)

    @staticmethod
    def append_new_entries(path: str | Path, entries: list[GlossaryEntry],
                           overwrite_terms: list[str] | None = None) -> dict:
        """Append entries whose source terms are not already in the glossary file at `path` (fold-insensitive). Creates the file with the standard header if missing.

        Returns {"added": int, "unchanged": [term, ...], "conflicts": [{"term", "kept", "offered"}, ...], "updated": [term, ...]}.
          "unchanged": colliding terms whose target already matches the file.
          "conflicts": collisions with a different target. The file's version is kept and both versions are returned.
          "updated": conflicting terms listed in `overwrite_terms`; their file lines are replaced with the new version in place, leaving all other lines untouched.
        """
        p = Path(path)
        existing: dict[str, GlossaryEntry] = {}
        if p.exists():
            for entry in DomainGlossary.parse_lines(p.read_text(encoding="utf-8")):
                for t in entry.source_terms:
                    existing[_fold(t)] = entry
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_FILE_HEADER, encoding="utf-8")
        overwrite_keys = {_fold(t) for t in (overwrite_terms or [])}
        added: list[GlossaryEntry] = []
        unchanged: list[str] = []
        conflicts: list[dict] = []
        to_update: list[tuple[GlossaryEntry, GlossaryEntry]] = []
        for e in entries:
            hit = next((existing[_fold(t)] for t in e.source_terms if _fold(t) in existing), None)
            if hit is None:
                added.append(e)
                for t in e.source_terms:
                    existing[_fold(t)] = e
                continue
            accepted = {_fold(x) for x in [hit.target] + hit.target_alts}
            if _fold(e.target) in accepted:
                unchanged.append(e.source_terms[0])
            elif overwrite_keys & {_fold(t) for t in e.source_terms}:
                to_update.append((hit, e))
            else:
                conflicts.append({"term": e.source_terms[0], "kept": hit.target, "offered": e.target})
        updated: list[str] = []
        if to_update:
            # Replace just the matching lines, leaving every other line (comments, hand edits) untouched.
            raw_lines = p.read_text(encoding="utf-8").splitlines()
            for hit, new in to_update:
                hit_keys = {_fold(t) for t in hit.source_terms}
                for i, raw in enumerate(raw_lines):
                    parsed = DomainGlossary.parse_lines(raw)
                    if parsed and any(_fold(t) in hit_keys for pe in parsed for t in pe.source_terms):
                        raw_lines[i] = DomainGlossary._entry_line(new)
                        updated.append(new.source_terms[0])
                        break
            p.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
        if added:
            with p.open("a", encoding="utf-8") as f:
                f.write("\n".join(DomainGlossary._entry_line(e) for e in added) + "\n")
        return {"added": len(added), "unchanged": unchanged, "conflicts": conflicts, "updated": updated}

    @staticmethod
    def delete(path: str | Path) -> None:
        """Delete the glossary file if it exists."""
        try:
            Path(path).unlink(missing_ok=True)
        except Exception as e:
            print(f"  [glossary] could not delete {path}: {e}")

    # ---- matching / verification -------------------------------------------

    def _source_hit(self, text: str, entry: GlossaryEntry) -> bool:
        # Fold both sides so a heading-cased chunk matches a natural-case entry.
        text_folded = _fold(text)
        return any(_fold(t) in text_folded for t in entry.source_terms)

    def _uncovered_hit_entries(self, text: str) -> set[int]:
        """Indices of entries with at least one source-term occurrence in `text` that is not overlapped by a LONGER occurrence from another entry.

        Longest-match precedence: when a short form is embedded in a longer matched form (e.g. "la Comisión" inside "la Comisión Interamericana de Derechos Humanos"), the longer entry owns that stretch of text, so the short entry is neither injected nor enforced for it. Standalone occurrences of the short form elsewhere in the text still count.
        """
        folded = _fold(text)
        spans_by_entry: dict[int, list[tuple[int, int]]] = {}
        for i, entry in enumerate(self._entries):
            spans = []
            for t in entry.source_terms:
                ft = _fold(t)
                if not ft:
                    continue
                start = 0
                while (idx := folded.find(ft, start)) >= 0:
                    spans.append((idx, idx + len(ft)))
                    start = idx + 1
            if spans:
                spans_by_entry[i] = spans
        live: set[int] = set()
        for i, spans in spans_by_entry.items():
            for s, e in spans:
                covered = any(
                    (oe - os_) > (e - s) and s < oe and os_ < e
                    for j, other in spans_by_entry.items() if j != i
                    for os_, oe in other
                )
                if not covered:
                    live.add(i)
                    break
        return live

    def _target_present(self, translation: str, entry: GlossaryEntry) -> bool:
        # Fold both sides so the translator can render the term in whatever case fits the surrounding sentence without tripping a violation.
        t_folded = _fold(translation)
        return any(_fold(t) in t_folded for t in [entry.target] + entry.target_alts)

    def prompt_section(self, source_text: str) -> str:
        """Return a glossary prompt block for terms found in source_text.

        Only entries whose source terms appear in the text are included.
        """
        lines = []
        live = self._uncovered_hit_entries(source_text)
        for i, entry in enumerate(self._entries):
            if i not in live:
                continue
            canonical = entry.source_terms[0]
            if entry.kind == "verbatim":
                line = f"  {canonical} → {entry.target}  (preserve exactly as written)"
            else:
                line = f"  {canonical} → {entry.target}"
            if entry.note:
                line += f"  [{entry.note}]"
            lines.append(line)
        if not lines:
            return ""
        return (
            "Required terminology — use these translations exactly "
            "whenever the source term appears:\n" + "\n".join(lines) + "\n"
        )

    def violations(self, source: str, translation: str) -> list[str]:
        """Return violation messages for 'require' and 'verbatim' entries.

        A violation occurs when a source term is present but none of the accepted target forms appear in the translation.
        """
        result = []
        live = self._uncovered_hit_entries(source)
        for i, entry in enumerate(self._entries):
            if entry.kind not in ("require", "verbatim"):
                continue
            if i not in live:
                continue
            if self._target_present(translation, entry):
                continue
            expected = entry.target
            if entry.target_alts:
                expected += " (or: " + ", ".join(entry.target_alts) + ")"
            trigger = entry.source_terms[0]
            result.append(
                f"'{trigger}' must translate to {expected}"
                + (f" — {entry.note}" if entry.note else "")
            )
        return result

    # ---- retry hints -------------------------------------------------------
    #
    # Two variants of the retry hint, kept side-by-side for A/B testing.
    # The Translator currently calls retry_hint_with_previous (variant B)

    def retry_hint_minimal(self, violations: list[str]) -> str:
        """Variant A — minimal hint. Does NOT reference the previous translation.

        Append to a fresh retry prompt that re-translates the source from scratch with the correct terminology in mind.
        """
        if not violations:
            return ""
        lines = ["For this translation, use exactly these terms:"]
        for v in violations:
            lines.append(f"  - {v.split(' — ')[0]}")
        return "\n".join(lines)

    def retry_hint_with_previous(self, violations: list[str],
                                 previous_translation: str) -> str:
        """Variant B (default) — includes the previous translation.

        Append to a retry prompt that shows the model its prior (incorrect) attempt,
        then asks it to correct only the listed terms while keeping everything else unchanged.
        Larger prompt but asks the model to edit instead of re-translating cold.
        """
        if not violations:
            return ""
        lines = [
            "You previously translated this passage as:",
            "---",
            previous_translation.strip(),
            "---",
            "The previous translation had terminology errors. "
            "Correct only these specific terms; keep everything else unchanged:",
        ]
        for v in violations:
            lines.append(f"  - {v.split(' — ')[0]}")
        return "\n".join(lines)

    # Backwards-compat shim: any old call site `glossary.retry_hint(violations)` still works, routing to the minimal variant.
    def retry_hint(self, violations: list[str]) -> str:
        return self.retry_hint_minimal(violations)
