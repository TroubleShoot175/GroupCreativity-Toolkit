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

The review logic itself lives in review_core.py (shared with the LAN web
server, server.py); this module is just the Tkinter view over it.
"""

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

# Re-exported so existing `import review_ideas as ri` callers keep working.
from review_core import (  # noqa: F401
    IDEA_PHASES,
    ReviewSession,
    ReviewState,
    load_idea_rows,
    parse_participant,
    write_output_csv,
)


class ReviewFrame(tk.Frame):
    """The idea-review UI. Reusable inside either a standalone Tk root
    (see main()/review_ideas.py CLI) or a Toplevel embedded in a larger app
    (see app.py)."""

    def __init__(self, master, csv_path: Path, simplified: bool = False):
        super().__init__(master)
        self.session = ReviewSession(csv_path, simplified=simplified)

        if not self.session.rows:
            messagebox.showinfo("No idea rows", "No ideaGenerationOne/Two rows found in this CSV.")
            self.winfo_toplevel().destroy()
            return

        self.pack(fill="both", expand=True)
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

    @staticmethod
    def _set_readonly(widget: tk.Text, text: str):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.config(state="disabled")

    def _render(self):
        snap = self.session.snapshot()
        self.progress_label.config(text=snap["progress_text"])
        self.meta_label.config(text=snap["meta_text"])
        self._set_readonly(self.original_text, snap["original_text"])

        self.edit_text.delete("1.0", "end")
        self.edit_text.insert("1.0", snap["edit_text"])
        if not snap["done"]:
            self.edit_text.focus_set()

        self.next_meta_label.config(text=snap["next"]["meta"])
        self._set_readonly(self.next_text, snap["next"]["text"])

    # ------------------------------------------------------------------

    def _save_next(self):
        if self.session.done:
            return
        idea_text = self.edit_text.get("1.0", "end").strip()
        if not idea_text:
            if not messagebox.askyesno("Empty idea", "Idea text is empty — drop this fragment?"):
                return
            self.session.drop_fragment()
        else:
            self.session.save_next(idea_text)
        self._render()

    def _discard(self):
        if self.session.done:
            return
        self.session.discard()
        self._render()

    def _combine_next(self):
        if self.session.combine_next():
            self._render()

    def _split_at_cursor(self):
        before = self.edit_text.get("1.0", "insert").strip()
        after = self.edit_text.get("insert", "end-1c").strip()
        try:
            self.session.split(before, after)
        except ValueError:
            messagebox.showinfo(
                "Can't split there",
                "Place the cursor between the two ideas (with text on both sides) before splitting.",
            )
            return
        self._render()

    def _back(self):
        if self.session.back():
            self._render()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="CSV produced by process_captions.py / batch_process.py")
    args = parser.parse_args()

    csv_path = Path(args.input)
    if not csv_path.exists():
        print(f"Error: file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    root = tk.Tk()
    root.title(f"Idea Review — {csv_path.name}")
    root.geometry("760x620")
    ReviewFrame(root, csv_path, simplified=False)
    root.mainloop()


if __name__ == "__main__":
    main()
