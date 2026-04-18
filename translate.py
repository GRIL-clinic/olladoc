"""
Translation Classes
-------------------
Translator: Translates text using a local LLM via Ollama.
TableTranslator: Translates tables and renders them to .docx.
FootnoteTranslator: Translates footnotes and renders them to .docx.
CommentTranslator: Translates comments and renders them to .docx.
DocumentTranslator: Translates a Block list and renders it to .docx.
    Format-agnostic — used by both the PDF and docx flows.

Extraction lives in the format-specific extractors (pdf_extract.py,
docx_extract.py); the translators here only translate and render.
"""

import io
import re
import ollama
from pathlib import Path
from docx import Document
from docx.shared import Pt, Inches, RGBColor

from blocks import (Run, Heading, BodyPara, ListItem, Footnote, Comment,
                    ImageBlock, TablePlaceholder, Separator, Block)
from prompts import TRANSLATEGEMMA_PROMPT, DEFAULT_PROMPT, GLOSSARY_SECTION


_BLOCK_IDX_RE = re.compile(r'^\[(\d+)\]\s*')
_LIST_MARKER_RE = re.compile(r'^\s*(\d+[\).])\s*')
_FN_MARKER_RE = re.compile(r'‹FN(\d+)›')
_SENT_SPLIT_RE = re.compile(
    r'(?<=[.!?][""\u201d\u2019\')])\s+'   # sentence end after closing quote
    r'|(?<=[.!?])\s+'                      # plain sentence end
)

# Path to a log file that accumulates translation warnings (hallucinations,
# prompt echoes, etc.). Callers can set this before starting a translation.
WARNINGS_LOG_PATH: str | None = None


def _log_warning(msg: str) -> None:
    """Print a warning to stdout and, if WARNINGS_LOG_PATH is set, also
    append it to that file."""
    print(msg)
    if WARNINGS_LOG_PATH:
        try:
            from datetime import datetime
            with open(WARNINGS_LOG_PATH, "a", encoding="utf-8") as f:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{ts}] {msg.strip()}\n")
        except Exception:
            pass


def _translate_preserving_fn(text: str, translate_fn) -> str:
    """Translate text containing ‹FN{id}› markers.

    Strips markers, translates the full text as one unit (preserving
    context and quality), then re-inserts each marker at the end of
    its corresponding sentence in the translation.
    """
    fn_matches = list(_FN_MARKER_RE.finditer(text))
    if not fn_matches:
        return translate_fn(text)

    # Figure out which source sentence each FN belongs to.
    clean_src = _FN_MARKER_RE.sub("", text)
    src_sents = _SENT_SPLIT_RE.split(clean_src)
    src_sents = [s for s in src_sents if s.strip()]

    fn_map: list[tuple[int, str]] = []  # (source_sent_idx, fn_id)
    for m in fn_matches:
        offset = len(_FN_MARKER_RE.sub("", text[:m.start()]))
        cumulative = 0
        sent_idx = len(src_sents) - 1
        for si, sent in enumerate(src_sents):
            cumulative += len(sent) + 1
            if offset < cumulative:
                sent_idx = si
                break
        fn_map.append((sent_idx, m.group(1)))

    # Translate the full clean text.
    translated = translate_fn(clean_src)

    # Split translation into sentences.
    trans_sents = _SENT_SPLIT_RE.split(translated)
    trans_sents = [s for s in trans_sents if s.strip()]

    # Attach each FN to the corresponding sentence in the translation.
    # If translation has fewer sentences, clamp to the last one.
    for sent_idx, fn_id in fn_map:
        idx = min(sent_idx, len(trans_sents) - 1)
        trans_sents[idx] = trans_sents[idx].rstrip() + f"‹FN{fn_id}›"

    return " ".join(trans_sents)


