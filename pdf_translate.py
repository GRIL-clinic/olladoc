"""
PDF Translation Pipeline
-------------------------
Extracts text from PDFs  and translates using Ollama.
"""

import fitz  # pymupdf
import pytesseract
from PIL import Image
import io
import ollama


# Language name to ISO code mapping for TranslateGemma
LANG_CODES = {
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


class Translator:
    """Translates text using a local LLM via Ollama."""
    def __init__(self, source_lang, target_lang="English", model="translategemma", model_temp=0.3):
        self.source_lang = source_lang
        self.target_lang = target_lang
        # Default model is TranslateGemma: 
        # https://blog.google/innovation-and-ai/technology/developers-tools/translategemma/
        self.model = model
        self.model_temp = model_temp  # Low temp for more faithful translations

    def _build_prompt(self, text):
        if "translategemma" in self.model:
            src_code = LANG_CODES.get(self.source_lang, "es")
            tgt_code = LANG_CODES.get(self.target_lang, "en")
            return (
                f"You are a professional {self.source_lang} ({src_code}) to "
                f"{self.target_lang} ({tgt_code}) translator. "
                f"Your goal is to accurately convey the meaning and nuances of "
                f"the original {self.source_lang} text while adhering to "
                f"{self.target_lang} grammar, vocabulary, and cultural "
                f"sensitivities.\n"
                f"Produce only the {self.target_lang} translation, without any "
                f"additional explanations or commentary. "
                f"Please translate the following {self.source_lang} text into "
                f"{self.target_lang}:\n"
                f"\n\n{text}"
            )
        return (
            f"Translate the following {self.source_lang} text into "
            f"{self.target_lang}.\n"
            f"Output ONLY the translation, nothing else.\n\n{text}"
        )

    def translate(self, text):
        resp = ollama.chat(
            model=self.model,
            messages=[{"role": "user", "content": self._build_prompt(text)}],
            options={"temperature": self.model_temp},
        )
        return resp["message"]["content"].strip()


class PDFTranslator:
    def __init__(self, translator, ocr_langs="spa+eng", dpi=300, min_text_chars=50):
        self.translator = translator
        self.ocr_langs = ocr_langs
        self.dpi = dpi
        self.min_text_chars = min_text_chars

    def _detect_page_type(self, page):
        text = page.get_text().strip()
        if len(text) >= self.min_text_chars:
            return "text"
        return "scanned"

    def _ocr_page(self, page):
        pix = page.get_pixmap(dpi=self.dpi)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(img, lang=self.ocr_langs).strip()

    def extract(self, filepath):
        doc = fitz.open(filepath)
        pages = []

        for i, page in enumerate(doc):
            page_type = self._detect_page_type(page)
            if page_type == "text":
                text = page.get_text().strip()
            else:
                text = self._ocr_page(page)
            pages.append({"page": i + 1, "type": page_type, "text": text})

        doc.close()

        full_text = "\n\n".join(p["text"] for p in pages if p["text"])
        return {"full_text": full_text, "pages": pages}

    def translate(self, filepath):
        result = self.extract(filepath)
        pages = result["pages"]

        text_count = sum(1 for p in pages if p["type"] == "text")
        ocr_count = sum(1 for p in pages if p["type"] == "scanned")
        print(f"Extracted {len(pages)} pages ({text_count} text, {ocr_count} OCR)")

        translated_pages = []
        for p in pages:
            translation = ""
            if p["text"]:
                print(f"  Translating page {p['page']}/{len(pages)}...")
                translation = self.translator.translate(p["text"])
            translated_pages.append({
                "page": p["page"],
                "type": p["type"],
                "source": p["text"],
                "translation": translation,
            })

        return translated_pages

    def translate_file(self, input_path, output_path):
        translated_pages = self.translate(input_path)

        full_translation = "\n\n".join(
            p["translation"] for p in translated_pages if p["translation"]
        )

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(full_translation)

        print(f"Saved translation to {output_path}")

        return {
            "input": input_path,
            "output": output_path,
            "total_pages": len(translated_pages),
            "text_pages": sum(1 for p in translated_pages if p["type"] == "text"),
            "ocr_pages": sum(1 for p in translated_pages if p["type"] == "scanned"),
            "chars_in": sum(len(p["source"]) for p in translated_pages),
            "chars_out": len(full_translation),
            "pages": translated_pages,
        }
    