"""Silver (mechanical, UNREVIEWED) KAG labels derived only from existing reviewed gold.

Sources: gold-cuad*.yaml question text and ids, expected-answer patterns and gold spans, and file-name
conventions. Every item is written `label_source: silver_unreviewed` with a `derivation` note. These are
candidates for human review (see review_sheet.py); they are never marked human_reviewed here.

Derivation rules
  clauses     id `cuad-<clause>-<slug>` + answerable:true  -> present (gold_spans copied)
              id `cuad-none-<clause>-<slug>` + answerable:false -> absent
  entities    the company named after "with" / "counterparty to" in a question about one document,
              and a single-name counterparty regex in `cuad-parties-*` -> a `party` of that document
  roles       each of those entities -> role party
  resolution  same: question company vs the file-name company when their suffix-stripped tokens are
              equal (circular with any name normaliser: expect ~100%); different: the two parties of one
              contract, and distinct companies of different contracts with no shared word
Nothing here is a complete entity list, so entity precision is not computed from silver.
"""

from __future__ import annotations

import random
import re
from pathlib import Path, PurePosixPath

import yaml

from docket.eval.kag.schema import KagGold, KagGoldError

CLAUSE_TOKENS = {
    "govlaw": "governing_law", "effdate": "effective_date", "expdate": "expiration", "renewal": "renewal",
    "tfc": "termination_for_convenience", "terminationforconvenience": "termination_for_convenience",
    "cap": "liability_cap", "exclusivity": "exclusivity", "antiassignment": "anti_assignment",
    "noncompete": "non_compete", "auditrights": "audit_rights", "insurance": "insurance",
}
_ID = re.compile(r"^cuad-(?P<none>none-)?(?P<tok>[a-z]+)-(?P<slug>[a-z0-9]+)$")
_WITH = re.compile(
    r"\bwith\s+(?P<name>.+?)(?:,?\s+(?:which|contain|restrict|require|give|cap|limit|for convenience|in the|renew|expire)\b|\?|$)"
)
_COUNTERPARTY = re.compile(r"counterparty to\s+(?P<name>.+?)\s+in the\b")
_SUFFIX = {"inc", "incorporated", "corp", "corporation", "co", "company", "llc", "ltd", "limited", "plc", "sa", "lp", "llp", "holdings", "holding"}


def _tokens(name: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("'", "")).split()


def _core_tokens(name: str) -> list[str]:
    toks = [t for t in _tokens(name) if t != "the"]
    while toks and toks[-1] in _SUFFIX:
        toks.pop()
    return toks


def filename_company(file: str) -> str | None:
    """Leading file-name segment, camel-case split: 'BloomEnergyCorp_2018_..' -> 'Bloom Energy Corp'."""
    stem = PurePosixPath(file).stem
    seg = re.split(r"[_\-]\s*\d|_|\s+-\s+|\s\(", stem)[0].strip()
    seg = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", seg)
    return seg if len(seg) >= 4 else None


def question_company(question: str) -> str | None:
    q = re.sub(r"\s+", " ", question)
    m = _COUNTERPARTY.search(q) or _WITH.search(q)
    if not m:
        return None
    name = m.group("name").strip(" ,")
    return name if 1 <= len(name.split()) <= 8 else None


def _plain_regex_names(patterns: list[str]) -> list[str]:
    """Names from simple `re:` expected-answer patterns (alternations become several names)."""
    names: list[str] = []
    for p in patterns:
        if not p.startswith("re:"):
            continue
        for alt in p[3:].split("|"):
            alt = re.sub(r"\\s\+", " ", alt)
            if re.search(r"[\\()\[\]?*+{}^$]", alt):
                continue
            names.append(alt.strip())
    return names