class Translator:
    """Translates text using a local LLM via Ollama."""

    GEMMA_LANG_CODES = {
        "English": "en",
        "Spanish": "es",
        "French": "fr",
        "German": "de",
        "Portuguese": "pt",
        "Italian": "it",
        "Chinese": "zh-Hans",
        "Japanese": "ja",
        "Korean": "ko",
        "Arabic": "ar",
        "Russian": "ru",
        "Dutch": "nl",
    }

    def __init__(self, source_lang, target_lang="English", model="translategemma",
                 model_temp=0.3, glossary=None):
        self.source_lang = source_lang
        self.target_lang = target_lang
        # Default model is TranslateGemma:
        # https://blog.google/innovation-and-ai/technology/developers-tools/translategemma/
        self.model = model
        self.model_temp = model_temp  # Low temp for more faithful translations
        self.glossary = glossary or {}

    def _build_prompt(self, text):
        if self.glossary:
            glossary_entries = "\n".join(f"  {src} → {tgt}" for src, tgt in self.glossary.items())
            glossary_section = GLOSSARY_SECTION.format(glossary_entries=glossary_entries)
        else:
            glossary_section = ""
        if "translategemma" in self.model:
            return TRANSLATEGEMMA_PROMPT.format(
                source_lang=self.source_lang,
                target_lang=self.target_lang,
                src_code=self.GEMMA_LANG_CODES.get(self.source_lang, "es"),
                tgt_code=self.GEMMA_LANG_CODES.get(self.target_lang, "en"),
                glossary_section=glossary_section,
                text=text,
            )
        return DEFAULT_PROMPT.format(
            source_lang=self.source_lang,
            target_lang=self.target_lang,
            glossary_section=glossary_section,
            text=text,
        )

    _PROMPT_LEAK = {"Use established legal phrasing",
                    "Produce ONLY the", "Use standard domain terminology",
                    "Output ONLY the translation"}

    def _is_prompt_echo(self, result):
        return any(phrase in result for phrase in self._PROMPT_LEAK)

    def translate(self, text):
        prompt = self._build_prompt(text)
        resp = ollama.chat(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": self.model_temp},
        )
        result = resp["message"]["content"].strip()

        if self._is_prompt_echo(result):
            # Retry once with lower temperature.
            resp = ollama.chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.1},
            )
            result = resp["message"]["content"].strip()

        if self._is_prompt_echo(result):
            _log_warning(f"  Warning: prompt echo detected, keeping original text: {text[:80]!r}")
            return text

        return result


class TableTranslator:
    """Translates table cell data and renders the result to .docx."""

    def __init__(self, translator):
        self.translator = translator

    def translate(self, tables):
        translated = []
        for table in tables:
            translated_rows = []
            for row in table:
                translated_rows.append([
                    self._translate_cell(cell) if cell.strip() else cell
                    for cell in row
                ])
            translated.append(translated_rows)
        return translated

    def _translate_cell(self, cell: str, max_ratio: float = 4.0) -> str:
        """Translate a table cell. If the result is drastically longer
        than the source (LLM hallucination from a short input), retry
        once at low temperature with explicit fragment instructions.
        If still bad, fall back to the original text."""

        def _looks_hallucinated(text):
            return text and len(text) > len(cell) * max_ratio + 50

        result = self.translator.translate(cell)
        if _looks_hallucinated(result):
            _log_warning(f"  Warning: table cell translation looks "
                         f"hallucinated ({len(cell)} chars → {len(result)} "
                         f"chars); retrying: {cell[:80]!r}")
            # Retry with a stricter prompt and low temperature.
            try:
                prompt = (self.translator._build_prompt(cell)
                          + "\n\nThis is a short fragment (table cell). "
                          "Translate ONLY this text literally. Do not add "
                          "any explanation, examples, or extra sentences.")
                resp = ollama.chat(
                    model=self.translator.model,
                    messages=[{"role": "user", "content": prompt}],
                    options={"temperature": 0.1},
                )
                retry = resp["message"]["content"].strip()
            except Exception as e:
                _log_warning(f"  Retry failed: {e}")
                retry = ""
            if retry and not _looks_hallucinated(retry):
                return retry
            _log_warning(f"  Retry also hallucinated; keeping original: "
                         f"{cell[:80]!r}")
            return cell
        return result

    def save_to_docx(self, translated_tables, referenced_indices, output_path):
        doc = Document()
        for idx in sorted(referenced_indices):
            if idx < len(translated_tables) and translated_tables[idx]:
                trans_table = translated_tables[idx]
                doc.add_paragraph(f"TABLE {idx + 1}")
                ncols = max(len(r) for r in trans_table)
                tbl = doc.add_table(rows=len(trans_table), cols=ncols)
                tbl.style = "Table Grid"
                for ri, row in enumerate(trans_table):
                    for ci, cell in enumerate(row):
                        if ci < ncols:
                            tbl.rows[ri].cells[ci].text = cell
                doc.add_paragraph()  # spacing between tables
        doc.save(output_path)
        print(f"Saved translated tables to {output_path}")


