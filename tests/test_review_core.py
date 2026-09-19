"""Tests for review_core.ReviewSession and the Tk view over it.

Uses synthetic data in a temp directory; never touches real session folders.
Run from the repo root:  python -m unittest discover -s tests
"""

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_core as rc  # noqa: E402

ROWS = [
    ("00:00:01", "NSF Research Team", "introduction", "Welcome everyone."),
    ("00:01:00", "NSF Research Team", "ideaGenerationOne", "Your time starts now"),
    ("00:01:10", "G99P1", "ideaGenerationOne", "We could add more parking, and also cheaper food."),
    ("00:01:20", "G99P2", "ideaGenerationOne", "Longer library hours"),
    ("00:01:30", "G99P2", "ideaGenerationOne", "so students can study late"),
    ("00:01:40", "G99P1", "ideaGenerationTwo", "Free bikes"),
    ("00:02:00", "NSF Research Team", "break", "Time is up"),
]
# indices into the idea-phase-only list
I_INTRO, I_PARKING, I_LIBRARY, I_LIBRARY2, I_BIKES = 0, 1, 2, 3, 4


def make_session_dir(root: Path, name="G99") -> Path:
    folder = root / name
    folder.mkdir()
    csv_path = folder / "transcript.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "speaker", "phase", "content"])
        w.writerows(ROWS)
    return csv_path


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class ReviewSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.csv_path = make_session_dir(Path(self._tmp.name))

    def session(self, simplified=False):
        return rc.ReviewSession(self.csv_path, simplified=simplified)

    # -- loading -----------------------------------------------------------

    def test_only_idea_generation_rows_are_loaded(self):
        s = self.session()
        self.assertEqual(len(s.rows), 5)
        self.assertEqual({r["phase"] for r in s.rows}, {"ideaGenerationOne", "ideaGenerationTwo"})

    def test_group_and_participant_derived(self):
        s = self.session()
        self.assertEqual(s.group, "G99")
        self.assertEqual(s.rows[I_PARKING]["participant"], "P1")
        self.assertEqual(s.rows[I_INTRO]["participant"], "")

    # -- basic flow / output formats ---------------------------------------

    def test_save_next_advances_and_writes_full_csv(self):
        s = self.session()
        s.save_next("Add more parking")
        self.assertEqual(s.current, 1)
        rows = read_csv(s.output_path)
        self.assertEqual(list(rows[0].keys()),
                         ["group", "participant", "speaker", "time", "phase", "content", "idea_text"])
        self.assertEqual(rows[0]["content"], "Your time starts now")
        self.assertEqual(rows[0]["idea_text"], "Add more parking")

    def test_simplified_csv_has_four_columns_with_idea_as_content(self):
        s = self.session(simplified=True)
        s.save_next("Just the idea")
        rows = read_csv(s.output_path)
        self.assertEqual(list(rows[0].keys()), ["time", "speaker", "phase", "content"])
        self.assertEqual(rows[0]["content"], "Just the idea")

    def test_empty_save_raises(self):
        s = self.session()
        with self.assertRaises(ValueError):
            s.save_next("   ")
        self.assertEqual(s.current, 0)

    # -- split -------------------------------------------------------------

    def test_split_queues_both_halves_and_saves_both_after_confirmation(self):
        s = self.session()
        s.save_next("skip intro")                    # row 0
        s.split("add more parking", "and cheaper food")
        snap = s.snapshot()
        self.assertEqual(snap["edit_text"], "add more parking")
        self.assertEqual(snap["next"]["kind"], "split_half")
        self.assertEqual(snap["next"]["text"], "and cheaper food")
        self.assertIn("second half of split", snap["next"]["meta"])

        s.save_next("add more parking [edited]")
        self.assertEqual(s.snapshot()["edit_text"], "and cheaper food")
        # nothing for this row is written until both halves are confirmed
        self.assertEqual(len(read_csv(s.output_path)), 1)

        s.save_next("cheaper food [edited]")
        rows = read_csv(s.output_path)
        self.assertEqual([r["idea_text"] for r in rows],
                         ["skip intro", "add more parking [edited]", "cheaper food [edited]"])
        self.assertEqual(rows[1]["time"], rows[2]["time"])
        self.assertEqual(rows[1]["speaker"], rows[2]["speaker"])
        self.assertEqual(rows[1]["content"], rows[2]["content"])

    def test_chained_split_of_leftover_yields_three_ideas(self):
        s = self.session()
        s.save_next("intro")
        s.split("one", "two three")
        s.save_next("one")                   # confirm first half
        s.split("two", "three")              # split the leftover
        s.save_next("two")
        s.save_next("three")
        self.assertEqual([r["idea_text"] for r in read_csv(s.output_path)],
                         ["intro", "one", "two", "three"])

    def test_split_requires_text_on_both_sides(self):
        s = self.session()
        for before, after in [("", "x"), ("x", ""), ("  ", "y")]:
            with self.assertRaises(ValueError):
                s.split(before, after)
        self.assertEqual(len(s.fragment_queue), 1)

    # -- drop / discard ----------------------------------------------------

    def test_drop_second_half_keeps_only_first(self):
        s = self.session()
        s.save_next("intro")
        s.split("keep me", "drop me")
        s.save_next("keep me")
        s.drop_fragment()
        self.assertEqual([r["idea_text"] for r in read_csv(s.output_path)], ["intro", "keep me"])

    def test_dropping_only_fragment_counts_as_discard(self):
        s = self.session()
        s.drop_fragment()
        self.assertEqual(s.current, 1)
        self.assertEqual(s.state.decisions["0"]["action"], "discarded")
        self.assertEqual(read_csv(s.output_path), [])

    def test_discard_removes_row_from_output(self):
        s = self.session()
        s.discard()
        s.save_next("parking")
        self.assertEqual([r["idea_text"] for r in read_csv(s.output_path)], ["parking"])

    # -- combine -----------------------------------------------------------

    def test_combine_merges_rows_into_one_idea(self):
        s = self.session()
        s.save_next("intro")
        s.save_next("parking")               # row 1 -> parking
        self.assertTrue(s.combine_next())    # row 2 + row 3
        snap = s.snapshot()
        self.assertEqual(snap["original_text"], "Longer library hours so students can study late")
        self.assertEqual(snap["next"]["text"], "Free bikes")
        s.save_next("Longer library hours so students can study late")
        rows = read_csv(s.output_path)
        self.assertEqual(rows[-1]["content"], "Longer library hours so students can study late")
        self.assertEqual(s.current, I_BIKES)

    def test_combine_at_last_row_returns_false(self):
        s = self.session()
        for _ in range(4):
            s.save_next("x")
        self.assertEqual(s.current, I_BIKES)
        self.assertFalse(s.combine_next())

    # -- back --------------------------------------------------------------

    def test_back_undoes_last_decision_and_rewrites_output(self):
        s = self.session()
        s.save_next("intro")
        s.save_next("parking")
        self.assertTrue(s.back())
        self.assertEqual(s.current, I_PARKING)
        self.assertEqual([r["idea_text"] for r in read_csv(s.output_path)], ["intro"])
        self.assertEqual(s.snapshot()["edit_text"], s.rows[I_PARKING]["content"])

    def test_back_restores_merged_group(self):
        s = self.session()
        s.save_next("intro")
        s.save_next("parking")
        s.combine_next()
        s.save_next("library")
        self.assertTrue(s.back())
        self.assertEqual(s.pending_merge, [I_LIBRARY, I_LIBRARY2])

    def test_back_with_nothing_to_undo(self):
        self.assertFalse(self.session().back())

    # -- persistence -------------------------------------------------------

    def test_resume_continues_at_first_undecided_row(self):
        s = self.session()
        s.save_next("intro")
        s.discard()
        s2 = self.session()
        self.assertEqual(s2.current, 2)
        self.assertEqual(s2.decided_count(), 2)

    def test_legacy_single_idea_text_state_is_migrated(self):
        state_path = self.csv_path.with_name("transcript_review_state.json")
        state_path.write_text(json.dumps({"decisions": {
            "0": {"action": "kept", "idea_text": "old style", "merged_indices": [0]},
            "1": {"action": "discarded", "merged_indices": [1]},
        }}), encoding="utf-8")
        s = self.session()
        self.assertEqual(s.state.decisions["0"]["idea_texts"], ["old style"])
        self.assertEqual(s.current, 2)
        s.save_next("new")                   # must not crash writing the CSV
        self.assertEqual(read_csv(s.output_path)[0]["idea_text"], "old style")

    def test_done_snapshot_and_noop_actions(self):
        s = self.session()
        for _ in range(5):
            s.save_next("x")
        snap = s.snapshot()
        self.assertTrue(snap["done"])
        self.assertEqual(snap["progress_text"], "Done — 5 / 5")
        self.assertTrue(snap["can_back"])
        s.save_next("ignored")
        s.discard()
        s.drop_fragment()
        self.assertFalse(s.combine_next())
        s.split("a", "b")
        self.assertEqual(len(read_csv(s.output_path)), 5)

    def test_revision_bumps_only_on_real_changes(self):
        s = self.session()
        r0 = s.snapshot()["revision"]
        s.snapshot()
        self.assertEqual(s.snapshot()["revision"], r0)      # reading never bumps
        with self.assertRaises(ValueError):
            s.save_next("")
        self.assertEqual(s.revision, r0)                    # failed action never bumps
        self.assertFalse(s.back())
        self.assertEqual(s.revision, r0)                    # no-op back never bumps
        s.split("a", "b")
        self.assertEqual(s.revision, r0 + 1)
        s.save_next("a")
        s.save_next("b")
        self.assertEqual(s.revision, r0 + 3)
        self.assertTrue(s.combine_next())
        self.assertEqual(s.revision, r0 + 4)
        self.assertTrue(s.back())
        self.assertEqual(s.revision, r0 + 5)

    def test_snapshot_next_row_preview_and_end(self):
        s = self.session()
        self.assertEqual(s.snapshot()["next"]["kind"], "row")
        for _ in range(4):
            s.save_next("x")
        self.assertEqual(s.snapshot()["next"]["kind"], "none")