def build_silver(gold_paths: list[Path], *, corpus: str = "", max_cross_pairs: int = 30, seed: int = 0) -> KagGold:
    by_key: dict[tuple[str, str], dict] = {}
    ent: dict[str, dict[str, str]] = {}  # file -> core key -> name
    counterpart_pairs: list[tuple[str, str, str, str]] = []  # a, b, file, source id
    for path in gold_paths:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        for q in raw.get("questions", []):
            docs = q.get("source_documents") or []
            if len(docs) != 1:
                continue
            file, qid = docs[0], q["id"]
            company = question_company(q["question"])
            if company:
                ent.setdefault(file, {}).setdefault(" ".join(_core_tokens(company)), company)
            if qid.startswith("cuad-parties-") and company:
                for other in _plain_regex_names(q.get("must_contain") or []):
                    counterpart_pairs.append((company, other, file, qid))
            m = _ID.match(qid)
            if not m or m.group("tok") not in CLAUSE_TOKENS:
                continue
            clause = CLAUSE_TOKENS[m.group("tok")]
            present = q.get("answerable") is True and not m.group("none")
            absent = q.get("answerable") is False and bool(m.group("none"))
            if not (present or absent):
                continue
            key = (file, clause)
            item = by_key.setdefault(key, {
                "id": f"clause-{len(by_key) + 1:03d}", "file": file, "clause_type": clause, "present": present,
                "question": re.sub(r"\s+", " ", q["question"]).strip(), "gold_spans": [],
                "derivation": f"gold id {qid} ({'answerable:true' if present else 'none- id, answerable:false'})",
            })
            if item["present"] != present:
                raise KagGoldError(f"gold files disagree on clause {clause} of {file}")
            if present:
                item["gold_spans"] = sorted({*item["gold_spans"], *(q.get("gold_spans") or [])})
    counterpart_pairs = sorted(set(counterpart_pairs))  # gold-cuad and gold-cuad-scale repeat the same items
    clauses = [by_key[k] for k in sorted(by_key)]
    for i, c in enumerate(clauses, 1):
        c["id"] = f"clause-{i:03d}"

    pairs: list[dict] = []
    per_q: dict[str, int] = {}
    for *_x, qid_ in counterpart_pairs:
        per_q[qid_] = per_q.get(qid_, 0) + 1
    for _a, other, file, qid_ in counterpart_pairs:
        if per_q[qid_] != 1:
            continue  # an alternation of possible names: not a single, certain entity
        ent.setdefault(file, {}).setdefault(" ".join(_core_tokens(other)), other)
    entity_docs, roles = [], []
    for n, file in enumerate(sorted(ent), 1):
        entity_docs.append({
            "id": f"ent-{n:03d}", "file": file, "complete": False,
            "entities": [{"name": name, "role": "party"} for name in ent[file].values()],
            "derivation": "company named in gold-cuad question(s) about this document",
        })
        for name in ent[file].values():
            roles.append({"id": f"role-{len(roles) + 1:03d}", "file": file, "entity": name, "role": "party",
                          "derivation": "CUAD party named in the question about this document"})
    for a, b, file, qid in counterpart_pairs:
        pairs.append({"id": f"res-{len(pairs) + 1:03d}", "a": a, "b": b, "same": False, "a_file": file, "b_file": file,
                      "derivation": f"two parties of one contract (gold id {qid})"})
    for file, names in sorted(ent.items()):
        fc = filename_company(file)
        for name in names.values():
            if fc and _core_tokens(fc) == _core_tokens(name):
                pairs.append({"id": f"res-{len(pairs) + 1:03d}", "a": name, "b": fc, "same": True, "a_file": file,
                              "derivation": "question company == file-name company after suffix stripping "
                                            "(circular with name normalisation)"})
    flat = sorted((name, file) for file, names in ent.items() for name in names.values())
    cross = [(a, b) for i, a in enumerate(flat) for b in flat[i + 1:]
             if a[1] != b[1] and not set(_core_tokens(a[0])) & set(_core_tokens(b[0]))]
    random.Random(seed).shuffle(cross)
    for (na, fa), (nb, fb) in sorted(cross[:max_cross_pairs]):
        pairs.append({"id": f"res-{len(pairs) + 1:03d}", "a": na, "b": nb, "same": False, "a_file": fa, "b_file": fb,
                      "derivation": "distinct companies of different contracts sharing no word"})
    return KagGold.model_validate({
        "version": 1, "population": "public_real_contracts", "corpus": corpus,
        "notes": "SILVER, UNREVIEWED: mechanically derived from gold-cuad*.yaml and file names. Not human labels.",
        "entities": entity_docs, "resolution_pairs": pairs, "roles": roles, "clauses": clauses,
    })


def write_gold_yaml(gold: KagGold, path: Path) -> None:
    body = yaml.safe_dump(gold.model_dump(mode="json", exclude_defaults=False), sort_keys=False, allow_unicode=True, width=110)
    header = ("# KAG gold (SILVER, UNREVIEWED). Generated by `docket eval kag silver`; every item is label_source: silver_unreviewed.\n"
              "# Derived from eval-public/gold-cuad*.yaml (CUAD v1, CC BY 4.0) and file names only. Do not edit labels to look human-reviewed.\n")
    Path(path).write_text(header + body, encoding="utf-8")
