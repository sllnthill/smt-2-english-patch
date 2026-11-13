-- SMT II script audit database
-- One row per extraction run, one row per dialogue line, one row per
-- control code occurrence. Rebuilt from scratch by src/load_script.py.

PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS control_codes;
DROP TABLE IF EXISTS lines;
DROP TABLE IF EXISTS extract_runs;

-- Data lineage: where each batch of lines came from.
CREATE TABLE extract_runs (
    run_id        INTEGER PRIMARY KEY,
    source_file   TEXT NOT NULL,          -- game file the text came from, e.g. SLPS_xxx.xx
    dump_path     TEXT NOT NULL,          -- romscript JSON file that was loaded
    dump_sha256   TEXT NOT NULL,          -- fingerprint of that JSON
    line_count    INTEGER NOT NULL,
    loaded_at     TEXT NOT NULL           -- ISO 8601 timestamp
);

CREATE TABLE lines (
    line_id            INTEGER PRIMARY KEY,
    run_id             INTEGER NOT NULL REFERENCES extract_runs(run_id),
    source_file        TEXT NOT NULL,
    string_index       INTEGER NOT NULL,  -- position in the pointer table / dump
    file_offset        INTEGER,           -- where the text starts in the file
    pointer_at         INTEGER,           -- where its pointer is (NULL for range dumps)
    pointer_value      INTEGER,
    text_raw           TEXT NOT NULL,     -- as dumped, control codes included
    text_clean         TEXT NOT NULL,     -- control codes removed, whitespace tidied
    byte_length        INTEGER NOT NULL,  -- bytes used in the game file
    char_count         INTEGER NOT NULL,  -- characters in text_clean
    line_breaks        INTEGER NOT NULL,  -- explicit line breaks inside the string
    control_code_count INTEGER NOT NULL,
    unknown_byte_count INTEGER NOT NULL,  -- [XX] bytes the table didn't cover
    kana_count         INTEGER NOT NULL,
    kanji_count        INTEGER NOT NULL,
    latin_count        INTEGER NOT NULL,  -- ASCII + fullwidth letters/digits
    is_empty           INTEGER NOT NULL,  -- 1 if nothing left after cleaning (always 1 for bad pointers)
    is_bad_pointer     INTEGER NOT NULL,  -- 1 if the pointer led outside the file
    is_pointer_reuse   INTEGER NOT NULL,  -- 1 if this slot points at text an earlier slot already used
                                          --   (text copied from that line, byte_length 0)
    duplicate_of       INTEGER REFERENCES lines(line_id)  -- first line with identical text
);

CREATE TABLE control_codes (
    line_id   INTEGER NOT NULL REFERENCES lines(line_id),
    code      TEXT NOT NULL,              -- e.g. <END>, [F3]
    position  INTEGER NOT NULL            -- character position in text_raw
);

CREATE INDEX idx_lines_source ON lines(source_file);
CREATE INDEX idx_lines_clean  ON lines(text_clean);
CREATE INDEX idx_codes_code   ON control_codes(code);
