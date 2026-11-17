"""Blind human-label sheet built from existing saved judged runs (CPU only, no model calls).

`export_label_sheet` samples answers from saved run/judged files, stratified by outcome (judge
pass / fail / doubt, abstain, cited and uncited absence), adds seeded mutations (a wrong number,
an entity swapped in from another document, an unsupported extra sentence) and writes:

    label_sheet.csv / label_sheet.jsonl   what the labeler sees: question, gold facts, answer,
                                          cited passage text, and EMPTY `correct` / `notes`
    LABELING_INSTRUCTIONS.txt             how to label
    labels_key.jsonl                      NOT for labelers: stratum, judge verdicts, seeded flags

All files are 0600 in a 0700 directory. `score_label_sheet` joins returned labels to the key and
reports raw agreement, Cohen's kappa with a bootstrap CI and a confusion matrix per judge --
always against HUMAN labels. Judge-to-judge agreement is model-to-model agreement and is never
reported as validation. Nothing here invents labels: the label columns are left empty.
"""

from __future__ import annotations

import csv
import json
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from docket.eval.judge import JudgedRun, JudgeVerdict, load_judged
from docket.eval.outputs import ensure_private_dir, write_private_text
from docket.eval.review import bucket_sample
from docket.eval.schema import GoldSet, GoldSetError, RunRecord, fingerprint, load_gold_set, load_records
from docket.eval.scoring import Verdict, score_run, strip_citations
from docket.eval.stats import cohen_kappa, kappa_ci

KAPPA_TARGET = 0.7
SHEET_COLUMNS = ("item_id", "question", "question_answerable", "gold_facts", "gold_quotes",
                 "answer", "cited_passages", "correct", "notes")
MUTATIONS = ("wrong_number", "wrong_entity", "unsupported_sentence")
MODEL_TO_MODEL_NOTE = "model-to-model agreement (not human validation)"
_CITATION_RE = re.compile(r"\[[^\[\]]*#[^\[\]]*\]")
_NUMBER_RE = re.compile(r"(?<![\w.#])\d[\d,]*(?:\.\d+)?(?![\w#])")
_ENTITY_RE = re.compile(
    r"\b(?:[A-Z][\w&.'-]*)(?:\s+(?:[A-Z][\w&.'-]*|of|and|&))*\s+(?:Inc\.?|Corp\.?|Corporation|LLC|Ltd\.?|"
    r"Company|Co\.|LLP|L\.P\.|GmbH|plc|Holdings|Partners|Technologies|Systems|Group|Foundation)\b"
)
_UNSUPPORTED = (
    "The agreement also requires the receiving party to pay an additional penalty of $250,000 for each breach.",
    "Either party may terminate the agreement immediately and without notice if its auditors so request.",
    "This provision was added by amendment in 2015 and applies retroactively to all earlier contracts.",
    "The obligations described here are guaranteed jointly and severally by every affiliate of the parties.",
)


@dataclass(frozen=True)
class SheetSource:
    name: str
    runs_path: Path
    judged_path: Path | None
    gold_path: Path


@dataclass
class Candidate:
    source: SheetSource
    gold_name: str
    record: RunRecord
    question_answerable: bool
    question_text: str
    facts: list[str]
    quotes: list[str]
    stratum: str
    judge_label: str
    verdict: str  # pass | fail | doubt | unjudged
    judge_models: list[str]
    self_judged: bool
    question_id: str
    doc_names: frozenset[str]


# -- discovery ------------------------------------------------------------------------------


def _is_run_file(path: Path) -> bool:
    n = path.name
    return n.endswith(".jsonl") and ".j14." not in n and ".judged." not in n and "manifest" not in n


