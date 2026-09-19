# GroupCreativity Toolkit

A small pipeline for researchers studying **team creativity**, **group creativity**, and **collaborative brainstorming**: turn raw Zoom transcripts into phase-labeled data, pull out the individual ideas from the brainstorming portions, and get a clean list ready for rating (e.g. via Qualtrics).

---

## For non-technical users — no installation, no command line

If you don't use the command line and just want to process sessions and review ideas, use the **GroupCreativity Toolkit app** instead of anything below:

1. Go to the [Releases page](https://github.com/TroubleShoot175/GroupCreativity-Toolkit/releases) and download `GroupCreativityToolkit-Windows.zip` from the latest release (Windows only).
2. Right-click the downloaded zip → **Extract All...**, then open the extracted folder and double-click `GroupCreativityToolkit.exe` inside it. No Python, no installation, no typing commands.
   - **Windows may show a blue "Windows protected your PC" screen** the first time — this happens for any small, unsigned app that isn't yet widely recognized by Microsoft, not because anything is wrong. Click **More info**, then **Run anyway**. (The app never accesses the internet or installs anything — it only reads/writes files in the folder you pick.)
3. Click **Choose Sessions Folder...** and pick the folder that contains your group folders (e.g. a folder with `G15`, `G18`, ... inside it).
4. The app finds the correct transcript in each group folder automatically and lists them.
5. Click **Review Ideas** next to a group to trim each brainstormed line down to the idea itself (you can split one line into two ideas, merge a split idea back across lines, or discard chatter that isn't an idea).
6. Each group produces a `..._ideas.csv` file (columns: `time, speaker, phase, content`) right next to its transcript, ready to hand off to whoever runs the Qualtrics rating survey.

**Want several people to review at the same time?** Click **Start Review Server** in the app's group list. It shows an address and a passcode; anyone on the same Wi-Fi or office network can open that address in a web browser (on Windows, Mac, or Linux — nothing to install), enter the passcode, and review a group from their own computer. See [Review from other computers on your network](#4-review-from-other-computers-on-your-network--the-review-server) below.

Everything below this point is for the command-line versions of these same tools (more flexible, but requires Python).

---

## Pipeline overview

```
raw Zoom transcript(s), one folder per group
        │
        ▼
process_captions.py  (single file)   or   batch_process.py  (many group folders at once)
        │                                   auto-picks the right transcript when a
        ▼                                   folder has several autosaved copies
phase-tagged CSV  (time, speaker, phase, content)
        │
        ▼
review_ideas.py  — GUI: trim each idea-generation line down to the idea itself,
                    split/merge/discard as needed
(or the review server — the same review screen in a browser, for several
 reviewers on your network at once)
        │
        ▼
idea-list CSV  (group, participant, speaker, time, phase, content, idea_text)
        │
        ▼
manually uploaded into a Qualtrics survey (e.g. a Loop & Merge rating survey)
for originality/feasibility scoring — the toolkit does not talk to Qualtrics
directly today (see ROADMAP.md)
```

---

## 1. `process_captions.py` / `process_captions.R` — transcript → phase-tagged CSV

Given a raw closed caption transcript (Zoom-style `.txt`, `.srt`, or `.vtt`), this:

1. Parses each utterance into **time**, **speaker**, and **content**
2. Detects **experiment phase boundaries** by scanning all speech for key experimenter phrases
3. Labels every row with its **phase**
4. Exports a clean **CSV**

### Output columns (meeting-transcript mode)

| Column | Description |
|--------|-------------|
| `time` | Timestamp of the utterance (HH:MM:SS) |
| `speaker` | Speaker name or ID as it appears in the transcript |
| `phase` | Experiment phase assigned to this utterance |
| `content` | The spoken text |

SRT/VTT input instead produces `index, start_time, end_time, duration_seconds, text` (Python only; the R script handles the meeting-transcript format only).

### Supported input formats

| Format | Extension | Notes |
|--------|-----------|-------|
| Meeting transcript | `.txt` | `[Speaker Name] HH:MM:SS` followed by content — default Zoom closed caption export format |
| SubRip | `.srt` | Python only |
| WebVTT | `.vtt` | Python only |

The format is detected automatically from the file content.

### Phase detection

Transitions alternate between a **start trigger** and a **stop trigger**:

| Trigger type | Recognized phrases |
|---|---|
| **Start** (begins next phase) | "your time starts now" · "your N-minute timer starts now" |
| **Stop** (ends current phase) | "your time is up" · "time is up" · "stop generating ideas" |

Default phase sequence:
```
introduction       — everything before the 1st start trigger
ideaGenerationOne  — 1st start trigger → 1st stop trigger
break              — 1st stop trigger  → 2nd start trigger
ideaGenerationTwo  — 2nd start trigger → 2nd stop trigger
break              — 2nd stop trigger  → 3rd start trigger
ideaSelection      — 3rd start trigger → 3rd stop trigger
debriefing         — 3rd stop trigger  → end of file
```
If a session is shorter, later phases are simply absent — the tool prints a warning listing which phases were never triggered.

> **Python vs. R:** the R script (`process_captions.R`) supports a `--phase-names` flag for a custom ordered phase sequence. The Python script's phase sequence above is currently fixed (not yet configurable from the CLI) — see ROADMAP.md.

### Installation

**Python** — requires Python 3.10+, standard library only:
```bash
python process_captions.py --help
```

**R** — requires R 4.0+ and the [`optparse`](https://cran.r-project.org/package=optparse) package:
```r
install.packages("optparse")
```
```bash
Rscript process_captions.R --help
```

### Usage

```bash
# Convert a transcript to CSV (written next to the input by default)
python process_captions.py transcript.txt
python process_captions.py transcript.txt -o results/session1.csv

# Inspect the phase breakdown before exporting
python process_captions.py transcript.txt --list-phases

# Keep only specific phases
python process_captions.py transcript.txt --phases ideaGenerationOne,ideaGenerationTwo

# Filler words (um, uh, like, you know, okay, yeah) are stripped by default —
# disable with:
python process_captions.py transcript.txt --keep-fillers

# SRT / VTT subtitles (Python only)
python process_captions.py video.srt
python process_captions.py video.vtt -o output.csv --include-timestamps
```

`--list-phases` prints a row-count and first/last-timestamp breakdown per phase:
```
Phase                   Rows     First      Last
----------------------------------------------------
introduction              57  17:03:12  17:16:12
ideaGenerationOne        149  17:16:16  17:26:08
break                    100  17:26:26  17:44:48
ideaGenerationTwo        162  17:31:04  17:40:49
ideaSelection            186  17:44:52  17:54:59
debriefing               190  17:54:59  18:19:15
----------------------------------------------------
TOTAL                    844
```

---

## 2. `batch_process.py` — auto-select the right transcript across many group folders

Zoom autosaves a session's transcript every so often, so a group's folder often ends up with several `.txt` candidates (plus unrelated files like `chat.txt`/`closed_caption.txt`). `batch_process.py` scans a parent directory of group folders (e.g. `G15/`, `G18/`, ...) and, in each one, scores every transcript-shaped `.txt` file 0–3:

- **+1** largest file size among that folder's candidates
- **+1** newest file (by modified time) among that folder's candidates
- **+1** contains **both** an intro marker and an ending marker pulled from the experimenter script (one combined "completeness" point)

The highest-scoring file is processed through the same pipeline as `process_captions.py`; files are never moved or deleted.

```bash
python batch_process.py ./sessions        # scans every subfolder of ./sessions
python batch_process.py . --keep-fillers --phases ideaGenerationOne,ideaGenerationTwo
```

Per folder, this writes `<transcript>.csv` (same format as `process_captions.py`) and `<transcript>_batch_log.txt` — the full candidate list with scores, which file was chosen (and its second-best runner-up), any phase-assignment warnings, and the final row count.

---

## 3. `review_ideas.py` — idea-extraction review GUI

There's no automatic way to tell "the idea" apart from elaboration/rambling in a transcript line — it takes a human reading it. `review_ideas.py` is a small Tkinter GUI for doing that quickly, one idea-generation row at a time.

**Requirements:** Tkinter, which ships with the standard Python installer on Windows/macOS/Linux. On some Mac Python installs (notably Homebrew's bare `python3`), you may need `brew install python-tk`.

```bash
python review_ideas.py G18/"<transcript>.csv"   # the CSV from process_captions.py / batch_process.py
```

Only `ideaGenerationOne`/`ideaGenerationTwo` rows are shown. The (highlighted, pale-yellow) edit box is pre-filled with the row's content — trim it down to just the idea.

| Control | Effect |
|---|---|
| **Save & Next** (Enter) | Confirms the current text as an idea, advances |
| **Discard** (Ctrl+D) | Drops the whole row — nothing written to output |
| **Combine with next →** | Folds the next row's content into the current one, for ideas split across rows |
| **Split at cursor ✂** | Splits the current text into two ideas at the cursor; each half gets its own edit/confirm step and both are saved once confirmed — either half can be split again |
| **Back** | Undoes the previous decision so you can redo it |

Progress autosaves after every decision to a sidecar `<csv_stem>_review_state.json`, so closing and relaunching resumes exactly where you left off.

**Output:** `<csv_stem>_ideas.csv` with columns `group, participant, speaker, time, phase, content, idea_text` (the desktop app and review server write the simplified `time, speaker, phase, content` version instead, with the idea as `content`) — `group` is auto-detected from the folder name, `participant` is parsed out of `speaker` (e.g. `G18P1` → `P1`). This is a plain one-idea-per-row CSV; map its columns however your Qualtrics import needs.

---

## 4. Review from other computers on your network — the review server

The desktop app can also host the review screen on your local network, so several reviewers can work at once, each from their own computer's web browser. Reviewers don't install anything and never see your files; all decisions are saved on the **host** computer, next to each group's transcript, exactly as in the desktop review.

**Host (the computer running the app):**

1. Choose your sessions folder, then click **Start Review Server** in the group list.
2. The window shows one or more addresses (like `http://192.168.1.20:8765`) and a six-character **passcode**. Give both to your reviewers. Each start of the server generates a new passcode.
3. The first time, Windows may ask whether to allow the app on the network — choose **Private networks**. (If reviewers can't connect, this is the usual cause; on a corporate/university network, client isolation or a firewall policy may also block it. Try another address from the list.)
4. The window shows which group each reviewer currently has open. **Stop Server** ends all web sessions; nothing already saved is lost.

**Reviewers:** open the address in any browser, enter the passcode (and optionally your name, so others can see who has a group), pick a group, and review. The screen works like the desktop one: Enter saves, Shift+Enter adds a line break, Ctrl+D discards, plus Combine / Split at cursor / Back buttons.

**How sharing works:**

- **One reviewer per group at a time.** Different people can review different groups simultaneously; a group someone else has open shows as "In use by …". The host's own desktop **Review Ideas** button follows the same rule.
- A group is released when its reviewer clicks **← Groups** or closes the page. If a reviewer's computer disappears, their group frees itself after about three minutes.
- Progress is saved after every decision. If you reload the page, you resume at the first row you haven't decided; only an unsaved, half-finished split of the current row is lost.

**Security — please read:**

- The connection is plain **HTTP**, so the passcode and transcript text are **not encrypted** on the network. Use this on a network you trust (home, lab, or office), not on public or shared Wi-Fi.
- The passcode keeps casual visitors out; it isn't a user account system, and reviewers are not individually identified beyond the optional name they type.
- The server only serves the review screen and the groups you loaded; it can't browse other files on the host.

Runs from source too: `app.py` is the host window (`python app.py`); the server itself is `server.py` (standard library only) and the browser screen is in `web/`.

---

## Tests

```bash
python -m unittest discover -s tests
```

Covers the review logic (`review_core.py`), the server's login/locking/API and hardening, and the host window's interaction with the server. Tests use synthetic data in temporary folders — they never touch your session folders. The window tests need a display and skip themselves without one.

---

## Typical end-to-end workflow

1. `python batch_process.py .` — phase-tag every group folder, auto-picking the correct transcript in each.
2. `python review_ideas.py G18/<the resulting CSV>` — for each group, review the idea-generation rows down to a clean idea list. (Or run the desktop app and use its review server to spread groups across several reviewers.)
3. Upload the resulting `_ideas.csv` files into your Qualtrics rating survey (manual step today).

---

## Notes for researchers

- **Consistent experimenter phrasing matters.** Any occurrence of a trigger phrase — even mid-session — advances the phase. Use `--list-phases` to verify detected boundaries before exporting.
- **Missing phases are flagged** with a warning if a trigger phrase was never said.
- **"break" can appear more than once** — both periods are labeled `break`; filtering on it includes rows from both.
- **CRLF-encoded transcripts are handled** — Windows-style line endings are normalized before parsing.

---

## License

MIT