class ReviewFrameTests(unittest.TestCase):
    """The Tk view: same scenarios driven through the widgets."""

    def setUp(self):
        import tkinter as tk
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display available: {exc}")
        self.addCleanup(self.root.destroy)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.csv_path = make_session_dir(Path(self._tmp.name))

    def set_text(self, frame, text):
        frame.edit_text.delete("1.0", "end")
        frame.edit_text.insert("1.0", text)

    def test_split_flow_through_widgets(self):
        import review_ideas as ri
        frame = ri.ReviewFrame(self.root, self.csv_path, simplified=True)
        frame._save_next()                           # intro, unedited
        content = frame.session.rows[I_PARKING]["content"]
        self.set_text(frame, content)
        cut = content.index("and also")
        frame.edit_text.mark_set("insert", f"1.{cut}")
        frame._split_at_cursor()
        self.assertEqual(len(frame.session.fragment_queue), 2)
        self.assertIn("second half of split", frame.next_meta_label.cget("text"))
        self.assertEqual(frame.edit_text.get("1.0", "end").strip(), "We could add more parking,")
        self.assertEqual(frame.next_text.get("1.0", "end").strip(), "and also cheaper food.")
        frame._save_next()
        frame._save_next()
        rows = read_csv(frame.session.output_path)
        self.assertEqual(list(rows[0].keys()), ["time", "speaker", "phase", "content"])
        self.assertEqual(len(rows), 3)               # intro + two halves

    def test_split_without_text_on_both_sides_is_rejected(self):
        import review_ideas as ri
        from unittest import mock
        frame = ri.ReviewFrame(self.root, self.csv_path)
        frame.edit_text.mark_set("insert", "1.0")
        with mock.patch("review_ideas.messagebox.showinfo") as info:
            frame._split_at_cursor()
        info.assert_called_once()
        self.assertEqual(len(frame.session.fragment_queue), 1)

    def test_empty_edit_box_can_drop_fragment(self):
        import review_ideas as ri
        from unittest import mock
        frame = ri.ReviewFrame(self.root, self.csv_path)
        self.set_text(frame, "")
        with mock.patch("review_ideas.messagebox.askyesno", return_value=True):
            frame._save_next()
        self.assertEqual(frame.session.current, 1)
        self.assertEqual(frame.session.state.decisions["0"]["action"], "discarded")


if __name__ == "__main__":
    unittest.main()
