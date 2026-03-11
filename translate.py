"""
Translation Classes
-------------------
Translator: Translates text using a local LLM via Ollama.
TableTranslator: Extracts and translates tables from Word documents.
FootnoteTranslator: Extracts and translates footnotes from Word documents.
CommentTranslator: Extracts and translates comments from Word documents.
DocumentTranslator: Orchestrates full .docx translation.
"""

import re
import ollama
from pathlib import Path
from docx import Document
from lxml import etree
from prompts import TRANSLATEGEMMA_PROMPT, DEFAULT_PROMPT, GLOSSARY_SECTION


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

    def translate(self, text):
        resp = ollama.chat(
            model=self.model,
            messages=[{"role": "user", "content": self._build_prompt(text)}],
            options={"temperature": self.model_temp},
        )
        return resp["message"]["content"].strip()


class TableTranslator:
    """Extracts and translates tables from .docx files."""

    def __init__(self, translator):
        self.translator = translator

    def extract(self, filepath):
        """Extract tables directly from a .docx.
        Deduplicates consecutive cells with identical text (merged cells)."""
        doc = Document(filepath)
        tables = []
        for table in doc.tables:
            rows = []
            for row in table.rows:
                deduped = []
                for cell in row.cells:
                    if not deduped or cell.text != deduped[-1]:
                        deduped.append(cell.text)
                rows.append(deduped)
            tables.append(rows)
        return tables

    def translate(self, tables):
        """Translate each non-empty cell in a list of tables."""
        translated = []
        for table in tables:
            translated_rows = []
            for row in table:
                translated_rows.append([
                    self.translator.translate(cell) if cell.strip() else cell
                    for cell in row
                ])
            translated.append(translated_rows)
        return translated

    def save_to_docx(self, translated_tables, referenced_indices, output_path):
        """Save translated tables to a docx file, labeled by index."""
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

    def translate_to_docx(self, filepath, output_path):
        """Extract all tables, translate them, and save to docx."""
        tables = self.extract(filepath)
        if not tables:
            return {"input": filepath, "output": output_path, "total_tables": 0}

        print(f"  Translating {len(tables)} table(s)...")
        translated = self.translate(tables)
        self.save_to_docx(translated, set(range(len(translated))), output_path)

        return {
            "input": filepath,
            "output": output_path,
            "total_tables": len(tables),
        }


class FootnoteTranslator:
    """Extracts and translates footnotes from .docx files."""

    _W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def __init__(self, translator):
        self.translator = translator

    def extract(self, filepath):
        """Extract footnotes directly from a .docx file using its XML structure.
        Returns list of {"number": str, "text": str}."""
        if Path(filepath).suffix.lower() != ".docx":
            print(f"Footnote extraction requires .docx, skipping {filepath}")
            return []

        doc = Document(filepath)
        footnotes = []

        for rel in doc.part.rels.values():
            if "footnote" not in rel.reltype.lower():
                continue
            root = etree.fromstring(rel.target_part.blob)
            for fn in root.findall(f"{{{self._W}}}footnote"):
                fn_id = fn.get(f"{{{self._W}}}id")
                fn_type = fn.get(f"{{{self._W}}}type", "")
                if fn_type in ("separator", "continuationSeparator"):
                    continue
                texts = []
                for t in fn.iter(f"{{{self._W}}}t"):
                    if t.text:
                        texts.append(t.text)
                text = "".join(texts).strip()
                if text:
                    footnotes.append({"number": str(len(footnotes) + 1), "text": text})

        print(f"Extracted {len(footnotes)} footnotes from {filepath}")
        return footnotes

    def translate(self, footnotes):
        """Translate a list of extracted footnotes.

        Args:
            footnotes: list of {"number": str, "text": str}
        Returns:
            The same list, with "translation" added to each dict.
        """
        print(f"  Translating {len(footnotes)} footnotes...")
        for fn in footnotes:
            fn["translation"] = self.translator.translate(fn["text"])
        return footnotes

    def save_to_docx(self, footnotes, output_path):
        """Save translated footnotes as a docx table.

        Args:
            footnotes: list of {"number": str, "text": str, "translation": str}
            output_path: path for the output .docx
        """
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

    def translate_to_docx(self, filepath, output_path):
        """Extract footnotes, translate them, and save as a docx table."""
        footnotes = self.extract(filepath)
        if not footnotes:
            return {"input": filepath, "output": output_path, "total_footnotes": 0}

        self.translate(footnotes)
        self.save_to_docx(footnotes, output_path)

        return {
            "input": filepath,
            "output": output_path,
            "total_footnotes": len(footnotes),
            "footnotes": footnotes,
        }