class FootnoteTranslator:
    """Translates Footnote blocks and renders them as a docx table."""

    def __init__(self, translator):
        self.translator = translator

    def translate(self, footnotes):
        """Translate a list of Footnote blocks. Returns dicts with
        number/text/translation, ready for save_to_docx."""
        print(f"  Translating {len(footnotes)} footnote(s)...")
        out = []
        for fn in footnotes:
            out.append({
                "number": fn.marker,
                "text": fn.text,
                "translation": self.translator.translate(fn.text),
            })
        return out

    def save_to_docx(self, footnotes, output_path):
        doc = Document()
        doc.add_paragraph("Translated Footnotes")
        table = doc.add_table(rows=1, cols=3)
        table.style = "Table Grid"
        for i, h in enumerate(["#", "Original", "Translation"]):
            table.rows[0].cells[i].text = h

        for fn in footnotes:
            row = table.add_row()
            row.cells[0].text = fn["number"]
            row.cells[1].text = fn["text"]
            row.cells[2].text = fn["translation"]

        doc.save(output_path)
        print(f"Saved translated footnotes to {output_path}")


class CommentTranslator:
    """Translates Comment blocks and renders them as a docx table."""

    def __init__(self, translator):
        self.translator = translator

    def translate(self, comments):
        """Translate a list of Comment blocks. Returns dicts with the
        original fields plus translation/anchor_translation."""
        out = []
        for i, c in enumerate(comments):
            print(f"  Translating comment {i+1}/{len(comments)}...")
            translation = self.translator.translate(c.text)
            anchor_translation = (self.translator.translate(c.anchor)
                                  if c.anchor else "")
            out.append({
                "id": c.id,
                "author": c.author,
                "date": c.date,
                "text": c.text,
                "anchor": c.anchor,
                "translation": translation,
                "anchor_translation": anchor_translation,
            })
        return out

    def save_to_docx(self, translated, output_path):
        doc = Document()
        doc.add_paragraph("Translated Comments")

        if not translated:
            doc.add_paragraph("No comments found.")
            doc.save(output_path)
            return

        table = doc.add_table(rows=1, cols=5)
        table.style = "Table Grid"
        headers = ["Author", "Date", "Anchor (src)", "Comment (src)", "Translation"]
        for i, h in enumerate(headers):
            table.rows[0].cells[i].text = h

        for c in translated:
            row = table.add_row()
            row.cells[0].text = c["author"]
            row.cells[1].text = c["date"]
            row.cells[2].text = c["anchor"]
            row.cells[3].text = c["text"]
            row.cells[4].text = c["translation"]

        doc.save(output_path)
        print(f"Saved translated comments to {output_path}")


