#!/usr/bin/env python3
"""
review_ideas.py — Line-by-line GUI review of a process_captions.py CSV,
trimming each idea-generation transcript row down to just the idea (or
discarding rows that aren't ideas at all), producing a clean list ready
for a Qualtrics idea-rating survey.

Usage:
    python review_ideas.py path/to/transcript.csv

Only rows whose phase is ideaGenerationOne or ideaGenerationTwo are shown.

Controls:
    Enter            Save & Next (accepts the edited text as the idea)
    Shift+Enter      Insert a newline in the edit box instead of saving
    Ctrl+D / Discard button   Drop this row — it will not appear in the output
    Combine with next →       Fold the next row's content into this one
    Split at cursor ✂         Turn the current text into two ideas at the cursor. The
                              text before the cursor loads first for you to trim/confirm
                              (Save & Next), then the text after the cursor loads the
                              same way — each gets its own edit step, and each can be
                              split/discarded again. Both resulting ideas keep the same
                              time/speaker/phase/content.
    Back             Undo the previous decision and revisit it

Output:
    <csv_stem>_ideas.csv          — kept ideas only
    <csv_stem>_review_state.json  — decisions, so the session can resume
"""

import argparse
import csv
import json
import re
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

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


def write_output_csv(output_path: Path, rows: list[dict], state: ReviewState):
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
                writer.writerow({
                    "group": first_row.get("group", ""),
                    "participant": first_row.get("participant", ""),
                    "speaker": first_row["speaker"],
                    "time": first_row["time"],
                    "phase": first_row["phase"],
                    "content": content,
                    "idea_text": idea_text,
                })


