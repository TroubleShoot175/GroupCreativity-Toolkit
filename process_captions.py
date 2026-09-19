#!/usr/bin/env python3
"""
process_captions.py — Convert closed caption files to CSV.

Supported formats:
  - Meeting transcript  [Speaker Name] HH:MM:SS / content
  - SRT (.srt)
  - WebVTT (.vtt)

Meeting transcript output columns:
  time, speaker, phase, content

Phase detection (from any speaker's content, case-insensitive):
  The experimenter's key phrases drive phase transitions in this order:

  introduction       — start of file until 1st "your time starts now"
  ideaGenerationOne  — 1st "your time starts now" until 1st "your time is up"
  break              — 1st "your time is up" until 2nd "your time starts now"
  ideaGenerationTwo  — 2nd "your time starts now" until 2nd "your time is up"
  break              — 2nd "your time is up" until 3rd "your time starts now"
  ideaSelection      — 3rd "your time starts now" until 3rd "your time is up"
  debriefing         — 3rd "your time is up" until end of file

  If a session is shorter (e.g. only 1 generation round), later phases will
  simply be absent and the script will report which phases were found.

Usage:
    python process_captions.py transcript.txt
    python process_captions.py transcript.txt -o out.csv
    python process_captions.py transcript.txt --list-phases
    python process_captions.py transcript.txt --phases ideaGenerationOne,ideaGenerationTwo
    python process_captions.py transcript.txt --keep-fillers
    python process_captions.py input.srt
    python process_captions.py input.vtt -o output.csv --include-timestamps

Filler cleaning (transcript mode, on by default):
  Removes filler words and phrases — um, uh, like, you know, okay, yeah —
  from content and drops rows that become empty after cleaning.
  Use --keep-fillers to disable.
"""

import re
import csv
import sys
import argparse
from collections import Counter
from pathlib import Path


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def parse_time_seconds(time_str: str) -> float:
    time_str = time_str.strip().replace(",", ".")
    parts = time_str.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    elif len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    return float(time_str)


def format_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


# ---------------------------------------------------------------------------
# Phase detection
# ---------------------------------------------------------------------------

# Ordered sequence of (trigger_pattern, phase_that_starts_after_trigger).
# Consumed one at a time as matching rows are encountered.
_STARTS_NOW = re.compile(r"your time starts now|your \d+[-\s]minute timer starts now", re.IGNORECASE)
_TIME_UP    = re.compile(r"your time is up|(?<!\w)time is up|stop generating ideas", re.IGNORECASE)

_TRANSITIONS = [
    (_STARTS_NOW, "ideaGenerationOne"),
    (_TIME_UP,    "break"),
    (_STARTS_NOW, "ideaGenerationTwo"),
    (_TIME_UP,    "break"),
    (_STARTS_NOW, "ideaSelection"),
    (_TIME_UP,    "debriefing"),
]

# Canonical order for display / filtering (break appears once as a filter name)
ALL_PHASES = [
    "introduction",
    "ideaGenerationOne",
    "break",
    "ideaGenerationTwo",
    "ideaSelection",
    "debriefing",
]