class DocumentTranslator:
    """Translates a list of Blocks and renders them to .docx.

    Used by both the PDF flow (PdfExtractor) and the docx flow
    (DocxExtractor). Tables and footnotes are routed to
    TableTranslator / FootnoteTranslator for their own output files.
    """

    def __init__(self, translator):
        self.translator = translator

    # ---- translation ---------------------------------------------------

    @staticmethod
    def _chunk_body_paras(body_indices, blocks, max_chars=1500, max_blocks=3):
        chunks: list[list[int]] = [[]]
        current_len = 0
        for i in body_indices:
            text_len = len(blocks[i].text)
            if chunks[-1] and (current_len + text_len + 1 > max_chars
                               or len(chunks[-1]) >= max_blocks):
                chunks.append([])
                current_len = 0
            chunks[-1].append(i)
            current_len += text_len + 1
        if not chunks[-1]:
            chunks.pop()
        return chunks

    def _translate_chunk_indices(self, indices, blocks):
        if len(indices) == 1:
            i = indices[0]
            return {i: self.translator.translate(blocks[i].text)}

        lines = [f"[{n}] {blocks[i].text}" for n, i in enumerate(indices)]
        raw = self.translator.translate("\n".join(lines))

        parsed: dict[int, str] = {}
        current_n = None
        current_parts: list[str] = []
        for line in raw.split("\n"):
            m = _BLOCK_IDX_RE.match(line.strip())
            if m:
                n = int(m.group(1))
                if 0 <= n < len(indices):
                    if current_n is not None:
                        parsed[current_n] = " ".join(current_parts).strip()
                    current_n = n
                    current_parts = [line.strip()[m.end():].strip()]
                    continue
            if current_n is not None:
                current_parts.append(line.strip())
        if current_n is not None:
            parsed[current_n] = " ".join(current_parts).strip()

        # Length-ratio sanity check; outliers retry as singletons.
        results: dict[int, str] = {}
        for n, i in enumerate(indices):
            src_text = blocks[i].text
            cand = parsed.get(n, "")
            ratio = len(cand) / max(len(src_text), 1)
            if cand and 0.4 <= ratio <= 2.5:
                results[i] = cand
            else:
                print(f"      Re-translating block {i} individually "
                      f"(chunk alignment/length mismatch: {len(cand)}/{len(src_text)})")
                results[i] = self.translator.translate(src_text)
        return results

    def _translate_blocks(self, blocks):
        results: dict = {}

        for i, b in enumerate(blocks):
            if isinstance(b, Heading):
                # Strip leading number prefix (e.g. "1.") before translating
                # so the LLM doesn't drop it, then re-prepend after.
                prefix = ""
                text = b.text
                m = re.match(r'^(\d+(?:\.\d+)*[\.\)]\s*)', text)
                if m:
                    prefix = m.group(1)
                    text = text[m.end():]
                translated = _translate_preserving_fn(
                    text, self.translator.translate)
                results[i] = prefix + translated

        li_blocks = [(i, b) for i, b in enumerate(blocks)
                     if isinstance(b, ListItem)]
        if li_blocks:
            print(f"  Translating {len(li_blocks)} list-item(s)...")
            for i, b in li_blocks:
                title = self.translator.translate(b.title)
                m = _LIST_MARKER_RE.match(b.title)
                if m and not _LIST_MARKER_RE.match(title):
                    title = f"{m.group(1)} {title.lstrip()}"
                body = _translate_preserving_fn(
                    b.body_text, self.translator.translate)
                results[i] = (title, body)

        body_idx = [i for i, b in enumerate(blocks) if isinstance(b, BodyPara)]
        fn_idx = [i for i in body_idx if _FN_MARKER_RE.search(blocks[i].text)]
        plain_idx = [i for i in body_idx if i not in set(fn_idx)]

        if fn_idx:
            print(f"  Translating {len(fn_idx)} paragraph(s) with footnotes...")
            for i in fn_idx:
                results[i] = _translate_preserving_fn(
                    blocks[i].text, self.translator.translate)

        chunks = self._chunk_body_paras(plain_idx, blocks)
        print(f"  Translating {len(chunks)} body chunk(s) "
              f"({len(plain_idx)} paragraphs)...")
        for ci, chunk in enumerate(chunks):
            chars = sum(len(blocks[i].text) for i in chunk)
            print(f"    Chunk {ci+1}/{len(chunks)} "
                  f"({len(chunk)} block(s), {chars} chars)...")
            results.update(self._translate_chunk_indices(chunk, blocks))

        return results

    # ---- rendering -----------------------------------------------------

    def _render_heading(self, doc, block: Heading, translated: str):
        heading = doc.add_heading(translated, level=min(block.level, 4))
        all_italic = (block.runs
                      and all(r.italic for r in block.runs if r.stripped_len))
        for run in heading.runs:
            run.font.color.rgb = RGBColor(0, 0, 0)
            if all_italic:
                run.italic = True

    @staticmethod
    def _emit_text_with_fn_refs(para, text, *, size=11,
                                bold=False, italic=False):
        """Write `text` to `para`, splitting on footnote-ref markers
        (`‹FN{id}›`) and emitting those ids as superscript runs."""
        parts = _FN_MARKER_RE.split(text)
        # Odd indices are captured footnote IDs; even indices are text.
        for idx, part in enumerate(parts):
            if not part:
                continue
            run = para.add_run(part)
            run.font.size = Pt(size)
            if idx % 2 == 1:
                run.font.superscript = True
            else:
                if bold:
                    run.bold = True
                if italic:
                    run.italic = True

    def _render_body(self, doc, block: BodyPara, translated: str):
        para = doc.add_paragraph()
        strip_runs = [r for r in block.runs if r.stripped_len]
        all_bold = bool(strip_runs) and all(r.bold for r in strip_runs)
        all_italic = bool(strip_runs) and all(r.italic for r in strip_runs)

        if all_bold or all_italic:
            self._emit_text_with_fn_refs(
                para, translated, bold=all_bold, italic=all_italic)
            return

        # Detect bold lead-in: first run(s) bold, rest not.
        # Count how many sentences are bold in the source, bold that many in the translation.
        if strip_runs and strip_runs[0].bold:
            bold_text = "".join(r.text for r in strip_runs if r.bold)
            bold_sents = _SENT_SPLIT_RE.split(bold_text)
            n_bold = len([s for s in bold_sents if s.strip()]) or 1

            trans_sents = _SENT_SPLIT_RE.split(translated)
            trans_sents = [s for s in trans_sents if s.strip()]

            if n_bold < len(trans_sents):
                bold_part = " ".join(trans_sents[:n_bold])
                rest_part = " ".join(trans_sents[n_bold:])
                self._emit_text_with_fn_refs(para, bold_part, bold=True)
                run = para.add_run(" ")
                run.font.size = Pt(11)
                self._emit_text_with_fn_refs(para, rest_part)
                return

        self._emit_text_with_fn_refs(para, translated)

    def _render_list_item(self, doc, block: ListItem,
                          trans_title: str, trans_body: str):
        para = doc.add_paragraph()
        t = para.add_run(trans_title)
        t.font.size = Pt(11)
        t.italic = True
        sep = para.add_run(block.separator)
        sep.font.size = Pt(11)
        self._emit_text_with_fn_refs(para, trans_body)

    def _render_image(self, doc, block: ImageBlock):
        try:
            para = doc.add_paragraph()
            run = para.add_run()
            run.add_picture(io.BytesIO(block.data),
                            width=Inches(block.width_inches))
            return True
        except Exception as e:
            print(f"  Warning: could not insert image: {e}")
            return False

    # ---- orchestration -------------------------------------------------

    def translate_to_docx(self, blocks, tables, output_path):
        out_p = Path(output_path)
        tables_path = str(out_p.parent / f"{out_p.stem}_tables{out_p.suffix}")
        footnotes_path = str(out_p.parent / f"{out_p.stem}_footnotes{out_p.suffix}")
        comments_path = str(out_p.parent / f"{out_p.stem}_comments{out_p.suffix}")

        footnote_blocks = [b for b in blocks if isinstance(b, Footnote)]
        comment_blocks = [b for b in blocks if isinstance(b, Comment)]
        image_blocks = [b for b in blocks if isinstance(b, ImageBlock)]
        table_blocks = [b for b in blocks if isinstance(b, TablePlaceholder)]
        chars_in = sum(len(b.text) for b in blocks if isinstance(b, BodyPara))
        chars_in += sum(len(b.body_text) + len(b.title)
                        for b in blocks if isinstance(b, ListItem))
        chars_in += sum(len(b.text) for b in blocks if isinstance(b, Heading))

        print(f"Extracted {len(blocks)} blocks "
              f"({chars_in} chars, {len(table_blocks)} table(s), "
              f"{len(image_blocks)} image(s), "
              f"{len(footnote_blocks)} footnote(s), "
              f"{len(comment_blocks)} comment(s))")

        translated = self._translate_blocks(blocks)

        doc = Document()
        img_count = 0
        for i, block in enumerate(blocks):
            if isinstance(block, ImageBlock):
                if self._render_image(doc, block):
                    img_count += 1
            elif isinstance(block, TablePlaceholder):
                doc.add_paragraph(f"[TABLE {block.index}]")
            elif isinstance(block, Separator):
                doc.add_paragraph(block.text)
            elif isinstance(block, Heading):
                self._render_heading(doc, block, translated[i])
            elif isinstance(block, ListItem):
                t_title, t_body = translated[i]
                self._render_list_item(doc, block, t_title, t_body)
            elif isinstance(block, BodyPara):
                self._render_body(doc, block, translated[i])
            # Footnote and Comment blocks are routed below to their own
            # sibling output files; not rendered into the body docx.
        doc.save(output_path)
        print(f"Saved translated text to {output_path}"
              + (f" ({img_count} image(s) preserved)" if img_count else ""))

        if footnote_blocks:
            ft = FootnoteTranslator(self.translator)
            ft.save_to_docx(ft.translate(footnote_blocks), footnotes_path)

        if comment_blocks:
            cx = CommentTranslator(self.translator)
            cx.save_to_docx(cx.translate(comment_blocks), comments_path)

        if tables:
            print(f"  Translating {len(tables)} table(s)...")
            tt = TableTranslator(self.translator)
            translated_tables = tt.translate(tables)
            tt.save_to_docx(translated_tables,
                            set(range(len(translated_tables))), tables_path)

        return {
            "text_output": output_path,
            "tables_output": tables_path if tables else None,
            "footnotes_output": footnotes_path if footnote_blocks else None,
            "comments_output": comments_path if comment_blocks else None,
            "total_blocks": len(blocks),
            "total_tables": len(tables),
            "total_images": len(image_blocks),
            "total_footnotes": len(footnote_blocks),
            "total_comments": len(comment_blocks),
            "chars_in": chars_in,
        }


