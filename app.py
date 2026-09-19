#!/usr/bin/env python3
"""
app.py — GroupCreativity Toolkit, combined standalone app.

No command line required: pick a folder containing your group session
folders, then either click "Review Ideas" for each group on this computer, or
start the review server so reviewers on the same network can do it from their
own browsers. This is a GUI shell around the same logic as batch_process.py +
review_ideas.py + server.py — it does not reimplement transcript selection,
phase detection, or idea review.

Output per group: <transcript>_ideas.csv with columns
time, speaker, phase, content (content = the reviewed idea text).
"""

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import batch_process as bp
import review_ideas as ri
import server


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


LOCAL_LABEL = "the host computer"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GroupCreativity Toolkit")
        self.geometry("720x680")
        self.results = []
        self.registry = None
        self.server = None
        self._local_reviews = {}     # group name -> open review Toplevel
        self._poll_id = None
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._show_folder_picker()

    # ------------------------------------------------------------------

    def _clear(self):
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        # Only remove this window's own frames — never an open review window.
        for child in self.winfo_children():
            if isinstance(child, tk.Frame):
                child.destroy()

    def _on_close(self):
        self._stop_server()
        self.destroy()

    def _show_folder_picker(self):
        if self._local_reviews:
            messagebox.showinfo(
                "Review windows are open",
                "Close your open review windows before choosing a different folder.",
            )
            return
        self._stop_server()
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

        self.registry = server.GroupRegistry(
            {r.folder.name: r.csv_path for r in self.results if r.csv_path is not None}
        )
        self._show_group_list(parent)

    # ------------------------------------------------------------------ groups

    def _show_group_list(self, parent: Path):
        self._clear()
        frame = tk.Frame(self)
        frame.pack(fill="both", expand=True, padx=20, pady=20)

        tk.Label(frame, text=f"Sessions folder: {parent}", font=("Segoe UI", 9), fg="#555").pack(anchor="w")
        tk.Label(frame, text="Groups found", font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(10, 10))

        list_frame = tk.Frame(frame)
        list_frame.pack(fill="x")

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

            tk.Label(row, text=status, fg=color, anchor="w", justify="left", wraplength=470).pack(
                side="left", fill="x", expand=True
            )
            if result.csv_path is not None:
                tk.Button(
                    row, text="Review Ideas",
                    command=lambda r=result: self._open_review(r),
                ).pack(side="right")

        self._build_server_panel(frame)

        tk.Button(frame, text="Choose a Different Folder...", command=self._show_folder_picker).pack(
            side="bottom", pady=(20, 0)
        )

    def _open_review(self, result: GroupResult):
        name = result.folder.name
        existing = self._local_reviews.get(name)
        if existing is not None:
            existing.lift()
            existing.focus_force()
            return

        ok, holder = self.registry.acquire(name, server.LOCAL_OWNER, LOCAL_LABEL, sticky=True)
        if not ok:
            messagebox.showinfo(
                "Group in use",
                f"{name} is currently being reviewed by {holder} over the network.\n"
                "Try again when they're finished, or pick another group.",
            )
            return

        top = tk.Toplevel(self)
        top.title(f"Review Ideas — {name}")
        top.geometry("760x620")
        self._local_reviews[name] = top

        def on_destroy(event, top=top, name=name):
            if event.widget is top:      # ignore <Destroy> from child widgets
                self._local_reviews.pop(name, None)
                self.registry.release(name, server.LOCAL_OWNER)

        top.bind("<Destroy>", on_destroy)
        ri.ReviewFrame(top, result.csv_path, simplified=True)

    # ------------------------------------------------------------ review server

    def _build_server_panel(self, parent):
        panel = tk.LabelFrame(parent, text="Review from other computers on this network")
        panel.pack(fill="x", pady=(16, 0))
        self._server_panel = panel
        self._render_server_panel()

    def _render_server_panel(self):
        panel = self._server_panel
        for child in panel.winfo_children():
            child.destroy()
        running = self.server is not None and self.server.running

        if not running:
            tk.Label(
                panel, justify="left", wraplength=620, anchor="w",
                text="Let reviewers on your Wi-Fi or office network open the review screen in their own "
                     "web browser. Each group can be reviewed by one person at a time.",
            ).pack(fill="x", padx=10, pady=(8, 4))
            tk.Button(panel, text="Start Review Server", command=self._start_server).pack(
                anchor="w", padx=10, pady=(4, 10)
            )
            return

        tk.Label(panel, anchor="w", text="Reviewers: open one of these addresses in a web browser").pack(
            fill="x", padx=10, pady=(8, 2)
        )
        for url in self.server.urls():
            row = tk.Frame(panel)
            row.pack(fill="x", padx=10)
            entry = tk.Entry(row, font=("Consolas", 11), width=32)
            entry.insert(0, url)
            entry.config(state="readonly")
            entry.pack(side="left")
            tk.Button(row, text="Copy", command=lambda u=url: self._copy(u)).pack(side="left", padx=6)

        code_row = tk.Frame(panel)
        code_row.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(code_row, text="Passcode:").pack(side="left")
        tk.Label(code_row, text=self.server.passcode, font=("Consolas", 16, "bold")).pack(side="left", padx=8)
        tk.Button(code_row, text="Copy", command=lambda: self._copy(self.server.passcode)).pack(side="left")

        self._activity_label = tk.Label(panel, anchor="w", justify="left", fg="#555", wraplength=620)
        self._activity_label.pack(fill="x", padx=10, pady=(8, 0))

        tk.Label(
            panel, anchor="w", justify="left", fg="#555", wraplength=620,
            text="If Windows asks whether to allow this app on the network, choose “Private networks”. "
                 "The connection is not encrypted, so only use this on a network you trust.",
        ).pack(fill="x", padx=10, pady=(4, 0))
        tk.Button(panel, text="Stop Server", command=self._stop_server_clicked).pack(
            anchor="w", padx=10, pady=(6, 10)
        )
        self._poll_activity()

    def _poll_activity(self):
        if self.server is None or not self.server.running:
            return
        holders = self.registry.holders()
        reviewers = self.server.reviewer_labels()
        if holders:
            in_use = "; ".join(f"{name} — {label}" for name, label in holders)
            text = f"In use now: {in_use}"
        elif reviewers:
            text = f"{len(reviewers)} reviewer(s) connected, no group open."
        else:
            text = "Waiting for reviewers to connect."
        self._activity_label.config(text=text)
        self._poll_id = self.after(2000, self._poll_activity)

    def _copy(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)

    def _start_server(self):
        if self.registry is None:
            return
        self.server = server.ReviewServerHandle(self.registry)
        try:
            self.server.start()
        except OSError as exc:
            self.server = None
            messagebox.showerror("Couldn't start the review server", str(exc))
            return
        self._render_server_panel()

    def _stop_server_clicked(self):
        self._stop_server()
        if hasattr(self, "_server_panel") and self._server_panel.winfo_exists():
            self._render_server_panel()

    def _stop_server(self):
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        if self.server is not None:
            self.server.stop()
            self.server = None


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
