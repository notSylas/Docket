"""Unit tests for `docket.query.latex.normalize_latex`.

The bulk of these use REAL answer text pulled verbatim from a physics eval
run (`docket eval run` against `gold_physics.yaml`, 93 questions, 28 of which
came back with LaTeX still in the answer despite the system prompt telling
the model not to use it) rather than invented examples, per the "don't
guess, use real data" instruction this module's docstring also mentions.
"""

from __future__ import annotations

from docket.query.latex import normalize_latex

# ---------------------------------------------------------------------------
# The two questions the task named explicitly as LaTeX-caused hard fails in
# the deterministic gold-fact check (`docket.eval.scoring.check_must_contain`
# via `matches`) -- both pulled verbatim from
# .../physics-v2/physics-runs-v2.jsonl.
# ---------------------------------------------------------------------------


def test_strong_force_range_dollar_braced_exponent() -> None:
    answer = (
        "The range of distance where the strong force is effective is very "
        "small, approximately $10^{-14}$ m. This is precisely the size of "
        "the nucleus. [leph101.pdf #chk_3a322f74]"
    )
    normalized = normalize_latex(answer)

    assert "$" not in normalized
    assert "{" not in normalized and "}" not in normalized
    # NFKC-decomposes back to "10-14" (docket.eval.scoring.normalize_text),
    # same as the already-passing unicode-superscript phrasing of this
    # answer ("~10⁻¹⁴ m") -- a bare "10^-14" would introduce a literal "^"
    # the gold fact "~10 -14 m" doesn't have and would NOT match.
    assert "10⁻¹⁴" in normalized
    assert "[leph101.pdf #chk_3a322f74]" in normalized


def test_strong_force_range_tilde_variant() -> None:
    answer = (
        "The range of distance where the strong force is effective is very "
        "small, approximately ~10$^{-14}$ m. This is precisely the size of "
        "the nucleus. [leph101.pdf #chk_3a322f74]"
    )
    normalized = normalize_latex(answer)

    assert normalized == (
        "The range of distance where the strong force is effective is very "
        "small, approximately ~10⁻¹⁴ m. This is precisely the size of "
        "the nucleus. [leph101.pdf #chk_3a322f74]"
    )


def test_magnetisation_units_dollar_braced_exponent() -> None:
    answer = (
        "The unit of measurement for magnetisation $ M $ is A m$^{-1}$ "
        "(ampere per meter).  \n[leph105.pdf #chk_57ff06d7]"
    )
    normalized = normalize_latex(answer)

    assert "$" not in normalized
    assert "A m⁻¹" in normalized
    assert "[leph105.pdf #chk_57ff06d7]" in normalized


def test_magnetisation_units_repeated_in_prose() -> None:
    answer = (
        'The unit of measurement for magnetisation $ M $ is A m$^{-1}$ '
        '(ampere per meter). This is stated in the table provided in '
        '[leph105.pdf #chk_57ff06d7], which lists "A m$^{-1}$" as the unit '
        "for magnetisation. Additionally, [leph105.pdf #chk_b131ecc2] "
        "confirms that $ H $ (which has the same dimensions as $ M $) is "
        "measured in units of A m$^{-1}$."
    )
    normalized = normalize_latex(answer)

    assert "$" not in normalized
    assert normalized.count("A m⁻¹") == 3
    assert "[leph105.pdf #chk_57ff06d7]" in normalized
    assert "[leph105.pdf #chk_b131ecc2]" in normalized


# ---------------------------------------------------------------------------
# Other real patterns from the same eval run.
# ---------------------------------------------------------------------------


def test_ohms_law_times_and_inline_dollars() -> None:
    answer = (
        "According to Ohm's law, the potential difference $ V $ across a "
        "resistor $ R $ is directly proportional to the current $ I $, "
        "expressed as $ V = R \\times I $. This relationship shows that "
        "$ V $ increases linearly with $ I $ when $ R $ is constant. \n\n"
        "[citation_label: [leph103.pdf #chk_da766a17]]"
    )
    normalized = normalize_latex(answer)

    assert "$" not in normalized
    assert "\\times" not in normalized
    assert "V = R × I" in normalized
    assert "[leph103.pdf #chk_da766a17]" in normalized