class ReviewApp(tk.Tk):
    def __init__(self, csv_path: Path):
        super().__init__()
        self.title(f"Idea Review — {csv_path.name}")
        self.geometry("760x620")

        self.csv_path = csv_path
        self.group = csv_path.resolve().parent.name
        self.rows = load_idea_rows(csv_path)
        for r in self.rows:
            r["group"] = self.group
            r["participant"] = parse_participant(r.get("speaker", ""))

        if not self.rows:
            messagebox.showinfo("No idea rows", "No ideaGenerationOne/Two rows found in this CSV.")
            self.destroy()
            return

        self.state_path = csv_path.with_name(csv_path.stem + "_review_state.json")
        self.output_path = csv_path.with_name(csv_path.stem + "_ideas.csv")
        self.state = ReviewState(self.state_path)

        self.current = self.state.first_undecided(len(self.rows))
        self.pending_merge = [self.current] if self.current < len(self.rows) else []
        self._reset_fragments()

        self._build_ui()
        self._render()

    # ------------------------------------------------------------------

    def _build_ui(self):
        top = tk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 0))
        self.progress_label = tk.Label(top, font=("Segoe UI", 10, "bold"))
        self.progress_label.pack(side="left")
        self.meta_label = tk.Label(top, fg="#555")
        self.meta_label.pack(side="right")

        original_frame = tk.LabelFrame(self, text="Original transcript content")
        original_frame.pack(fill="x", padx=10, pady=(10, 0))
        self.original_text = tk.Text(original_frame, height=4, wrap="word", state="disabled", bg="#f0f0f0")
        self.original_text.pack(fill="x", padx=5, pady=5)

        edit_frame = tk.LabelFrame(
            self, text="✏  EDIT HERE — idea text (trim down to just the idea)",
            font=("Segoe UI", 9, "bold"), fg="#b8860b",
        )
        edit_frame.pack(fill="x", padx=10, pady=(10, 0))
        self.edit_text = tk.Text(
            edit_frame, height=6, wrap="word", bg="#fffef0",
            highlightthickness=2, highlightbackground="#f0ad4e", highlightcolor="#f0ad4e",
        )
        self.edit_text.pack(fill="both", expand=True, padx=5, pady=5)
        self.edit_text.bind("<Return>", self._on_return)
        self.edit_text.bind("<Shift-Return>", lambda e: None)
        self.edit_text.bind("<Control-d>", lambda e: self._discard())

        self.next_frame = tk.LabelFrame(self, text="Next row (preview)")
        self.next_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.next_meta_label = tk.Label(self.next_frame, fg="#555", anchor="w")
        self.next_meta_label.pack(fill="x", padx=5, pady=(5, 0))
        self.next_text = tk.Text(self.next_frame, height=4, wrap="word", state="disabled", bg="#f0f0f0")
        self.next_text.pack(fill="both", expand=True, padx=5, pady=5)

        btn_frame = tk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        tk.Button(btn_frame, text="Back", command=self._back).pack(side="left")
        tk.Button(btn_frame, text="Combine with next →", command=self._combine_next).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Split at cursor ✂", command=self._split_at_cursor).pack(side="left")
        tk.Button(btn_frame, text="Discard", command=self._discard).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Save & Next", command=self._save_next).pack(side="right")

    def _on_return(self, event):
        self._save_next()
        return "break"

    # ------------------------------------------------------------------

    def _reset_fragments(self):
        """(Re)start fragment review for the current pending_merge row group."""
        if self.pending_merge:
            combined = " ".join(self.rows[i]["content"] for i in self.pending_merge)
            self.fragment_queue = [combined]
        else:
            self.fragment_queue = []
        self.finalized_idea_texts = []

    def _consume_fragment(self, idea_text):
        """Finalize (or drop, if idea_text is None) the fragment currently shown."""
        if idea_text is not None:
            self.finalized_idea_texts.append(idea_text)
        if self.fragment_queue:
            self.fragment_queue.pop(0)

        if self.fragment_queue:
            self._render()
            return

        if self.finalized_idea_texts:
            self.state.record_kept(list(self.pending_merge), list(self.finalized_idea_texts))
        else:
            self.state.record_discarded(list(self.pending_merge))
        self._advance()

    def _render(self):
        if self.current >= len(self.rows):
            self.progress_label.config(text=f"Done — {len(self.rows)} / {len(self.rows)}")
            self.meta_label.config(text="")
            self.original_text.config(state="normal")
            self.original_text.delete("1.0", "end")
            self.original_text.insert("1.0", "All rows reviewed.")
            self.original_text.config(state="disabled")
            self.edit_text.delete("1.0", "end")
            self._render_next_preview(None)
            return

        row = self.rows[self.current]
        self.progress_label.config(text=f"Row {self.current + 1} / {len(self.rows)}")
        self.meta_label.config(text=f"{row['phase']}  |  {row['speaker']}  |  {row['time']}")

        combined_content = " ".join(self.rows[i]["content"] for i in self.pending_merge)
        self.original_text.config(state="normal")
        self.original_text.delete("1.0", "end")
        self.original_text.insert("1.0", combined_content)
        self.original_text.config(state="disabled")

        fragment_text = self.fragment_queue[0] if self.fragment_queue else combined_content
        self.edit_text.delete("1.0", "end")
        self.edit_text.insert("1.0", fragment_text)
        self.edit_text.focus_set()

        if len(self.fragment_queue) > 1:
            self._render_next_fragment_preview(self.fragment_queue[1], row)
        else:
            next_idx = self.pending_merge[-1] + 1
            self._render_next_preview(next_idx if next_idx < len(self.rows) else None)

    def _render_next_preview(self, next_idx):
        self.next_text.config(state="normal")
        self.next_text.delete("1.0", "end")
        if next_idx is None:
            self.next_meta_label.config(text="(no more rows)")
        else:
            next_row = self.rows[next_idx]
            self.next_meta_label.config(
                text=f"{next_row['phase']}  |  {next_row['speaker']}  |  {next_row['time']}"
            )
            self.next_text.insert("1.0", next_row["content"])
        self.next_text.config(state="disabled")

    def _render_next_fragment_preview(self, fragment_text, row):
        self.next_text.config(state="normal")
        self.next_text.delete("1.0", "end")
        self.next_meta_label.config(
            text=f"(second half of split)  |  {row['phase']}  |  {row['speaker']}  |  {row['time']}"
        )
        self.next_text.insert("1.0", fragment_text)
        self.next_text.config(state="disabled")

    def _advance(self):
        self.current = self.pending_merge[-1] + 1 if self.pending_merge else self.current + 1
        self.pending_merge = [self.current] if self.current < len(self.rows) else []
        self._reset_fragments()
        write_output_csv(self.output_path, self.rows, self.state)
        self._render()

    def _save_next(self):
        idea_text = self.edit_text.get("1.0", "end").strip()
        if not idea_text:
            if not messagebox.askyesno("Empty idea", "Idea text is empty — drop this fragment?"):
                return
            self._consume_fragment(None)
            return
        self._consume_fragment(idea_text)

    def _discard(self):
        self.state.record_discarded(list(self.pending_merge))
        self._advance()

    def _combine_next(self):
        next_idx = self.pending_merge[-1] + 1
        if next_idx >= len(self.rows):
            return
        self.pending_merge.append(next_idx)
        self._reset_fragments()
        self._render()

    def _split_at_cursor(self):
        before = self.edit_text.get("1.0", "insert").strip()
        after = self.edit_text.get("insert", "end-1c").strip()
        if not before or not after:
            messagebox.showinfo(
                "Can't split there",
                "Place the cursor between the two ideas (with text on both sides) before splitting.",
            )
            return
        self.fragment_queue[0:1] = [before, after]
        self._render()

    def _back(self):
        primaries = self.state.primary_order()
        if not primaries:
            return
        last_primary = primaries[-1]
        entry = self.state.undo(last_primary)
        self.current = last_primary
        self.pending_merge = entry.get("merged_indices", [last_primary]) if entry else [last_primary]
        self._reset_fragments()
        write_output_csv(self.output_path, self.rows, self.state)
        self._render()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="CSV produced by process_captions.py / batch_process.py")
    args = parser.parse_args()

    csv_path = Path(args.input)
    if not csv_path.exists():
        print(f"Error: file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    app = ReviewApp(csv_path)
    app.mainloop()


if __name__ == "__main__":
    main()
