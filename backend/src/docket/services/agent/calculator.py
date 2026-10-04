"""Deterministic, allow-listed arithmetic for the agent's `calculate` tool
(Upgrade doc 05 section 8; provenance `derived`, doc 03 section 7).

This is NOT a general code interpreter: there is no `eval`, no free-form
expression, and no way for the model to supply a number. The model names
WHERE the numbers are (chunk + sheet + cell/range); this module reads them
itself through `docket.infra.evidence.workbook_reader.WorkbookReader` (the same
reader `read_range` uses, so an invented cell address simply fails), checks
that they are numeric and in comparable units, and does the arithmetic with
`Decimal` (so 21245 / 3 -> 7081.67 and percent changes round half-up, not
through binary floats).

Rules (doc 02 section 4: never guess):

* A blank cell, a text cell, an error value, or a formula whose cached result
  is missing is an error that names the cell -- never coerced to 0. The one
  exception is a blank cell INSIDE a range (a column with gaps): it is skipped,
  and every skipped cell is listed in the result.
* sum/average/difference/min/max over inputs with different stated units
  (currency or scale, e.g. `INR` vs `INR thousands`), or where only some inputs
  state units, are REFUSED. ratio/pct_change between different stated units are
  refused too; between same-unit (or unstated) values the result is unitless.
  Units are derived from header text and the sheet/workbook context lines, and
  reported as `not stated` when the workbook does not say.
* Hard bounds: at most `MAX_INPUTS` input cells; division by zero is an error.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, DivisionByZero, InvalidOperation, localcontext
from typing import Any

from docket.infra.evidence.workbook_reader import (
    AMBIGUOUS_PREFIX,
    NOT_STATED,
    PERCENT,
    CellRead,
    RangeRead,
    WorkbookReader,
    WorkbookReadError,
)

MAX_INPUTS = 200
MAX_ROUND_TO = 10
OPERATIONS = ("sum", "average", "difference", "ratio", "pct_change", "min", "max", "count")
_TWO_REF_OPERATIONS = {"difference", "ratio", "pct_change"}
_ADDITIVE = {"sum", "average", "difference", "min", "max"}
_EXPRESSION_VALUES = 8

_PRECISION = 50


class CalculationError(Exception):
    """A clear, model-facing reason the calculation was refused."""


def _fmt(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def _quantize(value: Decimal, round_to: int | None) -> Decimal:
    places = MAX_ROUND_TO if round_to is None else round_to
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _result_text(value: Decimal, round_to: int | None) -> str:
    quantized = _quantize(value, round_to)
    text = format(quantized, "f")
    if round_to is None and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _where(read: RangeRead, cell: CellRead) -> str:
    return f"{read.source} > {read.sheet}!{cell.address}"


def _as_ref_dict(ref: Any) -> dict[str, Any]:
    if hasattr(ref, "model_dump"):
        ref = ref.model_dump()
    if not isinstance(ref, dict):
        raise CalculationError(
            "each ref must be an object like {\"chunk_id\": ..., \"cell\": \"B7\"} "
            "or {\"chunk_id\": ..., \"range\": \"B2:B7\"}"
        )
    return ref


def _numeric(read: RangeRead, cell: CellRead, *, in_range: bool) -> Decimal | None:
    """Decimal for a numeric cell; None for a blank inside a range (skipped);
    CalculationError naming the cell for anything else."""
    where = _where(read, cell)
    if cell.cached_value_missing:
        raise CalculationError(
            f"{where} is a formula with no cached value in this workbook; "
            "it is not treated as 0 and cannot be used"
        )
    if cell.error:
        raise CalculationError(f"{where} holds an error value ({cell.error}); it cannot be used")
    if cell.blank:
        if in_range:
            return None
        raise CalculationError(f"{where} is blank (not 0); it cannot be used as a number")
    if not cell.is_number:
        raise CalculationError(
            f"{where} is not a number (value: {cell.value!r}); only numeric cells can be used"
        )
    return Decimal(str(cell.value))


def _check_units(operation: str, units_by_label: list[tuple[str, str]]) -> str:
    """Common unit of the inputs (`units_by_label` = (unit, 'where') pairs), or a
    refusal. Returns the unit string for the result's `units`."""
    distinct = sorted({u for u, _ in units_by_label})
    detail = "; ".join(f"{unit} ({where})" for unit, where in units_by_label[:6])
    if any(u.startswith(AMBIGUOUS_PREFIX) for u in distinct):
        if operation == "count":
            return "count"
        raise CalculationError(
            f"refusing to compute {operation}: the units of the inputs are ambiguous ({detail}); "
            "read the sheet's notes/context and compute only with a clearly stated unit"
        )
    if operation in _ADDITIVE:
        if len(distinct) > 1:
            raise CalculationError(
                f"refusing to compute {operation}: the inputs have different stated units "
                f"or only some state units ({detail}). Do not combine them; report each "
                "separately or convert units explicitly"
            )
        return distinct[0]
    if operation in ("ratio", "pct_change"):
        stated = [u for u in distinct if u != NOT_STATED]
        if len(stated) > 1:
            raise CalculationError(
                f"refusing to compute {operation}: the inputs have different stated units ({detail})"
            )
        return "percent" if operation == "pct_change" else "unitless"
    return "count"