def discover_sources(results_dir: Path, gold_dir: Path) -> list[SheetSource]:
    """Pair each saved run file with its judged file (same stem + `.j14`/`.judged`, verified by
    record fingerprints) and with the gold file whose fingerprint the records carry."""
    golds: dict[str, Path] = {}
    for g in sorted(Path(gold_dir).glob("gold*.yaml")):
        try:
            golds[fingerprint(load_gold_set(g))] = g
        except GoldSetError:
            continue
    sources: list[SheetSource] = []
    root = Path(results_dir)
    for path in sorted(root.rglob("*.jsonl")):
        if "labels" in path.relative_to(root).parts[:-1] or not _is_run_file(path):
            continue
        try:
            records = load_records(path)
        except (GoldSetError, ValueError):
            continue
        digests = {r.gold_fingerprint for r in records if r.gold_fingerprint}
        gold = next((golds[d] for d in digests if d in golds), None)
        if gold is None:
            continue
        judged = next((p for suffix in (".j14", ".judged") if (p := path.with_name(path.stem + suffix + ".jsonl")).exists()), None)
        if judged is not None:
            fps = {(j.question_id, j.repeat): j.record_fingerprint for j in load_judged(judged)}
            ok = sum(fps.get((r.question_id, r.repeat)) == fingerprint(r) for r in records)
            if ok < max(1, len(records) // 2):
                judged = None
        sources.append(SheetSource(str(path.relative_to(root).with_suffix("")), path, judged, gold))
    return sources


def _stratum(question_answerable: bool, record: RunRecord, verdict: str) -> str:
    if not question_answerable:
        return "cited_absence" if record.citations else "absence_uncited"
    if record.abstained:
        return "abstain"
    return verdict if verdict in ("pass", "fail", "doubt") else "unjudged"


def collect_candidates(sources: list[SheetSource]) -> list[Candidate]:
    out: list[Candidate] = []
    for src in sources:
        gold: GoldSet = load_gold_set(src.gold_path)
        questions = gold.by_id()
        judged = {(j.question_id, j.repeat): j for j in load_judged(src.judged_path)} if src.judged_path else {}
        for rec in load_records(src.runs_path):
            q = questions.get(rec.question_id)
            if q is None or rec.error:
                continue
            j: JudgedRun | None = judged.get((rec.question_id, rec.repeat))
            if j is not None and j.record_fingerprint is not None and j.record_fingerprint != fingerprint(rec):
                j = None
            if j is not None:
                verdict = {JudgeVerdict.PASS: "pass", JudgeVerdict.FAIL: "fail"}.get(j.verdict, "doubt")
                label = "+".join(j.judge_models) if j.source == "judge" else "deterministic"
                models, selfj = list(j.judge_models), j.self_judged
            else:
                det = score_run(q, rec).verdict
                verdict = {Verdict.PASS: "pass", Verdict.FAIL: "fail"}.get(det, "unjudged")
                label, models, selfj = "deterministic", [], False
            out.append(Candidate(
                source=src, gold_name=src.gold_path.name, record=rec, question_answerable=q.answerable,
                question_text=q.question, facts=list(q.must_contain) + list(q.enumeration),
                quotes=list(q.gold_spans), stratum=_stratum(q.answerable, rec, verdict), judge_label=label,
                verdict=verdict, judge_models=models, self_judged=selfj, question_id=q.id,
                doc_names=frozenset(c.source_display_name for c in rec.retrieved if c.chunk_id in {x.chunk_id for x in rec.citations}),
            ))
    return out


# -- passages & mutations --------------------------------------------------------------------


def passages_for(rec: RunRecord, *, max_uncited: int = 3) -> str:
    cited = {c.chunk_id for c in rec.citations}
    blocks = [f"{c.citation_label}\n{c.text}" for c in rec.retrieved if c.chunk_id in cited]
    if blocks:
        return "\n\n---\n\n".join(blocks)
    shown = [f"{c.citation_label} (retrieved, not cited)\n{c.text}" for c in rec.retrieved[:max_uncited]]
    return "\n\n---\n\n".join(shown) if shown else "(no passages)"


def _masked_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _CITATION_RE.finditer(text)]


def _outside(span: tuple[int, int], masks: list[tuple[int, int]]) -> bool:
    return not any(a < span[1] and span[0] < b for a, b in masks)


