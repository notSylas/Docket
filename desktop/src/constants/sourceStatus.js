// Single source of truth for source lifecycle state.
//
// Mirrors `attest.db.models.SourceStatus` (backend/src/attest/db/models.py)
// exactly -- same string values -- so this file is a drop-in mapping target
// once the real API is wired up: no renaming needed on either side.
export const SOURCE_STATUS = {
  ACTIVE: "active",
  MISSING: "missing",
  REVOKED: "revoked",
  TOMBSTONED: "tombstoned",
  HARD_DELETE_PENDING: "hard_delete_pending",
  DELETED: "deleted",
};

// Display metadata for StatusBadge. Colors are Tailwind utility classes
// (background/text/dot) chosen for a light neutral UI with clear semantic
// meaning: green = healthy, amber = needs attention, red = revoked/gone,
// slate = terminal/inert states.
export const SOURCE_STATUS_META = {
  [SOURCE_STATUS.ACTIVE]: {
    label: "Active",
    badgeClass: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
    dotClass: "bg-emerald-500",
  },
  [SOURCE_STATUS.MISSING]: {
    label: "Missing",
    badgeClass: "bg-amber-50 text-amber-700 ring-amber-600/20",
    dotClass: "bg-amber-500",
  },
  [SOURCE_STATUS.REVOKED]: {
    label: "Revoked",
    badgeClass: "bg-red-50 text-red-700 ring-red-600/20",
    dotClass: "bg-red-500",
  },
  [SOURCE_STATUS.TOMBSTONED]: {
    label: "Tombstoned",
    badgeClass: "bg-slate-100 text-slate-600 ring-slate-500/20",
    dotClass: "bg-slate-400",
  },
  [SOURCE_STATUS.HARD_DELETE_PENDING]: {
    label: "Delete pending",
    badgeClass: "bg-orange-50 text-orange-700 ring-orange-600/20",
    dotClass: "bg-orange-500",
  },
  [SOURCE_STATUS.DELETED]: {
    label: "Deleted",
    badgeClass: "bg-slate-100 text-slate-500 ring-slate-500/20",
    dotClass: "bg-slate-400",
  },
};
