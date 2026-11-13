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


def run(*args) -> str:
    r = subprocess.run([sys.executable, "-I", *args], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"FAILED: {' '.join(args)}\n{r.stdout}{r.stderr}")
    return r.stdout + r.stderr


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
        assert n == 4, f"expected 4 lines (1 repeated pointer skipped), got {n}"
        codes = dict(con.execute("SELECT code, COUNT(*) FROM control_codes GROUP BY code"))
        assert codes == {"<END>": 4, "[01]": 1}, codes
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
        assert "| Lines | 4 |" in text, text
        assert "旅" not in text, "report must not contain game text"
    print("all pipeline tests passed")


if __name__ == "__main__":
    main()
