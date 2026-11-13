#!/usr/bin/env python3
"""
summary.py - write a Markdown summary of data/script.db.

    python src/summary.py --db data/script.db --out reports/summary.md

The report holds only counts and statistics, never the game's text,
so it is safe to commit and share.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
from datetime import date


def q(con, sql, *args):
    return con.execute(sql, args).fetchall()


def table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---:" if i else "---" for i in range(len(headers))) + "|"]
    for r in rows:
        out.append("| " + " | ".join(f"{v:,}" if isinstance(v, int) else str(v)
                                     for v in r) + " |")
    return "\n".join(out)


def build(db: str) -> str:
    con = sqlite3.connect(db)
    total, bytes_, files = q(con, "SELECT COUNT(*), COALESCE(SUM(byte_length),0), "
                                  "COUNT(DISTINCT source_file) FROM lines")[0]
    if total == 0:
        return "# Script summary\n\nNo lines loaded yet.\n"
    unique = q(con, "SELECT COUNT(*) FROM lines WHERE duplicate_of IS NULL AND is_empty=0")[0][0]
    lengths = [r[0] for r in q(con, "SELECT char_count FROM lines WHERE is_empty=0")]
    unknown_lines, unknown_bytes = q(con, "SELECT SUM(unknown_byte_count>0), "
                                          "COALESCE(SUM(unknown_byte_count),0) FROM lines")[0]
    # bad pointers are stored as empty rows; count them once, as bad pointers
    empty, bad, dups, reused = q(con, """SELECT
            SUM(is_empty = 1 AND is_bad_pointer = 0),
            SUM(is_bad_pointer),
            SUM(duplicate_of IS NOT NULL AND is_pointer_reuse = 0),
            SUM(is_pointer_reuse)
        FROM lines""")[0]
    kana, kanji, latin = q(con, "SELECT SUM(kana_count), SUM(kanji_count), "
                                "SUM(latin_count) FROM lines")[0]
    chars = (kana or 0) + (kanji or 0) + (latin or 0)

    def pct(n, d):
        return f"{100 * n / d:.1f}%" if d else "n/a"

    md = [f"# Script summary\n\n_Generated {date.today().isoformat()} from `{os.path.basename(db)}`._\n",
          "## Overview\n",
          table(["Metric", "Value"], [
              ("Game files", files),
              ("Lines", total),
              ("Unique lines", unique),
              ("Bytes of text", bytes_),
              ("Median line length (chars)", int(statistics.median(lengths)) if lengths else 0),
              ("Longest line (chars)", max(lengths) if lengths else 0),
          ]), "",
          "## By game file\n",
          table(["File", "Lines", "Bytes", "Avg chars", "Duplicates"],
                q(con, """SELECT source_file, COUNT(*), SUM(byte_length),
                          ROUND(AVG(char_count),1),
                          SUM(duplicate_of IS NOT NULL AND is_pointer_reuse = 0)
                          FROM lines GROUP BY source_file ORDER BY 3 DESC""")), "",
          "## Line length distribution\n",
          table(["Characters", "Lines"],
                q(con, """SELECT ((char_count/10)*10) || '-' || ((char_count/10)*10+9),
                          COUNT(*) FROM lines WHERE is_empty=0
                          GROUP BY char_count/10 ORDER BY char_count/10""")), "",
          "## Control codes\n",
          table(["Code", "Uses", "Lines using it"],
                q(con, """SELECT '`' || code || '`', COUNT(*), COUNT(DISTINCT line_id)
                          FROM control_codes GROUP BY code ORDER BY 2 DESC LIMIT 20""")
                or [("(none)", 0, 0)]), "",
          "## Data quality\n",
          table(["Check", "Count", "Share of lines"], [
              ("Lines with undecoded bytes", unknown_lines or 0, pct(unknown_lines or 0, total)),
              ("Undecoded bytes", unknown_bytes, ""),
              ("Empty lines", empty or 0, pct(empty or 0, total)),
              ("Bad pointers", bad or 0, pct(bad or 0, total)),
              ("Duplicate lines (same text stored twice)", dups or 0, pct(dups or 0, total)),
              ("Reused pointers (same stored text used again)", reused or 0,
               pct(reused or 0, total)),
          ]), "",
          "## Character makeup\n",
          table(["Type", "Characters", "Share"], [
              ("Kana", kana or 0, pct(kana or 0, chars)),
              ("Kanji", kanji or 0, pct(kanji or 0, chars)),
              ("Latin letters/digits", latin or 0, pct(latin or 0, chars)),
          ]), ""]
    con.close()
    return "\n".join(md)


def main() -> None:
    ap = argparse.ArgumentParser(description="Markdown summary of the script database")
    ap.add_argument("--db", default="data/script.db")
    ap.add_argument("--out", default="reports/summary.md")
    a = ap.parse_args()
    report = build(a.db)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