def test_biot_savart_frac_with_nested_braces() -> None:
    answer = (
        "The formula used to calculate the magnetic field $ B $ due to a "
        "circular current loop along its axis is:  \n$$\n"
        "B = \\frac{\\mu_0 I R^2}{2(x^2 + R^2)^{3/2}}\n$$  \n"
        "This expression is derived from the Biot-Savart law and applies to "
        "a point at a distance $ x $ from the center of the loop along its "
        "axis, where $ I $ is the current, $ R $ is the radius of the loop, "
        "and $ \\mu_0 $ is the permeability of free space. For "
        "$ x \\gg R $, this simplifies to a dipole-like field.  \n\n"
        "[citation_label: [leph104.pdf #chk_d1720204]]"
    )
    normalized = normalize_latex(answer)

    assert "$" not in normalized
    assert "\\frac" not in normalized
    assert "{" not in normalized and "}" not in normalized
    # The physical content (a fraction with μ0, I, R^2 over 2(x^2+R^2)^(3/2))
    # must survive recognizably, not just have its LaTeX wrapper stripped.
    assert "μ_0 I R^2" in normalized
    assert "2(x^2 + R^2)^(3/2)" in normalized
    assert "x ≫ R" in normalized
    assert "[leph104.pdf #chk_d1720204]" in normalized


def test_emf_formula_frac_varepsilon_omega_block_math() -> None:
    answer = (
        "The formula for the induced emf across the ends of a rotating "
        "metal rod in a magnetic field is given by:  \n$$\n"
        "\\varepsilon = \\frac{1}{2} B \\omega R^2\n$$  \n"
        "where $ B $ is the magnetic field strength, $ \\omega $ is the "
        "angular velocity, and $ R $ is the radius of the rod's rotation. "
        "This result is derived by integrating the contribution from each "
        "infinitesimal segment of the rod, with $ v = \\omega r $ relating "
        "linear velocity to angular velocity.  \n\n"
        "citation_label: [leph106.pdf #chk_6639f41a]"
    )
    normalized = normalize_latex(answer)

    assert "$" not in normalized
    assert "\\varepsilon" not in normalized and "\\omega" not in normalized
    assert "ε = 1/2 B ω R^2" in normalized
    assert "v = ω r" in normalized
    assert "[leph106.pdf #chk_6639f41a]" in normalized


def test_potential_difference_frac_pi_varepsilon_subscript() -> None:
    answer = (
        "The potential difference between conductors 1 and 2 with charges "
        "$ Q' $ and $ -Q' $ is given by $ V = \\frac{Q'}{2\\pi\\varepsilon_0 r} $, "
        "where $ r $ is the distance between the conductors."
    )
    normalized = normalize_latex(answer)

    assert "$" not in normalized
    assert "\\frac" not in normalized and "\\pi" not in normalized
    # Numerator "Q'" is simple on its own, but gets parenthesized to match
    # the (complex) denominator's parenthesization -- consistent, and still
    # an unambiguous, correct rendering of the original fraction.
    assert "V = (Q')/(2πε_0 r)" in normalized


def test_electron_volt_frac_text_times_spacing_commands() -> None:
    answer = (
        "The energy gained by an electron accelerated by a potential "
        "difference of 1 volt is $1.6 \\times 10^{-19}$ J. This unit of "
        "energy is defined as 1 electron volt (1 eV), i.e., "
        "$1 \\, \\text{eV} = 1.6 \\times 10^{-19} \\, \\text{J}$ "
        "[leph102.pdf #chk_bcc806cc]."
    )
    normalized = normalize_latex(answer)

    assert "$" not in normalized
    assert "\\text" not in normalized and "\\times" not in normalized
    assert "1.6 × 10⁻¹⁹ J" in normalized
    # "\," (LaTeX thin space) becomes a plain space, so runs of whitespace
    # collapse before checking -- the extra spaces left behind are cosmetic
    # (eval scoring's normalize_text collapses them too).
    collapsed = " ".join(normalized.split())
    assert "1 eV = 1.6 × 10⁻¹⁹ J" in collapsed
    assert "[leph102.pdf #chk_bcc806cc]" in normalized


