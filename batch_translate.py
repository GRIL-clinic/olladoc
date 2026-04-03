"""
Usage:
    python batch_translate.py ./docs/short-pdfs ./translated-short-pdfs --source-lang Spanish
    python batch_translate.py ./short-docs ./translated-short-docs --source-lang Spanish
"""

from pathlib import Path
from translate import Translator, DocumentTranslator
from pdf_translate_pymupdf import translate_pdf

SUPPORTED_EXTENSIONS = {".docx", ".pdf"}


def batch_translate(input_dir, output_dir, source_lang="Spanish",
                    target_lang="English", model="translategemma",
                    glossary=None):
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    files = sorted(
        f for f in input_path.iterdir()
        if f.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    print(f"Found {len(files)} file(s) in {input_dir}")

    if not files:
        return []

    t = Translator(source_lang=source_lang, target_lang=target_lang, model=model,
                   glossary=glossary)
    dt = DocumentTranslator(t)

    results = []
    for i, filepath in enumerate(files):
        print(f"\n[{i+1}/{len(files)}] {filepath.name}")
        out_file = output_path / f"{filepath.stem}_translated.docx"
        try:
            if filepath.suffix.lower() == ".pdf":
                meta = translate_pdf(
                    str(filepath), str(out_file),
                    source_lang=source_lang, target_lang=target_lang,
                    model=model, glossary=glossary,
                )
            else:
                meta = dt.translate_to_docx(str(filepath), str(out_file))
            results.append(meta)
        except Exception as e:
            print(f"  Error: {e}")
            results.append({"input": str(filepath), "error": str(e)})

    print(f"\nDone. {len(results)} file(s) processed.")
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Batch translate Word documents")
    parser.add_argument("input_dir", help="Directory containing .docx/.pdf files")
    parser.add_argument("output_dir", help="Directory for translated output")
    parser.add_argument("--source-lang", default="Spanish")
    parser.add_argument("--target-lang", default="English")
    parser.add_argument("--model", default="translategemma")
    args = parser.parse_args()

    batch_translate(
        args.input_dir, args.output_dir,
        source_lang=args.source_lang, target_lang=args.target_lang,
        model=args.model,
    )
