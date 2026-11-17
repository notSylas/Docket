"""KAG gold-set schema, loader and dataset validation.

One YAML file holds five item lists (all optional): `entities` (per-document entity lists),
`resolution_pairs` (same/different company pairs), `roles` (document->entity role),
`relations` (amends/supersedes between two documents) and `clauses` (clause presence/absence,
with the question to retrieve with and expected quote spans).

Label provenance is explicit on every item. `silver_unreviewed` labels are mechanical candidates
(see silver.py); `human_reviewed` requires `human_reviewed: true` AND `reviewed_by`. Nothing in
this repository sets `human_reviewed: true` on its own.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from docket.eval.schema import GoldSetError
from docket.services.documents import kag as kag_graph

SILVER = "silver_unreviewed"
HUMAN = "human_reviewed"
Role = Literal["party", "mention"]
Relation = Literal["amends", "supersedes", "none"]
CLAUSE_NAMES = frozenset(c.name for c in kag_graph.CLAUSE_TYPES)


class KagGoldError(GoldSetError):
    pass


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label_source: Literal["silver_unreviewed", "human_reviewed"] = SILVER
    human_reviewed: bool = False
    reviewed_by: str | None = None
    review_status: Literal["pending", "confirmed", "rejected"] = "pending"
    derivation: str | None = None  # how a silver label was derived (rule + source gold id)
    notes: str = ""

    @model_validator(mode="after")
    def _provenance(self):
        if self.label_source == HUMAN:
            if not self.human_reviewed or not self.reviewed_by:
                raise ValueError("label_source human_reviewed needs human_reviewed: true and reviewed_by")
        elif self.human_reviewed:
            raise ValueError("human_reviewed: true requires label_source: human_reviewed")
        return self

    @property
    def is_human(self) -> bool:
        return self.label_source == HUMAN and self.human_reviewed


def _check_file(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"bad document path {value!r}: must be relative, no '..'")
    return value


class GoldEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    role: Role = "party"
    aliases: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("entity name is empty")
        return v.strip()


class EntityDoc(_Item):
    id: str
    file: str
    entities: list[GoldEntity]
    # complete: the list names EVERY entity of the document, so precision is meaningful.
    # Silver lists are never complete.
    complete: bool = False

    _f = field_validator("file")(_check_file)


class ResolutionPair(_Item):
    id: str
    a: str
    b: str
    same: bool
    a_file: str | None = None
    b_file: str | None = None


class RoleItem(_Item):
    id: str
    file: str
    entity: str
    role: Role

    _f = field_validator("file")(_check_file)


class RelationItem(_Item):
    id: str
    from_file: str
    to_file: str
    relation: Relation

    _f = field_validator("from_file", "to_file")(_check_file)


class ClauseItem(_Item):
    id: str
    file: str
    clause_type: str
    present: bool
    question: str | None = None
    gold_spans: list[str] = Field(default_factory=list)

    _f = field_validator("file")(_check_file)

    @field_validator("clause_type")
    @classmethod
    def _clause(cls, v: str) -> str:
        if v not in CLAUSE_NAMES:
            raise ValueError(f"unknown clause_type {v!r}; expected one of {sorted(CLAUSE_NAMES)}")
        return v

    @model_validator(mode="after")
    def _spans(self):
        if not self.present and self.gold_spans:
            raise ValueError("an absent clause cannot have gold_spans")
        return self


class KagGold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    population: str = "unspecified"
    corpus: str = ""
    notes: str = ""
    entities: list[EntityDoc] = Field(default_factory=list)
    resolution_pairs: list[ResolutionPair] = Field(default_factory=list)
    roles: list[RoleItem] = Field(default_factory=list)
    relations: list[RelationItem] = Field(default_factory=list)
    clauses: list[ClauseItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self):
        for name in ("entities", "resolution_pairs", "roles", "relations", "clauses"):
            ids = [i.id for i in getattr(self, name)]
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                raise ValueError(f"duplicate ids in {name}: {', '.join(dupes)}")
        keys = {}
        for c in self.clauses:
            if keys.setdefault((c.file, c.clause_type), c.present) != c.present:
                raise ValueError(f"clause {c.clause_type} for {c.file} is labelled both present and absent")
        return self

    def all_items(self) -> list[_Item]:
        return [*self.entities, *self.resolution_pairs, *self.roles, *self.relations, *self.clauses]

    def files(self) -> set[str]:
        out = {e.file for e in self.entities} | {r.file for r in self.roles} | {c.file for c in self.clauses}
        out |= {r.from_file for r in self.relations} | {r.to_file for r in self.relations}
        out |= {f for p in self.resolution_pairs for f in (p.a_file, p.b_file) if f}
        return out


def _format_error(path: Path, exc: ValidationError) -> str:
    lines = [f"invalid KAG gold {path}:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_kag_gold(path: str | Path) -> KagGold:
    """Read and validate a KAG gold YAML; raises KagGoldError with readable messages."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise KagGoldError(f"cannot read KAG gold {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise KagGoldError(f"KAG gold {path} must be a YAML mapping")
    try:
        return KagGold.model_validate(raw)
    except ValidationError as exc:
        raise KagGoldError(_format_error(path, exc)) from exc


def validate_against_library(gold: KagGold, library_files: set[str]) -> list[str]:
    """Problems linking the gold to a concrete library: files that are not in it (compared by
    base name). Empty list = consistent."""
    have = {PurePosixPath(f).name for f in library_files}
    missing = sorted(f for f in gold.files() if PurePosixPath(f).name not in have)
    return [f"gold references a document that is not in the library: {f}" for f in missing]


def select_items(items: list, *, human_only: bool = False) -> list:
    """Items that count toward metrics: rejected items never do; `human_only` also drops
    silver/pending labels."""
    return [i for i in items if i.review_status != "rejected" and (i.is_human or not human_only)]


def provenance_counts(items: list) -> dict[str, int]:
    kept = select_items(items)
    return {"human_reviewed": sum(i.is_human for i in kept), "silver_unreviewed": sum(not i.is_human for i in kept)}
