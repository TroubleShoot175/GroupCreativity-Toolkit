#!/usr/bin/env python3
"""
review_core.py — UI-independent idea-review logic.

Shared by the Tkinter review window (review_ideas.py) and the LAN web server
(server.py). Nothing here imports tkinter or touches the network; a
ReviewSession is driven by calling its action methods and rendering
snapshot().
"""

import csv
import json
import re
from pathlib import Path

IDEA_PHASES = {"ideaGenerationOne", "ideaGenerationTwo"}
_PARTICIPANT_RE = re.compile(r"(P\d+)\s*$", re.IGNORECASE)


def load_idea_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("phase") in IDEA_PHASES]


def parse_participant(speaker: str) -> str:
    m = _PARTICIPANT_RE.search(speaker or "")
    return m.group(1).upper() if m else ""


class ReviewState:
    """Tracks per-row decisions and persists them to a sidecar JSON file."""

    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.decisions: dict[str, dict] = {}
        if state_path.exists():
            self.decisions = json.loads(state_path.read_text(encoding="utf-8")).get("decisions", {})
            for entry in self.decisions.values():
                if entry.get("action") == "kept" and "idea_texts" not in entry:
                    entry["idea_texts"] = [entry.pop("idea_text")]

    def save(self):
        self.state_path.write_text(
            json.dumps({"decisions": self.decisions}, indent=2), encoding="utf-8"
        )

    def is_decided(self, index: int) -> bool:
        return str(index) in self.decisions

    def primary_order(self) -> list[int]:
        primaries = [
            int(k) for k, v in self.decisions.items() if v["action"] in ("kept", "discarded")
        ]
        return sorted(primaries)

    def record_kept(self, indices: list[int], idea_texts: list[str]):
        primary = indices[0]
        self.decisions[str(primary)] = {
            "action": "kept",
            "idea_texts": idea_texts,
            "merged_indices": indices,
        }
        for idx in indices[1:]:
            self.decisions[str(idx)] = {"action": "folded"}
        self.save()

    def record_discarded(self, indices: list[int]):
        primary = indices[0]
        self.decisions[str(primary)] = {"action": "discarded", "merged_indices": indices}
        for idx in indices[1:]:
            self.decisions[str(idx)] = {"action": "folded"}
        self.save()

    def undo(self, primary_index: int):
        entry = self.decisions.pop(str(primary_index), None)
        if entry:
            for idx in entry.get("merged_indices", [primary_index])[1:]:
                self.decisions.pop(str(idx), None)
        self.save()
        return entry

    def first_undecided(self, total: int) -> int:
        for i in range(total):
            if not self.is_decided(i):
                return i
        return total


def write_output_csv(output_path: Path, rows: list[dict], state: ReviewState, simplified: bool = False):
    """Write kept ideas. `simplified=True` emits just time/speaker/phase/content
    (content = idea text) for the standalone non-technical app; the default
    (`simplified=False`) emits the full group/participant/content/idea_text
    format used by the CLI tool."""
    if simplified:
        fields = ["time", "speaker", "phase", "content"]
    else:
        fields = ["group", "participant", "speaker", "time", "phase", "content", "idea_text"]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for key in sorted(state.decisions, key=int):
            entry = state.decisions[key]
            if entry["action"] != "kept":
                continue
            merged = entry["merged_indices"]
            first_row = rows[merged[0]]
            content = " ".join(rows[i]["content"] for i in merged)
            for idea_text in entry["idea_texts"]:
                if simplified:
                    writer.writerow({
                        "time": first_row["time"],
                        "speaker": first_row["speaker"],
                        "phase": first_row["phase"],
                        "content": idea_text,
                    })
                else:
                    writer.writerow({
                        "group": first_row.get("group", ""),
                        "participant": first_row.get("participant", ""),
                        "speaker": first_row["speaker"],
                        "time": first_row["time"],
                        "phase": first_row["phase"],
                        "content": content,
                        "idea_text": idea_text,
                    })