def calculate(
    reader: WorkbookReader,
    operation: str,
    refs: list[Any],
    round_to: int | None = 2,
) -> dict[str, Any]:
    """Run `operation` over the cells named by `refs`. Raises
    `CalculationError`/`WorkbookReadError` with a model-facing message; the
    tool wrapper turns those into ``{"error": ...}``."""
    op = str(operation or "").strip().lower()
    if op not in OPERATIONS:
        raise CalculationError(f"unknown operation '{operation}'; allowed: {', '.join(OPERATIONS)}")
    if round_to is not None:
        if isinstance(round_to, bool) or not isinstance(round_to, int) or not 0 <= round_to <= MAX_ROUND_TO:
            raise CalculationError(f"round_to must be an integer from 0 to {MAX_ROUND_TO}, or null")
    if not isinstance(refs, list) or not refs:
        raise CalculationError("refs must be a non-empty list of cell/range references")
    if len(refs) > MAX_INPUTS:
        raise CalculationError(f"too many refs ({len(refs)}); at most {MAX_INPUTS} inputs")
    if op in _TWO_REF_OPERATIONS and len(refs) != 2:
        raise CalculationError(f"{op} needs exactly two refs (a, b); got {len(refs)}")

    values: list[Decimal] = []
    inputs: list[dict[str, Any]] = []
    units_by_label: list[tuple[str, str]] = []
    blanks_skipped: list[str] = []
    hidden_notes: list[str] = []
    chunk_ids: list[str] = []
    labels: list[str] = []
    budget = MAX_INPUTS

    for index, raw in enumerate(refs):
        ref = _as_ref_dict(raw)
        chunk_id = ref.get("chunk_id")
        cell_text, range_text = ref.get("cell"), ref.get("range")
        if bool(cell_text) == bool(range_text):
            raise CalculationError(f"ref {index + 1}: give exactly one of 'cell' or 'range'")
        in_range = bool(range_text)
        read = reader.read(
            chunk_id, cell_text or range_text, ref.get("sheet"),
            max_cells=budget, max_rows=MAX_INPUTS, truncate=False,
        )
        if op in _TWO_REF_OPERATIONS and len(read.cells) != 1:
            raise CalculationError(
                f"ref {index + 1}: {op} needs a single cell per ref, not {read.requested_range}"
            )
        budget -= len(read.cells)
        for cell in read.cells:
            number = _numeric(read, cell, in_range=in_range and len(read.cells) > 1)
            if number is None:
                blanks_skipped.append(_where(read, cell))
                continue
            values.append(number)
            if cell.hidden_row:
                hidden_notes.append(_where(read, cell))
            units_by_label.append((cell.units, _where(read, cell)))
            inputs.append({
                "ref": index + 1,
                "source": read.source,
                "sheet": read.sheet,
                "address": cell.address,
                "value": cell.value,
                "header": cell.header,
                "units": cell.units,
                "number_format": cell.number_format,
                **({"hidden_row": True} if cell.hidden_row else {}),
            })
        for chunk_id_ in read.chunk_ids:
            if chunk_id_ not in chunk_ids:
                chunk_ids.append(chunk_id_)
        for label in read.citation_labels:
            if label not in labels:
                labels.append(label)

    if op in _TWO_REF_OPERATIONS and len(values) != 2:
        raise CalculationError(f"{op} needs two numeric cells; got {len(values)}")
    if op != "count" and not values:
        raise CalculationError("no numeric cells to compute with (every referenced cell was blank)")

    units = _check_units(op, units_by_label) if values or op != "count" else "count"

    try:
        with localcontext() as ctx:
            ctx.prec = _PRECISION
            result, expression = _compute(op, values)
            if op == "count":
                text = str(result)
                number: float | int = int(result)
            else:
                text = _result_text(result, round_to)
                number = float(_quantize(result, round_to))
            percent_text = (
                _result_text(result * 100, round_to) + "%"
                if units == PERCENT and op not in ("count", "ratio", "pct_change")
                else None
            )
    except (DivisionByZero, InvalidOperation) as exc:
        raise CalculationError(f"{op}: division by zero or invalid operation") from exc
    out: dict[str, Any] = {
        "provenance": "derived",
        "operation": op,
        "result": number,
        "result_text": text,
        "expression": f"{expression} = {text}",
        "units": units,
        "rounding": {"round_to": round_to, "mode": "half_up"} if op != "count" else None,
        "inputs": inputs,
        "input_count": len(values),
        "chunk_ids": chunk_ids,
        "citation_labels": labels,
    }
    if percent_text is not None:
        out["result_percent_text"] = percent_text
        out["note"] = (
            "inputs are percent-formatted cells stored as fractions; result is a fraction "
            "(result_percent_text is the same value x100)"
        )
    if blanks_skipped:
        out["blank_cells_skipped"] = blanks_skipped
    warnings: list[str] = []
    if hidden_notes:
        warnings.append(f"inputs include cells in hidden rows: {', '.join(hidden_notes[:10])}")
    if blanks_skipped:
        warnings.append(
            f"{len(blanks_skipped)} blank cell(s) in the range were skipped (not treated as 0)"
        )
    if units == NOT_STATED:
        warnings.append("the workbook does not state units for these values")
    if warnings:
        out["warnings"] = warnings
    return out