def assign_phases(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Walk rows in order, assigning a 'phase' to each one by consuming
    _TRANSITIONS as their trigger phrases are encountered.

    Returns:
        rows        — same list, each row now has a 'phase' key
        warnings    — list of human-readable warning strings (missing phases, etc.)
    """
    pending = list(_TRANSITIONS)
    current_phase = "introduction"
    triggered: list[str] = []

    for row in rows:
        if pending:
            pattern, next_phase = pending[0]
            if pattern.search(row["content"]):
                current_phase = next_phase
                triggered.append(next_phase)
                pending.pop(0)
        row["phase"] = current_phase

    # Build warnings for any transitions that were never triggered
    warnings: list[str] = []
    if pending:
        phases_found = set(r["phase"] for r in rows)
        missing_phases = [phase for _, phase in pending if phase not in phases_found]
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_missing: list[str] = []
        for p in missing_phases:
            if p not in seen:
                seen.add(p)
                unique_missing.append(p)
        if unique_missing:
            warnings.append(
                f"Note: the following phase(s) were never triggered in this file "
                f"(key phrase not found): {', '.join(unique_missing)}"
            )

    return rows, warnings


# ---------------------------------------------------------------------------
# Filler language cleaning
# ---------------------------------------------------------------------------

# Multi-word phrases must come before single words so they are matched first.
_FILLER_PATTERNS = [
    re.compile(r'\byou\s+know\b',  re.IGNORECASE),
    re.compile(r'\bum+\b',         re.IGNORECASE),
    re.compile(r'\buh+\b',         re.IGNORECASE),
    re.compile(r'\blike\b',        re.IGNORECASE),
    re.compile(r'\bokay\b',        re.IGNORECASE),
    re.compile(r'\byeah\b',        re.IGNORECASE),
]


def clean_fillers(text: str) -> str:
    """Remove filler words/phrases from a string and tidy up leftover punctuation."""
    for pat in _FILLER_PATTERNS:
        text = pat.sub("", text)
    # Remove orphaned commas, ellipses, and dots left after extraction
    text = re.sub(r"[,…\.]+\s*[,…\.]+", ".", text)  # collapse repeated punctuation
    text = re.sub(r"^\s*[,;…\.]+", "", text)          # strip leading punctuation
    text = re.sub(r"[,;…\.]+\s*$", "", text)          # strip trailing punctuation
    text = re.sub(r"[ \t]{2,}", " ", text)             # collapse multiple spaces
    return text.strip()


def apply_filler_cleaning(rows: list[dict]) -> tuple[list[dict], int]:
    """
    Apply filler cleaning to every row's content in-place.
    Rows whose content becomes empty after cleaning are dropped.

    Returns:
        cleaned rows, number of rows dropped
    """
    cleaned = []
    dropped = 0
    for row in rows:
        text = clean_fillers(row["content"])
        if text:
            row["content"] = text
            cleaned.append(row)
        else:
            dropped += 1
    return cleaned, dropped


# ---------------------------------------------------------------------------
# Meeting transcript parser
# ---------------------------------------------------------------------------

_TRANSCRIPT_HEADER = re.compile(r"^\[([^\]]+)\]\s+(\d{1,2}:\d{2}:\d{2})\s*$")


def is_transcript_format(content: str) -> bool:
    for line in content.splitlines():
        if _TRANSCRIPT_HEADER.match(line.strip()):
            return True
    return False


def parse_transcript(content: str) -> list[dict]:
    rows: list[dict] = []
    current_speaker: str | None = None
    current_time: str | None = None
    current_lines: list[str] = []

    def flush():
        if current_speaker is not None and current_lines:
            text = " ".join(l.strip() for l in current_lines if l.strip())
            if text:
                rows.append({"time": current_time, "speaker": current_speaker, "content": text})

    for raw_line in content.splitlines():
        line = raw_line.strip()
        m = _TRANSCRIPT_HEADER.match(line)
        if m:
            flush()
            current_speaker = m.group(1).strip()
            current_time = m.group(2).strip()
            current_lines = []
        elif line == "":
            continue
        else:
            current_lines.append(line)

    flush()
    return rows


def write_transcript_csv(rows: list[dict], output_path: str) -> None:
    fields = ["time", "speaker", "phase", "content"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# SRT parser
# ---------------------------------------------------------------------------

def parse_srt(content: str) -> list[dict]:
    blocks = re.split(r"\n{2,}", content.strip())
    cues = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        idx_line = lines[0].strip()
        index = idx_line if re.match(r"^\d+$", idx_line) else None
        time_line_idx = 1 if index is not None else 0
        time_line = lines[time_line_idx]
        time_match = re.match(
            r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})",
            time_line,
        )
        if not time_match:
            continue
        start_s = parse_time_seconds(time_match.group(1))
        end_s = parse_time_seconds(time_match.group(2))
        text = strip_tags(" ".join(l.strip() for l in lines[time_line_idx + 1:] if l.strip()))
        cues.append({
            "index": index or str(len(cues) + 1),
            "start_time": format_time(start_s),
            "end_time": format_time(end_s),
            "start_seconds": round(start_s, 3),
            "end_seconds": round(end_s, 3),
            "duration_seconds": round(end_s - start_s, 3),
            "text": text,
        })
    return cues


# ---------------------------------------------------------------------------
# VTT parser
# ---------------------------------------------------------------------------

def parse_vtt(content: str) -> list[dict]:
    content = re.sub(r"^WEBVTT[^\n]*\n", "", content, flags=re.MULTILINE)
    for block_type in ("NOTE", "STYLE", "REGION"):
        content = re.sub(rf"{block_type}\s.*?(?=\n{{2,}}|\Z)", "", content, flags=re.DOTALL)
    cues = []
    for block in re.split(r"\n{2,}", content.strip()):
        lines = block.strip().splitlines()
        if not lines:
            continue
        time_line_idx = 0
        index = None
        if "-->" not in lines[0] and len(lines) > 1:
            index = lines[0].strip()
            time_line_idx = 1
        if time_line_idx >= len(lines):
            continue
        time_match = re.match(
            r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}|\d{1,2}:\d{2}[,\.]\d{1,3})\s*-->\s*"
            r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}|\d{1,2}:\d{2}[,\.]\d{1,3})",
            lines[time_line_idx],
        )
        if not time_match:
            continue
        start_s = parse_time_seconds(time_match.group(1))
        end_s = parse_time_seconds(time_match.group(2))
        text = strip_tags(" ".join(l.strip() for l in lines[time_line_idx + 1:] if l.strip()))
        cues.append({
            "index": index or str(len(cues) + 1),
            "start_time": format_time(start_s),
            "end_time": format_time(end_s),
            "start_seconds": round(start_s, 3),
            "end_seconds": round(end_s, 3),
            "duration_seconds": round(end_s - start_s, 3),
            "text": text,
        })
    return cues


def write_subtitle_csv(cues: list[dict], output_path: str, include_timestamps: bool) -> None:
    if include_timestamps:
        fields = ["index", "start_time", "end_time", "start_seconds", "end_seconds", "duration_seconds", "text"]
    else:
        fields = ["index", "start_time", "end_time", "duration_seconds", "text"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(cues)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_format(content: str, path: Path) -> str:
    if content.lstrip().startswith("WEBVTT") or path.suffix.lower() == ".vtt":
        return "vtt"
    if is_transcript_format(content):
        return "transcript"
    return "srt"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Convert closed caption / meeting transcript files to CSV.\n\n"
            "Meeting transcript output columns: time, speaker, phase, content\n"
            "SRT / VTT output columns:          index, start_time, end_time, duration_seconds, text"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", help="Input file (.txt transcript, .srt, or .vtt)")
    parser.add_argument(
        "-o", "--output",
        help="Output CSV path (default: same name as input with .csv extension)",
    )
    parser.add_argument(
        "--phases",
        help=(
            "Comma-separated list of phases to keep (transcript mode only).\n"
            f"Valid values: {', '.join(ALL_PHASES)}\n"
            "Example: --phases ideaGenerationOne,ideaGenerationTwo"
        ),
    )
    parser.add_argument(
        "--list-phases",
        action="store_true",
        help="Print a phase breakdown with row counts and first/last timestamps, then exit.",
    )
    parser.add_argument(
        "--keep-fillers",
        action="store_true",
        help=(
            "(Transcript mode only) Disable filler language cleaning. "
            "By default, filler words (um, uh, like, you know, okay, yeah) are removed "
            "and rows that become empty after cleaning are dropped."
        ),
    )
    parser.add_argument(
        "--include-timestamps",
        action="store_true",
        help="(SRT/VTT only) Also include raw start_seconds and end_seconds columns",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = str(args.output or input_path.with_suffix(".csv"))
    content = input_path.read_text(encoding="utf-8-sig")
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    fmt = detect_format(content, input_path)

    # ---- Transcript mode -------------------------------------------------
    if fmt == "transcript":
        rows = parse_transcript(content)
        if not rows:
            print("Warning: no entries found in transcript.", file=sys.stderr)
            sys.exit(1)

        rows, warnings = assign_phases(rows)

        for w in warnings:
            print(f"WARNING: {w}", file=sys.stderr)

        # Filler cleaning (on by default, disabled by --keep-fillers)
        if not args.keep_fillers:
            rows, dropped = apply_filler_cleaning(rows)
            if dropped:
                print(f"Note: {dropped} row(s) dropped after filler cleaning.", file=sys.stderr)

        # --list-phases: summary and exit
        if args.list_phases:
            counts = Counter(r["phase"] for r in rows)
            # first/last timestamps per phase
            first_time: dict[str, str] = {}
            last_time: dict[str, str] = {}
            for r in rows:
                p = r["phase"]
                if p not in first_time:
                    first_time[p] = r["time"]
                last_time[p] = r["time"]

            print(f"\n{'Phase':<22} {'Rows':>5}  {'First':>8}  {'Last':>8}")
            print("-" * 52)
            for phase in ALL_PHASES:
                c = counts.get(phase, 0)
                ft = first_time.get(phase, "—")
                lt = last_time.get(phase, "—")
                print(f"{phase:<22} {c:>5}  {ft:>8}  {lt:>8}")
            print("-" * 52)
            print(f"{'TOTAL':<22} {len(rows):>5}\n")
            return

        # --phases: filter
        if args.phases:
            requested = [p.strip() for p in args.phases.split(",")]
            invalid = [p for p in requested if p not in ALL_PHASES]
            if invalid:
                print(f"Error: unknown phase(s): {', '.join(invalid)}", file=sys.stderr)
                print(f"Valid phases: {', '.join(ALL_PHASES)}", file=sys.stderr)
                sys.exit(1)
            rows = [r for r in rows if r["phase"] in requested]
            if not rows:
                print("Warning: no rows remain after phase filter.", file=sys.stderr)
                sys.exit(1)

        write_transcript_csv(rows, output_path)
        phases_present = sorted(set(r["phase"] for r in rows), key=lambda p: ALL_PHASES.index(p))
        print(
            f"Done: {len(rows)} entries written to {output_path}\n"
            f"      Phases included: {', '.join(phases_present)}"
        )

    # ---- VTT mode --------------------------------------------------------
    elif fmt == "vtt":
        cues = parse_vtt(content)
        if not cues:
            print("Warning: no cues found in VTT file.", file=sys.stderr)
            sys.exit(1)
        write_subtitle_csv(cues, output_path, args.include_timestamps)
        print(f"Done: {len(cues)} cues written to {output_path}")

    # ---- SRT mode --------------------------------------------------------
    else:
        cues = parse_srt(content)
        if not cues:
            print("Warning: no cues found in SRT file.", file=sys.stderr)
            sys.exit(1)
        write_subtitle_csv(cues, output_path, args.include_timestamps)
        print(f"Done: {len(cues)} cues written to {output_path}")


if __name__ == "__main__":
    main()