def mutate_wrong_number(answer: str, passages: str, rng: random.Random) -> tuple[str, str] | None:
    masks = _masked_spans(answer)
    found = [m for m in _NUMBER_RE.finditer(answer) if _outside(m.span(), masks)]
    in_passage = [m for m in found if m.group(0) in passages]
    pool = in_passage or found
    if not pool:
        return None
    m = rng.choice(pool)
    raw = m.group(0)
    digits = raw.replace(",", "")
    try:
        value = float(digits) if "." in digits else int(digits)
    except ValueError:
        return None
    new_value = value * rng.choice([2, 3, 5]) + (1 if isinstance(value, int) else 0.5)
    if isinstance(value, int):
        new = f"{int(new_value):,}" if "," in raw else str(int(new_value))
    else:
        new = f"{new_value:.{len(digits.split('.')[1])}f}"
    if new == raw:
        return None
    return answer[: m.start()] + new + answer[m.end():], f"{raw} -> {new}"


def entity_pool(cands: list[Candidate]) -> dict[str, set[str]]:
    """Organisation-like names found in each document's cited text, keyed by document name."""
    pool: dict[str, set[str]] = {}
    for c in cands:
        cited = {x.chunk_id for x in c.record.citations}
        for chunk in c.record.retrieved:
            if chunk.chunk_id in cited:
                pool.setdefault(chunk.source_display_name, set()).update(m.group(0) for m in _ENTITY_RE.finditer(chunk.text))
    return pool


def mutate_wrong_entity(
    answer: str, passages: str, own_docs: frozenset[str], pool: dict[str, set[str]], rng: random.Random
) -> tuple[str, str] | None:
    masks = _masked_spans(answer)
    present = [m for m in _ENTITY_RE.finditer(answer) if _outside(m.span(), masks) and m.group(0) in passages]
    if not present:
        return None
    foreign = sorted(
        {e for doc, ents in pool.items() if doc not in own_docs for e in ents if e not in passages and e not in answer}
    )
    if not foreign:
        return None
    m = rng.choice(present)
    new = rng.choice(foreign)
    return answer[: m.start()] + new + answer[m.end():], f"{m.group(0)} -> {new} (entity from another document)"


def mutate_unsupported(answer: str, passages: str, rng: random.Random) -> tuple[str, str] | None:
    options = [s for s in _UNSUPPORTED if s not in passages]
    if not options:
        return None
    sentence = rng.choice(options)
    return f"{answer.rstrip()} {sentence}", f"appended: {sentence}"


# -- export ---------------------------------------------------------------------------------


def _sheet_row(item_id: str, c: Candidate, answer: str) -> dict:
    return {
        "item_id": item_id,
        "question": c.question_text,
        "question_answerable": "yes" if c.question_answerable else "no",
        "gold_facts": " | ".join(c.facts),
        "gold_quotes": " | ".join(c.quotes),
        "answer": answer.strip(),
        "cited_passages": passages_for(c.record),
        "correct": "",
        "notes": "",
    }


INSTRUCTIONS = """LABELING INSTRUCTIONS (plain text; do not share the key file)

You are checking whether answers produced by a document question-answering system are CORRECT.
Each row of label_sheet.csv (or label_sheet.jsonl) has:

  question              what was asked
  question_answerable   yes = the documents contain the answer; no = they do not (a correct
                        answer then declines or says the provision is absent)
  gold_facts / quotes   facts and passages the question author expects (may be empty)
  answer                the system's answer, with [file #id] citation tags
  cited_passages        the text of the passages the answer cites (or, if it cites nothing, the top
                        retrieved passages, marked "retrieved, not cited")

Fill in the `correct` column for every row you can decide:
  yes   the answer states the right facts, contradicts nothing in the passages, AND every claim in
        it is supported by the cited passages. For question_answerable=no, yes means it declines
        or correctly states the provision is absent, and invents no value.
  no    anything wrong: a wrong number, a wrong company or party, a claim the passages do not
        support, a missing required fact, or answering when it should have declined.
Leave `correct` EMPTY if you genuinely cannot tell and write why in `notes`. Do not guess.

Rules:
  * Judge only from the passages shown. Do not use outside knowledge or search the web.
  * Some rows deliberately contain mistakes. Check every row on its own merits; never assume
    an answer is right or wrong because of how it reads.
  * Work independently of the system and of other labelers. If you wrote the gold facts, ask someone
    else to label.
  * Accepted values: yes/no (also true/false, 1/0, y/n). Anything else is rejected on import.
  * Keep a copy of the file and send back the filled-in CSV (or JSONL).

After labelling, score with:
  docket eval calibrate sheet-score --labels <filled sheet> --key labels_key.jsonl
"""


