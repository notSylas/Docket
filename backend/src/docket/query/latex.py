"""Deterministic LaTeX-to-plain-text normalization for generated answers.

The system prompt (`docket.query.prompts.SYSTEM_PROMPT`) already tells the
model never to use LaTeX/markdown math syntax. It doesn't reliably comply:
a real eval run (93 physics questions, `docket eval run` against
`gold_physics.yaml`) came back with 28/93 answers (30%) still containing
`$...$`, `$$...$$`, `\\frac{}{}`, `\\times`, `\\varepsilon`, and similar --
e.g. literally ``The unit ... is A m$^{-1}$`` and
``approximately $10^{-14}$ m``.

This is not just a readability problem. `docket.eval.scoring.matches` does a
normalized substring match against gold facts (e.g. ``"A m -1"``,
``"~10 -14 m"``); a literal `$`/`{`/`}`/`\\frac` sitting in the middle of an
otherwise-correct number or unit breaks that match and fails an otherwise
correct answer. Since prompt compliance is probabilistic and this failure
mode is easy to characterize, `normalize_latex` fixes it deterministically,
independent of whatever the model actually did -- belt-and-suspenders with
the prompt instruction, not a replacement for it.

`normalize_latex` is intentionally narrow: it only handles the LaTeX
commands actually observed in real model output (see the eval artifact this
was built from) plus the handful of common physics-notation commands the
task called out explicitly (theta, pi, mu, lambda, Delta, sigma). It never
guesses at commands it hasn't seen a real example of -- an unrecognized
`\\command` is left alone rather than mangled.

Must never touch a real citation tag (`docket.retrieval.resolver
._citation_label`'s ``[source.pdf #chunk_id]`` format): those never contain
`$` or `\\`, so none of the patterns below can match inside one. See
`test_latex.py::test_citation_tag_survives_untouched`.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Remove balanced math delimiters only; unpaired dollar signs may be currency.
# Citation labels are protected separately because filenames can contain "$".
# ---------------------------------------------------------------------------

_MATH_DELIMITER_RE = re.compile(r"\$\$(.*?)\$\$|\$([^$\n]+)\$", re.DOTALL)
_CITATION_RE = re.compile(r"(\[[^\[\]]*#[^\[\]]*\])")


def _remove_math_delimiters(match: re.Match) -> str:
    body = match.group(1) if match.group(1) is not None else match.group(2)
    # "$100 and $200" is two prices, not a math span. Preserve ambiguous prose.
    if (match.group(1) is None and (
        re.fullmatch(r"\s*[\d,.]+\s*[-–—+]\s*", body)
        or (re.match(r"\s*\d", body) and re.search(r"[A-Za-z]{2,}", body)
            and not re.search(r"[\\_^=]", body))
    )):
        return match.group(0)
    return body

# LaTeX inline spacing commands seen in real output (e.g.
# "1 \, \text{eV} = 1.6 \times 10^{-19} \, \text{J}") -- each becomes a
# single space (or nothing, for the zero-width "\!").
_SPACING_RE = re.compile(r"\\[,;: ]")
_NEGSPACE_RE = re.compile(r"\\!")

# \mathbf{E} / \text{eV} / \vec{r} and similar "wrap content, change how it's
# rendered" commands -- physics notation cares about the bold/vector/roman
# meaning only visually; in plain text, the content is what matters.
_WRAPPER_RE = re.compile(
    r"\\(?:mathbf|mathrm|mathit|boldsymbol|mathcal|mathbb|operatorname|text)"
    r"\{([^{}]*)\}"
)

# Greek letters and math symbols seen in real eval output (\times, \frac,
# \mu, \varepsilon, \omega, \pi, \dots, \gg, \mathbf -- see the task's eval
# artifact grep) plus the minimum set called out explicitly (\theta, \pi,
# \mu, \lambda, \Delta, \sigma) and their close relatives. Longer/more
# specific names are not a problem here since none of these command names
# are prefixes of one another in ways that would misparse (e.g. "epsilon"
# and "varepsilon" are distinct words), but the lookahead below still
# guards against a real command name being a *prefix* of a longer unknown
# one (e.g. never matching "\thetaXYZ" as "\theta" + "XYZ").
_SYMBOL_MAP = {
    # symbols / operators
    "times": "×",
    "div": "÷",
    "pm": "±",
    "mp": "∓",
    "leq": "≤",
    "geq": "≥",
    "neq": "≠",
    "approx": "≈",
    "sim": "~",
    "cdot": "·",
    "infty": "∞",
    "partial": "∂",
    "nabla": "∇",
    "rightarrow": "→",
    "to": "→",
    "gg": "≫",
    "ll": "≪",
    "ldots": "…",
    "cdots": "…",
    "dots": "…",
    # greek (lowercase)
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "varepsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "vartheta": "θ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "pi": "π",
    "rho": "ρ",
    "sigma": "σ",
    "tau": "τ",
    "upsilon": "υ",
    "phi": "φ",
    "varphi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    # greek (uppercase)
    "Gamma": "Γ",
    "Delta": "Δ",
    "Lambda": "Λ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
}

# Longest names first so e.g. "varepsilon" is tried before a hypothetical
# shorter overlapping name; not strictly required given the current map (no
# command name is a prefix of another), but keeps this correct if the map
# grows. `(?![a-zA-Z])` (not `\b`) is required, not cosmetic: `\b` does NOT
# see a boundary between a letter and "_" (both are \w), so `\bvarepsilon\b`
# would fail to match inside "\varepsilon_0" -- a real pattern from the eval
# data.
_SYMBOL_RE = re.compile(
    "\\\\(" + "|".join(sorted(_SYMBOL_MAP, key=len, reverse=True)) + ")(?![a-zA-Z])"
)

# A braced exponent/subscript whose content is *only* digits and a leading
# sign -- the overwhelmingly common case in this domain ("10^{-19}",
# "A m$^{-1}$", "10^{-14}"). Rendered as real unicode super/subscript
# characters rather than bare "^-19": besides reading better, this is what
# makes `docket.eval.scoring.normalize_text`'s NFKC fold turn it back into
# plain "-19" for gold-fact matching, whereas a bare "^-19" would introduce a
# literal "^" character the gold text never has.
_NUMERIC_BRACED_RE = re.compile(r"[_^]\{\s*([+-]?\d+)\s*\}")

# The same notation, but without braces -- the model emits "10^-14" as
# often as "10^{-14}" (see the real example in this module's docstring:
# "~10^-14 m" from a live eval run). Restricted to a digit *immediately
# before* the `^`/`_` (lookbehind) so it only fires on genuine power-of-ten
# / numeric notation ("10^-14", "2^10") and never on an algebraic bare
# exponent whose base is a variable letter, e.g. "R^2" or "x^2" in
# "\frac{\mu_0 I R^2}{2(x^2 + R^2)^{3/2}}" or the subscript "q_1" in
# "q_1, q_2, \dots, q_n" -- both real eval examples (see
# test_biot_savart_frac_with_nested_braces and
# test_electric_field_mathbf_dots) that require the letter-based form to
# survive completely untouched. Subscripts get the same treatment for
# symmetry with the braced case above and in case a numeric subscript
# ("H_2" style) shows up in bare form too, even though no real eval sample
# has one yet -- the digit-lookbehind guard makes this safe either way.
_NUMERIC_BARE_RE = re.compile(r"(?<=\d)[_^]([+-]?\d+)")
_SUPERSCRIPT_RE = re.compile(r"\^\{([^{}]*)\}")
_SUBSCRIPT_RE = re.compile(r"_\{([^{}]*)\}")

_SUPER_DIGITS = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")
_SUB_DIGITS = str.maketrans("0123456789+-", "₀₁₂₃₄₅₆₇₈₉₊₋")

# Characters that make a \frac argument "complex" enough to need
# parenthesizing when flattened to "a/b" -- whitespace or another operator,
# e.g. the denominator of "\frac{\mu_0 I R^2}{2(x^2 + R^2)^{3/2}}".
_COMPLEX_ARG_RE = re.compile(r"[\s+\-*/]")


def _find_matching_brace(text: str, open_index: int) -> int:
    """Index of the `}` matching the `{` at `open_index`, or -1 if the
    braces in `text` from that point on never balance (malformed input --
    the caller falls back to leaving the source text alone)."""
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _format_frac(numerator: str, denominator: str) -> str:
    numerator = numerator.strip()
    denominator = denominator.strip()
    if _COMPLEX_ARG_RE.search(numerator) or _COMPLEX_ARG_RE.search(denominator):
        return f"({numerator})/({denominator})"
    return f"{numerator}/{denominator}"


def _replace_frac(text: str) -> str:
    """Flattens `\\frac{a}{b}` to `a/b` (or `(a)/(b)` for complex a/b).

    Hand-rolled brace matching rather than a regex: real answers nest braces
    inside a \\frac argument (e.g. the denominator `2(x^2 + R^2)^{3/2}` of
    `\\frac{\\mu_0 I R^2}{2(x^2 + R^2)^{3/2}}` contains its own `{3/2}`), and
    a `[^{}]*`-style regex can't see past that. `a`/`b` are left otherwise
    unprocessed here -- the later greek/symbol/exponent passes run over the
    whole string afterward and clean up whatever ends up inside them.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("\\frac{", i):
            open1 = i + 5
            close1 = _find_matching_brace(text, open1)
            if close1 != -1 and close1 + 1 < n and text[close1 + 1] == "{":
                open2 = close1 + 1
                close2 = _find_matching_brace(text, open2)
                if close2 != -1:
                    numerator = text[open1 + 1 : close1]
                    denominator = text[open2 + 1 : close2]
                    out.append(_format_frac(numerator, denominator))
                    i = close2 + 1
                    continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _replace_wrappers(text: str) -> str:
    text = re.sub(r"\\(vec|hat|bar|overline)\{([^{}]*)\}",
                  lambda m: f"{m.group(1)}({m.group(2)})", text)
    # A couple of passes handle the (rare, not seen in real data but cheap
    # to allow) case of one wrapper immediately inside another, e.g.
    # \mathbf{\text{x}} -- each pass peels one layer.
    for _ in range(3):
        new_text = _WRAPPER_RE.sub(lambda m: m.group(1), text)
        if new_text == text:
            break
        text = new_text
    return text


