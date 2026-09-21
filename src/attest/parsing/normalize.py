"""Text normalization helpers applied to parsed document markdown.

Docling's ``export_to_markdown()`` CommonMark-escapes special characters in
prose by prefixing them with a backslash (e.g. ``evidence_version_id`` becomes
``evidence\\_version\\_id``). See ``spike/RESULTS.md`` under "Tier 2 --
Document Parsing Quality (Docling)": if that escaped text is what gets
chunked and indexed, exact-identifier lexical search (FTS5) silently
degrades on any snake_case identifier, because the indexed token no longer
matches the identifier as the user would type or search for it.

``unescape_markdown`` reverses that escaping before chunking/indexing.

Empirical finding on fenced/inline code (verified against installed
Docling 2.129.0 by round-tripping small markdown documents through
``DocumentConverter`` + ``export_to_markdown()``): Docling does **not**
escape special characters inside fenced code blocks (```` ```...``` ````) or
inline code spans (`` `...` ``) -- only prose/inline text is escaped. For
example, prose ``under_score`` is exported as ``under\\_score``, but the same
text inside a fenced block or inline code span is left as ``under_score``.
This means there is nothing to reverse inside code regions, so applying
``unescape_markdown`` globally (without special-casing fences) is safe for
anything Docling itself produces. The one theoretical edge case this does
not protect against is source text whose *code* legitimately contains a
backslash immediately followed by one of the CommonMark special characters
(e.g. a regex literal like ``\\.``) -- such a sequence would also be
unescaped. This is considered an acceptable, out-of-scope limitation: Docling
never introduces that pattern itself (confirmed above), so it would only
bite on pre-existing literal backslash-escapes inside source code blocks,
which is rare and not something this checkpoint's fix needs to guard against.
"""

from __future__ import annotations

import re

# CommonMark backslash-escapable punctuation:
# \ ` * _ { } [ ] ( ) # + - . ! >
_ESCAPED_CHAR_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>])")


def unescape_markdown(text: str) -> str:
    """Reverse CommonMark backslash-escaping of special characters.

    E.g. ``evidence\\_version\\_id`` -> ``evidence_version_id``. This must run
    before chunking/indexing so exact-identifier lexical search sees the
    real identifier text rather than Docling's escaped markdown form.

    Idempotent: running this twice produces the same result as running it
    once, since the output contains no remaining ``\\<special char>``
    sequences for it to act on.
    """
    return _ESCAPED_CHAR_RE.sub(r"\1", text)
