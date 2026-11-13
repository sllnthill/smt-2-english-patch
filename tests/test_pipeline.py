#!/usr/bin/env python3
"""
End-to-end test on a small fake PS1 disc (no game data involved):
  build disc -> romscript disc extract -> ptrdump to JSON
  -> load_script into SQLite -> summary report

    python tests/test_pipeline.py
"""

import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
LOAD_ADDR = 0x80010000

# made-up placeholder lines, including a repeat, a control code and a
# byte the decoder won't know
LINES = [
    "こんにちは、旅の人。".encode("cp932"),
    "ここは　テスト　の町です。".encode("cp932") + b"\x01" + "次へ".encode("cp932"),
    "こんにちは、旅の人。".encode("cp932"),
    "ポーション　を　手に入れた！".encode("cp932"),
    "さようなら".encode("cp932"),
]


def make_exe() -> bytes:
    body = bytearray(0x600)
    ptr_table, pos = 0x100, 0x200
    seen = {}
    for i, raw in enumerate(LINES):
        if raw not in seen:
            seen[raw] = pos
            body[pos:pos + len(raw) + 1] = raw + b"\0"
            pos += len(raw) + 1
        struct.pack_into("<I", body, ptr_table + 4 * i, LOAD_ADDR + seen[raw])
    header = bytearray(0x800)
    header[:8] = b"PS-X EXE"
    struct.pack_into("<I", header, 0x18, LOAD_ADDR)
    return bytes(header + body)


