// Small formatting helpers shared across screens. Kept dependency-free
// (no date-fns/etc.) since the needs here are modest.

/** Absolute, readable timestamp, e.g. "Sep 22, 2026, 4:05 PM". */
export function formatDateTime(isoString) {
  if (!isoString) return "--";
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return "--";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Short date only, e.g. "Sep 22, 2026". Used for authority/date badges
 * where a full timestamp would be noisy. */
export function formatDate(isoString) {
  if (!isoString) return "--";
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return "--";
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/** Coarse relative time ("3m ago", "2h ago", "5d ago") for compact rows. */
export function formatRelativeTime(isoString) {
  if (!isoString) return "--";
  const then = new Date(isoString).getTime();
  if (Number.isNaN(then)) return "--";
  const diffMs = Date.now() - then;
  const diffSec = Math.round(diffMs / 1000);
  if (diffSec < 60) return "just now";
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  const diffDay = Math.round(diffHour / 24);
  return `${diffDay}d ago`;
}

/**
 * Truncates a chunk_id to the first N hex characters, matching
 * `attest.retrieval.resolver._citation_label`'s `chunk_id[:12]` convention.
 */
export function truncateChunkId(chunkId, length = 12) {
  if (!chunkId) return "";
  return chunkId.slice(0, length);
}

/** Builds the exact `[{source_display_name} #{chunk_id[:12]}]` citation
 * label format used by the real backend, for anywhere the mock data needs
 * to derive one rather than hardcode it. */
export function citationLabel(sourceDisplayName, chunkId) {
  return `[${sourceDisplayName} #${truncateChunkId(chunkId)}]`;
}

export function formatNumber(n) {
  if (n === null || n === undefined) return "--";
  return n.toLocaleString();
}