class CommentTranslator:
    """Extracts and translates comments from .docx files."""

    _W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def __init__(self, translator):
        self.translator = translator

    def _get_comment_text(self, comment_elem):
        texts = []
        for t in comment_elem.iter(f"{{{self._W}}}t"):
            if t.text:
                texts.append(t.text)
        return "".join(texts).strip()

    def _get_anchor_text(self, body, comment_id):
        start = body.find(
            f".//{{{self._W}}}commentRangeStart[@{{{self._W}}}id='{comment_id}']"
        )
        end = body.find(
            f".//{{{self._W}}}commentRangeEnd[@{{{self._W}}}id='{comment_id}']"
        )
        if start is None or end is None:
            return ""
        collecting = False
        texts = []
        for elem in body.iter():
            if elem is start:
                collecting = True
                continue
            if elem is end:
                break
            if collecting and elem.tag == f"{{{self._W}}}t" and elem.text:
                texts.append(elem.text)
        return "".join(texts).strip()

    def extract(self, filepath):
        doc = Document(filepath)

        comments_part = None
        for rel in doc.part.rels.values():
            if rel.reltype.endswith("/comments"):
                comments_part = rel.target_part
                break

        if not comments_part:
            print("No comments found in document.")
            return []

        root = comments_part._element
        comment_elems = root.findall(f"{{{self._W}}}comment")

        # Build a map of comment id -> position in document body
        body = doc.element.body
        body_order = {}
        for i, elem in enumerate(body.iter()):
            if elem.tag == f"{{{self._W}}}commentRangeStart":
                body_order[elem.get(f"{{{self._W}}}id")] = i

        comments = []
        for elem in comment_elems:
            cid = elem.get(f"{{{self._W}}}id")
            author = elem.get(f"{{{self._W}}}author", "")
            date = elem.get(f"{{{self._W}}}date", "")
            text = self._get_comment_text(elem)
            anchor = self._get_anchor_text(body, cid)

            if text:
                comments.append({
                    "id": cid,
                    "author": author,
                    "date": date,
                    "text": text,
                    "anchor": anchor,
                })

        comments.sort(key=lambda c: body_order.get(c["id"], float("inf")))
        print(f"Extracted {len(comments)} comments from {filepath}")
        return comments

    def translate(self, filepath):
        comments = self.extract(filepath)

        translated_comments = []
        for i, c in enumerate(comments):
            print(f"  Translating comment {i+1}/{len(comments)}...")
            translation = self.translator.translate(c["text"])
            anchor_translation = ""
            if c["anchor"]:
                anchor_translation = self.translator.translate(c["anchor"])
            translated_comments.append({
                **c,
                "translation": translation,
                "anchor_translation": anchor_translation,
            })

        return translated_comments

    def translate_to_docx(self, filepath, output_path):
        """Translate comments and write them to a .docx as a table."""
        translated = self.translate(filepath)

        doc = Document()
        doc.add_paragraph("Translated Comments")

        if not translated:
            doc.add_paragraph("No comments found.")
            doc.save(output_path)
            return {"input": filepath, "output": output_path, "total_comments": 0}

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

        return {
            "input": filepath,
            "output": output_path,
            "total_comments": len(translated),
            "comments": translated,
        }


