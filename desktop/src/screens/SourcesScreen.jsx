import { useState } from "react";
import { useSources } from "../hooks/useSources";
import StatusBadge from "../components/StatusBadge";
import Card from "../components/Card";
import Badge from "../components/Badge";
import { formatDateTime, formatRelativeTime, formatNumber } from "../lib/formatters";
import { SOURCE_STATUS } from "../constants/sourceStatus";

function JobHistoryRow({ job }) {
  const statusColor =
    job.status === "succeeded"
      ? "text-emerald-700"
      : job.status === "partial"
        ? "text-amber-700"
        : "text-red-700";
  return (
    <div className="border-t border-slate-100 py-2 text-xs">
      <div className="flex items-center justify-between">
        <span className={`font-medium ${statusColor}`}>{job.status}</span>
        <span className="text-slate-400">{formatDateTime(job.ranAt)}</span>
      </div>
      <div className="mt-0.5 text-slate-500">
        {job.files_processed} processed &middot; {job.files_failed} failed &middot; job {job.job_id}
      </div>
      {job.file_results?.length > 0 && (
        <ul className="mt-1 space-y-0.5 pl-3 text-slate-500">
          {job.file_results.map((f, i) => (
            <li key={i} className="flex items-start gap-1">
              <span
                className={
                  f.status === "failed"
                    ? "text-red-600"
                    : f.status === "ingested"
                      ? "text-emerald-600"
                      : "text-slate-400"
                }
              >
                &bull;
              </span>
              <span>
                <span className="font-mono">{f.path}</span> -- {f.status}
                {f.status === "ingested" && ` (${f.chunks_written} chunks)`}
                {f.error && <span className="text-red-500"> -- {f.error}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SourceRow({ source, isIngesting, onIngest, onToggleWatch, onRevoke }) {
  const [expanded, setExpanded] = useState(false);
  const canIngest = source.status === SOURCE_STATUS.ACTIVE && !isIngesting;
  const canRevoke = source.status === SOURCE_STATUS.ACTIVE || source.status === SOURCE_STATUS.MISSING;

  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="truncate font-mono text-sm text-slate-800">{source.path}</p>
            <StatusBadge status={source.status} />
            {source.watched && (
              <Badge className="bg-indigo-50 text-indigo-700 ring-indigo-600/20">Watching</Badge>
            )}
          </div>
          <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
            <span>{formatNumber(source.chunkCount)} chunks</span>
            <span>
              last ingested{" "}
              {source.lastIngestedAt ? formatRelativeTime(source.lastIngestedAt) : "never"}
            </span>
            <span className="text-slate-400">{source.id}</span>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            disabled={!canIngest}
            onClick={() => onIngest(source.id)}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {isIngesting ? (
              <span className="inline-flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-white" />
                Ingesting&hellip;
              </span>
            ) : (
              "Ingest now"
            )}
          </button>
          <button
            type="button"
            onClick={() => onToggleWatch(source.id)}
            className={`rounded-md border px-3 py-1.5 text-xs font-medium ${
              source.watched
                ? "border-indigo-300 bg-indigo-50 text-indigo-700 hover:bg-indigo-100"
                : "border-slate-300 bg-white text-slate-600 hover:bg-slate-50"
            }`}
          >
            {source.watched ? "Unwatch" : "Watch"}
          </button>
          <button
            type="button"
            disabled={!canRevoke}
            onClick={() => onRevoke(source.id)}
            className="rounded-md border border-red-200 bg-white px-3 py-1.5 text-xs font-medium text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-300"
          >
            Revoke
          </button>
        </div>
      </div>

      {source.ingestionHistory?.length > 0 && (
        <div className="mt-3">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-xs font-medium text-sky-700 hover:text-sky-900"
          >
            {expanded ? "Hide" : "Show"} ingestion history ({source.ingestionHistory.length})
          </button>
          {expanded && (
            <div className="mt-1">
              {source.ingestionHistory.map((job) => (
                <JobHistoryRow key={job.job_id} job={job} />
              ))}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

export default function SourcesScreen() {
  const {
    sources,
    ingestingIds,
    lastJobSummary,
    addSource,
    revokeSource,
    toggleWatch,
    ingestNow,
    dismissJobSummary,
  } = useSources();
  const [newPath, setNewPath] = useState("");

  const handleAdd = (e) => {
    e.preventDefault();
    if (!newPath.trim()) return;
    addSource(newPath);
    setNewPath("");
  };

  return (
    <div className="mx-auto max-w-4xl">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Sources</h1>
          <p className="mt-1 text-sm text-slate-500">
            Folders registered as evidence sources for ingestion and retrieval.
          </p>
        </div>
      </div>

      <form onSubmit={handleAdd} className="mb-6 flex gap-2">
        <input
          type="text"
          value={newPath}
          onChange={(e) => setNewPath(e.target.value)}
          placeholder="/home/user/Documents/project-folder"
          className="flex-1 rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-sm text-slate-800 shadow-sm focus:border-sky-400 focus:outline-none focus:ring-2 focus:ring-sky-200"
        />
        <button
          type="submit"
          className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-700"
        >
          Add source
        </button>
      </form>

      {lastJobSummary && (
        <div className="mb-6 flex items-start justify-between gap-3 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900">
          <div>
            <p className="font-medium">
              Ingestion {lastJobSummary.status} for{" "}
              <span className="font-mono">{lastJobSummary.sourcePath}</span>
            </p>
            <p className="mt-0.5 text-emerald-700">
              {lastJobSummary.files_processed} files processed &middot;{" "}
              {lastJobSummary.files_failed} failed &middot; job {lastJobSummary.job_id}
            </p>
          </div>
          <button
            type="button"
            onClick={dismissJobSummary}
            className="text-emerald-700 hover:text-emerald-900"
            aria-label="Dismiss"
          >
            &times;
          </button>
        </div>
      )}

      <div className="space-y-3">
        {sources.map((source) => (
          <SourceRow
            key={source.id}
            source={source}
            isIngesting={ingestingIds.has(source.id)}
            onIngest={ingestNow}
            onToggleWatch={toggleWatch}
            onRevoke={revokeSource}
          />
        ))}
        {sources.length === 0 && (
          <p className="py-12 text-center text-sm text-slate-400">
            No sources yet. Add a folder above to get started.
          </p>
        )}
      </div>
    </div>
  );
}
