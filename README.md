# SMT II Script Audit

**In one sentence:** this project reads all the Japanese dialogue out of the
PlayStation version of *Shin Megami Tensei II* and puts it in a searchable
list, so that an English fan translation of the game can be checked line by
line for mistakes.

> **This is not a translation patch yet.** Nothing here changes the game or
> makes it playable in English. It is the groundwork: getting the original
> text out cleanly so a translation can be checked against it. See
> [Where this is going](#where-this-is-going).

## Why this exists

*Shin Megami Tensei II* was only ever released in Japan. Fans have written an
English translation of the PlayStation version
([Roman215/smt2-psx-translation](https://github.com/Roman215/smt2-psx-translation)).
A fan translation of a game this size has thousands of lines, and
some kinds of mistake are very hard to spot by playing:

- a line that was never translated and still shows Japanese
- English text that is too long and spills out of its text box
- a broken instruction inside a line (the game hides commands such as
  "wait for a button press" or "insert the hero's name" inside its dialogue)
- the same name or term translated two different ways in different places

A computer can find all of these, but only if it has every original line and
every translated line side by side. This project builds the first half of
that: the original lines.

## What works today

| Step | What it does | Status |
|---|---|---|
| 1. Open the disc | Reads a copy of the game disc and pulls out the files on it | Works |
| 2. Find the text | Locates the dialogue inside those files and turns it into readable Japanese | Works |
| 3. Build the list | Stores every line in a small database with facts about it (length, special codes, repeats) | Works |
| 4. Summarize | Writes a one-page report of counts and statistics | Works |
| 5. Add the English | Loads the fan translation and pairs each English line with its Japanese line | Not started |
| 6. Run the checks | Finds untranslated lines, overflowing text, broken codes, inconsistent terms | Not started |
| 7. Share the findings | Reports confirmed problems to the translation project | Not started |

"Works" means the step runs correctly on a small made-up disc used for
testing. A summary from the real game has not been published in this
repository yet.

## What you need to use it

- Your own copy of the game, saved as a disc image file (a `.bin` file).
  **The game is not included** and never will be.
- [Python](https://www.python.org/downloads/) 3.8 or newer. Nothing else
  needs to be installed.
- Some comfort with typing commands. Steps 2 and 3 below are detective work:
  you have to find where the text lives inside the game's files, and that
  takes trial and error.

## Words used on this page

| Word | Meaning |
|---|---|
| Disc image | A single file that is an exact copy of a game disc. |
| Script | All of the game's written text: dialogue, menus, item names. |
| Line | One piece of text the game shows at a time, such as one speech box. |
| Control code | A hidden instruction inside a line, such as "new line" or "wait for a button press". It is not shown on screen. |
| Pointer table | The game's own index of where each line is stored. Following it is how the lines are found in the right order. |
| Shift-JIS | The standard way Japanese text was stored on computers of that era. |
| Database | A file that holds the lines as a table, like a spreadsheet you can search and count. |

---

## For people running it

Everything below is technical.

### How the pieces fit together

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

### Commands

Words in capitals (`SLPS_XXX.XX`, `0xTABLE`, `COUNT`) are placeholders. The
real values come from the output of the step before.

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

The disc image must be in MODE2/2352 format (the usual `.bin` of a
`.bin`/`.cue` pair) or a plain 2048-byte-sector `.iso`.

### The database

| Table | One row per | Key columns |
|---|---|---|
| `extract_runs` | JSON dump loaded | source file, SHA-256 of the dump, load time |
| `lines` | pointer-table slot (one line of dialogue) | raw and clean text, byte length, character counts, flags for empty, bad pointer, reused pointer, and duplicate text |
| `control_codes` | control code occurrence | code, position in the line |

Every slot in a pointer table gets a row, including slots that point at text
an earlier slot already used (`is_pointer_reuse = 1`). This keeps row order
identical to the game's own order, which step 5 depends on.

Full schema: [`sql/schema.sql`](sql/schema.sql).

### Tests

```bash
python tests/test_pipeline.py
```

This builds a small fake disc and runs the whole pipeline on it. No game
data is needed.

### Known limits

- **Control codes are not mapped yet.** Without a table file describing the
  game's codes, every byte that is not text is written as `[XX]` and counted
  as an "undecoded byte". Until the codes are mapped, that count mixes real
  control codes with genuine decoding problems, and a code that carries extra
  data can be misread as text.
- **Text in angle brackets.** A piece of game text that looks like a code
  name, such as `<HP>`, is treated as a control code.
- **Only the PlayStation path is tested.** `romscript.py` also has options
  for other consoles; those are untested here.

## Where this is going

1. **Now:** audit groundwork (this repository).
2. **Next:** load the English translation, match it to the Japanese, and run
   the checks listed under [Why this exists](#why-this-exists).
3. **Later, if wanted:** turn this into a real patch project. That needs
   tools this repository does not have: putting changed text back into the
   game files, rebuilding the disc image, and producing a patch file that
   players apply to their own copy of the game.

## A note on the game's text

The game's script belongs to Atlus. This repository contains code only. The
disc image, extracted files, dumps and database stay on your computer and
are blocked from being uploaded (see `.gitignore`). Published reports contain
statistics, never the text itself.

## License and credits

- The code is MIT licensed (see [`LICENSE`](LICENSE)). The license covers
  this repository's code only, not the game or its script.
- English fan translation being audited:
  [Roman215/smt2-psx-translation](https://github.com/Roman215/smt2-psx-translation) (MIT)
- *Shin Megami Tensei II* © Atlus. This is an unofficial fan project.
