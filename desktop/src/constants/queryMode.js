// Mirrors `attest.query.classifier.QueryMode` (backend/src/attest/query/classifier.py)
// and the `mode` field on `attest.query.service.QueryResult`.
export const QUERY_MODE = {
  FAST: "fast",
  AGENT: "agent",
};

export const QUERY_MODE_META = {
  [QUERY_MODE.FAST]: {
    label: "Fast",
    description: "Single-pass retrieval + generation",
    badgeClass: "bg-sky-50 text-sky-700 ring-sky-600/20",
  },
  [QUERY_MODE.AGENT]: {
    label: "Agent",
    description: "Bounded multi-step investigation",
    badgeClass: "bg-violet-50 text-violet-700 ring-violet-600/20",
  },
};
