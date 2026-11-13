#!/usr/bin/env python3
"""
load_script.py - load romscript JSON dumps into a SQLite database.

Each input is a JSON file written by:
    python src/romscript.py ptrdump ... -f json -o data/dumps/NAME.json
    python src/romscript.py dump    ... -f json -o data/dumps/NAME.json

Usage:
    python src/load_script.py data/dumps/*.json --db data/script.db
    python src/load_script.py data/dumps/main.json=SLPS_xxx.xx --db data/script.db

Append "=SOURCE" to a path to record which game file it came from;
otherwise the JSON file name (without .json) is used.

Standard library only (Python 3.8+). Rebuilds the database each run, so it
is safe to re-run after changing the dumps.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from typing import Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "..", "sql", "schema.sql")

# control codes as romscript writes them: [F3] for unknown bytes,
# <END>, <WAIT> etc. for codes named in a .tbl file
CODE_RE = re.compile(r"\[[0-9A-F]{2}\]|<[^<>\n]{1,24}>")
UNKNOWN_RE = re.compile(r"\[[0-9A-F]{2}\]")
# romscript's own placeholders, not game text
META_RE = re.compile(r"^<(SAME AS \[\d+\]|BAD POINTER)>$")


def char_kind(ch: str) -> str:
    cp = ord(ch)
    if 0x3040 <= cp <= 0x30FF or 0xFF66 <= cp <= 0xFF9F:
        return "kana"
    if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
        return "kanji"
    if ch.isascii() and ch.isalnum():
        return "latin"
    if 0xFF10 <= cp <= 0xFF5A and unicodedata.category(ch)[0] in "LN":
        return "latin"   # fullwidth Ａ-ｚ, ０-９
    return "other"


def clean_text(raw: str) -> str:
    text = CODE_RE.sub("", raw)
    text = text.replace("　", " ")                # fullwidth space
    text = "\n".join(" ".join(part.split()) for part in text.split("\n"))
    return text.strip()


def profile(raw: str) -> dict:
    clean = clean_text(raw)
    kinds = {"kana": 0, "kanji": 0, "latin": 0, "other": 0}
    for ch in clean:
        if not ch.isspace():
            kinds[char_kind(ch)] += 1
    return dict(
        text_clean=clean,
        char_count=len(clean.replace("\n", "")),
        line_breaks=raw.count("\n"),
        control_code_count=len(CODE_RE.findall(raw)),
        unknown_byte_count=len(UNKNOWN_RE.findall(raw)),
        kana_count=kinds["kana"],
        kanji_count=kinds["kanji"],
        latin_count=kinds["latin"],
        is_empty=int(clean == ""),
    )


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def parse_input(arg: str) -> Tuple[str, str]:
    if "=" in arg and not os.path.exists(arg):
        path, source = arg.split("=", 1)
    else:
        path, source = arg, os.path.splitext(os.path.basename(arg))[0]
    if not os.path.exists(path):
        sys.exit(f"not found: {path}")
    return path, source


def load(inputs: List[str], db_path: str) -> Dict[str, int]:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    con = sqlite3.connect(db_path)
    with open(SCHEMA, encoding="utf-8") as f:
        con.executescript(f.read())

    totals = {"files": 0, "lines": 0, "codes": 0, "skipped_refs": 0}
    first_seen: Dict[str, int] = {}          # text_clean -> line_id
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for arg in inputs:
        path, source = parse_input(arg)
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
        if not isinstance(records, list):
            sys.exit(f"{path}: expected a JSON list from romscript -f json")

        cur = con.execute(
            "INSERT INTO extract_runs (source_file, dump_path, dump_sha256, line_count, loaded_at)"
            " VALUES (?, ?, ?, 0, ?)", (source, path, sha256(path), now))
        run_id = cur.lastrowid
        n = 0
        for r in records:
            raw = r.get("text", "")
            # "<SAME AS [0003]>" is romscript pointing at a line it already
            # dumped - a reuse, not a new line of dialogue
            if META_RE.match(raw) and raw != "<BAD POINTER>":
                totals["skipped_refs"] += 1
                continue
            bad = int(raw == "<BAD POINTER>")
            p = profile("" if bad else raw)
            dup = None if p["is_empty"] else first_seen.get(p["text_clean"])
            cur = con.execute(
                """INSERT INTO lines (run_id, source_file, string_index, file_offset,
                       pointer_at, pointer_value, text_raw, text_clean, byte_length,
                       char_count, line_breaks, control_code_count, unknown_byte_count,
                       kana_count, kanji_count, latin_count, is_empty, is_bad_pointer,
                       duplicate_of)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, source, r.get("index"), r.get("offset"), r.get("pointer_at"),
                 r.get("pointer"), raw, p["text_clean"], r.get("length", 0),
                 p["char_count"], p["line_breaks"], p["control_code_count"],
                 p["unknown_byte_count"], p["kana_count"], p["kanji_count"],
                 p["latin_count"], p["is_empty"], bad, dup))
            line_id = cur.lastrowid
            if dup is None and not p["is_empty"]:
                first_seen[p["text_clean"]] = line_id
            codes = [(line_id, m.group(0), m.start()) for m in CODE_RE.finditer(raw)]
            con.executemany("INSERT INTO control_codes VALUES (?,?,?)", codes)
            totals["codes"] += len(codes)
            n += 1
        con.execute("UPDATE extract_runs SET line_count=? WHERE run_id=?", (n, run_id))
        totals["files"] += 1
        totals["lines"] += n
        print(f"  {source}: {n} lines from {path}")

    con.commit()
    con.close()
    return totals


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("inputs", nargs="+", help="romscript JSON dumps (PATH or PATH=SOURCE)")
    ap.add_argument("--db", default="data/script.db", help="SQLite file to (re)build")
    a = ap.parse_args()
    t = load(a.inputs, a.db)
    print(f"loaded {t['lines']} lines and {t['codes']} control codes from "
          f"{t['files']} file(s) into {a.db}"
          + (f" ({t['skipped_refs']} repeated pointers skipped)" if t["skipped_refs"] else ""))


if __name__ == "__main__":
    main()
