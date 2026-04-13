"""
Docx extractor
--------------
Walks a .docx and produces a list of Blocks (Heading, BodyPara,
ListItem, TablePlaceholder, Footnote, Comment) plus the raw table data.
"""

import re
from docx import Document
from lxml import etree
from typing import List

from blocks import (Run, Heading, BodyPara, ListItem, TablePlaceholder,
                    Footnote, Comment, Block)


_HEADING_STYLE_RE = re.compile(r"Heading\s*(\d+)", re.IGNORECASE)
_LIST_STYLE_RE = re.compile(r"List\s+(Number|Bullet|Paragraph)",
                            re.IGNORECASE)
_LIST_MARKER_RE = re.compile(r'^\s*\d+[\.\)]\s+\S')


class DocxExtractor:
    """Reads a .docx and emits Blocks + raw tables."""

    _W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def __init__(self, filepath):
        self.filepath = filepath
        self.doc = Document(filepath)

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _heading_level(para) -> int:
        try:
            name = (para.style.name or "").strip()
        except Exception:
            return 0
        m = _HEADING_STYLE_RE.match(name)
        return max(1, min(6, int(m.group(1)))) if m else 0

    @staticmethod
    def _is_list_paragraph(para) -> bool:
        try:
            name = (para.style.name or "").strip()
        except Exception:
            name = ""
        if _LIST_STYLE_RE.search(name):
            return True
        # Numbered/bulleted via numPr element
        ppr = para._p.find(f"{{{DocxExtractor._W}}}pPr")
        if ppr is not None and ppr.find(f"{{{DocxExtractor._W}}}numPr") is not None:
            return True
        return _LIST_MARKER_RE.match(para.text or "") is not None

    @classmethod
    def _para_runs(cls, para) -> List[Run]:
        """Convert a paragraph's runs into Run model. Footnote
        references (inline superscripts with no <w:t>) are emitted as
        sentinel markers of the form ``‹FN{id}›`` so they survive
        translation and can be re-rendered as superscript runs by the
        translator's docx writer."""
        out: List[Run] = []
        fn_tag = f"{{{cls._W}}}footnoteReference"
        for r in para.runs:
            text = r.text or ""
            fn_ref = r._r.find(fn_tag)
            if fn_ref is not None:
                fn_id = fn_ref.get(f"{{{cls._W}}}id") or ""
                text += f"‹FN{fn_id}›"
            if not text:
                continue
            out.append(Run(text=text,
                           bold=bool(r.bold),
                           italic=bool(r.italic),
                           size=float(r.font.size.pt) if r.font.size else 0.0))
        return Run.consolidate(out)

    # ---- table extraction ----------------------------------------------

    @staticmethod
    def _extract_table_rows(table):
        """Extract table cell text, deduping merged cells in each row."""
        rows = []
        for row in table.rows:
            deduped = []
            for cell in row.cells:
                if not deduped or cell.text != deduped[-1]:
                    deduped.append(cell.text)
            rows.append(deduped)
        return rows

    # ---- footnote / comment extraction (docx XML) ---------------------

    def _extract_footnotes(self) -> List[Footnote]:
        out: List[Footnote] = []
        for rel in self.doc.part.rels.values():
            if "footnote" not in rel.reltype.lower():
                continue
            root = etree.fromstring(rel.target_part.blob)
            for fn in root.findall(f"{{{self._W}}}footnote"):
                fn_type = fn.get(f"{{{self._W}}}type", "")
                if fn_type in ("separator", "continuationSeparator"):
                    continue
                texts = [t.text for t in fn.iter(f"{{{self._W}}}t") if t.text]
                text = "".join(texts).strip()
                if text:
                    out.append(Footnote(marker=str(len(out) + 1), text=text))
        return out

    @staticmethod
    def _comment_text(comment_elem, ns):
        return "".join(t.text for t in comment_elem.iter(f"{{{ns}}}t")
                       if t.text).strip()

    @classmethod
    def _anchor_text(cls, body, comment_id):
        ns = cls._W
        start = body.find(f".//{{{ns}}}commentRangeStart[@{{{ns}}}id='{comment_id}']")
        end = body.find(f".//{{{ns}}}commentRangeEnd[@{{{ns}}}id='{comment_id}']")
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
            if collecting and elem.tag == f"{{{ns}}}t" and elem.text:
                texts.append(elem.text)
        return "".join(texts).strip()

    def _extract_comments(self) -> List[Comment]:
        """Pull review comments from docx comments, ordered by
        their anchor position in the body."""
        comments_part = None
        for rel in self.doc.part.rels.values():
            if rel.reltype.endswith("/comments"):
                comments_part = rel.target_part
                break
        if comments_part is None:
            return []

        body = self.doc.element.body
        body_order = {}
        for i, elem in enumerate(body.iter()):
            if elem.tag == f"{{{self._W}}}commentRangeStart":
                body_order[elem.get(f"{{{self._W}}}id")] = i

        out: List[Comment] = []
        for elem in comments_part._element.findall(f"{{{self._W}}}comment"):
            cid = elem.get(f"{{{self._W}}}id")
            text = self._comment_text(elem, self._W)
            if not text:
                continue
            out.append(Comment(
                id=cid,
                author=elem.get(f"{{{self._W}}}author", ""),
                date=elem.get(f"{{{self._W}}}date", ""),
                text=text,
                anchor=self._anchor_text(body, cid),
            ))
        out.sort(key=lambda c: body_order.get(c.id, float("inf")))
        return out

    # ---- main extract --------------------------------------------------

    def extract(self) -> tuple[List[Block], list]:
        body = self.doc.element.body
        # Map xml elements to python-docx Paragraph / Table objects.
        para_by_elem = {p._p: p for p in self.doc.paragraphs}
        table_by_elem = {t._tbl: t for t in self.doc.tables}

        blocks: List[Block] = []
        tables: list = []
        table_idx = 0

        for child in body:
            tag = child.tag
            if tag == f"{{{self._W}}}tbl":
                tbl = table_by_elem.get(child)
                if tbl is not None:
                    tables.append(self._extract_table_rows(tbl))
                    table_idx += 1
                    blocks.append(TablePlaceholder(index=table_idx))
                continue
            if tag != f"{{{self._W}}}p":
                continue
            para = para_by_elem.get(child)
            if para is None or not (para.text or "").strip():
                continue

            level = self._heading_level(para)
            runs = self._para_runs(para)
            if not runs:
                continue

            if level > 0:
                blocks.append(Heading(level=level, runs=runs))
                continue

            if self._is_list_paragraph(para):
                title_text = (para.text or "").split(None, 1)[0]
                blocks.append(ListItem(
                    title=title_text, body_runs=runs, separator=" "))
                continue

            blocks.append(BodyPara(runs=runs))

        # Footnotes and comments are appended after body blocks; the
        # translator routes them to their own output files, so
        # their position in the list is incidental.
        blocks.extend(self._extract_footnotes())
        blocks.extend(self._extract_comments())

        return blocks, tables

    def close(self):
        pass  # python-docx doesn't hold a file handle once Document() returns
