# GroupCreativity Toolkit — Roadmap

Feature ideas and planned improvements, roughly ordered from most immediately useful to more advanced. Community contributions welcome.

---

## Milestone 1 — Data Cleaning

Small, high-value additions that make the CSV cleaner before analysis.

### Filter filler language
Skip or flag utterances made up mostly of filler words and sounds — "um", "uh", "like", "you know", "okay", "yeah", etc. Useful for cleaning idea generation data before qualitative coding.

```bash
python process_captions.py transcript.txt --drop-fillers
python process_captions.py transcript.txt --flag-fillers   # keeps rows but adds a `filler` column
```

### Merge consecutive same-speaker utterances
When the same speaker has several short consecutive rows (a common artifact of auto-captioning), merge them into one row. Reduces noise and makes turn-taking analysis cleaner.

```bash
python process_captions.py transcript.txt --merge-consecutive
```

### Word count column
Add a `word_count` column to every row. Useful for measuring idea fluency and speaker contribution by phase.

### Minimum length filter
Drop rows below a word or character threshold — eliminates isolated "okay", "yes", "mm-hmm" rows that add little analytical value.

```bash
python process_captions.py transcript.txt --min-words 3
```

---

## Milestone 2 — Workflow Improvements

Make the tool easier to use across a full study with many sessions.

### Qualtrics integration *(not yet implemented)*
`review_ideas.py` produces a clean idea-list CSV, but getting it into a Qualtrics rating survey is still a manual step — you build/maintain the Loop & Merge survey yourself and upload the CSV by hand. A future milestone could use the Qualtrics API to push the idea list directly into a survey and/or pull rating results back down automatically.

### Configuration file support
Save your phase names, trigger phrases, and preferred flags in a `.yaml` or `.toml` config file so you do not have to repeat them on every run.

```yaml
# captions_config.yaml
phase_names:
  - introduction
  - ideaGenerationOne
  - break
  - ideaGenerationTwo
  - ideaSelection
  - debriefing
drop_fillers: true
min_words: 3
```

```bash
python process_captions.py transcript.txt --config captions_config.yaml
```

### Speaker anonymization
Replace real speaker names with anonymous IDs (e.g. `Participant_1`, `Experimenter`) based on a mapping file. Important for sharing de-identified data.

```bash
python process_captions.py transcript.txt --anonymize speaker_map.csv
```

---

## Milestone 3 — Summary Statistics

Generate reports directly from the tool without needing a separate analysis step.

### Per-phase speaker report
Output a summary CSV or printed table showing each speaker's row count, word count, and share of total speech per phase. Useful for measuring participation balance.

```bash
python process_captions.py transcript.txt --speaker-report
```

Example output:
```
Phase: ideaGenerationOne
Speaker          Rows  Words  Share
G1P1               89   312   58%
G1P2               60   198   37%
Experimenter 0      0     0    0%
```

### Turn-taking analysis
Count how often speakers switch, who tends to follow whom, and average utterance length per speaker. Useful for studying group dynamics and dominance patterns.

### Idea density over time
Track how many new utterances are produced per minute within each idea generation phase. Useful for studying the classic "idea rate decline" pattern in brainstorming research.

---

## Milestone 4 — Output Formats

### Excel export (`.xlsx`)
Export directly to Excel with each phase on a separate sheet. No manual filtering required.

```bash
python process_captions.py transcript.txt --format xlsx
```

### JSON export
Structured JSON output, useful for downstream NLP pipelines or web applications.

### Combined multi-session dataset
When batch processing, produce a single tidy CSV with a `session_id` column prepended, ready for multi-level analysis in R or Python.

---

## Milestone 5 — Content Analysis Helpers

More advanced features that move toward automated content analysis.

### Keyword flagging
Mark rows that contain user-specified keywords or phrases. Useful for quickly finding all mentions of a topic across sessions.

```bash
python process_captions.py transcript.txt --keywords "parking,transportation,food"
```

### Custom filler word list
Let researchers supply their own list of filler words or domain-specific noise terms to filter.

```bash
python process_captions.py transcript.txt --filler-list my_fillers.txt
```

### Idea boundary detection *(experimental)*
Attempt to segment idea generation phases into individual ideas based on pauses, speaker switches, and sentence boundaries. Each candidate idea gets its own row. This would complement manual coding rather than replace it.

---

## Milestone 6 — Visualization

### Participation timeline
Generate a simple chart showing who spoke and when across the session, color-coded by phase.

### Speaker pie charts per phase
Visual breakdown of speech share per speaker within each phase.

### Idea rate curve
Plot utterances-per-minute over the course of the idea generation phase.

---

## Contributing

If you use this tool in your research and add a feature that might help others, pull requests are welcome. Please include a short description of the use case that motivated the change.

---

## Completed

- [x] Parse Zoom-style meeting transcripts (`[Speaker] HH:MM:SS` format)
- [x] Parse SRT and WebVTT subtitle files
- [x] Automatic format detection
- [x] Phase detection from experimenter speech (configurable trigger phrases)
- [x] Phase filtering via `--phases`
- [x] Phase summary with `--list-phases` (row counts + timestamps)
- [x] Warning when a phase is never triggered in a file
- [x] Python and R implementations
- [x] Batch processing across many group folders, auto-selecting the correct transcript among several autosaved copies (`batch_process.py`)
- [x] Manual idea-extraction review GUI — trim/split/merge/discard transcript rows down to individual ideas (`review_ideas.py`)
