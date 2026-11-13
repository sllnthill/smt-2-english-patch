-- Starter queries for data/script.db
-- Run all:  sqlite3 data/script.db < sql/explore.sql
-- Or open the db in DB Browser for SQLite and run them one at a time.

.headers on
.mode column

-- 1. How much text came from each game file?
SELECT source_file,
       COUNT(*)                         AS lines,
       SUM(byte_length)                 AS bytes,
       ROUND(AVG(char_count), 1)        AS avg_chars,
       SUM(duplicate_of IS NOT NULL AND is_pointer_reuse = 0) AS duplicates,
       SUM(is_pointer_reuse)            AS reused_pointers
FROM lines
GROUP BY source_file
ORDER BY bytes DESC;

-- 2. Length distribution: how many lines fall in each size bucket?
SELECT ((char_count / 10) * 10) || '-' || ((char_count / 10) * 10 + 9) AS char_range,
       COUNT(*) AS lines
FROM lines
WHERE is_empty = 0
GROUP BY char_count / 10
ORDER BY char_count / 10;

-- 3. The 10 longest lines (the hardest to fit in a text box once translated)
SELECT line_id, source_file, string_index, char_count, line_breaks,
       substr(replace(text_clean, char(10), ' / '), 1, 40) AS preview
FROM lines
ORDER BY char_count DESC
LIMIT 10;

-- 4. Which control codes appear, and how often?
SELECT code, COUNT(*) AS uses, COUNT(DISTINCT line_id) AS lines_using
FROM control_codes
GROUP BY code
ORDER BY uses DESC;

-- 5. Data quality: lines with bytes the table couldn't decode
SELECT source_file, COUNT(*) AS lines_with_unknown_bytes,
       SUM(unknown_byte_count) AS unknown_bytes
FROM lines
WHERE unknown_byte_count > 0
GROUP BY source_file;

-- 6. Data quality: empty strings and bad pointers
--    (bad pointers are stored as empty rows, so count them separately)
SELECT source_file,
       SUM(is_empty = 1 AND is_bad_pointer = 0) AS empty_lines,
       SUM(is_bad_pointer)                      AS bad_pointers
FROM lines
GROUP BY source_file;

-- 7. Most repeated lines (same text, whether stored again or reused by pointer)
SELECT l.duplicate_of AS first_line_id, COUNT(*) + 1 AS copies,
       substr(f.text_clean, 1, 40) AS preview
FROM lines l JOIN lines f ON f.line_id = l.duplicate_of
GROUP BY l.duplicate_of
ORDER BY copies DESC
LIMIT 10;

-- 8. Script makeup: kana vs kanji vs Latin characters
SELECT SUM(kana_count)  AS kana,
       SUM(kanji_count) AS kanji,
       SUM(latin_count) AS latin,
       ROUND(100.0 * SUM(kanji_count) /
             NULLIF(SUM(kana_count + kanji_count + latin_count), 0), 1) AS pct_kanji
FROM lines;
