// Inline, clickable citation tag rendered in an answer's body. Shows the
// exact `citation_label` format from the backend
// (`[{source_display_name} #{chunk_id[:12]}]`) and, when the mock data
// carries the UI-only extensions, small dots for "inferred" and
// "historical" so the distinction is visible even at chip size.
export default function CitationChip({ citation, onClick }) {
  const isInferred = citation.claim_type === "inferred";
  const isHistorical = citation.is_historical;

  return (
    <button
      type="button"
      onClick={() => onClick?.(citation)}
      title={citation.heading ? `${citation.heading} -- click to view evidence` : "View evidence"}
      className="mx-0.5 inline-flex items-center gap-1 rounded-md border border-sky-200 bg-sky-50 px-1.5 py-0.5 align-baseline font-mono text-[11px] font-medium text-sky-800 hover:border-sky-400 hover:bg-sky-100 focus:outline-none focus:ring-2 focus:ring-sky-400"
    >
      {citation.citation_label}
      {isInferred && (
        <span
          className="h-1.5 w-1.5 rounded-full bg-purple-400"
          title="Inferred (agent-derived), not directly quoted"
        />
      )}
      {isHistorical && (
        <span
          className="h-1.5 w-1.5 rounded-full bg-amber-500"
          title="References a historical document version"
        />
      )}
    </button>
  );
}