def export_label_sheet(
    sources: list[SheetSource], out_dir: Path, *, n: int = 80, seeded: int = 12, seed: int = 0
) -> dict:
    """Write the blind sheet, instructions and key; returns a summary (counts, paths)."""
    if seeded < 0 or n <= seeded:
        raise GoldSetError("n must exceed the number of seeded items")
    cands = collect_candidates(sources)
    if not cands:
        raise GoldSetError("no run files with matching gold were found")
    rng = random.Random(seed)
    order = ("pass", "fail", "doubt", "abstain", "cited_absence", "absence_uncited", "unjudged")
    judged_cands = [c for c in cands if c.stratum != "unjudged"]  # no verdict to compare: last resort only
    real = bucket_sample(
        judged_cands if len(judged_cands) >= n - seeded else cands, n - seeded,
        bucket_key=lambda c: c.stratum, sort_key=lambda c: (c.source.name, c.record.question_id, c.record.repeat),
        order_key=order.index, seed=seed, distinct_key=lambda c: (c.gold_name, c.question_id),
    )
    used_q = {(c.gold_name, c.question_id) for c in real}
    pool = entity_pool(cands)
    bases = [
        c for c in cands
        if c.stratum == "pass" and c.record.citations and not c.record.abstained and (c.gold_name, c.question_id) not in used_q
    ]
    rng.shuffle(bases)
    # one seeded item per distinct question; mutations assigned round-robin
    chosen: list[tuple[Candidate, str, str, str]] = []
    seen_q: set[tuple[str, str]] = set()
    want = [MUTATIONS[i % len(MUTATIONS)] for i in range(seeded)]
    for slot, preferred in enumerate(want):
        kinds = [preferred] + [k for k in MUTATIONS if k != preferred]  # fall back if a kind is not applicable
        done = False
        for kind in kinds:
            for c in bases:
                qk = (c.gold_name, c.question_id)
                if qk in seen_q:
                    continue
                passages = passages_for(c.record)
                if kind == "wrong_number":
                    res = mutate_wrong_number(c.record.answer, passages, rng)
                elif kind == "wrong_entity":
                    res = mutate_wrong_entity(c.record.answer, passages, c.doc_names, pool, rng)
                else:
                    res = mutate_unsupported(c.record.answer, passages, rng)
                if res is not None:
                    seen_q.add(qk)
                    chosen.append((c, kind, res[0], res[1]))
                    done = True
                    break
            if done:
                break
    entries: list[tuple[Candidate, str | None, str, str]] = [(c, None, c.record.answer, "") for c in real]
    entries += [(c, kind, text, detail) for c, kind, text, detail in chosen]
    rng.shuffle(entries)

    rows, keys = [], []
    for i, (c, kind, answer, detail) in enumerate(entries, start=1):
        item_id = f"L{i:03d}"
        rows.append(_sheet_row(item_id, c, answer))
        keys.append({
            "item_id": item_id, "kind": "seeded" if kind else "real", "stratum": "seeded" if kind else c.stratum,
            "seeded": {"mutation": kind, "detail": detail, "expected_correct": False} if kind else None,
            "source": c.source.name, "gold": c.gold_name, "question_id": c.question_id,
            "repeat": c.record.repeat, "record_fingerprint": fingerprint(c.record),
            "judge_label": c.judge_label, "judge_verdict": c.verdict, "judge_models": c.judge_models,
            "self_judged": c.self_judged, "answer_model": (c.record.configuration or {}).get("gen_model"),
        })
    out = ensure_private_dir(Path(out_dir))
    csv_path, jsonl_path = out / "label_sheet.csv", out / "label_sheet.jsonl"
    key_path, instr_path = out / "labels_key.jsonl", out / "LABELING_INSTRUCTIONS.txt"
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SHEET_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    write_private_text(csv_path, buf.getvalue())
    write_private_text(jsonl_path, "".join(json.dumps({**r, "correct": None}, ensure_ascii=False) + "\n" for r in rows))
    write_private_text(key_path, "".join(json.dumps(k, ensure_ascii=False) + "\n" for k in keys))
    write_private_text(instr_path, INSTRUCTIONS)
    return {
        "items": len(rows), "seeded": len(chosen), "real": len(real),
        "strata": dict(Counter(k["stratum"] for k in keys)),
        "mutations": dict(Counter(k[1] for k in chosen)),
        "sources": len({k["source"] for k in keys}),
        "paths": {"csv": csv_path, "jsonl": jsonl_path, "key": key_path, "instructions": instr_path},
    }