def make_disc(path: str, exe: bytes) -> None:
    def both16(v): return struct.pack("<H", v) + struct.pack(">H", v)
    def both32(v): return struct.pack("<I", v) + struct.pack(">I", v)

    def rec(name: bytes, lba, size, is_dir):
        r = bytearray(33 + len(name) + (len(name) % 2 == 0))
        r[0] = len(r); r[2:10] = both32(lba); r[10:18] = both32(size)
        r[25] = 2 if is_dir else 0; r[28:32] = both16(1)
        r[32] = len(name); r[33:33 + len(name)] = name
        return bytes(r)

    exe_lba, total = 24, 24 + (len(exe) + 2047) // 2048 + 2
    user = [bytes(2048)] * total
    pvd = bytearray(2048); pvd[0] = 1; pvd[1:6] = b"CD001"; pvd[6] = 1
    pvd[80:88] = both32(total); pvd[156:190] = rec(b"\0", 20, 2048, True)
    user[16] = bytes(pvd)
    term = bytearray(2048); term[0] = 255; term[1:6] = b"CD001"; user[17] = bytes(term)
    root = (rec(b"\0", 20, 2048, True) + rec(b"\1", 20, 2048, True)
            + rec(b"SLPS_000.00;1", exe_lba, len(exe), False))
    user[20] = root.ljust(2048, b"\0")
    for k in range((len(exe) + 2047) // 2048):
        user[exe_lba + k] = exe[k * 2048:(k + 1) * 2048].ljust(2048, b"\0")
    sync = b"\x00" + b"\xff" * 10 + b"\x00"
    with open(path, "wb") as f:
        for i, u in enumerate(user):
            f.write(sync + bytes(3) + b"\x02" + bytes([0, 0, 8, 0]) * 2 + u + bytes(280))


def run(*args, expect_fail: bool = False) -> str:
    r = subprocess.run([sys.executable, "-I", *args], capture_output=True, text=True)
    if bool(r.returncode) != expect_fail:
        sys.exit(f"FAILED: {' '.join(args)}\n{r.stdout}{r.stderr}")
    return r.stdout + r.stderr


def test_loader_edge_cases(tmp: str) -> None:
    """Hand-written dump covering what the fake disc can't: a bad pointer,
    angle-bracket game text, and a failed load leaving the database alone."""
    dump = os.path.join(tmp, "edge.json")
    db = os.path.join(tmp, "edge.db")
    report = os.path.join(tmp, "edge.md")
    records = [
        dict(index=0, offset=16, pointer_at=0, pointer=1, length=7, text="あいう<END>"),
        dict(index=1, offset=-5, pointer_at=4, pointer=2, length=0, text="<BAD POINTER>"),
        dict(index=2, offset=32, pointer_at=8, pointer=3, length=7, text="あいう<END>"),
        dict(index=3, offset=48, pointer_at=12, pointer=4, length=9, text="<1+1=2> です<END>"),
    ]
    with open(dump, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    run(os.path.join(SRC, "load_script.py"), dump, "--db", db)

    con = sqlite3.connect(db)
    bad = con.execute("SELECT line_id, text_raw, is_empty, control_code_count "
                      "FROM lines WHERE is_bad_pointer=1").fetchall()
    assert len(bad) == 1 and bad[0][1:] == ("", 1, 0), bad
    n = con.execute("SELECT COUNT(*) FROM control_codes WHERE line_id=?",
                    (bad[0][0],)).fetchone()[0]
    assert n == 0, "a bad pointer must not be recorded as a control code"
    codes = dict(con.execute("SELECT code, COUNT(*) FROM control_codes GROUP BY code"))
    assert codes == {"<END>": 3}, codes
    clean = con.execute("SELECT text_clean FROM lines WHERE string_index=3").fetchone()[0]
    assert clean == "<1+1=2> です", clean      # not a control code: kept as text
    dup = con.execute("SELECT duplicate_of, is_pointer_reuse FROM lines "
                      "WHERE string_index=2").fetchone()
    assert dup == (1, 0), dup                   # same text stored twice
    con.close()

    run(os.path.join(SRC, "summary.py"), "--db", db, "--out", report)
    text = open(report, encoding="utf-8").read()
    assert "| Empty lines | 0 |" in text, text
    assert "| Bad pointers | 1 |" in text, text
    assert "| Duplicate lines (same text stored twice) | 1 |" in text, text

    # a missing input must fail before the existing database is rebuilt
    run(os.path.join(SRC, "load_script.py"), dump, os.path.join(tmp, "missing.json"),
        "--db", db, expect_fail=True)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 4
    con.close()


def test_unsafe_disc_names() -> None:
    sys.path.insert(0, SRC)
    from romscript import safe_disc_path
    assert safe_disc_path("SLPS_000.00") and safe_disc_path("DATA/FILE.BIN")
    for name in ("..", "../x", "DATA/../../x", "/abs", "DATA//x", "C:x", "a\\..\\x"):
        assert not safe_disc_path(name), name


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        disc = os.path.join(tmp, "fake.bin")
        make_disc(disc, make_exe())
        files = os.path.join(tmp, "files")
        dump = os.path.join(tmp, "main.json")
        db = os.path.join(tmp, "script.db")
        report = os.path.join(tmp, "summary.md")

        run(os.path.join(SRC, "romscript.py"), "disc", "extract", disc, "-o", files)
        exe = os.path.join(files, "SLPS_000.00")
        run(os.path.join(SRC, "romscript.py"), "ptrdump", exe, "--sjis", "-m", "psxexe",
            "-p", "0x900", "-c", str(len(LINES)), "-f", "json", "-o", dump)
        assert len(json.load(open(dump, encoding="utf-8"))) == len(LINES)

        run(os.path.join(SRC, "load_script.py"), f"{dump}=SLPS_000.00", "--db", db)
        con = sqlite3.connect(db)
        n = con.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
        assert n == 5, f"expected one row per pointer-table slot, got {n}"
        reuse = con.execute("SELECT string_index, duplicate_of, byte_length, text_clean "
                            "FROM lines WHERE is_pointer_reuse=1").fetchall()
        assert reuse == [(2, 1, 0, "こんにちは、旅の人。")], reuse
        codes = dict(con.execute("SELECT code, COUNT(*) FROM control_codes GROUP BY code"))
        assert codes == {"<END>": 5, "[01]": 1}, codes
        clean = con.execute("SELECT text_clean FROM lines WHERE unknown_byte_count=1").fetchone()[0]
        assert clean == "ここは テスト の町です。次へ", clean
        kana, kanji = con.execute("SELECT SUM(kana_count), SUM(kanji_count) FROM lines").fetchone()
        assert kana > 0 and kanji > 0
        assert con.execute("SELECT source_file FROM extract_runs").fetchone()[0] == "SLPS_000.00"
        con.close()

        # the starter queries must all run
        with open(os.path.join(ROOT, "sql", "explore.sql"), encoding="utf-8") as f:
            sql = "\n".join(l for l in f.read().splitlines() if not l.startswith("."))
        con = sqlite3.connect(db)
        for stmt in filter(str.strip, sql.split(";")):
            if "SELECT" in stmt.upper():
                con.execute(stmt).fetchall()
        con.close()

        run(os.path.join(SRC, "summary.py"), "--db", db, "--out", report)
        text = open(report, encoding="utf-8").read()
        assert "| Lines | 5 |" in text, text
        assert "| Unique lines | 4 |" in text, text
        assert "| Reused pointers (same stored text used again) | 1 |" in text, text
        assert "旅" not in text, "report must not contain game text"

        test_loader_edge_cases(tmp)
    test_unsafe_disc_names()
    print("all pipeline tests passed")


if __name__ == "__main__":
    main()
