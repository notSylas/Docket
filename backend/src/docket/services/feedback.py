"""Private, opt-in feedback and explicitly reviewed draft evaluation cases.

Captures are never training data. Reviews append an event rather than changing
the original answer. Every exported question still needs gold-set review.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

import yaml
from docket.core.writer_lock import WriterLock
from docket.core.privacy import redact_text
from docket.eval.schema import GoldSet, Question


class FeedbackError(ValueError):
    pass


class FeedbackStore:
    def __init__(self, data_dir, enabled=False, *, writer_lock=None):
        self.root = Path(data_dir).expanduser().resolve()
        self.enabled = enabled
        self.lock = writer_lock or WriterLock(self.root)

    @property
    def path(self):
        return self.root / "feedback" / "items.jsonl"

    def _append(self, event):
        try:
            with self.lock.hold("saving private feedback"):
                folder = self.path.parent
                if folder.is_symlink():
                    raise FeedbackError("The feedback folder must not be a symbolic link.")
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                folder.chmod(0o700)
                fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    # Discard only an incomplete trailing event after a crash.
                    end = os.lseek(fd, 0, os.SEEK_END)
                    if end and os.pread(fd, 1, end - 1) != b"\n":
                        cursor = end
                        while cursor:
                            start = max(0, cursor - 65536)
                            block = os.pread(fd, cursor - start, start)
                            newline = block.rfind(b"\n")
                            if newline >= 0:
                                os.ftruncate(fd, start + newline + 1)
                                break
                            cursor = start
                        else:
                            os.ftruncate(fd, 0)
                    payload = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
                    remaining = memoryview(payload)
                    while remaining:
                        n = os.write(fd, remaining)
                        if n == 0:
                            raise OSError("short write")
                        remaining = remaining[n:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError:
            raise FeedbackError("Feedback could not be saved. Check data-folder permissions and free space.") from None

    def capture(self, question, answer, citations, label, correction=""):
        if not self.enabled:
            raise FeedbackError("Feedback capture is off. Set DOCKET_FEEDBACK_ENABLED=true to opt in.")
        if label not in ("good", "not_good"):
            raise FeedbackError("Choose good or not good.")
        event = {
            "schema_version": 1, "event": "capture", "id": uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "question": question, "answer": answer, "label": label,
            "correction": " ".join(correction.splitlines())[:500],
            "citations": [{"chunk_id": c[0], "evidence_version_id": c[1] or None}
                          for c in dict.fromkeys((c["chunk_id"], c.get("evidence_version_id")) for c in citations)],
            "reviewed": False,
        }
        self._append(event)
        return event["id"]

    def items(self):
        if not self.path.exists():
            return []
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise FeedbackError("The feedback folder and records must not be symbolic links.")
        try:
            # An interrupted final append cannot corrupt earlier complete records.
            raw = self.path.read_bytes()
            lines = raw[:raw.rfind(b"\n") + 1].decode("utf-8").splitlines()
            items = {}
            for line in lines:
                event = json.loads(line)
                if event["schema_version"] != 1:
                    raise ValueError()
                if event["event"] == "capture":
                    items[event["id"]] = event
                elif event["event"] == "review" and event["id"] in items:
                    items[event["id"]].update({k: event[k] for k in ("reviewed", "answerable", "expected", "gold_spans", "reviewed_at")})
            return list(items.values())
        except (OSError, ValueError, KeyError, TypeError):
            raise FeedbackError("Private feedback records could not be read. Check the file or restore a backup.") from None

    def _item(self, item_id):
        item = next((item for item in self.items() if item["id"] == item_id), None)
        if item is None:
            raise FeedbackError("Feedback item not found. Use its full saved ID.")
        return item

    @staticmethod
    def _evidence(item, resolver):
        try:
            evidence = [resolver.resolve(c["chunk_id"]) for c in item["citations"]]
            if any(ev.evidence_version_id != citation["evidence_version_id"]
                   for ev, citation in zip(evidence, item["citations"])):
                raise ValueError()
            return evidence
        except Exception:
            raise FeedbackError("Cited evidence is unavailable or changed. Review it against current evidence first.") from None

    def review(self, item_id, resolver, *, expected="", spans=(), answerable=True):
        item = self._item(item_id)
        try:
            Question(id="draft", type="single_fact" if answerable else "out_of_corpus",
                     question=item["question"], answerable=answerable,
                     must_contain=[expected] if answerable and expected.strip() else [], gold_spans=list(spans))
        except ValueError:
            raise FeedbackError("An answerable draft needs an expected fact and a verbatim 20–60 character quote.") from None
        evidence = self._evidence(item, resolver)
        if any(not any(span in ev.text for ev in evidence) for span in spans):
            raise FeedbackError("Every gold quote must appear verbatim in a cited passage from the saved version.")
        self._append({"schema_version": 1, "event": "review", "id": item_id, "reviewed": True,
                      "reviewed_at": datetime.now(timezone.utc).isoformat(), "answerable": answerable,
                      "expected": expected if answerable else "", "gold_spans": list(spans)})

    def export_gold(self, resolver):
        questions, skipped = [], 0
        for item in self.items():
            if not item.get("reviewed"):
                continue
            try:
                evidence = self._evidence(item, resolver)
                spans = item["gold_spans"]
                if any(not any(span in ev.text for ev in evidence) for span in spans):
                    raise FeedbackError("Evidence no longer matches.")
                questions.append(Question(
                    id="feedback-" + item["id"], type="single_fact" if item["answerable"] else "out_of_corpus",
                    question=redact_text(item["question"]), answerable=item["answerable"],
                    must_contain=[redact_text(item["expected"])] if item["answerable"] else [],
                    gold_spans=[redact_text(span) for span in spans],
                    reviewed=False, split="dev", origin="DRAFT redacted derivative of private feedback; review against a sanitized corpus",
                    source_documents=sorted({redact_text(Path(ev.source_display_name).name) for ev in evidence}),
                ))
            except (FeedbackError, ValueError, KeyError):
                skipped += 1
        gold = GoldSet(population="private_feedback_draft", questions=questions)
        return ("# DRAFT — never auto-accepted or used for training\n"
                "# Redacted derivative. Recheck quotes against a sanitized corpus before acceptance.\n"
                "# Pattern masking cannot remove every kind of personal data.\n") + yaml.safe_dump(
            gold.model_dump(mode="json"), allow_unicode=True, sort_keys=False), skipped