# -- import & scoring -----------------------------------------------------------------------

_TRUE = {"yes", "y", "true", "1", "t"}
_FALSE = {"no", "n", "false", "0", "f"}


def _parse_label(raw: object, item_id: str) -> bool | None:
    if raw is None or isinstance(raw, str) and not raw.strip():
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise GoldSetError(f"item {item_id}: label {raw!r} is not yes/no (true/false, 1/0)")


def load_returned_labels(path: Path) -> dict[str, bool | None]:
    """Read a returned sheet (CSV or JSONL); empty `correct` means unlabeled."""
    path = Path(path)
    try:
        if path.suffix.lower() == ".jsonl":
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            with path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
    except (OSError, ValueError) as exc:
        raise GoldSetError(f"cannot read labels {path}: {exc}") from exc
    labels: dict[str, bool | None] = {}
    for row in rows:
        item_id = str(row.get("item_id", "")).strip()
        if not item_id or "correct" not in row:
            raise GoldSetError(f"{path}: every row needs item_id and a correct column")
        if item_id in labels:
            raise GoldSetError(f"duplicate label for {item_id}")
        labels[item_id] = _parse_label(row["correct"], item_id)
    return labels


def load_key(path: Path) -> dict[str, dict]:
    try:
        keys = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise GoldSetError(f"cannot read key {path}: {exc}") from exc
    return {k["item_id"]: k for k in keys}


@dataclass
class JudgeAgreement:
    judge: str
    n: int  # labeled real items with a pass/fail verdict
    excluded_doubt: int  # doubt / unjudged verdicts left out of kappa
    agreement: float | None
    kappa: float | None
    kappa_ci: tuple[float, float] | None
    confusion: dict[str, int]  # "human=pass,judge=fail" -> count
    self_judged: bool = False

    @property
    def meets_target(self) -> bool:
        return self.kappa is not None and self.kappa >= KAPPA_TARGET


@dataclass
class SheetScore:
    labeled: int
    unlabeled: int
    real_labeled: int
    judges: list[JudgeAgreement] = field(default_factory=list)
    seeded_labeled: int = 0
    seeded_caught: int = 0  # labeler marked the seeded mistake incorrect
    seeded_by_mutation: dict[str, tuple[int, int]] = field(default_factory=dict)  # kind -> (caught, labeled)
    human_pass_rate: float | None = None