def _compute(op: str, values: list[Decimal]) -> tuple[Decimal | int, str]:
    shown = [_fmt(v) for v in values]
    if len(values) <= _EXPRESSION_VALUES:
        listing = ", ".join(shown)
    else:
        listing = ", ".join(shown[:_EXPRESSION_VALUES]) + f", ... ({len(values)} values)"
    if op == "sum":
        return sum(values, Decimal(0)), (
            " + ".join(shown) if len(values) <= _EXPRESSION_VALUES else f"sum({listing})"
        )
    if op == "average":
        total = sum(values, Decimal(0))
        return total / len(values), f"average({listing})"
    if op == "min":
        return min(values), f"min({listing})"
    if op == "max":
        return max(values), f"max({listing})"
    if op == "count":
        return len(values), f"count({listing})" if values else "count()"
    a, b = values
    if op == "difference":
        return a - b, f"{shown[0]} - {shown[1]}"
    if op == "ratio":
        if b == 0:
            raise CalculationError("ratio: the denominator (second ref) is zero")
        return a / b, f"{shown[0]} / {shown[1]}"
    if a == 0:  # pct_change = (b - a) / a * 100
        raise CalculationError("pct_change: the starting value (first ref) is zero")
    return (b - a) / a * 100, f"({shown[1]} - {shown[0]}) / {shown[0]} * 100"


def safe_calculate(
    reader: WorkbookReader, operation: str, refs: list[Any], round_to: int | None = 2
) -> dict[str, Any]:
    """`calculate`, with every expected failure returned as ``{"error": ...}``."""
    try:
        return calculate(reader, operation, refs, round_to)
    except (CalculationError, WorkbookReadError) as exc:
        return {"error": str(exc)}