def test_electric_field_mathbf_dots() -> None:
    answer = (
        "The electric field at a point in space due to a system of charges "
        "is defined as the force experienced by a unit test charge placed "
        "at that point, without disturbing the original positions of the "
        "charges $ q_1, q_2, \\dots, q_n $. This field can be determined "
        "using Coulomb's law and the superposition principle, as the "
        "electric field is the vector sum of the fields due to individual "
        "charges [leph101.pdf #chk_48033922]. The electric field "
        "$ \\mathbf{E} $ is a vector quantity that varies from one point to "
        "another in space and is independent of the test charge placed to "
        "determine it, depending only on the positions of the source "
        "charges [leph101.pdf #chk_cb276df6][leph101.pdf #chk_d57eb5bd]."
    )
    normalized = normalize_latex(answer)

    assert "$" not in normalized
    assert "\\mathbf" not in normalized and "\\dots" not in normalized
    assert "q_1, q_2, …, q_n" in normalized
    assert "electric field  E  is a vector quantity" in normalized
    assert "[leph101.pdf #chk_48033922]" in normalized
    assert "[leph101.pdf #chk_cb276df6]" in normalized
    assert "[leph101.pdf #chk_d57eb5bd]" in normalized


def test_biot_savart_direction_mathbf_cross_product() -> None:
    answer = (
        "The direction of the magnetic field $ d\\mathbf{B} $ is "
        "perpendicular to the plane containing $ d\\mathbf{l} $ and "
        "$ \\mathbf{r} $, as determined by the right-hand screw rule. This "
        "is consistent with the vector cross product $ d\\mathbf{l} \\times "
        "\\mathbf{r} $, which defines the direction of $ d\\mathbf{B} $.\n\n"
        "[leph104.pdf #chk_14dfd72d]"
    )
    normalized = normalize_latex(answer)

    assert "$" not in normalized
    assert "\\mathbf" not in normalized
    assert "dB" in normalized
    assert "dl" in normalized
    assert "dl × r" in normalized
    assert "[leph104.pdf #chk_14dfd72d]" in normalized


# ---------------------------------------------------------------------------
# Safety properties: idempotency, citation-tag preservation, no crash on
# malformed input.
# ---------------------------------------------------------------------------


def test_plain_text_with_no_latex_is_unchanged() -> None:
    answer = (
        "Ohm's law states that the potential difference V across a "
        "resistor is directly proportional to the current I, with the "
        "relationship given by V = R x I, where R is the resistance of the "
        "conductor. [leph103.pdf #chk_da766a17]"
    )
    assert normalize_latex(answer) == answer


def test_idempotent_on_already_normalized_text() -> None:
    original = "approximately ~10$^{-14}$ m [leph101.pdf #chk_3a322f74]"
    once = normalize_latex(original)
    twice = normalize_latex(once)
    assert once == twice


def test_citation_tag_survives_untouched() -> None:
    """A real citation_label (docket.retrieval.resolver._citation_label's
    "[source.pdf #chunk_id]" format) never contains "$" or "\\", so it must
    come out byte-for-byte identical even embedded in heavily-LaTeX'd text."""
    tag = "[report.pdf #a1b2c3d4e5f6]"
    answer = f"$V = I \\times R$ (Ohm's law), where $ \\omega $ is unused here {tag}."
    normalized = normalize_latex(answer)
    assert tag in normalized


def test_malformed_unbalanced_frac_does_not_crash() -> None:
    answer = "This is broken LaTeX: \\frac{a}{b and some \\frac{ trailing text"
    normalized = normalize_latex(answer)
    assert isinstance(normalized, str)


def test_malformed_unbalanced_braces_does_not_crash() -> None:
    answer = "Unbalanced: $x^{-1$ and \\mathbf{E and \\text{oops"
    normalized = normalize_latex(answer)
    assert isinstance(normalized, str)


def test_empty_string_is_a_noop() -> None:
    assert normalize_latex("") == ""


def test_minimum_required_greek_letters() -> None:
    """The task's explicit minimum set (theta, pi, mu, lambda, Delta, sigma),
    not all of which appeared in the real eval sample."""
    answer = "\\theta, \\pi, \\mu, \\lambda, \\Delta, \\sigma"
    assert normalize_latex(answer) == "θ, π, μ, λ, Δ, σ"


