import { useState } from "react";
import Card from "../components/Card";
import Badge from "../components/Badge";
import ProgressBar from "../components/ProgressBar";
import { mockSources } from "../mock/sources";
import { formatDateTime } from "../lib/formatters";

// Defaults mirror `attest.config.Settings` (backend/src/attest/config.py):
// gen_model "qwen3:14b", embed_model "qwen3-embedding:0.6b",
// chunk_size_words 200, chunk_overlap_words 40, max_agent_iterations 3,
// max_agent_tool_calls 8, data_dir ~/.local/share/attest.
const DEFAULT_SETTINGS = {
  genModel: "qwen3:14b",
  dataDir: "/home/pc/.local/share/attest",
  chunkSizeWords: 200,
  chunkOverlapWords: 40,
  maxAgentIterations: 3,
  maxAgentToolCalls: 8,
};

function Field({ label, hint, children }) {
  return (
    <label className="block">
      <span className="text-sm font-medium text-slate-700">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-400">{hint}</span>}
    </label>
  );
}

function inputClass() {
  return "mt-1 block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 shadow-sm focus:border-sky-400 focus:outline-none focus:ring-2 focus:ring-sky-200";
}

export default function SettingsScreen() {
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);

  const update = (key) => (e) => {
    const value = e.target.type === "number" ? Number(e.target.value) : e.target.value;
    setSettings((prev) => ({ ...prev, [key]: value }));
  };

  const recentJobs = mockSources
    .flatMap((s) => s.ingestionHistory.map((job) => ({ ...job, sourcePath: s.path })))
    .sort((a, b) => new Date(b.ranAt) - new Date(a.ranAt))
    .slice(0, 5);

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Settings</h1>
        <p className="mt-1 text-sm text-slate-500">
          Model, storage, and agent configuration. Changes here are local to this preview.
        </p>
      </div>

      <Card className="space-y-4 p-5">
        <h2 className="text-sm font-semibold text-slate-900">Model &amp; storage</h2>

        <Field label="Generation model">
          <select value={settings.genModel} onChange={update("genModel")} className={inputClass()}>
            <option value="qwen3:14b">qwen3:14b (recommended -- fits 16GB VRAM)</option>
            <option value="qwen3:30b">qwen3:30b (higher quality, needs more VRAM headroom)</option>
          </select>
        </Field>

        <Field label="Data directory" hint="Where the SQLite catalog, LanceDB index, and evidence store live.">
          <input
            type="text"
            value={settings.dataDir}
            onChange={update("dataDir")}
            className={`${inputClass()} font-mono`}
          />
        </Field>

        <div className="grid grid-cols-2 gap-4">
          <Field label="Chunk size (words)">
            <input
              type="number"
              min={20}
              value={settings.chunkSizeWords}
              onChange={update("chunkSizeWords")}
              className={inputClass()}
            />
          </Field>
          <Field label="Chunk overlap (words)">
            <input
              type="number"
              min={0}
              value={settings.chunkOverlapWords}
              onChange={update("chunkOverlapWords")}
              className={inputClass()}
            />
          </Field>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <Field label="Max agent iterations" hint="Bounded investigation loop cap.">
            <input
              type="number"
              min={1}
              value={settings.maxAgentIterations}
              onChange={update("maxAgentIterations")}
              className={inputClass()}
            />
          </Field>
          <Field label="Max agent tool calls">
            <input
              type="number"
              min={1}
              value={settings.maxAgentToolCalls}
              onChange={update("maxAgentToolCalls")}
              className={inputClass()}
            />
          </Field>
        </div>

        <p className="rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-500">
          Current: <span className="font-mono">{settings.genModel}</span> &middot; chunks of{" "}
          {settings.chunkSizeWords}w / {settings.chunkOverlapWords}w overlap &middot; agent capped
          at {settings.maxAgentIterations} iterations / {settings.maxAgentToolCalls} tool calls.
        </p>
      </Card>

      <Card className="space-y-4 p-5">
        <h2 className="text-sm font-semibold text-slate-900">Diagnostics</h2>

        <div className="flex items-center justify-between">
          <span className="text-sm text-slate-600">Ollama connection</span>
          <Badge className="bg-emerald-50 text-emerald-700 ring-emerald-600/20" dotClassName="bg-emerald-500">
            Connected
          </Badge>
        </div>

        <div className="space-y-1.5">
          <div className="flex items-center justify-between text-xs text-slate-500">
            <span>VRAM</span>
            <span>11.4 GB / 17.1 GB</span>
          </div>
          <ProgressBar percent={66.7} colorClassName="bg-sky-500" />
        </div>

        <div className="space-y-1.5">
          <div className="flex items-center justify-between text-xs text-slate-500">
            <span>RAM</span>
            <span>9.8 GB / 32 GB</span>
          </div>
          <ProgressBar percent={30.6} colorClassName="bg-violet-500" />
        </div>

        <div>
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">
            Recent ingestion jobs
          </p>
          <ul className="space-y-1.5">
            {recentJobs.map((job) => (
              <li
                key={job.job_id}
                className="flex items-center justify-between rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-600"
              >
                <span className="truncate font-mono">{job.sourcePath}</span>
                <span className="ml-3 shrink-0 text-slate-400">
                  {job.status} &middot; {formatDateTime(job.ranAt)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </Card>
    </div>
  );
}