class ReviewSession:
    """One group's review, independent of any UI.

    Actions mutate the session (and persist decisions/CSV as the desktop
    tool always has); snapshot() returns a plain dict describing what a UI
    should currently show.
    """

    def __init__(self, csv_path, simplified: bool = False):
        self.csv_path = Path(csv_path)
        self.simplified = simplified
        self.group = self.csv_path.resolve().parent.name

        self.rows = load_idea_rows(self.csv_path)
        for r in self.rows:
            r["group"] = self.group
            r["participant"] = parse_participant(r.get("speaker", ""))

        self.state_path = self.csv_path.with_name(self.csv_path.stem + "_review_state.json")
        self.output_path = self.csv_path.with_name(self.csv_path.stem + "_ideas.csv")
        self.state = ReviewState(self.state_path)

        self.current = self.state.first_undecided(len(self.rows))
        self.pending_merge = [self.current] if self.current < len(self.rows) else []
        self._reset_fragments()

        # Bumped by every mutating action. A remote UI echoes it back with
        # each action so a stale view (second tab, double-submit) is rejected
        # instead of applying text to the wrong row.
        self.revision = 0

    # ------------------------------------------------------------------ state

    @property
    def done(self) -> bool:
        return self.current >= len(self.rows)

    def decided_count(self) -> int:
        """Rows already decided (kept, discarded, or folded into another)."""
        return sum(1 for i in range(len(self.rows)) if self.state.is_decided(i))

    def _combined_content(self) -> str:
        return " ".join(self.rows[i]["content"] for i in self.pending_merge)

    def _reset_fragments(self):
        """(Re)start fragment review for the current pending_merge row group."""
        self.fragment_queue = [self._combined_content()] if self.pending_merge else []
        self.finalized_idea_texts = []

    def _advance(self):
        self.current = self.pending_merge[-1] + 1 if self.pending_merge else self.current + 1
        self.pending_merge = [self.current] if self.current < len(self.rows) else []
        self._reset_fragments()
        self._write_output()

    def _write_output(self):
        write_output_csv(self.output_path, self.rows, self.state, simplified=self.simplified)

    def _consume_fragment(self, idea_text):
        """Finalize (or drop, if idea_text is None) the fragment currently shown."""
        if idea_text is not None:
            self.finalized_idea_texts.append(idea_text)
        if self.fragment_queue:
            self.fragment_queue.pop(0)

        if self.fragment_queue:
            return

        if self.finalized_idea_texts:
            self.state.record_kept(list(self.pending_merge), list(self.finalized_idea_texts))
        else:
            self.state.record_discarded(list(self.pending_merge))
        self._advance()

    # ---------------------------------------------------------------- actions

    def save_next(self, text: str):
        """Confirm `text` as an idea and move on. Empty text is an error —
        callers that want to drop an empty fragment use drop_fragment()."""
        if self.done:
            return
        text = (text or "").strip()
        if not text:
            raise ValueError("Idea text is empty.")
        self.revision += 1
        self._consume_fragment(text)

    def drop_fragment(self):
        """Drop the fragment currently shown without keeping it."""
        if self.done:
            return
        self.revision += 1
        self._consume_fragment(None)

    def discard(self):
        """Discard the whole current row group."""
        if self.done:
            return
        self.revision += 1
        self.state.record_discarded(list(self.pending_merge))
        self._advance()

    def combine_next(self) -> bool:
        """Fold the next transcript row into the current group. Returns
        False if there is no next row."""
        if self.done:
            return False
        next_idx = self.pending_merge[-1] + 1
        if next_idx >= len(self.rows):
            return False
        self.revision += 1
        self.pending_merge.append(next_idx)
        self._reset_fragments()
        return True

    def split(self, before: str, after: str):
        """Split the fragment currently shown into two, each reviewed in turn."""
        if self.done:
            return
        before = (before or "").strip()
        after = (after or "").strip()
        if not before or not after:
            raise ValueError("Both sides of the split need text.")
        self.revision += 1
        self.fragment_queue[0:1] = [before, after]

    def back(self) -> bool:
        """Undo the previous decision and revisit it. Returns False if there
        is nothing to undo."""
        primaries = self.state.primary_order()
        if not primaries:
            return False
        self.revision += 1
        last_primary = primaries[-1]
        entry = self.state.undo(last_primary)
        self.current = last_primary
        self.pending_merge = entry.get("merged_indices", [last_primary]) if entry else [last_primary]
        self._reset_fragments()
        self._write_output()
        return True

    # --------------------------------------------------------------- snapshot

    def snapshot(self) -> dict:
        total = len(self.rows)
        can_back = bool(self.state.primary_order())

        if self.done:
            return {
                "done": True,
                "index": total,
                "total": total,
                "phase": "",
                "speaker": "",
                "time": "",
                "progress_text": f"Done — {total} / {total}",
                "meta_text": "",
                "original_text": "All rows reviewed.",
                "edit_text": "",
                "next": {"kind": "none", "meta": "(no more rows)", "text": ""},
                "can_back": can_back,
                "output_name": self.output_path.name,
                "revision": self.revision,
            }

        row = self.rows[self.current]
        meta = f"{row['phase']}  |  {row['speaker']}  |  {row['time']}"
        combined = self._combined_content()

        if len(self.fragment_queue) > 1:
            nxt = {
                "kind": "split_half",
                "meta": f"(second half of split)  |  {meta}",
                "text": self.fragment_queue[1],
            }
        else:
            next_idx = self.pending_merge[-1] + 1
            if next_idx < total:
                nrow = self.rows[next_idx]
                nxt = {
                    "kind": "row",
                    "meta": f"{nrow['phase']}  |  {nrow['speaker']}  |  {nrow['time']}",
                    "text": nrow["content"],
                }
            else:
                nxt = {"kind": "none", "meta": "(no more rows)", "text": ""}

        return {
            "done": False,
            "index": self.current + 1,
            "total": total,
            "phase": row["phase"],
            "speaker": row["speaker"],
            "time": row["time"],
            "progress_text": f"Row {self.current + 1} / {total}",
            "meta_text": meta,
            "original_text": combined,
            "edit_text": self.fragment_queue[0] if self.fragment_queue else combined,
            "next": nxt,
            "can_back": can_back,
            "output_name": self.output_path.name,
            "revision": self.revision,
        }
