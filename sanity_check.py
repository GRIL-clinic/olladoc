"""Post-translation sanity check.

Compares the structure of the source document against the translated output and flags anything that looks off:
- heading/paragraph/table count mismatches
- headings that lost their number prefix
- paragraphs with suspicious length ratios
"""

import re
from pathlib import Path

from blocks import Heading, BodyPara, TablePlaceholder, Footnote, ListItem
from docx_extract import DocxExtractor


_NUM_PREFIX_RE = re.compile(r'^\s*\d+(?:\.\d+)*[\.\)]\s')
# Matches the "[TABLE N]" placeholder text emitted by our renderer.
_TABLE_PLACEHOLDER_RE = re.compile(r'^\s*\[TABLE\s+\d+\]\s*$')


def _is_real_body(block):
    """True if this is a real body paragraph (not a table placeholder text)."""
    if not isinstance(block, BodyPara):
        return False
    return not _TABLE_PLACEHOLDER_RE.match(block.text)


def _counts(blocks):
    return {
        "headings": sum(1 for b in blocks if isinstance(b, Heading)),
        "body": sum(1 for b in blocks if _is_real_body(b)),
        "lists": sum(1 for b in blocks if isinstance(b, ListItem)),
        "tables": sum(1 for b in blocks
                      if isinstance(b, TablePlaceholder)
                      or (isinstance(b, BodyPara)
                          and _TABLE_PLACEHOLDER_RE.match(b.text))),
        "footnotes": sum(1 for b in blocks if isinstance(b, Footnote)),
    }


def compare(source_path: str, output_path: str) -> list[str]:
    """Return a list of warning messages about structural mismatches."""
    warnings: list[str] = []

    src_ext = DocxExtractor(source_path)
    src_blocks, src_tables = src_ext.extract()
    src_ext.close()

    out_ext = DocxExtractor(output_path)
    out_blocks, out_tables = out_ext.extract()
    out_ext.close()

    # Tables are rendered separately to an _tables.docx sibling file.
    tables_out_path = str(Path(output_path).with_name(
        Path(output_path).stem + "_tables.docx"))
    if Path(tables_out_path).exists():
        try:
            tbl_ext = DocxExtractor(tables_out_path)
            _, out_tables = tbl_ext.extract()
            tbl_ext.close()
        except Exception:
            pass

    src_counts = _counts(src_blocks)
    out_counts = _counts(out_blocks)

    # Block count diffs
    for kind in ("headings", "body", "tables"):
        if src_counts[kind] != out_counts[kind]:
            warnings.append(
                f"{kind} count mismatch: source={src_counts[kind]}, "
                f"output={out_counts[kind]}"
            )

    # Headings: compare number prefixes and flag lost ones
    src_headings = [b for b in src_blocks if isinstance(b, Heading)]
    out_headings = [b for b in out_blocks if isinstance(b, Heading)]
    for i, (src_h, out_h) in enumerate(zip(src_headings, out_headings)):
        src_has_num = bool(_NUM_PREFIX_RE.match(src_h.text))
        out_has_num = bool(_NUM_PREFIX_RE.match(out_h.text))
        if src_has_num and not out_has_num:
            warnings.append(
                f"heading #{i+1} lost its number prefix: "
                f"source={src_h.text[:60]!r}, output={out_h.text[:60]!r}"
            )

    # Table cell length ratios — flag extremes (hallucinations).
    for ti, (src_tbl, out_tbl) in enumerate(zip(src_tables, out_tables)):
        for ri, (src_row, out_row) in enumerate(zip(src_tbl, out_tbl)):
            for ci, (src_cell, out_cell) in enumerate(zip(src_row, out_row)):
                src_len = len(src_cell.strip())
                out_len = len(out_cell.strip())
                if src_len < 10:
                    continue
                ratio = out_len / src_len
                if ratio < 0.3 or ratio > 3.0:
                    warnings.append(
                        f"table {ti+1} row {ri+1} cell {ci+1} "
                        f"length ratio {ratio:.2f} "
                        f"(src={src_len}, out={out_len}): "
                        f"source={src_cell[:60]!r}"
                    )

    # Body paragraph length ratios — flag extremes
    src_body = [b for b in src_blocks if _is_real_body(b)]
    out_body = [b for b in out_blocks if _is_real_body(b)]
    for i, (src_b, out_b) in enumerate(zip(src_body, out_body)):
        src_len = len(src_b.text)
        out_len = len(out_b.text)
        if src_len < 20:
            continue  # too short to meaningfully compare
        ratio = out_len / src_len
        if ratio < 0.3 or ratio > 3.0:
            warnings.append(
                f"paragraph #{i+1} length ratio {ratio:.2f} "
                f"(src={src_len}, out={out_len}): "
                f"source={src_b.text[:60]!r}"
            )

    return warnings


def write_report(source_path: str, output_path: str, log_path: str) -> int:
    """Run compare() and append findings to the given log file.
    Returns the number of warnings written."""
    warnings = compare(source_path, output_path)
    if not warnings:
        return 0
    from datetime import datetime
    with open(log_path, "a", encoding="utf-8") as f:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"\n[{ts}] Sanity check: {Path(output_path).name}\n")
        for w in warnings:
            f.write(f"  - {w}\n")
    return len(warnings)
