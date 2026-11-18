// Client-side mirror of `attest.query.classifier.HeuristicQueryClassifier`
// (backend/src/attest/query/classifier.py), reimplemented purely for demo
// realism in QueryScreen's "Auto" routing mode -- this is NOT the real
// classifier and never talks to the backend. Pattern list intentionally
// kept in sync with the backend's `_AGENT_PATTERNS` so the demo genuinely
// mirrors when the real app would pick agent mode (comparisons, history
// over time, causal "why", impact/relationship tracing, exhaustive
// enumeration, explicit cross-document breadth).
import { QUERY_MODE } from "../constants/queryMode";

const AGENT_PATTERNS = [
  /\bcompare\b/i,
  /\bcomparison\b/i,
  /\bdifference between\b/i,
  /\bdifferences between\b/i,
  /\bversus\b/i,
  /\bvs\.?\b/i,
  /\bwhich (?:is|are) (?:better|worse|more|less|preferred|recommended)\b/i,
  /\bhistory of\b/i,
  /\bevolv(?:ed|ing|e)\b/i,
  /\bchanged? over time\b/i,
  /\bover the years\b/i,
  /\btimeline of\b/i,
  /\bwhy did\b/i,
  /\bwhy does\b/i,
  /\bwhy is\b/i,
  /\bwhat led to\b/i,
  /\bwhat caused\b/i,
  /\broot cause\b/i,
  /\bimpact of\b/i,
  /\beffect(?:s)? of\b/i,
  /\bhow does .+ affect\b/i,
  /\brelationship between\b/i,
  /\btrace\b/i,
  /\bdownstream of\b/i,
  /\ball instances of\b/i,
  /\bevery time\b/i,
  /\beach place where\b/i,
  /\blist all\b/i,
  /\bacross\b/i,
  /\bthroughout\b/i,
  /\bin every document\b/i,
  /\bwhich sources\b/i,
];

export function classifyQueryMode(question) {
  if (AGENT_PATTERNS.some((re) => re.test(question))) {
    return QUERY_MODE.AGENT;
  }
  return QUERY_MODE.FAST;
}