def score_label_sheet(labels: dict[str, bool | None], key: dict[str, dict], *, n_boot: int = 5000, seed: int = 0) -> SheetScore:
    unknown = sorted(set(labels) - set(key))
    if unknown:
        raise GoldSetError(f"labels for unknown items: {', '.join(unknown[:5])}")
    done = {i: v for i, v in labels.items() if v is not None}
    if not done:
        raise GoldSetError("no labeled items: fill in the `correct` column first")
    real = {i: v for i, v in done.items() if key[i]["kind"] == "real"}
    seeded = {i: v for i, v in done.items() if key[i]["kind"] == "seeded"}
    result = SheetScore(
        labeled=len(done), unlabeled=len(key) - len(done), real_labeled=len(real), seeded_labeled=len(seeded),
        seeded_caught=sum(not v for v in seeded.values()),
        human_pass_rate=(sum(real.values()) / len(real)) if real else None,
    )
    by_kind: dict[str, list[bool]] = {}
    for i, v in seeded.items():
        by_kind.setdefault(key[i]["seeded"]["mutation"], []).append(not v)
    result.seeded_by_mutation = {k: (sum(v), len(v)) for k, v in sorted(by_kind.items())}
    groups: dict[str, list[str]] = {}
    for i in real:
        groups.setdefault(key[i]["judge_label"], []).append(i)
    for judge, ids in sorted(groups.items()):
        usable = [i for i in ids if key[i]["judge_verdict"] in ("pass", "fail")]
        human = ["pass" if real[i] else "fail" for i in usable]
        auto = [key[i]["judge_verdict"] for i in usable]
        conf = Counter(f"human={h},judge={a}" for h, a in zip(human, auto, strict=True))
        for i in ids:
            if key[i]["judge_verdict"] not in ("pass", "fail"):
                conf[f"human={'pass' if real[i] else 'fail'},judge={key[i]['judge_verdict']}"] += 1
        agree = kappa = ci = None
        if usable:
            agree = sum(h == a for h, a in zip(human, auto, strict=True)) / len(usable)
            kappa = cohen_kappa(human, auto)
            ci = kappa_ci(human, auto, n_boot=n_boot, seed=seed)[1]
        result.judges.append(JudgeAgreement(
            judge=judge, n=len(usable), excluded_doubt=len(ids) - len(usable), agreement=agree,
            kappa=kappa, kappa_ci=ci, confusion=dict(sorted(conf.items())),
            self_judged=any(key[i].get("self_judged") for i in ids),
        ))
    return result


def format_sheet_score(s: SheetScore) -> str:
    lines = [
        f"Labeled items: {s.labeled} ({s.unlabeled} unlabeled); real items {s.real_labeled}, seeded {s.seeded_labeled}",
        "Kappa below is judge vs HUMAN labels only. Judge-to-judge comparisons are "
        f"{MODEL_TO_MODEL_NOTE}.",
    ]
    if s.human_pass_rate is not None:
        lines.append(f"Humans marked {s.human_pass_rate:.1%} of the real items correct (n={s.real_labeled})")
    for j in s.judges:
        head = f"Judge {j.judge}{' [SELF-JUDGED]' if j.self_judged else ''}: n={j.n} pass/fail items"
        if j.excluded_doubt:
            head += f", {j.excluded_doubt} doubt/unjudged excluded"
        lines.append(head)
        if j.kappa is None:
            lines.append("  no pass/fail items labeled: kappa unavailable")
        else:
            verdict = "meets" if j.meets_target else "BELOW"
            lines.append(
                f"  agreement {j.agreement:.1%}, kappa {j.kappa:.2f} "
                f"(95% bootstrap CI {j.kappa_ci[0]:.2f} to {j.kappa_ci[1]:.2f}) -- {verdict} the {KAPPA_TARGET} target"
            )
        lines.append("  confusion: " + (", ".join(f"{k}: {v}" for k, v in j.confusion.items()) or "none"))
    if s.seeded_labeled:
        lines.append(f"Seeded mistakes caught by the labeler: {s.seeded_caught}/{s.seeded_labeled} (labeler sanity check, not a judge metric)")
        for kind, (caught, total) in s.seeded_by_mutation.items():
            lines.append(f"  {kind}: {caught}/{total}")
    return "\n".join(lines)
