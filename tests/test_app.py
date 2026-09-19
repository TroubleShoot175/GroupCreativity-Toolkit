"""Integration tests for the host window (app.py) with the review server.

Builds synthetic Zoom-style transcripts in a temp directory (never touches
real session folders). Skipped when no display is available.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_server import Client  # noqa: E402

TRANSCRIPT_HEAD = """\
[NSF Research Team] 19:00:00
Hello everyone, welcome to this experiment on collaborative creativity.

[NSF Research Team] 19:01:00
Your time starts now

[{p1}] 19:01:10
We could add more parking and cheaper food

[{p2}] 19:01:20
Longer library hours

[{p1}] 19:01:30
Free bikes for everyone

[NSF Research Team] 19:11:00
Time is up. Please stop generating ideas.

[NSF Research Team] 19:12:00
Your time starts now

[{p2}] 19:12:10
A rooftop garden
"""

TRANSCRIPT_TAIL = """
[NSF Research Team] 19:22:00
Time is up. Please stop generating ideas.

[NSF Research Team] 19:23:00
Your time starts now

[{p1}] 19:23:10
Pick the bikes

[NSF Research Team] 19:33:00
Time is up.

[NSF Research Team] 19:34:00
This concludes the experimental tasks. Thank you.
"""


def make_group(root: Path, name: str):
    """A group folder with an early partial autosave and a newer complete one."""
    folder = root / name
    folder.mkdir()
    fmt = {"p1": f"{name}P1", "p2": f"{name}P2"}
    partial = folder / "transcript_early.txt"
    partial.write_text(TRANSCRIPT_HEAD.format(**fmt), encoding="utf-8")
    final = folder / "transcript_final.txt"
    final.write_text(TRANSCRIPT_HEAD.format(**fmt) + TRANSCRIPT_TAIL.format(**fmt), encoding="utf-8")
    os.utime(partial, (1_700_000_000, 1_700_000_000))
    os.utime(final, (1_700_000_900, 1_700_000_900))
    return folder


class AppServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        try:
            import app
            self.app_module = app
            self.app = app.App()
        except tk.TclError as exc:
            self.skipTest(f"no display available: {exc}")
        self.addCleanup(self._close_app)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sessions = Path(self._tmp.name) / "sessions"
        self.sessions.mkdir()
        make_group(self.sessions, "G81")
        make_group(self.sessions, "G82")
        (self.sessions / "notes-only").mkdir()      # a folder with no transcript

        self.app._process_folder(self.sessions)

    def _close_app(self):
        try:
            self.app._on_close()
        except Exception:
            pass

    def result(self, name):
        return next(r for r in self.app.results if r.folder.name == name)

    def start_server(self):
        self.app._start_server()
        self.assertTrue(self.app.server.running)
        client = Client(self.app.server.port)
        status, _d, _c = client.login(passcode=self.app.server.passcode, name="Web Reviewer")
        self.assertEqual(status, 200)
        return client

    def test_processing_selects_complete_transcript_and_registers_groups(self):
        self.assertEqual({r.folder.name for r in self.app.results}, {"G81", "G82", "notes-only"})
        g81 = self.result("G81")
        self.assertIsNone(g81.error)
        self.assertEqual(g81.csv_path.name, "transcript_final.csv")     # newest + complete beat the partial
        self.assertIsNone(g81.warning)
        # 2 trigger rows ("Your time starts now" belongs to its phase) + 4 idea rows
        self.assertEqual(g81.idea_row_count, 6)
        self.assertIsNotNone(self.result("notes-only").error)
        self.assertEqual(sorted(self.app.registry.names()), ["G81", "G82"])

    def test_web_reviewer_and_host_share_the_same_lock(self):
        client = self.start_server()
        self.assertTrue(self.app.server.urls())
        status, data = client.post("/api/groups/G81/open")
        self.assertEqual(status, 200)
        self.assertEqual(data["snapshot"]["progress_text"], "Row 1 / 6")

        # host tries the group a web reviewer holds -> refused with a message
        with mock.patch.object(self.app_module.messagebox, "showinfo") as info:
            self.app._open_review(self.result("G81"))
        info.assert_called_once()
        self.assertIn("Web Reviewer", info.call_args[0][1])
        self.assertNotIn("G81", self.app._local_reviews)

        # the other group is free for the host, and then blocked for the web
        self.app._open_review(self.result("G82"))
        self.assertIn("G82", self.app._local_reviews)
        status, data = client.post("/api/groups/G82/open")
        self.assertEqual((status, data["error"], data["holder"]), (409, "in_use", self.app_module.LOCAL_LABEL))

        # closing the host's window frees the group for the web reviewer
        self.app._local_reviews["G82"].destroy()
        self.app.update()
        self.assertNotIn("G82", self.app._local_reviews)
        self.assertEqual(client.post("/api/groups/G82/open")[0], 200)

    def test_opening_same_group_twice_locally_reuses_window(self):
        self.app._open_review(self.result("G81"))
        first = self.app._local_reviews["G81"]
        self.app._open_review(self.result("G81"))
        self.assertIs(self.app._local_reviews["G81"], first)
        self.assertEqual(len(self.app._local_reviews), 1)

    def test_review_window_survives_relisting_and_blocks_folder_switch(self):
        self.app._open_review(self.result("G81"))
        top = self.app._local_reviews["G81"]
        self.app._show_group_list(self.sessions)          # rebuilds the frames
        self.assertTrue(top.winfo_exists())
        with mock.patch.object(self.app_module.messagebox, "showinfo") as info:
            self.app._show_folder_picker()
        info.assert_called_once()
        self.assertTrue(top.winfo_exists())
        self.assertIsNotNone(self.app.registry)

    def test_local_review_writes_the_simplified_csv(self):
        self.app._open_review(self.result("G81"))
        frame = next(w for w in self.app._local_reviews["G81"].winfo_children())
        frame._save_next()
        out = self.result("G81").csv_path.with_name("transcript_final_ideas.csv")
        self.assertEqual(out.read_text(encoding="utf-8").splitlines()[0], "time,speaker,phase,content")

    def test_stopping_server_releases_web_locks_and_closes_port(self):
        client = self.start_server()
        client.post("/api/groups/G81/open")
        port = self.app.server.port
        self.app._stop_server_clicked()
        self.assertIsNone(self.app.server)
        self.assertEqual(self.app.registry.holders(), [])
        with self.assertRaises(OSError):
            Client(port).get("/api/me")

    def test_server_can_be_restarted(self):
        self.start_server()
        first_code = self.app.server.passcode
        self.app._stop_server_clicked()
        self.start_server()
        self.assertNotEqual(self.app.server.passcode, first_code)   # fresh passcode each start

    def test_activity_label_reports_who_is_reviewing(self):
        client = self.start_server()
        client.post("/api/groups/G81/open")
        self.app._poll_activity()
        text = self.app._activity_label.cget("text")
        self.assertIn("G81", text)
        self.assertIn("Web Reviewer", text)


if __name__ == "__main__":
    unittest.main()
