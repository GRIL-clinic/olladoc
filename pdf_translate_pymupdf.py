"""
PDF Translation using PyMuPDF
------------------------------
Uses PyMuPDF (fitz) to extract text with per-span font metadata (size, bold,
italic), allowing us to detect headers and preserve formatting in the
translated output.

Tables are extracted via find_tables(), with merged columns collapsed, and
output as [TABLE N] placeholders + a separate translated tables file.
Other non-text content (diagrams, images) is rendered from the page as images.

Usage:
    python pdf_translate_pymupdf.py input.pdf output.docx
"""

import io
import fitz
import re

from pathlib import Path
from docx import Document
from docx.shared import Pt, Inches, RGBColor

from translate import Translator, TableTranslator, FootnoteTranslator

fitz.no_recommend_layout = True
_BLOCK_IDX_RE = re.compile(r'^\[(\d+)\]\s*')


def _has_font_trait(span, flag_bit, *keywords):
    """Check if a span has a font trait by flag bit or font-name keywords."""
    if span.get("flags", 0) & (1 << flag_bit):
        return True
    font = span.get("font", "").lower()
    return any(kw in font for kw in keywords)


def _is_bold(span):
    return _has_font_trait(span, 4, "bold", "heavy", "black")


def _is_italic(span):
    return _has_font_trait(span, 1, "italic", "oblique")


class FormattedBlock:
    """A block of text or image with formatting metadata."""

    def __init__(self, text="", font_size=0, bold=False, italic=False,
                 is_header=False, header_level=0,
                 is_footnote=False, footnote_marker=None,
                 is_image=False, image_data=None, image_width=None,
                 is_table=False):
        self.text = text
        self.font_size = font_size
        self.bold = bold
        self.italic = italic
        self.is_header = is_header
        self.header_level = header_level
        self.is_footnote = is_footnote
        self.footnote_marker = footnote_marker
        self.is_image = is_image
        self.image_data = image_data
        self.image_width = image_width
        self.is_table = is_table

    @property
    def is_passthrough(self):
        """True if this block should not be translated (images, table placeholders)."""
        return self.is_image or self.is_table

    def __repr__(self):
        if self.is_image:
            return f"<IMG width={self.image_width:.1f}in> {len(self.image_data)} bytes"
        if self.is_footnote:
            return f"<FN[{self.footnote_marker}] size={self.font_size:.1f}> {self.text[:60]!r}"
        tag = f"H{self.header_level}" if self.is_header else "P"
        return f"<{tag} size={self.font_size:.1f} bold={self.bold}> {self.text[:60]!r}"


