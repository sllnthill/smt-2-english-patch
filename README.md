# SMT II Script Audit

A data pipeline that extracts the dialogue from the PlayStation release of
*Shin Megami Tensei II* (Atlus, 2002, Japan-only) and loads it into a SQLite
database for analysis. The end goal is a quality audit of the open-source
English fan translation: finding untranslated lines, text that overflows its
text box, broken control codes, and inconsistent terminology.

**Status: in progress.** Stage 1 (extract → clean → load → summarize) works.
Stage 2 (matching the English translation and running the audit) is next.

## Pipeline

```
disc image (.bin)
   │  romscript.py disc extract     pull the files off the PS1 disc
   ▼
game files (SLPS_xxx.xx, data files)
   │  romscript.py ptrdump -f json  follow pointer tables, decode Shift-JIS
   ▼
JSON dumps (one per game file)
   │  load_script.py                clean text, profile each line, load
   ▼
SQLite: extract_runs · lines · control_codes
   │  sql/explore.sql, summary.py   query and report
   ▼
reports/summary.md                  statistics only, no game text
```

## Database

| Table | One row per | Key columns |
|---|---|---|
| `extract_runs` | JSON dump loaded | source file, SHA-256 of the dump, load time |
| `lines` | line of dialogue | raw and clean text, byte length, char count, kana/kanji/Latin counts, duplicate and data-quality flags |
| `control_codes` | control code occurrence | code, position in the line |

Full schema: [`sql/schema.sql`](sql/schema.sql).

## How to run

Requires Python 3.8+ and nothing else (standard library only). You need
your own copy of the game as a MODE2/2352 `.bin` image.

```bash
# 1. list and extract the files on the disc
python src/romscript.py disc list "data/SMT2.bin"
python src/romscript.py disc extract "data/SMT2.bin" -o data/files

# 2. find text and pointer tables in a file
python src/romscript.py strings data/files/SLPS_XXX.XX -e sjis
python src/romscript.py ptrscan data/files/SLPS_XXX.XX -m psxexe

# 3. dump a pointer table to JSON (repeat per table / file)
python src/romscript.py ptrdump data/files/SLPS_XXX.XX --sjis -m psxexe \
       -p 0xTABLE -c COUNT -f json -o data/dumps/main.json

# 4. load into SQLite and summarize
python src/load_script.py data/dumps/main.json=SLPS_XXX.XX --db data/script.db
python src/summary.py --db data/script.db --out reports/summary.md

# 5. explore
sqlite3 data/script.db < sql/explore.sql   # or open it in DB Browser for SQLite
```

Run the tests (builds a small fake disc; no game data needed):

```bash
python tests/test_pipeline.py
```

## Roadmap

- [x] Extract files from the PS1 disc image
- [x] Decode Shift-JIS dialogue through pointer tables
- [x] Clean and load into SQLite with lineage and data-quality flags
- [x] Summary report and starter SQL queries
- [ ] Load the English translation and match it to the Japanese lines
- [ ] Audit checks: untranslated lines, overflow, control-code mismatches,
      terminology consistency
- [ ] Dashboard of findings
- [ ] Report confirmed issues to the translation project

## A note on data

The game's script belongs to Atlus. This repository contains code only:
the disc image, extracted files, JSON dumps and database are kept out of
git (see `.gitignore`). Published reports contain statistics, not text.

## Credits

- English fan translation audited:
  [Roman215/smt2-psx-translation](https://github.com/Roman215/smt2-psx-translation) (MIT)
- *Shin Megami Tensei II* © Atlus. This is an unofficial fan project.
