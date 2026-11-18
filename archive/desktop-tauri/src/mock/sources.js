// Mock `Source` + `IngestionJobResult` data, shaped to match
// `attest.db.models.Source` and `attest.ingestion.pipeline.IngestionJobResult`
// / `FileIngestResult` (backend/src/attest/db/models.py,
// backend/src/attest/ingestion/pipeline.py) field-for-field, so wiring in
// the real API later is a drop-in swap of the data source, not a reshape.
//
// Fields prefixed with nothing extra ARE the real backend shape:
//   id, workspace_id, source_type, path, status, created_at, updated_at
// Fields below that are UI-only additions (not on the ORM model) are called
// out explicitly -- `chunkCount`, `watched`, `lastIngestedAt`,
// `ingestionHistory` -- since the real API doesn't expose them on `Source`
// directly yet (chunk count would come from a join/count query, watch state
// doesn't exist server-side at all in this checkpoint).

import { SOURCE_STATUS } from "../constants/sourceStatus";

let jobCounter = 0;
function nextJobId() {
  jobCounter += 1;
  return `job_${String(jobCounter).padStart(4, "0")}`;
}

/** Builds an `IngestionJobResult`-shaped object (job_id, status,
 * files_processed, files_failed, file_results, plus a `source_id` and a
 * `ranAt` timestamp for display -- `ranAt` is UI-only, real jobs would
 * carry started_at/finished_at on the `IngestionJob` row). */
export function makeJobResult({
  sourceId,
  status = "succeeded",
  files,
  ranAt,
}) {
  const fileResults = files || [];
  const filesProcessed = fileResults.filter((f) => f.status !== "failed").length;
  const filesFailed = fileResults.filter((f) => f.status === "failed").length;
  return {
    source_id: sourceId,
    job_id: nextJobId(),
    status,
    files_processed: filesProcessed,
    files_failed: filesFailed,
    file_results: fileResults,
    ranAt: ranAt || new Date().toISOString(),
  };
}

function daysAgoIso(days, hours = 0) {
  const d = new Date();
  d.setDate(d.getDate() - days);
  d.setHours(d.getHours() - hours);
  return d.toISOString();
}

export const mockSources = [
  {
    id: "src_a1b2c3d4",
    workspace_id: "ws_default",
    source_type: "local_folder",
    path: "/home/pc/Documents/attest/specs",
    status: SOURCE_STATUS.ACTIVE,
    created_at: daysAgoIso(21),
    updated_at: daysAgoIso(1),
    // -- UI-only fields --
    chunkCount: 184,
    watched: true,
    lastIngestedAt: daysAgoIso(1),
    ingestionHistory: [
      makeJobResult({
        sourceId: "src_a1b2c3d4",
        status: "succeeded",
        ranAt: daysAgoIso(1),
        files: [
          { path: "attest-architecture.md", status: "ingested", chunks_written: 34, error: null },
          { path: "ingestion-pipeline-spec.docx", status: "unchanged", chunks_written: 0, error: null },
          { path: "query-service-design.pdf", status: "ingested", chunks_written: 21, error: null },
        ],
      }),
      makeJobResult({
        sourceId: "src_a1b2c3d4",
        status: "partial",
        ranAt: daysAgoIso(6),
        files: [
          { path: "attest-architecture.md", status: "ingested", chunks_written: 31, error: null },
          {
            path: "legacy-draft.docx",
            status: "failed",
            chunks_written: 0,
            error: "Docling parser raised: unsupported embedded object type",
          },
        ],
      }),
      makeJobResult({
        sourceId: "src_a1b2c3d4",
        status: "succeeded",
        ranAt: daysAgoIso(14),
        files: [
          { path: "attest-architecture.md", status: "ingested", chunks_written: 29, error: null },
          { path: "ingestion-pipeline-spec.docx", status: "ingested", chunks_written: 18, error: null },
        ],
      }),
    ],
  },
  {
    id: "src_e5f6a7b8",
    workspace_id: "ws_default",
    source_type: "local_folder",
    path: "/home/pc/Documents/attest/meeting-notes",
    status: SOURCE_STATUS.ACTIVE,
    created_at: daysAgoIso(18),
    updated_at: daysAgoIso(3),
    chunkCount: 62,
    watched: false,
    lastIngestedAt: daysAgoIso(3),
    ingestionHistory: [
      makeJobResult({
        sourceId: "src_e5f6a7b8",
        status: "succeeded",
        ranAt: daysAgoIso(3),
        files: [
          { path: "meeting-notes-2026-08-15.docx", status: "ingested", chunks_written: 9, error: null },
          { path: "meeting-notes-2026-08-20.docx", status: "ingested", chunks_written: 7, error: null },
        ],
      }),
    ],
  },
  {
    id: "src_c9d0e1f2",
    workspace_id: "ws_default",
    source_type: "local_folder",
    path: "/home/pc/Downloads/vendor-contracts",
    status: SOURCE_STATUS.MISSING,
    created_at: daysAgoIso(40),
    updated_at: daysAgoIso(9),
    chunkCount: 47,
    watched: true,
    lastIngestedAt: daysAgoIso(9),
    ingestionHistory: [
      makeJobResult({
        sourceId: "src_c9d0e1f2",
        status: "failed",
        ranAt: daysAgoIso(9),
        files: [
          {
            path: "vendor-contracts",
            status: "failed",
            chunks_written: 0,
            error: "Source path not found on disk (folder may have been moved or unmounted)",
          },
        ],
      }),
      makeJobResult({
        sourceId: "src_c9d0e1f2",
        status: "succeeded",
        ranAt: daysAgoIso(30),
        files: [
          { path: "msa-acme-corp.pdf", status: "ingested", chunks_written: 22, error: null },
          { path: "sow-q3-2026.pdf", status: "ingested", chunks_written: 25, error: null },
        ],
      }),
    ],
  },
  {
    id: "src_b3c4d5e6",
    workspace_id: "ws_default",
    source_type: "local_folder",
    path: "/home/pc/Documents/attest/old-proposals",
    status: SOURCE_STATUS.REVOKED,
    created_at: daysAgoIso(60),
    updated_at: daysAgoIso(25),
    chunkCount: 30,
    watched: false,
    lastIngestedAt: daysAgoIso(25),
    ingestionHistory: [
      makeJobResult({
        sourceId: "src_b3c4d5e6",
        status: "succeeded",
        ranAt: daysAgoIso(25),
        files: [{ path: "proposal-draft-v1.docx", status: "ingested", chunks_written: 30, error: null }],
      }),
    ],
  },
];
