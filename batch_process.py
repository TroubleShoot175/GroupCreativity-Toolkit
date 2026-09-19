#!/usr/bin/env python3
"""
batch_process.py — Run process_captions.py's transcript pipeline across many
group folders, auto-selecting the correct transcript file in each one.

Each folder (e.g. G15, G18) typically contains several *.txt files: the real
meeting transcript is autosaved multiple times during a Zoom call, plus
unrelated files (chat.txt, closed_caption.txt, meeting_saved_new_chat.txt)
that are not the transcript at all.

Selection scoring (0-3 points per transcript candidate):
  +1  largest file size among this folder's candidates
  +1  newest file (by mtime) among this folder's candidates
  +1  contains BOTH the intro marker and the ending marker below
        (one combined point — completeness, not two separate checks)

The highest-scoring candidate is processed; the CSV and a log file are
written into the same folder. No files are moved or deleted.

Usage:
    python batch_process.py <parent_dir> [--keep-fillers] [--phases ...]
"""

import argparse
import sys
from pathlib import Path

import process_captions as pc

# Short, stable substrings pulled from experimenter_script_v02.docx.txt.
# Edit these if the experimenter script wording changes.
INTRO_MARKER = "experiment on collaborative creativity"
ENDING_MARKER = "concludes the experimental tasks"


def find_transcript_candidates(folder: Path) -> list[Path]:
    candidates = []
    for txt_path in folder.glob("*.txt"):
        try:
            content = txt_path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        if pc.is_transcript_format(content):
            candidates.append(txt_path)
    return candidates


def score_candidates(candidates: list[Path]) -> list[dict]:
    sizes = {p: p.stat().st_size for p in candidates}
    mtimes = {p: p.stat().st_mtime for p in candidates}
    max_size = max(sizes.values())
    max_mtime = max(mtimes.values())

    scored = []
    for p in candidates:
        content = p.read_text(encoding="utf-8-sig")
        content_lower = content.lower()
        is_largest = sizes[p] == max_size
        is_newest = mtimes[p] == max_mtime
        is_complete = INTRO_MARKER in content_lower and ENDING_MARKER in content_lower
        score = int(is_largest) + int(is_newest) + int(is_complete)
        scored.append({
            "path": p,
            "size": sizes[p],
            "mtime": mtimes[p],
            "is_largest": is_largest,
            "is_newest": is_newest,
            "is_complete": is_complete,
            "score": score,
        })

    scored.sort(key=lambda r: (r["score"], r["mtime"], r["size"]), reverse=True)
    return scored


def process_best(best: dict, folder: Path, args) -> tuple[list[dict], list[str], int]:
    content = best["path"].read_text(encoding="utf-8-sig")
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    rows = pc.parse_transcript(content)
    rows, warnings = pc.assign_phases(rows)

    dropped = 0
    if not args.keep_fillers:
        rows, dropped = pc.apply_filler_cleaning(rows)

    if args.phases:
        requested = [p.strip() for p in args.phases.split(",")]
        rows = [r for r in rows if r["phase"] in requested]

    output_path = best["path"].with_suffix(".csv")
    pc.write_transcript_csv(rows, str(output_path))
    return rows, warnings, dropped


def write_log(folder: Path, best: dict, second_best: dict | None,
              scored: list[dict], warnings: list[str], dropped: int,
              row_count: int, output_path: Path) -> Path:
    lines = [f"Batch processing log for: {folder}", ""]

    lines.append("Candidates found:")
    for r in scored:
        marker = " <== BEST" if r is best else (" <== second-best" if r is second_best else "")
        lines.append(
            f"  {r['path'].name}\n"
            f"      size={r['size']}  mtime={r['mtime']:.0f}  "
            f"score={r['score']}/3 (largest={r['is_largest']}, newest={r['is_newest']}, "
            f"complete={r['is_complete']}){marker}"
        )
    lines.append("")

    lines.append(f"Selected: {best['path'].name} (score {best['score']}/3)")
    if best["score"] < 3:
        lines.append(
            f"NOTE: best-scoring transcript was only {best['score']}/3 "
            f"— treat this file's completeness with caution."
        )
    if second_best is not None:
        lines.append(f"Second-best kept: {second_best['path'].name} (score {second_best['score']}/3)")
    else:
        lines.append("No second-best candidate available (only one transcript found).")
    lines.append("")

    if warnings:
        lines.append("Phase-assignment warnings:")
        for w in warnings:
            lines.append(f"  - {w}")
        lines.append("")

    if dropped:
        lines.append(f"Filler cleaning: {dropped} row(s) dropped.")
        lines.append("")

    lines.append(f"Rows written: {row_count}")
    lines.append(f"CSV output: {output_path}")

    log_path = best["path"].with_name(best["path"].stem + "_batch_log.txt")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def process_folder(folder: Path, args) -> None:
    print(f"\n=== {folder.name} ===")
    candidates = find_transcript_candidates(folder)
    if not candidates:
        print(f"WARNING: no transcript-format .txt files found in {folder}", file=sys.stderr)
        return

    scored = score_candidates(candidates)
    best = scored[0]
    second_best = scored[1] if len(scored) > 1 else None

    if best["score"] < 2:
        print(
            f"WARNING: no transcript in {folder} scored 2/3 or better "
            f"(best was {best['path'].name} at {best['score']}/3); processing it anyway.",
            file=sys.stderr,
        )
    elif best["score"] == 2:
        print(f"Note: best transcript ({best['path'].name}) scored only 2/3.")

    rows, warnings, dropped = process_best(best, folder, args)
    output_path = best["path"].with_suffix(".csv")
    log_path = write_log(folder, best, second_best, scored, warnings, dropped, len(rows), output_path)

    print(f"Selected: {best['path'].name} (score {best['score']}/3)")
    print(f"Wrote: {output_path}")
    print(f"Log:   {log_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Auto-select and process the correct transcript in each group folder.",
    )
    parser.add_argument("parent_dir", help="Directory containing group folders (e.g. G15, G18, ...)")
    parser.add_argument("--keep-fillers", action="store_true", help="Disable filler language cleaning")
    parser.add_argument("--phases", help="Comma-separated list of phases to keep")
    args = parser.parse_args()

    parent = Path(args.parent_dir)
    if not parent.is_dir():
        print(f"Error: not a directory: {parent}", file=sys.stderr)
        sys.exit(1)

    folders = sorted(
        p for p in parent.iterdir()
        if p.is_dir() and not p.name.startswith((".", "__"))
    )
    if not folders:
        print(f"Error: no subfolders found in {parent}", file=sys.stderr)
        sys.exit(1)

    for folder in folders:
        process_folder(folder, args)


if __name__ == "__main__":
    main()
