import { useCallback, useRef, useState } from "react";
import { pickMockResponse } from "../mock/queryResponses";
import { classifyQueryMode } from "../lib/classifyQuery";
import { QUERY_MODE } from "../constants/queryMode";

let messageCounter = 0;
function nextMessageId() {
  messageCounter += 1;
  return `msg_${messageCounter}`;
}

/**
 * Local mock-state for the ask/answer chat flow. Submitting a question:
 *  1. appends a "question" message immediately,
 *  2. sets `pending` (question + resolved mode) so the screen can show a
 *     "thinking..." (fast) or "investigating..." (agent) indicator,
 *  3. after a simulated delay (longer for agent mode, mirroring the real
 *     multi-tool-call investigation loop being slower than a single-pass
 *     retrieval), appends an "answer" message built from
 *     `mock/queryResponses.pickMockResponse`.
 *
 * `routingMode` is "auto" | "fast" | "agent": "auto" runs the question
 * through `classifyQueryMode` (a client-side mirror of the backend's
 * HeuristicQueryClassifier) same as the real service would; "fast"/"agent"
 * force that mode regardless of phrasing, so a demo can reliably show both
 * loading states without having to phrase a question just right.
 */
export function useQuery() {
  const [messages, setMessages] = useState([]);
  const [pending, setPending] = useState(null);
  const timeoutRef = useRef(null);

  const submitQuestion = useCallback((questionText, routingMode = "auto") => {
    const question = questionText.trim();
    if (!question) return;

    const mode =
      routingMode === "auto" ? classifyQueryMode(question) : routingMode === "agent" ? QUERY_MODE.AGENT : QUERY_MODE.FAST;

    const questionMessage = {
      id: nextMessageId(),
      role: "question",
      text: question,
      createdAt: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, questionMessage]);
    setPending({ mode, question });

    const delay =
      mode === QUERY_MODE.AGENT ? 2400 + Math.random() * 1400 : 700 + Math.random() * 500;

    timeoutRef.current = window.setTimeout(() => {
      const result = pickMockResponse(question, mode);
      const answerMessage = {
        id: nextMessageId(),
        role: "answer",
        result,
        createdAt: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, answerMessage]);
      setPending(null);
    }, delay);
  }, []);

  const clearConversation = useCallback(() => {
    if (timeoutRef.current) window.clearTimeout(timeoutRef.current);
    setMessages([]);
    setPending(null);
  }, []);

  return { messages, pending, submitQuestion, clearConversation };
}
