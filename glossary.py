"""
Domain glossary for translation verification.

Three entry kinds:
  "require"  — if any source_term appears in source text, one of (target, *target_alts) must appear in the translation.
               A targeted retry is triggered when this rule fails.
  "verbatim" — the source term must appear unchanged in the translation (org names, brand names, intentional naming choices).
  "prefer"   — soft hint included in the prompt but not verified.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

EntryKind = Literal["require", "verbatim", "prefer"]

_FILE_HEADER = """\
# Translation glossary — auto-generated from document review.
# You may edit this file before translation completes.
# Formats:  TRANSLATE: source → target   (enforced; triggers retry if wrong)
#           KEEP: term                   (verbatim; never translated)
#           PREFER: source → target      (soft guidance in prompt)
# Variants: list multiple source forms of the same entity separated by '|',
#           e.g.  TRANSLATE: full name | abbrev | short form → target
# This file is deleted automatically when the translation finishes.

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


class DomainGlossary:
    def __init__(self, entries: list[GlossaryEntry] | None = None,
                 extra: dict[str, str] | None = None):
        self._entries: list[GlossaryEntry] = list(entries or [])
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
                entries.append(GlossaryEntry(variants, target.strip(), kind="require"))
            elif kind_raw == "PREFER":
                tgt = target.strip() if target else variants[0]
                entries.append(GlossaryEntry(variants, tgt, kind="prefer"))
        return entries

    def save(self, path: str | Path) -> None:
        """Write the glossary to a human-readable TRANSLATE/KEEP/PREFER file.

        Entries with multiple source variants are emitted with '|' between the variants — e.g. `TRANSLATE: foo | bar | baz → result`.
        """
        lines = [_FILE_HEADER]
        for entry in self._entries:
            src = " | ".join(entry.source_terms) if entry.source_terms else ""
            if entry.kind == "verbatim":
                lines.append(f"KEEP: {src}")
            elif entry.kind == "require":
                lines.append(f"TRANSLATE: {src} → {entry.target}")
            else:
                # PREFER: if target equals the canonical (placeholder used when Step 1b dropped the term), display without the "→ target" suffix
                canonical = entry.source_terms[0] if entry.source_terms else ""
                if entry.target and entry.target != canonical:
                    lines.append(f"PREFER: {src} → {entry.target}")
                else:
                    lines.append(f"PREFER: {src}")
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

    @staticmethod
    def delete(path: str | Path) -> None:
        """Delete the glossary file if it exists."""
        try:
            Path(path).unlink(missing_ok=True)
        except Exception as e:
            print(f"  [glossary] could not delete {path}: {e}")

    # ---- matching / verification -------------------------------------------

    def _source_hit(self, text: str, entry: GlossaryEntry) -> bool:
        text_lower = text.lower()
        return any(t.lower() in text_lower for t in entry.source_terms)

    def _target_present(self, translation: str, entry: GlossaryEntry) -> bool:
        t_lower = translation.lower()
        return any(t.lower() in t_lower for t in [entry.target] + entry.target_alts)

    def prompt_section(self, source_text: str) -> str:
        """Return a glossary prompt block for terms found in source_text.

        Only entries whose source terms appear in the text are included.
        """
        lines = []
        for entry in self._entries:
            if not self._source_hit(source_text, entry):
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
        for entry in self._entries:
            if entry.kind not in ("require", "verbatim"):
                continue
            if not self._source_hit(source, entry):
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