class PdfExtractor:
    """Extracts structured content (text, tables, images, footnotes) from a PDF."""

    def __init__(self, filepath):
        self.doc = fitz.open(filepath)
        self.page_text_dicts = [
            page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
            for page in self.doc
        ]
        self.body_size, self.size_to_level, self.num_heading_sizes, self.fn_threshold = (
            self._analyze_font_sizes()
        )

    def close(self):
        self.doc.close()

    def _analyze_font_sizes(self):
        """Determine body size, heading levels, footnote threshold, and body bold baseline."""
        size_char_counts = {}
        size_bold_chars = {}
        for text_dict in self.page_text_dicts:
            for block in text_dict["blocks"]:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"].strip()
                        if text:
                            char_count = len(text)
                            rounded = round(span["size"], 1)
                            size_char_counts[rounded] = size_char_counts.get(rounded, 0) + char_count
                            if _is_bold(span):
                                size_bold_chars[rounded] = size_bold_chars.get(rounded, 0) + char_count

        if not size_char_counts:
            return None, {}, 0, 0

        body_size = max(size_char_counts, key=size_char_counts.get)

        body_total = size_char_counts.get(body_size, 0)
        body_bold = size_bold_chars.get(body_size, 0)
        self.body_is_bold = (body_total > 0 and body_bold > body_total / 2)

        unique_larger = sorted((s for s in size_char_counts if s > body_size), reverse=True)
        size_to_level = {size: i + 1 for i, size in enumerate(unique_larger[:4])}
        return body_size, size_to_level, len(unique_larger), body_size * 0.85

    @staticmethod
    def _extract_block_text(block):
        """Extract text and dominant formatting from a dict block.

        Formatting is determined by majority vote across all characters.
        Returns (text, dominant_size, dominant_bold, dominant_italic, leading_marker).
        """
        parts = []
        leading_marker = None
        size_chars = {}
        bold_chars = 0
        italic_chars = 0
        total_chars = 0

        for li, line in enumerate(block["lines"]):
            line_text = ""
            for si, span in enumerate(line["spans"]):
                text = span["text"]
                if not text:
                    continue
                if li == 0 and si == 0 and len(text.strip()) <= 4:
                    next_spans = line["spans"][1:] if len(line["spans"]) > 1 else []
                    if next_spans and span["size"] < next_spans[0]["size"] * 0.8:
                        leading_marker = text.strip()
                        continue
                line_text += text
                char_count = len(text.strip())
                if char_count > 0:
                    total_chars += char_count
                    rounded = round(span["size"], 1)
                    size_chars[rounded] = size_chars.get(rounded, 0) + char_count
                    if _is_bold(span):
                        bold_chars += char_count
                    if _is_italic(span):
                        italic_chars += char_count
            if line_text.strip():
                parts.append(line_text.strip())

        full_text = " ".join(parts).strip()
        if not total_chars:
            return full_text, 0, False, False, leading_marker

        dominant_size = max(size_chars, key=size_chars.get) if size_chars else 0
        return (full_text, dominant_size,
                bold_chars > total_chars / 2,
                italic_chars > total_chars / 2,
                leading_marker)

    @staticmethod
    def _render_rect(page, rect, scale=2.0):
        """Render a region of a page as PNG bytes."""
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect)
        return pix.tobytes("png")

    @staticmethod
    def _collapse_merged_columns(raw_rows):
        """Collapse columns that are artifacts of merged cells.

        PyMuPDF's find_tables() reports the raw grid, so a 3-column table with
        merged cells may appear as 7 columns with None/empty filler.  For each
        row, adjacent empty cells are merged into their neighbour.
        """
        if not raw_rows:
            return raw_rows
        ncols = max(len(r) for r in raw_rows)
        if ncols <= 1:
            return raw_rows

        merged_rows = []
        for row in raw_rows:
            padded = [(row[i] if i < len(row) else "") or "" for i in range(ncols)]
            collapsed = [padded[0]]
            for ci in range(1, ncols):
                prev = collapsed[-1].strip()
                curr = padded[ci].strip()
                if not prev and not curr:
                    collapsed[-1] = ""
                elif not prev:
                    collapsed[-1] = curr
                elif not curr:
                    pass
                else:
                    collapsed.append(curr)
            merged_rows.append(collapsed)

        max_cols = max(len(r) for r in merged_rows)
        for row in merged_rows:
            while len(row) < max_cols:
                row.append("")

        keep = [ci for ci in range(max_cols)
                if any(merged_rows[ri][ci].strip() for ri in range(len(merged_rows)))]
        if len(keep) == max_cols:
            return merged_rows
        return [[row[ci] for ci in keep] for row in merged_rows]

    def _find_image_regions(self, page, table_rects, drawings, min_size=50):
        """Find non-table image/diagram regions on a page.

        Returns list of (fitz.Rect, png_bytes, width_inches).
        """
        expanded_tables = [fitz.Rect(r.x0 - 5, r.y0 - 5, r.x1 + 5, r.y1 + 5)
                           for r in table_rects]
        results = []
        seen = set()

        for img_info in page.get_images(full=True):
            xref = img_info[0]
            for rect in page.get_image_rects(xref):
                if rect.width < min_size or rect.height < min_size:
                    continue
                if any(rect.intersects(tr) for tr in expanded_tables):
                    continue
                key = (round(rect.x0), round(rect.y0), round(rect.x1), round(rect.y1))
                if key in seen:
                    continue
                seen.add(key)
                png_data = self._render_rect(page, rect)
                results.append((fitz.Rect(rect), png_data, min(rect.width / 72.0, 6.0)))

        draw_rects = []
        for d in drawings:
            r = fitz.Rect(d["rect"])
            if abs(r.height) < 3 and r.width > 30:
                continue
            if r.width < 3 and r.height < 3:
                continue
            if any(r.intersects(tr) for tr in expanded_tables):
                continue
            draw_rects.append(r)

        if len(draw_rects) >= 10:
            union = fitz.Rect(draw_rects[0])
            for r in draw_rects[1:]:
                union |= r
            if (union.width >= min_size and union.height >= min_size
                    and not any(union.intersects(fitz.Rect(*k)) for k in seen)):
                png_data = self._render_rect(page, union)
                results.append((union, png_data, min(union.width / 72.0, 6.0)))

        return results

    @staticmethod
    def _find_footnote_separator_y(drawings, page_height):
        """Find the y-position of the footnote separator line, or a fallback."""
        for d in drawings:
            rect = d["rect"]
            if (abs(rect.height) < 3
                    and 30 < rect.width < 300
                    and rect.y0 > page_height * 0.25):
                return rect.y0
        return page_height * 0.6

    def _process_page(self, page, text_dict, table_idx):
        """Process a single page and return (page_blocks, page_tables, page_footnotes, new_table_idx)."""
        page_height = page.rect.height
        drawings = page.get_drawings()
        fn_separator_y = self._find_footnote_separator_y(drawings, page_height)

        # Tables
        page_table_info = []
        for table in page.find_tables().tables:
            raw_rows = [[cell or "" for cell in row] for row in table.extract()]
            if raw_rows:
                page_table_info.append((
                    fitz.Rect(table.bbox),
                    self._collapse_merged_columns(raw_rows),
                ))
        table_bboxes = [info[0] for info in page_table_info]

        # Images / diagrams
        image_info = [
            (rect, FormattedBlock(is_image=True, image_data=png, image_width=w))
            for rect, png, w in self._find_image_regions(page, table_bboxes, drawings)
        ]

        page_elements = []
        page_tables = []
        page_footnotes = []

        for tbbox, tdata in page_table_info:
            page_tables.append(tdata)
            table_idx += 1
            page_elements.append((tbbox.y0, FormattedBlock(
                text=f"[TABLE {table_idx}]", font_size=self.body_size, is_table=True,
            )))

        for irect, ifb in image_info:
            page_elements.append((irect.y0, ifb))

        for block in text_dict["blocks"]:
            if block["type"] != 0:
                continue
            block_rect = fitz.Rect(block["bbox"])

            if any(block_rect.intersects(r) for r, _ in page_table_info):
                continue
            if any(block_rect.intersects(r) for r, _ in image_info):
                continue

            full_text, dominant_size, dominant_bold, dominant_italic, leading_marker = (
                self._extract_block_text(block)
            )
            if not full_text:
                continue

            # Footnotes
            effective_bold = dominant_bold and not self.body_is_bold
            if dominant_size < self.fn_threshold and block_rect.y0 > fn_separator_y:
                if re.match(r'^-?\s*\d+\s*-?$', full_text):
                    page_elements.append((block_rect.y0, FormattedBlock(
                        text=full_text, font_size=dominant_size,
                        bold=effective_bold, italic=dominant_italic,
                    )))
                    continue

                marker = leading_marker
                if not marker:
                    m = re.match(r'^(\d+|[*†‡§¶])\s+', full_text)
                    if m:
                        marker = m.group(1)
                        full_text = full_text[m.end():]

                page_footnotes.append(FormattedBlock(
                    text=full_text, font_size=dominant_size,
                    bold=effective_bold, italic=dominant_italic,
                    is_footnote=True, footnote_marker=marker or "?",
                ))
                continue

            # Headers
            header_level = self.size_to_level.get(dominant_size, 0)
            if header_level == 0 and effective_bold and len(full_text) < 120:
                header_level = min(self.num_heading_sizes + 1, 4)

            page_elements.append((block_rect.y0, FormattedBlock(
                text=full_text, font_size=dominant_size,
                bold=effective_bold, italic=dominant_italic,
                is_header=(header_level > 0), header_level=header_level,
            )))

        page_elements.sort(key=lambda x: x[0])
        page_blocks = [fb for _, fb in page_elements]
        return page_blocks, page_tables, page_footnotes, table_idx

    def extract(self):
        """Extract all content from the PDF. Returns (blocks, tables, footnotes)."""
        if self.body_size is None:
            return [], [], []

        all_blocks = []
        all_tables = []
        all_footnotes = []
        table_idx = 0

        for page, text_dict in zip(self.doc, self.page_text_dicts):
            page_blocks, page_tables, page_footnotes, table_idx = (
                self._process_page(page, text_dict, table_idx)
            )
            all_blocks.extend(page_blocks)
            all_tables.extend(page_tables)
            all_footnotes.extend(page_footnotes)

        return all_blocks, all_tables, all_footnotes