class DocumentTranslator:
    """Orchestrates full .docx translation.

    Translates body text, tables, footnotes, and comments, writing each
    to a separate output file.
    """

    _W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def __init__(self, translator):
        self.translator = translator

    @classmethod
    def _extract_text_with_placeholders(cls, filepath):
        """Extract paragraph text from a .docx, inserting [TABLE N] placeholders
        where tables appear in the document flow. Footnote reference numbers
        are included as inline text.

        Returns (parts, table_count) where parts is a list of paragraph strings."""
        doc = Document(filepath)
        body = doc.element.body
        table_idx = 0
        parts = []

        for child in body:
            if child.tag == f"{{{cls._W}}}tbl":
                table_idx += 1
                parts.append(f"[TABLE {table_idx}]")
            elif child.tag == f"{{{cls._W}}}p":
                para_text = ""
                for elem in child.iter():
                    if elem.tag == f"{{{cls._W}}}t" and elem.text:
                        para_text += elem.text
                    elif elem.tag == f"{{{cls._W}}}footnoteReference":
                        fn_id = elem.get(f"{{{cls._W}}}id")
                        if fn_id:
                            para_text += fn_id
                if para_text.strip():
                    parts.append(para_text)

        return parts, table_idx

    @staticmethod
    def _chunk_paragraphs(parts, max_chars=3000):
        """Group paragraphs into chunks that fit within max_chars."""
        chunks = []
        current = ""
        for para in parts:
            if current and len(current) + len(para) + 1 > max_chars:
                chunks.append(current)
                current = para
            else:
                current = current + "\n" + para if current else para
        if current:
            chunks.append(current)
        return chunks

    def translate_to_docx(self, input_path, output_path):
        """Translate a .docx and write four files:
        - output_path: translated text with [TABLE N] placeholders
        - {stem}_tables.docx: translated tables
        - {stem}_footnotes.docx: translated footnotes
        - {stem}_comments.docx: translated comments
        """
        out_p = Path(output_path)
        tables_path = str(out_p.parent / f"{out_p.stem}_tables{out_p.suffix}")
        footnotes_path = str(out_p.parent / f"{out_p.stem}_footnotes{out_p.suffix}")
        comments_path = str(out_p.parent / f"{out_p.stem}_comments{out_p.suffix}")

        # Text
        parts, num_tables = self._extract_text_with_placeholders(input_path)
        total_chars = sum(len(p) for p in parts)
        print(f"Extracted text ({total_chars} chars, {num_tables} tables)")

        chunks = self._chunk_paragraphs(parts)
        print(f"  Translating {len(chunks)} text chunk(s)...")
        translated_chunks = []
        for ci, chunk in enumerate(chunks):
            print(f"    Chunk {ci+1}/{len(chunks)} ({len(chunk)} chars)...")
            translated_chunks.append(self.translator.translate(chunk))

        doc = Document()
        for chunk in translated_chunks:
            doc.add_paragraph(chunk)
        doc.save(output_path)
        print(f"Saved translated text to {output_path}")

        # Tables
        tt = TableTranslator(self.translator)
        tt.translate_to_docx(input_path, tables_path)

        # Footnotes
        ft = FootnoteTranslator(self.translator)
        ft.translate_to_docx(input_path, footnotes_path)

        # Comments
        cx = CommentTranslator(self.translator)
        cx.translate_to_docx(input_path, comments_path)

        return {
            "input": input_path,
            "text_output": output_path,
            "tables_output": tables_path,
            "footnotes_output": footnotes_path,
            "comments_output": comments_path,
            "total_tables": num_tables,
            "total_chunks": len(chunks),
            "chars_in": total_chars,
            "chars_out": sum(len(c) for c in translated_chunks),
        }


def translate_docx(filepath, output_path, source_lang="Spanish",
                   target_lang="English", model="translategemma",
                   glossary=None):
    """Translate a .docx file."""
    t = Translator(source_lang=source_lang, target_lang=target_lang, model=model,
                   glossary=glossary)
    dt = DocumentTranslator(t)
    return dt.translate_to_docx(filepath, output_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Translate a Word document")
    parser.add_argument("input_docx", help="Path to the input .docx file")
    parser.add_argument("output_docx", help="Path for the translated output .docx")
    parser.add_argument("--source-lang", default="Spanish")
    parser.add_argument("--target-lang", default="English")
    parser.add_argument("--model", default="translategemma")
    args = parser.parse_args()

    translate_docx(
        args.input_docx, args.output_docx,
        source_lang=args.source_lang, target_lang=args.target_lang,
        model=args.model,
    )
