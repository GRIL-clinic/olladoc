"""
Block data model
----------------
Shared between extractors (PDF, docx) and the document translator.

A document is a list of Blocks. Each Block is a discriminated type
(Heading, BodyPara, ListItem, Footnote, ImageBlock, TablePlaceholder, Separator).

Run is the atomic formatted text unit; both extractors emit Runs to preserve per-span bold/italic/size through the pipeline.
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Union


@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    size: float = 0.0

    @property
    def stripped_len(self) -> int:
        return len(self.text.strip())

    @staticmethod
    def consolidate(runs: List["Run"]) -> List["Run"]:
        """Merge adjacent runs that share formatting."""
        out: List[Run] = []
        for r in runs:
            if out and out[-1].bold == r.bold and out[-1].italic == r.italic \
                    and out[-1].size == r.size:
                out[-1] = Run(text=out[-1].text + r.text, bold=r.bold,
                              italic=r.italic, size=r.size)
            else:
                out.append(r)
        return out


def _runs_text(runs: List[Run]) -> str:
    return re.sub(r'\s+', ' ', "".join(r.text for r in runs)).strip()


def _runs_to_markdown(runs: List[Run]) -> str:
    """Encode runs as markdown, consolidating adjacent same-format runs.

    Bold+italic -> ***text***, bold -> **text**, italic -> *text*, plain -> text.
    Adjacent runs with identical formatting are merged before encoding so we don't emit *word* *word* when *word word* is correct.
    """
    consolidated = Run.consolidate(runs)
    parts = []
    for r in consolidated:
        if not r.text:
            continue
        text = r.text
        if r.bold and r.italic:
            parts.append(f"***{text}***")
        elif r.bold:
            parts.append(f"**{text}**")
        elif r.italic:
            parts.append(f"*{text}*")
        else:
            parts.append(text)
    return re.sub(r'\s+', ' ', ''.join(parts)).strip()


@dataclass
class Heading:
    level: int
    runs: List[Run]

    @property
    def text(self) -> str:
        return _runs_text(self.runs)

    def to_markdown(self) -> str:
        return _runs_to_markdown(self.runs)


@dataclass
class BodyPara:
    runs: List[Run]

    @property
    def text(self) -> str:
        return _runs_text(self.runs)

    def to_markdown(self) -> str:
        return _runs_to_markdown(self.runs)


@dataclass
class ListItem:
    title: str
    body_runs: List[Run]
    separator: str = " "
    translated_title: Optional[str] = None

    @property
    def body_text(self) -> str:
        return _runs_text(self.body_runs)

    def body_to_markdown(self) -> str:
        return _runs_to_markdown(self.body_runs)


@dataclass
class Footnote:
    marker: str
    text: str


@dataclass
class Comment:
    id: str
    author: str
    date: str
    text: str
    anchor: str = ""


@dataclass
class ImageBlock:
    data: bytes
    width_inches: float


@dataclass
class TablePlaceholder:
    index: int


@dataclass
class Separator:
    text: str


Block = Union[Heading, BodyPara, ListItem, Footnote, Comment,
              ImageBlock, TablePlaceholder, Separator]
