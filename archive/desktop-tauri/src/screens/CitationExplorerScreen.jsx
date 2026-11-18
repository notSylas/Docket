import { useEffect, useMemo, useState } from "react";
import Card from "../components/Card";
import Badge from "../components/Badge";
import { formatDate } from "../lib/formatters";
import { ALL_MOCK_ANSWERS } from "../mock/queryResponses";

function dedupeCitations(citations) {
  const seen = new Map();
  for (const c of citations) {
    if (!seen.has(c.chunk_id)) seen.set(c.chunk_id, c);
  }
  return [...seen.values()];
}

function ClaimTypeBadge({ claimType }) {
  if (claimType === "inferred") {
    return <Badge className="bg-purple-50 text-purple-700 ring-purple-600/20">Inferred</Badge>;
  }
  return <Badge className="bg-emerald-50 text-emerald-700 ring-emerald-600/20">Evidenced</Badge>;
}

function AuthorityBadge({ authority }) {
  const styles =
    authority === "Approved Spec"
      ? "bg-indigo-50 text-indigo-700 ring-indigo-600/20"
      : authority === "Meeting Note"
        ? "bg-slate-100 text-slate-600 ring-slate-400/30"
        : "bg-sky-50 text-sky-700 ring-sky-600/20";
  return <Badge className={styles}>{authority}</Badge>;
}

function EvidenceCard({ citation, expanded, onToggle }) {
  return (
    <Card className="p-4">
      <button type="button" onClick={onToggle} className="w-full text-left">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-mono text-sm font-medium text-slate-900">
            {citation.source_display_name}
          </span>
          <ClaimTypeBadge claimType={citation.claim_type} />
          {citation.authority && <AuthorityBadge authority={citation.authority} />}
          {citation.is_historical && (
            <Badge className="bg-amber-50 text-amber-700 ring-amber-600/20">
              Historical{citation.superseded ? " -- superseded" : ""}
            </Badge>
          )}
        </div>
        <p className="mt-1 font-mono text-xs text-slate-500">{citation.citation_label}</p>
        {citation.heading && (
          <p className="mt-1 text-sm font-medium text-slate-700">{citation.heading}</p>
        )}
        <div className="mt-1 flex items-center gap-1 text-xs text-slate-400">
          <span>as of {formatDate(citation.as_of)}</span>
          <span aria-hidden="true">&middot;</span>
          <span>{expanded ? "click to collapse" : "click to expand"}</span>
        </div>
      </button>
      {expanded && (
        <p className="mt-3 border-t border-slate-100 pt-3 text-sm leading-relaxed text-slate-700">
          {citation.chunk_text}
        </p>
      )}
    </Card>
  );
}

function ConflictGroup({ citations, expandedId, onToggle }) {
  return (
    <div>
      <div className="mb-2 flex items-center gap-2">
        <span className="text-lg" aria-hidden="true">
          &#9888;
        </span>
        <p className="text-sm font-medium text-amber-800">
          Conflicting sources -- shown side by side rather than auto-resolved
        </p>
      </div>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {citations.map((c) => (
          <EvidenceCard
            key={c.chunk_id}
            citation={c}
            expanded={expandedId === c.chunk_id}
            onToggle={() => onToggle(c.chunk_id)}
          />
        ))}
      </div>
    </div>
  );
}

export default function CitationExplorerScreen({ selectedCitation }) {
  const allCitations = useMemo(
    () => dedupeCitations(ALL_MOCK_ANSWERS.flatMap((a) => a.citations)),
    [],
  );

  const [expandedId, setExpandedId] = useState(selectedCitation?.chunk_id ?? null);

  useEffect(() => {
    if (selectedCitation?.chunk_id) {
      setExpandedId(selectedCitation.chunk_id);
    }
  }, [selectedCitation]);

  const toggle = (chunkId) => setExpandedId((prev) => (prev === chunkId ? null : chunkId));

  const grouped = useMemo(() => {
    const groups = new Map();
    const solo = [];
    for (const c of allCitations) {
      if (c.conflict_group) {
        if (!groups.has(c.conflict_group)) groups.set(c.conflict_group, []);
        groups.get(c.conflict_group).push(c);
      } else {
        solo.push(c);
      }
    }
    return { groups, solo };
  }, [allCitations]);

  return (
    <div className="mx-auto max-w-4xl">
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-slate-900">Citation Explorer</h1>
        <p className="mt-1 text-sm text-slate-500">
          {selectedCitation
            ? "Showing the citation you selected, expanded below."
            : "All evidence referenced across recent answers."}
        </p>
      </div>

      <div className="space-y-4">
        {[...grouped.groups.values()].map((items) => (
          <ConflictGroup
            key={items[0].conflict_group}
            citations={items}
            expandedId={expandedId}
            onToggle={toggle}
          />
        ))}

        {grouped.solo.map((c) => (
          <EvidenceCard
            key={c.chunk_id}
            citation={c}
            expanded={expandedId === c.chunk_id}
            onToggle={() => toggle(c.chunk_id)}
          />
        ))}
      </div>
    </div>
  );
}
