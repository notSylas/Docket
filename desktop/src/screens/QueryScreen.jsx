import { useState } from "react";
import { useQuery } from "../hooks/useQuery";
import ModeBadge from "../components/ModeBadge";
import CitationChip from "../components/CitationChip";
import Badge from "../components/Badge";
import Card from "../components/Card";
import { formatDate } from "../lib/formatters";
import { QUERY_MODE } from "../constants/queryMode";

const SAMPLE_QUESTIONS = [
  "What vector database does Attest use for the embedding index?",
  "Compare the fast query path and the agent investigation path -- why does Attest need both?",
  "What is Attest's pricing model for enterprise customers?",
  "What embedding model does Attest use, and is that settled?",
  "How has the chunking recipe changed over the years?",
];

/** Splits an answer string on inline `[label #hex]` citation tags and
 * interleaves matching CitationChip components, so citations render inline
 * with the prose instead of only in a trailing list. */
function AnswerBody({ text, citations, onCiteClick }) {
  const byLabel = new Map(citations.map((c) => [c.citation_label, c]));
  const pattern = /(\[[^\]]+\s#[0-9a-f]+\])/g;
  const parts = text.split(pattern);

  return (
    <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-800">
      {parts.map((part, i) => {
        const citation = byLabel.get(part);
        if (citation) {
          return <CitationChip key={i} citation={citation} onClick={onCiteClick} />;
        }
        return <span key={i}>{part}</span>;
      })}
    </p>
  );
}

function ConflictPanel({ citations, onCiteClick }) {
  const groups = new Map();
  for (const c of citations) {
    if (!c.conflict_group) continue;
    if (!groups.has(c.conflict_group)) groups.set(c.conflict_group, []);
    groups.get(c.conflict_group).push(c);
  }
  if (groups.size === 0) return null;

  return (
    <div className="mt-3 space-y-3">
      {[...groups.entries()].map(([group, items]) => (
        <div key={group}>
          <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-amber-700">
            Conflicting sources
          </p>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {items.map((c) => (
              <button
                type="button"
                key={c.chunk_id}
                onClick={() => onCiteClick(c)}
                className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-left hover:border-amber-400"
              >
                <div className="mb-1 flex flex-wrap items-center gap-1.5">
                  <Badge className="bg-white text-slate-700 ring-slate-300">{c.authority}</Badge>
                  <span className="text-xs text-slate-500">{formatDate(c.as_of)}</span>
                </div>
                <p className="font-mono text-[11px] text-slate-600">{c.citation_label}</p>
                <p className="mt-1 line-clamp-2 text-xs text-slate-600">{c.chunk_text}</p>
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function AbstentionCard({ result }) {
  return (
    <Card className="border-amber-300 bg-amber-50 p-4">
      <div className="flex items-start gap-2">
        <span className="mt-0.5 text-lg" aria-hidden="true">
          &#9888;
        </span>
        <div className="flex-1">
          <p className="text-sm font-semibold text-amber-900">Insufficient evidence</p>
          <p className="mt-1 text-sm text-amber-900">{result.answer}</p>
          {result.checked_items?.length > 0 && (
            <div className="mt-3">
              <p className="text-xs font-medium uppercase tracking-wide text-amber-700">
                What was checked
              </p>
              <ul className="mt-1.5 space-y-1">
                {result.checked_items.map((item, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs text-amber-800">
                    <span className="mt-0.5">&bull;</span>
                    <span>
                      <span className="font-medium">{item.label}:</span> {item.result}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}

function AnswerCard({ result, onCiteClick }) {
  if (result.abstained) {
    return <AbstentionCard result={result} />;
  }

  const hasConflict = result.citations.some((c) => c.conflict_group);

  return (
    <Card className="p-4">
      <div className="mb-2 flex items-center gap-2">
        <ModeBadge mode={result.mode} />
        {result.validation_warnings?.length > 0 && (
          <Badge className="bg-amber-50 text-amber-700 ring-amber-600/20">
            {result.validation_warnings.length} warning
            {result.validation_warnings.length > 1 ? "s" : ""}
          </Badge>
        )}
      </div>
      <AnswerBody text={result.answer} citations={result.citations} onCiteClick={onCiteClick} />
      {hasConflict && <ConflictPanel citations={result.citations} onCiteClick={onCiteClick} />}
      {result.citations.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5 border-t border-slate-100 pt-3">
          {result.citations.map((c) => (
            <CitationChip key={c.chunk_id} citation={c} onClick={onCiteClick} />
          ))}
        </div>
      )}
    </Card>
  );
}

function ThinkingIndicator({ mode }) {
  const label = mode === QUERY_MODE.AGENT ? "Investigating" : "Thinking";
  return (
    <div className="flex items-center gap-2 self-start rounded-xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-500 shadow-sm">
      <span className="flex gap-1">
        <span className="attest-dot h-1.5 w-1.5 rounded-full bg-slate-400" style={{ animationDelay: "0ms" }} />
        <span className="attest-dot h-1.5 w-1.5 rounded-full bg-slate-400" style={{ animationDelay: "150ms" }} />
        <span className="attest-dot h-1.5 w-1.5 rounded-full bg-slate-400" style={{ animationDelay: "300ms" }} />
      </span>
      {label}&hellip;
      {mode === QUERY_MODE.AGENT && (
        <span className="text-xs text-slate-400">(multi-step: searching, reading evidence)</span>
      )}
    </div>
  );
}

export default function QueryScreen({ onSelectCitation }) {
  const { messages, pending, submitQuestion } = useQuery();
  const [input, setInput] = useState("");
  const [routingMode, setRoutingMode] = useState("auto");

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!input.trim() || pending) return;
    submitQuestion(input, routingMode);
    setInput("");
  };

  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col">
      <div className="mb-4">
        <h1 className="text-xl font-semibold text-slate-900">Ask Attest</h1>
        <p className="mt-1 text-sm text-slate-500">
          Questions are answered from your ingested sources, with citations back to the
          evidence.
        </p>
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-2 text-xs">
        <span className="font-medium text-slate-500">Routing:</span>
        {["auto", "fast", "agent"].map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => setRoutingMode(m)}
            className={`rounded-full px-3 py-1 font-medium ring-1 ring-inset ${
              routingMode === m
                ? "bg-slate-900 text-white ring-slate-900"
                : "bg-white text-slate-600 ring-slate-300 hover:bg-slate-50"
            }`}
          >
            {m === "auto" ? "Auto (heuristic)" : m === "fast" ? "Force fast" : "Force agent"}
          </button>
        ))}
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto pb-4">
        {messages.length === 0 && !pending && (
          <div className="rounded-xl border border-dashed border-slate-300 p-6">
            <p className="text-sm text-slate-500">Try one of these:</p>
            <div className="mt-2 flex flex-wrap gap-2">
              {SAMPLE_QUESTIONS.map((q) => (
                <button
                  key={q}
                  type="button"
                  onClick={() => setInput(q)}
                  className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 hover:border-sky-300 hover:text-sky-700"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m) =>
          m.role === "question" ? (
            <div key={m.id} className="flex justify-end">
              <div className="max-w-[80%] rounded-xl bg-sky-600 px-4 py-2.5 text-sm text-white shadow-sm">
                {m.text}
              </div>
            </div>
          ) : (
            <div key={m.id} className="flex justify-start">
              <div className="w-full max-w-[85%]">
                <AnswerCard result={m.result} onCiteClick={onSelectCitation} />
              </div>
            </div>
          ),
        )}

        {pending && (
          <div className="flex justify-start">
            <ThinkingIndicator mode={pending.mode} />
          </div>
        )}
      </div>

      <form onSubmit={handleSubmit} className="flex gap-2 border-t border-slate-200 pt-4">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask a question about your sources..."
          className="flex-1 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 shadow-sm focus:border-sky-400 focus:outline-none focus:ring-2 focus:ring-sky-200"
        />
        <button
          type="submit"
          disabled={!input.trim() || !!pending}
          className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          Ask
        </button>
      </form>
    </div>
  );
}