def test_currency_and_citation_filename_are_preserved():
    answer = "Budget is $1,200,000 and fees are $200 [cost$report.pdf #chk_12345678]."
    assert normalize_latex(answer) == answer


def test_math_and_currency_can_coexist():
    assert normalize_latex("$V = IR$ costs $10.") == "V = IR costs $10."


def test_vector_and_hat_notation_keep_their_meaning():
    assert normalize_latex(r"$\vec{r} = r \hat{r}$") == "vec(r) = r hat(r)"


# ---------------------------------------------------------------------------
# Bare (unbraced) numeric exponent/subscript -- "10^-14" as opposed to
# "10^{-14}". The model emits both forms; only the braced one was handled
# before this fix, so a literal "^" survived into the final answer and
# broke the whitespace-insensitive substring match against gold facts
# (e.g. gold "~10 -14 m" never matches answer "~10^-14 m" post-normalize
# because "^" has no counterpart to strip).
# ---------------------------------------------------------------------------


def test_bare_numeric_exponent_renders_as_unicode_superscript() -> None:
    assert normalize_latex("10^-14") == "10⁻¹⁴"


def test_strong_force_range_bare_unbraced_exponent_real_example() -> None:
    """The exact real answer from the task: physically correct, correctly
    cited, but previously failed the deterministic gold-fact check because
    of the literal "^" left behind by the unbraced "10^-14" form."""
    answer = (
        "The range of distance where the strong force is effective is "
        "approximately ~10^-14 m. [leph101.pdf #chk_3a322f74]"
    )
    normalized = normalize_latex(answer)

    assert "^" not in normalized
    assert "approximately ~10⁻¹⁴ m." in normalized
    assert "[leph101.pdf #chk_3a322f74]" in normalized


def test_bare_numeric_exponent_inside_longer_sentence() -> None:
    answer = "One electron volt is 1.6 \\times 10^-19 J, a very small amount."
    normalized = normalize_latex(answer)
    assert "10⁻¹⁹" in normalized
    assert "^" not in normalized
    assert "1.6 × 10⁻¹⁹ J" in normalized


def test_bare_numeric_subscript_after_a_digit_renders_as_unicode() -> None:
    # Symmetric with the exponent case above -- a bare numeric subscript is
    # only converted when it follows a digit (genuine numeric notation),
    # mirroring the braced regex's support for both "^" and "_".
    assert normalize_latex("10_2") == "10₂"


def test_bare_algebraic_exponent_on_a_variable_is_left_alone() -> None:
    """Bare "R^2"/"x^2" (base is a letter, not a digit) must NOT be
    converted -- this is genuine algebraic notation, not a power-of-ten, and
    converting it would break the existing letter-based cases like
    "R^2" in test_biot_savart_frac_with_nested_braces. The digit-lookbehind
    guard is what tells these two apart."""
    assert normalize_latex("R^2") == "R^2"
    assert normalize_latex("x^-3") == "x^-3"


def test_bare_subscript_on_a_variable_letter_is_left_alone() -> None:
    """Bare "q_1" (letter base) stays untouched, same as the existing
    test_electric_field_mathbf_dots real-data case (q_1, q_2, ..., q_n)."""
    assert normalize_latex("q_1, q_2, \\dots, q_n") == "q_1, q_2, …, q_n"


def test_braced_exponents_still_work_unchanged_regression() -> None:
    assert normalize_latex("10^{-14}") == "10⁻¹⁴"
    assert normalize_latex(r"\times 10^{-19}") == "× 10⁻¹⁹"


def test_bare_exponent_next_to_citation_tag_does_not_touch_the_tag() -> None:
    """A citation label ([source.pdf #chunk_id]) is split out before
    fragment normalization runs (`normalize_latex`'s `_CITATION_RE.split`),
    so even a chunk id that happens to contain "^" followed by digits must
    survive completely untouched, while a bare exponent just outside the
    brackets still gets normalized."""
    answer = "approximately 10^-14 m [file.pdf #chk_abc123^9]"
    normalized = normalize_latex(answer)
    assert "10⁻¹⁴" in normalized
    assert "[file.pdf #chk_abc123^9]" in normalized


def test_bare_exponent_idempotent() -> None:
    once = normalize_latex("10^-14")
    twice = normalize_latex(once)
    assert once == twice == "10⁻¹⁴"