def _replace_numeric_exponents(text: str) -> str:
    def render(match: re.Match) -> str:
        is_super = text[match.start()] == "^"
        table = _SUPER_DIGITS if is_super else _SUB_DIGITS
        return match.group(1).translate(table)

    # Braced form first (e.g. "10^{-14}" -> "10⁻¹⁴"). Doing this first means
    # the bare-form pass below can never see a "^{"/"_{" left over from a
    # braced exponent that didn't get replaced -- by the time it runs, any
    # digit immediately after "^"/"_" is genuinely bare, not the first
    # character inside a brace.
    text = _NUMERIC_BRACED_RE.sub(render, text)
    return _NUMERIC_BARE_RE.sub(render, text)


def _replace_remaining_braced_exponents(text: str) -> str:
    """Whatever `{...}` after `^`/`_` survived the numeric-only pass (e.g.
    the "{3/2}" left over from the loop formula) -- strip the braces,
    parenthesizing if the content isn't a single simple token."""

    def render(prefix: str, match: re.Match) -> str:
        content = match.group(1).strip()
        if _COMPLEX_ARG_RE.search(content):
            return f"{prefix}({content})"
        return f"{prefix}{content}"

    text = _SUPERSCRIPT_RE.sub(lambda m: render("^", m), text)
    text = _SUBSCRIPT_RE.sub(lambda m: render("_", m), text)
    return text


