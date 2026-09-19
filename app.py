#!/usr/bin/env python3
"""
app.py — GroupCreativity Toolkit, combined standalone app.

No command line required: pick a folder containing your group session
folders, then click "Review Ideas" for each group. This is a GUI shell
around the same logic as batch_process.py + review_ideas.py — it does not
reimplement transcript selection, phase detection, or idea review.

Output per group: <transcript>_ideas.csv with columns
time, speaker, phase, content (content = the reviewed idea text).
"""

import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import batch_process as bp
import process_captions as pc
import review_ideas as ri


class GroupResult:
    def __init__(self, folder: Path):
        self.folder = folder
        self.csv_path = None
        self.idea_row_count = 0
        self.warning = None
        self.error = None


def process_group_folder(folder: Path, args) -> GroupResult:
    result = GroupResult(folder)
    candidates = bp.find_transcript_candidates(folder)
    if not candidates:
        result.error = "No transcript-format .txt file found in this folder."
        return result

    scored = bp.score_candidates(candidates)
    best = scored[0]
    if best["score"] < 2:
        result.warning = f"Best transcript only scored {best['score']}/3 — check the results carefully."
    elif best["score"] == 2:
        result.warning = "Best transcript scored 2/3 (not fully confirmed complete)."

    second_best = scored[1] if len(scored) > 1 else None
    rows, warnings, dropped = bp.process_best(best, folder, args)
    output_path = best["path"].with_suffix(".csv")
    bp.write_log(folder, best, second_best, scored, warnings, dropped, len(rows), output_path)

    result.csv_path = output_path
    result.idea_row_count = len(ri.load_idea_rows(output_path))
    return result


class _Args:
    """Mimics batch_process.py's argparse Namespace with defaults."""
    keep_fillers = False
    phases = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GroupCreativity Toolkit")
        self.geometry("640x480")
        self.results = []
        self._show_folder_picker()

    # ------------------------------------------------------------------

    def _clear(self):
        for child in self.winfo_children():
            child.destroy()

    def _show_folder_picker(self):
        self._clear()
        frame = tk.Frame(self)
        frame.pack(fill="both", expand=True, padx=20, pady=20)

        tk.Label(frame, text="GroupCreativity Toolkit", font=("Segoe UI", 16, "bold")).pack(pady=(20, 10))
        tk.Label(
            frame,
            text="Choose the folder that contains your group session folders\n"
                 "(e.g. a folder containing G15, G18, ...).",
            justify="center",
        ).pack(pady=(0, 20))
        tk.Button(
            frame, text="Choose Sessions Folder...", font=("Segoe UI", 11),
            command=self._pick_folder,
        ).pack()

    def _pick_folder(self):
        chosen = filedialog.askdirectory(title="Choose sessions folder")
        if not chosen:
            return
        self._process_folder(Path(chosen))

    def _process_folder(self, parent: Path):
        subfolders = sorted(
            p for p in parent.iterdir()
            if p.is_dir() and not p.name.startswith((".", "__"))
        )
        if not subfolders:
            messagebox.showinfo("No group folders found", f"No subfolders were found in:\n{parent}")
            return

        args = _Args()
        self.results = []
        for folder in subfolders:
            try:
                self.results.append(process_group_folder(folder, args))
            except Exception as exc:
                r = GroupResult(folder)
                r.error = str(exc)
                self.results.append(r)

        self._show_group_list(parent)

    def _show_group_list(self, parent: Path):
        self._clear()
        frame = tk.Frame(self)
        frame.pack(fill="both", expand=True, padx=20, pady=20)

        tk.Label(frame, text=f"Sessions folder: {parent}", font=("Segoe UI", 9), fg="#555").pack(anchor="w")
        tk.Label(frame, text="Groups found", font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(10, 10))

        list_frame = tk.Frame(frame)
        list_frame.pack(fill="both", expand=True)

        for result in self.results:
            row = tk.Frame(list_frame, pady=4)
            row.pack(fill="x")

            if result.error:
                status = f"{result.folder.name} — {result.error}"
                color = "#a94442"
            elif result.warning:
                status = f"{result.folder.name} — {result.idea_row_count} idea rows — ⚠ {result.warning}"
                color = "#8a6d3b"
            else:
                status = f"{result.folder.name} — {result.idea_row_count} idea rows — ready"
                color = "#3c763d"

            tk.Label(row, text=status, fg=color, anchor="w", justify="left", wraplength=420).pack(
                side="left", fill="x", expand=True
            )
            if result.csv_path is not None:
                tk.Button(
                    row, text="Review Ideas",
                    command=lambda r=result: self._open_review(r),
                ).pack(side="right")

        tk.Button(frame, text="Choose a Different Folder...", command=self._show_folder_picker).pack(pady=(20, 0))

    def _open_review(self, result: GroupResult):
        top = tk.Toplevel(self)
        top.title(f"Review Ideas — {result.folder.name}")
        top.geometry("760x620")
        ri.ReviewFrame(top, result.csv_path, simplified=True)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
