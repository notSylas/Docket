import { useCallback, useState } from "react";
import { mockSources, makeJobResult } from "../mock/sources";
import { SOURCE_STATUS } from "../constants/sourceStatus";

let sourceCounter = 0;
function nextSourceId() {
  sourceCounter += 1;
  return `src_new_${String(sourceCounter).padStart(3, "0")}`;
}

// Believable file names to hand out when an "Ingest now" run pretends to
// discover new content in a freshly added mock source.
const SAMPLE_FILES = [
  "notes.md",
  "design-doc.pdf",
  "onboarding.docx",
  "readme.md",
  "decisions-log.docx",
];

/**
 * Local mock-state management for the Sources screen: list, add, ingest,
 * toggle watch, revoke. No network/Tauri calls -- everything here is
 * `setState` against `mock/sources.js`'s seed data, standing in for what
 * will eventually be real IPC calls into the backend's `SourceManager` /
 * `IngestionPipeline`.
 */
export function useSources() {
  const [sources, setSources] = useState(mockSources);
  const [ingestingIds, setIngestingIds] = useState(() => new Set());
  const [lastJobSummary, setLastJobSummary] = useState(null);

  const addSource = useCallback((path) => {
    const trimmed = path.trim();
    if (!trimmed) return;
    const newSource = {
      id: nextSourceId(),
      workspace_id: "ws_default",
      source_type: "local_folder",
      path: trimmed,
      status: SOURCE_STATUS.ACTIVE,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      chunkCount: 0,
      watched: false,
      lastIngestedAt: null,
      ingestionHistory: [],
    };
    setSources((prev) => [newSource, ...prev]);
    return newSource.id;
  }, []);

  const revokeSource = useCallback((id) => {
    // Revocation is intentionally instant in local state -- the real
    // backend's cleanup is async, but the product requirement is that the
    // UI reflects "revoked" immediately, not after a round trip.
    setSources((prev) =>
      prev.map((s) =>
        s.id === id
          ? { ...s, status: SOURCE_STATUS.REVOKED, updated_at: new Date().toISOString() }
          : s,
      ),
    );
  }, []);

  const toggleWatch = useCallback((id) => {
    setSources((prev) => prev.map((s) => (s.id === id ? { ...s, watched: !s.watched } : s)));
  }, []);

  const ingestNow = useCallback((id) => {
    setIngestingIds((prevIngesting) => {
      if (prevIngesting.has(id)) return prevIngesting; // already running, ignore
      return new Set(prevIngesting).add(id);
    });

    const delay = 1100 + Math.random() * 900;
    window.setTimeout(() => {
      setSources((prev) =>
        prev.map((s) => {
          if (s.id !== id) return s;

          const fileCount = 1 + Math.floor(Math.random() * 2);
          const files = Array.from({ length: fileCount }).map((_, i) => {
            const failed = Math.random() < 0.12;
            const name = SAMPLE_FILES[Math.floor(Math.random() * SAMPLE_FILES.length)];
            return failed
              ? {
                  path: name,
                  status: "failed",
                  chunks_written: 0,
                  error: "Docling parser raised: could not detect document structure",
                }
              : {
                  path: name,
                  status: i === 0 ? "ingested" : "unchanged",
                  chunks_written: i === 0 ? 4 + Math.floor(Math.random() * 20) : 0,
                  error: null,
                };
          });

          const job = makeJobResult({
            sourceId: id,
            status: files.some((f) => f.status === "failed")
              ? files.every((f) => f.status === "failed")
                ? "failed"
                : "partial"
              : "succeeded",
            files,
          });

          const chunksWritten = files.reduce((sum, f) => sum + f.chunks_written, 0);

          setLastJobSummary({ sourceId: id, sourcePath: s.path, ...job });

          return {
            ...s,
            chunkCount: s.chunkCount + chunksWritten,
            lastIngestedAt: job.ranAt,
            ingestionHistory: [job, ...s.ingestionHistory],
          };
        }),
      );
      setIngestingIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }, delay);
  }, []);

  const dismissJobSummary = useCallback(() => setLastJobSummary(null), []);

  return {
    sources,
    ingestingIds,
    lastJobSummary,
    addSource,
    revokeSource,
    toggleWatch,
    ingestNow,
    dismissJobSummary,
  };
}