def _replace_symbols(text: str) -> str:
    return _SYMBOL_RE.sub(lambda m: _SYMBOL_MAP[m.group(1)], text)


def _normalize_fragment(text: str) -> str:
    """Converts common LaTeX math syntax in `text` to plain, readable text.

    Deliberately conservative and defensive:
    - No-op on text with no LaTeX at all (every substitution below is a
      regex/scan that simply finds nothing to do).
    - Idempotent: running it twice gives the same result as running it once
      (there's nothing in the output of any step here that looks like an
      input to an earlier step -- e.g. the unicode it emits is never `$`,
      `\\`, or a brace).
    - Never raises. Malformed/nested LaTeX it can't cleanly parse (e.g. an
      unbalanced `\\frac{`) is left as-is for that occurrence rather than
      crashing the whole pass; if something genuinely unexpected still slips
      through, the whole function falls back to returning `text` unchanged.
    """
    try:
        result = _MATH_DELIMITER_RE.sub(_remove_math_delimiters, text)
        result = _replace_frac(result)
        result = _replace_wrappers(result)
        result = _replace_numeric_exponents(result)
        result = _replace_remaining_braced_exponents(result)
        result = _replace_symbols(result)
        result = _SPACING_RE.sub(" ", result)
        result = _NEGSPACE_RE.sub("", result)
        return result
    except Exception:
        return text


def normalize_latex(text: str) -> str:
    """Render known math syntax while preserving citation labels and currency."""
    parts = _CITATION_RE.split(text)
    return "".join(part if i % 2 else _normalize_fragment(part) for i, part in enumerate(parts))
