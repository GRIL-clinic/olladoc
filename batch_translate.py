"""
Usage:
    python batch_translate.py ./pdfs ./translated --source-lang Spanish
"""

from pathlib import Path
from pdf_translate import Translator, PDFTranslator


def batch_translate(input_dir, output_dir, source_lang,
                    target_lang="English", model="translategemma",
                    ocr_langs="spa+eng"):
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(input_path.glob("*.pdf"))
    print(f"Found {len(pdfs)} PDF(s) in {input_dir}")

    if not pdfs:
        return []

    t = Translator(source_lang=source_lang, target_lang=target_lang, model=model)
    pdf = PDFTranslator(t, ocr_langs=ocr_langs)

    results = []
    for i, filepath in enumerate(pdfs):
        print(f"\n[{i+1}/{len(pdfs)}] {filepath.name}")
        out_file = output_path / f"{filepath.stem}_translated.txt"

        try:
            meta = pdf.translate_file(str(filepath), str(out_file))
            results.append(meta)
        except Exception as e:
            print(f"  Error: {e}")
            results.append({"input": str(filepath), "error": str(e)})

    print(f"\nDone. {len(results)} file(s) processed.")
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Batch translate PDFs")
    parser.add_argument("input_dir", help="Directory containing PDFs")
    parser.add_argument("output_dir", help="Directory for translated output")
    parser.add_argument("--source-lang", default="Spanish")
    parser.add_argument("--target-lang", default="English")
    parser.add_argument("--model", default="translategemma")
    parser.add_argument("--ocr-langs", default="spa+eng")
    args = parser.parse_args()

    batch_translate(
        args.input_dir, args.output_dir,
        source_lang=args.source_lang, target_lang=args.target_lang,
        model=args.model, ocr_langs=args.ocr_langs,
    )