class PdfDocumentTranslator:
    """Translates pre-extracted PDF content and writes docx output."""

    def __init__(self, translator):
        self.translator = translator

    @staticmethod
    def _chunk_blocks(blocks, max_chars=3000):
        """Group text blocks into translation chunks.

        Only consecutive body-text blocks are batched together.  Headers, images,
        and placeholders break the chunk so they are translated individually —
        this prevents the numbered-line parser from mixing up block boundaries.
        """
        chunks = []
        current_chunk = []
        current_len = 0

        def _flush():
            nonlocal current_chunk, current_len
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = []
                current_len = 0

        for block in blocks:
            if block.is_header or block.is_passthrough:
                _flush()
                chunks.append([block])
                continue

            block_len = len(block.text)
            if current_chunk and current_len + block_len + 1 > max_chars:
                _flush()
            current_chunk.append(block)
            current_len += block_len + 1

        _flush()
        return chunks

    def _translate_chunk(self, blocks):
        if len(blocks) == 1:
            b = blocks[0]
            if b.is_passthrough:
                return [b.text]
            return [self.translator.translate(b.text)]

        lines = [f"[{i}] {b.text}" for i, b in enumerate(blocks)]
        translated = self.translator.translate("\n".join(lines))

        results = {}
        current_idx = None
        current_parts = []
        for line in translated.split("\n"):
            m = _BLOCK_IDX_RE.match(line.strip())
            if m:
                idx = int(m.group(1))
                if idx < len(blocks):
                    if current_idx is not None:
                        results[current_idx] = " ".join(current_parts).strip()
                    current_idx = idx
                    current_parts = [line.strip()[m.end():].strip()]
                    continue
            if current_idx is not None:
                current_parts.append(line.strip())
        if current_idx is not None:
            results[current_idx] = " ".join(current_parts).strip()

        output = []
        for i, b in enumerate(blocks):
            if b.is_passthrough:
                output.append(b.text)
            elif i in results and results[i]:
                output.append(results[i])
            else:
                output.append(self.translator.translate(b.text))
        return output

    def _save_footnotes(self, footnotes, output_path):
        fn_dicts = [{"number": fn.footnote_marker or "", "text": fn.text}
                    for fn in footnotes]
        ft = FootnoteTranslator(self.translator)
        ft.translate(fn_dicts)
        ft.save_to_docx(fn_dicts, output_path)

    def _save_tables(self, tables, output_path):
        print(f"  Translating {len(tables)} table(s)...")
        tt = TableTranslator(self.translator)
        translated = tt.translate(tables)
        tt.save_to_docx(translated, set(range(len(translated))), output_path)

    def translate_to_docx(self, blocks, tables, footnotes, output_path):
        out_p = Path(output_path)
        tables_path = str(out_p.parent / f"{out_p.stem}_tables{out_p.suffix}")
        footnotes_path = str(out_p.parent / f"{out_p.stem}_footnotes{out_p.suffix}")

        num_images = sum(1 for b in blocks if b.is_image)
        num_tables = len(tables)
        total_chars = sum(len(b.text) for b in blocks if not b.is_image)
        print(f"Extracted {len(blocks)} blocks ({total_chars} chars, "
              f"{num_tables} tables, {num_images} images, "
              f"{len(footnotes)} footnotes)")

        chunks = self._chunk_blocks(blocks)
        print(f"  Translating {len(chunks)} text chunk(s)...")
        translated_texts = []
        for ci, chunk in enumerate(chunks):
            print(f"    Chunk {ci+1}/{len(chunks)} "
                  f"({sum(len(b.text) for b in chunk)} chars)...")
            translated_texts.extend(self._translate_chunk(chunk))

        doc = Document()
        img_count = 0
        for block, translated in zip(blocks, translated_texts):
            if block.is_image:
                try:
                    para = doc.add_paragraph()
                    run = para.add_run()
                    run.add_picture(io.BytesIO(block.image_data),
                                   width=Inches(block.image_width))
                    img_count += 1
                except Exception as e:
                    print(f"  Warning: could not insert image: {e}")
            elif block.is_header:
                level = min(block.header_level, 4)
                heading = doc.add_heading(translated, level=level)
                for run in heading.runs:
                    run.font.color.rgb = RGBColor(0, 0, 0)
            else:
                para = doc.add_paragraph()
                run = para.add_run(translated)
                run.font.size = Pt(11)
                if block.bold:
                    run.bold = True
                if block.italic:
                    run.italic = True

        doc.save(output_path)
        print(f"Saved translated text to {output_path}"
              + (f" ({img_count} image(s) preserved)" if img_count else ""))

        if tables:
            self._save_tables(tables, tables_path)
        if footnotes:
            self._save_footnotes(footnotes, footnotes_path)

        return {
            "text_output": output_path,
            "tables_output": tables_path if tables else None,
            "footnotes_output": footnotes_path if footnotes else None,
            "total_blocks": len(blocks),
            "total_tables": num_tables,
            "total_images": num_images,
            "total_footnotes": len(footnotes),
            "total_chunks": len(chunks),
            "chars_in": total_chars,
            "chars_out": sum(len(t) for t in translated_texts),
        }


def translate_pdf(filepath, output_path, source_lang="Spanish",
                  target_lang="English", model="translategemma",
                  glossary=None):
    extractor = PdfExtractor(filepath)
    try:
        blocks, tables, footnotes = extractor.extract()
    finally:
        extractor.close()

    t = Translator(source_lang=source_lang, target_lang=target_lang, model=model,
                   glossary=glossary)
    dt = PdfDocumentTranslator(t)
    return dt.translate_to_docx(blocks, tables, footnotes, output_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Translate a PDF with formatting (PyMuPDF)")
    parser.add_argument("input_pdf", help="Path to the input PDF file")
    parser.add_argument("output_docx", help="Path for the translated output .docx")
    parser.add_argument("--source-lang", default="Spanish")
    parser.add_argument("--target-lang", default="English")
    parser.add_argument("--model", default="translategemma")
    args = parser.parse_args()

    translate_pdf(
        args.input_pdf, args.output_docx,
        source_lang=args.source_lang, target_lang=args.target_lang,
        model=args.model,
    )