def translate_docx(filepath, output_path, source_lang="Spanish",
                   target_lang="English", model="translategemma",
                   glossary=None):
    """Translate a .docx file. DocxExtractor pulls body, tables,
    footnotes, and comments; DocumentTranslator handles all routing."""
    from docx_extract import DocxExtractor

    extractor = DocxExtractor(filepath)
    blocks, tables = extractor.extract()
    extractor.close()

    t = Translator(source_lang=source_lang, target_lang=target_lang,
                   model=model, glossary=glossary)
    dt = DocumentTranslator(t)
    result = dt.translate_to_docx(blocks, tables, output_path)
    result["input"] = filepath
    return result


def translate_pdf(filepath, output_path, source_lang="Spanish",
                  target_lang="English", model="translategemma",
                  glossary=None):
    """Translate a .pdf file via the PyMuPDF extractor and the shared
    DocumentTranslator."""
    from pdf_extract import PdfExtractor

    extractor = PdfExtractor(filepath)
    try:
        blocks, tables = extractor.extract()
    finally:
        extractor.close()

    t = Translator(source_lang=source_lang, target_lang=target_lang,
                   model=model, glossary=glossary)
    dt = DocumentTranslator(t)
    return dt.translate_to_docx(blocks, tables, output_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Translate a document (.docx or .pdf)")
    parser.add_argument("input_path",
                        help="Path to the input .docx or .pdf file")
    parser.add_argument("output_docx", help="Path for the translated output .docx")
    parser.add_argument("--source-lang", default="Spanish")
    parser.add_argument("--target-lang", default="English")
    parser.add_argument("--model", default="translategemma")
    args = parser.parse_args()

    fn = translate_pdf if args.input_path.lower().endswith(".pdf") else translate_docx
    fn(args.input_path, args.output_docx,
       source_lang=args.source_lang, target_lang=args.target_lang,
       model=args.model)
