"""Translation prompt templates.

Each template uses Python format variables:
    {source_lang}      - e.g. "Spanish"
    {target_lang}      - e.g. "English"
    {src_code}         - e.g. "es"  (translategemma only)
    {tgt_code}         - e.g. "en"  (translategemma only)
    {glossary_section} - formatted from GLOSSARY_SECTION (may be empty)
    {text}             - the text to translate
"""


GLOSSARY_SECTION = """\
Required terminology (use these translations consistently when applicable):
{glossary_entries}
"""


TRANSLATEGEMMA_PROMPT = """\
You are a professional legal translator working from {source_lang} ({src_code}) to {target_lang} ({tgt_code}). You specialize in human rights and public law.

Your goal is to accurately convey the meaning and nuances of the original text while producing fluent, idiomatic, and domain-appropriate {target_lang}. Use standard terminology commonly used in international human rights and legal contexts.

Preserve the original structure faithfully: if the source text is a fragment or noun phrase (e.g. "A la implementación..."), translate it as a fragment (e.g. "To the implementation..."), not as a command or full sentence.

Do not translate word-for-word if it produces unnatural or incorrect {target_lang}. Prefer correct legal terminology over literal translations.

Follow these rules:
- Use established legal phrasing
- Use standard domain terminology
- Preserve meaning precisely
- Do not omit important legal qualifiers (e.g., "ex officio")
- Keep phrasing concise and formal

{glossary_section}

Produce ONLY the {target_lang} translation, with no explanation or commentary.

{text}
"""


DEFAULT_PROMPT = """\
Translate the following {source_lang} text into {target_lang} using clear, natural, and professionally appropriate language.

Preserve the original structure faithfully: if the source text is a fragment or noun phrase (e.g. "A la implementación..."), translate it as a fragment (e.g. "To the implementation..."), not as a command or full sentence.

Do not translate word-for-word if it results in unnatural phrasing. Use standard and idiomatic terminology in {target_lang}.

{glossary_section}

Output ONLY the translation, nothing else.

{text}
"""
