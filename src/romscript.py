#!/usr/bin/env python3
"""
romscript.py - pull the script (dialogue/text) out of game ROMs.

Pure standard library, Python 3.8+. One file, several subcommands:

  strings    Find plain ASCII or Shift-JIS text (no table needed).
  relsearch  Find text in a custom encoding by "relative search" and
             write a starter .tbl table file for it.
  dump       Decode a byte range with a .tbl file, split into strings.
  ptrdump    Read a pointer table and decode every string it points to.
  info       Show basic facts about a ROM (size, header, guessed system).
  disc       PS1: list / extract files from a .bin (MODE2/2352) or .iso.
  ptrscan    PS1: find pointer tables (runs of 0x80xxxxxx pointers).
  findptr    Find which pointers point at a given string offset.

PS1 quick start:
  romscript.py disc list game.bin
  romscript.py disc extract game.bin -o files
  romscript.py strings files/SLPS_xxx.xx -e sjis
  romscript.py ptrscan files/SLPS_xxx.xx -m psxexe
  romscript.py ptrdump files/SLPS_xxx.xx --sjis -m psxexe -p 0x... -c N

Table (.tbl) file format (the usual romhacking convention):
  80=A             one byte -> text
  8A81=ga          multi-byte sequences are fine (longest match wins)
  /FF=<END>        leading "/" marks an end-of-string code
  *FE              leading "*" marks a line break (optionally *FE=<br>)
  ; or # at line start = comment
Unknown bytes are written as [XX] so nothing is lost.

Run `python romscript.py <command> -h` for each command's options.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def parse_int(s: str) -> int:
    """Accept 0x1F, $1F, 1Fh, or decimal."""
    s = s.strip().replace("_", "")
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if s.lower().startswith("0x"):
        v = int(s[2:], 16)
    elif s.startswith("$"):
        v = int(s[1:], 16)
    elif s.lower().endswith("h"):
        v = int(s[:-1], 16)
    else:
        v = int(s, 10)
    return -v if neg else v


def read_rom(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def open_out(path: Optional[str]):
    if not path or path == "-":
        return sys.stdout
    return open(path, "w", encoding="utf-8", newline="")


def sjis_char(data: bytes, i: int, stop: int) -> Tuple[Optional[str], int]:
    """Decode one Shift-JIS character at data[i]. Returns (char, width in
    bytes), or (None, 0) if the bytes there are not a Shift-JIS character.
    Uses cp932, the superset real games were written against."""
    b = data[i]
    if 0x20 <= b < 0x7F or 0xA1 <= b <= 0xDF:      # ASCII, halfwidth katakana
        return data[i:i + 1].decode("cp932"), 1
    if (0x81 <= b <= 0x9F or 0xE0 <= b <= 0xFC) and i + 1 < stop:
        t = data[i + 1]
        if 0x40 <= t <= 0xFC and t != 0x7F:
            try:
                return data[i:i + 2].decode("cp932"), 2
            except UnicodeDecodeError:
                pass
    return None, 0


# --------------------------------------------------------------------------- #
# table files
# --------------------------------------------------------------------------- #

@dataclass
class Table:
    entries: Dict[bytes, str] = field(default_factory=dict)
    end_codes: Dict[bytes, str] = field(default_factory=dict)
    newline_codes: Dict[bytes, str] = field(default_factory=dict)
    max_len: int = 1
    sjis: bool = False      # fall back to Shift-JIS for bytes the table lacks

    @classmethod
    def load(cls, path: str) -> "Table":
        t = cls()
        with open(path, "rb") as f:
            raw = f.read()
        for enc in ("utf-8-sig", "shift_jis", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith((";", "#")):
                continue
            kind = None
            if line[0] in "/*":
                kind, line = line[0], line[1:]
            if "=" in line:
                hexpart, val = line.split("=", 1)
            else:
                hexpart, val = line, ""
            hexpart = hexpart.strip()
            try:
                key = bytes.fromhex(hexpart)
            except ValueError:
                print(f"warning: {path}:{lineno}: bad hex '{hexpart}', skipped",
                      file=sys.stderr)
                continue
            if not key:
                continue
            if kind == "/":
                t.end_codes[key] = val if val else "<END>"
            elif kind == "*":
                t.newline_codes[key] = val
            else:
                t.entries[key] = val
            t.max_len = max(t.max_len, len(key))
        return t

    @classmethod
    def for_args(cls, a) -> "Table":
        """Load -t TABLE if given; with --sjis, fall back to Shift-JIS for
        anything the table doesn't cover. --end-code adds an end code."""
        t = cls.load(a.table) if a.table else cls()
        t.sjis = bool(getattr(a, "sjis", False))
        if not a.table and not t.sjis:
            sys.exit("give a table with -t, or use --sjis for Shift-JIS text")
        for code in (getattr(a, "end_code", None) or []):
            key = bytes.fromhex(code)
            t.end_codes[key] = "<END>"
            t.max_len = max(t.max_len, len(key))
        if t.sjis and not t.end_codes:
            t.end_codes[b"\x00"] = "<END>"
        return t

    def decode(self, data: bytes, start: int, stop: int,
               stop_at_end: bool = True) -> Tuple[str, int]:
        """Decode data[start:stop]. Returns (text, offset after last byte used).
        If stop_at_end, stops right after the first end code."""
        out: List[str] = []
        i = start
        while i < stop:
            matched = False
            for n in range(min(self.max_len, stop - i), 0, -1):
                chunk = data[i:i + n]
                if chunk in self.end_codes:
                    out.append(self.end_codes[chunk])
                    i += n
                    if stop_at_end:
                        return "".join(out), i
                    out.append("\n")
                    matched = True
                    break
                if chunk in self.newline_codes:
                    out.append(self.newline_codes[chunk] + "\n")
                    i += n
                    matched = True
                    break
                if chunk in self.entries:
                    out.append(self.entries[chunk])
                    i += n
                    matched = True
                    break
            if not matched and self.sjis:
                ch, w = sjis_char(data, i, stop)
                if ch is not None:
                    out.append(ch)
                    i += w
                    matched = True
            if not matched:
                out.append(f"[{data[i]:02X}]")
                i += 1
        return "".join(out), i


# --------------------------------------------------------------------------- #
# address mapping (pointer value -> file offset)
# --------------------------------------------------------------------------- #

def snes_header_size(rom: bytes) -> int:
    return 512 if len(rom) % 1024 == 512 else 0


def map_pointer(ptr: int, mapping: str, add: int, header: int,
                bank: Optional[int]) -> int:
    if bank is not None:
        ptr |= bank << 16
    if mapping == "none":
        off = ptr
    elif mapping == "gba":
        off = ptr & 0x01FFFFFF          # strips 0x08/0x09 ROM region
    elif mapping == "lorom":
        off = ((ptr >> 16) & 0x7F) * 0x8000 + (ptr & 0x7FFF)
    elif mapping == "hirom":
        off = ptr & 0x3FFFFF
    elif mapping == "nes":
        # 16 KB bank switched into $8000-$BFFF; give --bank as the PRG bank
        b = (ptr >> 16) & 0xFF
        off = b * 0x4000 + ((ptr & 0xFFFF) - 0x8000)
    else:
        raise ValueError(mapping)
    return off + add + header


def psx_exe_load_addr(data: bytes) -> Optional[int]:
    """t_addr from a PS-X EXE header (file body starts at 0x800)."""
    if data[:8] == b"PS-X EXE" and len(data) >= 0x800:
        return int.from_bytes(data[0x18:0x1C], "little")
    return None


def linear_add(a, data: bytes) -> Tuple[str, int]:
    """For PS1-style (linear) mappings, turn --mapping/--load/--add into a
    plain 'file_offset = pointer + add'. Returns (mapping, add)."""
    add = parse_int(a.add) if getattr(a, "add", None) else 0
    m = a.mapping
    if m == "psxexe":
        t = psx_exe_load_addr(data)
        if t is None:
            sys.exit("--mapping psxexe: file has no 'PS-X EXE' header. For an "
                     "overlay or data file use --mapping psx --load ADDRESS")
        return "none", add + 0x800 - t
    if m == "psx":
        if not getattr(a, "load", None):
            sys.exit("--mapping psx needs --load (RAM address the file is "
                     "loaded at, e.g. 0x80100000)")
        return "none", add - parse_int(a.load)
    if m == "gba":
        return "none", add - 0x08000000
    return m, add


# --------------------------------------------------------------------------- #
# PS1 / CD disc images (.bin MODE2/2352, MODE1/2352, or 2048-byte .iso)
# --------------------------------------------------------------------------- #

SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


class Disc:
    def __init__(self, path: str):
        self.f = open(path, "rb")
        self.size = os.path.getsize(path)
        head = self.f.read(16)
        if head[:12] == SYNC and self.size % 2352 == 0:
            self.raw = True
            self.sector_size = 2352
        elif self.size % 2048 == 0:
            self.raw = False
            self.sector_size = 2048
        else:
            sys.exit(f"{path}: not a 2352- or 2048-byte-sector disc image. "
                     "Use the .bin (not the .cue), and only the data track.")
        self.sectors = self.size // self.sector_size

    def close(self) -> None:
        self.f.close()

    def raw_sector(self, lba: int) -> bytes:
        self.f.seek(lba * self.sector_size)
        return self.f.read(self.sector_size)

    def sector_info(self, lba: int) -> Tuple[bytes, bool]:
        """(user data, is_form2). Form 1 = 2048 bytes, Form 2 = 2324 bytes."""
        s = self.raw_sector(lba)
        if not self.raw:
            return s, False
        mode = s[15]
        if mode == 1:
            return s[16:16 + 2048], False
        submode = s[18]
        if submode & 0x20:                       # Mode 2 Form 2 (XA audio, STR video)
            return s[24:24 + 2324], True
        return s[24:24 + 2048], False

    def read(self, lba: int, size: int) -> bytes:
        out = bytearray()
        n = (size + 2047) // 2048
        for k in range(n):
            out += self.sector_info(lba + k)[0][:2048]
        return bytes(out[:size])

    # ISO 9660 ---------------------------------------------------------------
    def files(self) -> List[dict]:
        pvd = self.read(16, 2048)
        if pvd[1:6] != b"CD001":
            sys.exit("no ISO 9660 filesystem found at sector 16")
        result: List[dict] = []
        self._walk(pvd[156:156 + 34], "", result, set())
        return result

    def _walk(self, rec: bytes, path: str, out: List[dict], seen: set) -> None:
        lba = int.from_bytes(rec[2:6], "little")
        size = int.from_bytes(rec[10:14], "little")
        if lba in seen:
            return
        seen.add(lba)
        data = self.read(lba, size)
        pos = 0
        while pos < len(data):
            ln = data[pos]
            if ln == 0:                          # records never cross sectors
                pos = (pos // 2048 + 1) * 2048
                continue
            r = data[pos:pos + ln]
            pos += ln
            # a directory record is 33 bytes plus its name; anything shorter
            # (or cut off by the end of the directory) is a damaged image
            if len(r) < 34 or len(r) < 33 + r[32]:
                sys.exit(f"damaged directory record in '{path or '/'}' "
                         f"(sector {lba}); is this a clean disc image?")
            nlen = r[32]
            name = r[33:33 + nlen]
            if name in (b"\x00", b"\x01"):
                continue
            name = name.decode("ascii", "replace").split(";")[0]
            flags = r[25]
            full = f"{path}/{name}" if path else name
            if flags & 0x02:
                self._walk(r, full, out, seen)
            else:
                flba = int.from_bytes(r[2:6], "little")
                fsize = int.from_bytes(r[10:14], "little")
                form2 = self.raw and fsize > 0 and self.sector_info(flba)[1]
                out.append(dict(path=full, lba=flba, size=fsize, form2=form2))


def safe_disc_path(path: str) -> bool:
    """True if a path read from a disc image is safe to create under the
    output folder: no empty, '.' or '..' parts, no drive or backslash tricks."""
    parts = path.split("/")
    return all(p not in ("", ".", "..") and "\\" not in p and ":" not in p
               for p in parts)


def cmd_disc(a) -> None:
    d = Disc(a.image)
    try:
        _disc_action(d, a)
    finally:
        d.close()


def _disc_action(d: Disc, a) -> None:
    files = d.files()
    if a.action == "list":
        kind = "raw 2352-byte sectors" if d.raw else "2048-byte sectors (.iso)"
        print(f"{d.sectors} sectors, {kind}\n")
        print(f"{'LBA':>8}  {'size':>10}  path")
        for f in files:
            tag = "  [XA/STR form2]" if f["form2"] else ""
            print(f"{f['lba']:>8}  {f['size']:>10}  {f['path']}{tag}")
        print(f"\n{len(files)} files")
        return
    want = {w.upper() for w in a.files} if a.files else None
    os.makedirs(a.out, exist_ok=True)
    done = []
    for f in files:
        if want and f["path"].upper() not in want and \
                f["path"].split("/")[-1].upper() not in want:
            continue
        # names come from the image, so never let one escape the output folder
        if not safe_disc_path(f["path"]):
            print(f"  skipped (unsafe name): {f['path']!r}", file=sys.stderr)
            continue
        dest = os.path.join(a.out, *f["path"].split("/"))
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        if f["form2"] and d.raw:
            # audio/video: keep whole raw sectors so they stay playable/usable
            n = (f["size"] + 2047) // 2048
            with open(dest, "wb") as o:
                for k in range(n):
                    o.write(d.raw_sector(f["lba"] + k))
        else:
            with open(dest, "wb") as o:
                o.write(d.read(f["lba"], f["size"]))
        done.append(f)
        print(f"  {f['path']}  ({f['size']} bytes)")
    with open(os.path.join(a.out, "_manifest.json"), "w", encoding="utf-8") as m:
        json.dump(done, m, indent=2)
    print(f"{len(done)} files extracted to {a.out} (LBAs/sizes in _manifest.json)")


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def write_records(records: List[dict], fmt: str, out_path: Optional[str]) -> None:
    f = open_out(out_path)
    try:
        if fmt == "json":
            json.dump(records, f, ensure_ascii=False, indent=2)
            f.write("\n")
        elif fmt == "csv":
            if records:
                w = csv.DictWriter(f, fieldnames=list(records[0].keys()))
                w.writeheader()
                for r in records:
                    w.writerow(r)
        else:  # txt
            for r in records:
                head = f"#### [{r['index']:04d}] offset=0x{r['offset']:X}"
                if r.get("pointer_at") is not None:
                    head += f"  ptr@0x{r['pointer_at']:X}=0x{r['pointer']:X}"
                head += f"  len={r['length']}"
                f.write(head + "\n")
                f.write(r["text"] + "\n\n")
    finally:
        if f is not sys.stdout:
            f.close()


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_info(a) -> None:
    rom = read_rom(a.rom)
    size = len(rom)
    print(f"file:  {a.rom}")
    print(f"size:  {size} bytes (0x{size:X}, {size / 1024:.1f} KB)")
    guess = []
    if rom[:4] == b"NES\x1a":
        prg, chr_ = rom[4], rom[5]
        guess.append(f"NES (iNES header 16 bytes; PRG {prg}x16KB, CHR {chr_}x8KB)")
    if len(rom) > 0xC0 and rom[0xB2] == 0x96:
        title = rom[0xA0:0xAC].split(b"\0")[0].decode("ascii", "replace")
        guess.append(f"GBA (title '{title}', pointers usually 0x08xxxxxx -> use --mapping gba)")
    if len(rom) > 0x150 and rom[0x104:0x108] == bytes.fromhex("CEED6666"):
        title = rom[0x134:0x143].split(b"\0")[0].decode("ascii", "replace")
        guess.append(f"Game Boy / GBC (title '{title}')")
    if rom[0x100:0x104] in (b"SEGA",):
        guess.append("Sega Genesis / Mega Drive (big-endian pointers)")
    hdr = snes_header_size(rom)
    for base, kind in ((0x7FC0, "LoROM"), (0xFFC0, "HiROM")):
        p = base + hdr
        if p + 0x20 <= len(rom):
            chk = rom[p + 0x1C] | rom[p + 0x1D] << 8
            cpl = rom[p + 0x1E] | rom[p + 0x1F] << 8
            if chk ^ cpl == 0xFFFF and chk != 0:
                title = rom[p:p + 21].decode("ascii", "replace").strip()
                guess.append(f"SNES {kind} (title '{title}', copier header {hdr} bytes)")
    print("guess: " + ("; ".join(guess) if guess else "unknown"))


def cmd_strings(a) -> None:
    rom = read_rom(a.rom)
    start = parse_int(a.start) if a.start else 0
    end = parse_int(a.end) if a.end else len(rom)
    records = []
    if a.encoding == "ascii":
        i, run_start = start, None
        printable = set(range(0x20, 0x7F)) | {0x0A}
        while i <= end:
            b = rom[i] if i < end else None
            if b is not None and b in printable:
                if run_start is None:
                    run_start = i
            else:
                if run_start is not None and i - run_start >= a.min:
                    s = rom[run_start:i].decode("ascii")
                    records.append(dict(index=len(records), offset=run_start,
                                        pointer_at=None, pointer=None,
                                        length=i - run_start, text=s))
                run_start = None
            i += 1
    else:  # shift-jis
        i = start
        while i < end:
            j, chars, has_wide = i, [], False
            while j < end:
                ch, w = sjis_char(rom, j, end)
                if ch is None:
                    break
                chars.append(ch)
                has_wide = has_wide or w == 2
                j += w
            # require at least one double-byte char so we skip random ASCII noise
            if len(chars) >= a.min and has_wide:
                records.append(dict(index=len(records), offset=i, pointer_at=None,
                                    pointer=None, length=j - i, text="".join(chars)))
                i = j
            else:
                i += 1
    write_records(records, a.format, a.out)
    print(f"{len(records)} strings found", file=sys.stderr)


def cmd_relsearch(a) -> None:
    rom = read_rom(a.rom)
    word = a.text
    if len(word) < 3:
        sys.exit("relsearch needs at least 3 characters to be meaningful")
    width = a.width
    fmt_char = "<" if a.endian == "little" else ">"
    if word[0] == "?":
        sys.exit("the first character can't be the ? wildcard")
    # '?' = wildcard (use it for characters like the long-vowel mark, which
    # usually sit far away from the rest of the kana)
    diffs = [None if c == "?" else ord(c) - ord(word[0]) for c in word]
    if sum(d is not None for d in diffs) < 3:
        sys.exit("need at least 3 non-wildcard characters")

    def val(pos: int) -> int:
        return int.from_bytes(rom[pos:pos + width], a.endian)

    hits = []
    span = len(word) * width
    for pos in range(0, len(rom) - span + 1, a.align or 1):
        base = val(pos)
        ok = True
        for k in range(1, len(word)):
            if diffs[k] is not None and val(pos + k * width) - base != diffs[k]:
                ok = False
                break
        if ok:
            hits.append((pos, base))
            if len(hits) >= a.max:
                break

    if not hits:
        print("no matches. try another word, other case (all lower / all caps), "
              "--width 2, or --endian big.")
        return

    # Group by inferred value of the first letter; the most common one is
    # very likely the real encoding.
    from collections import Counter
    counts = Counter(b for _, b in hits)
    print(f"{len(hits)} match(es) for '{word}':")
    for pos, base in hits:
        print(f"  offset 0x{pos:X}  '{word[0]}' = 0x{base:0{width*2}X}")
    print()
    best, n = counts.most_common(1)[0]
    print(f"most common encoding: '{word[0]}' = 0x{best:0{width*2}X} ({n} hit(s))")

    if a.make_tbl:
        lines = []
        origin = best - ord(word[0])
        hira = all("\u3041" <= c <= "\u3096" or c in "?\u30fc" for c in word)
        kata = all("\u30a1" <= c <= "\u30fa" or c in "?\u30fc" for c in word)
        if hira or kata:
            lo, hi = (0x3041, 0x3096) if hira else (0x30A1, 0x30FA)
            for cp in range(lo, hi + 1):
                v = origin + cp
                if 0 <= v < 256 ** width:
                    lines.append(f"{v:0{width*2}X}={chr(cp)}")
        elif word.isupper() or word.islower() or not word.isalpha():
            letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if word[0].isupper() \
                else "abcdefghijklmnopqrstuvwxyz"
            if not word[0].isalpha():
                letters = "".join(chr(c) for c in range(0x20, 0x7F))
            for ch in letters:
                v = origin + ord(ch)
                if 0 <= v < 256 ** width:
                    lines.append(f"{v:0{width*2}X}={ch}")
        with open(a.make_tbl, "w", encoding="utf-8") as f:
            f.write(f"; starter table generated by relsearch on '{word}'\n")
            f.write("; verify these, then add the other case, digits,\n")
            f.write("; punctuation, space, line break (*XX) and end code (/XX=<END>)\n")
            f.write("\n".join(lines) + "\n")
        print(f"wrote starter table to {a.make_tbl}")


def cmd_dump(a) -> None:
    rom = read_rom(a.rom)
    tbl = Table.for_args(a)
    start = parse_int(a.start)
    end = parse_int(a.end) if a.end else len(rom)
    end = min(end, len(rom))
    records = []
    i = start
    while i < end:
        text, nxt = tbl.decode(rom, i, end, stop_at_end=bool(tbl.end_codes))
        if nxt == i:
            break
        core = text
        for e in tbl.end_codes.values():
            core = core.replace(e, "")
        if not a.skip_empty or core.strip():
            records.append(dict(index=len(records), offset=i, pointer_at=None,
                                pointer=None, length=nxt - i, text=text))
        i = nxt
        if not tbl.end_codes:
            break
    write_records(records, a.format, a.out)
    print(f"{len(records)} strings dumped", file=sys.stderr)


def cmd_ptrdump(a) -> None:
    rom = read_rom(a.rom)
    tbl = Table.for_args(a)
    pstart = parse_int(a.ptr_start)
    size = a.ptr_size or (4 if a.mapping in ("psx", "psxexe", "gba") else 2)
    stride = a.stride or size
    mapping, add = linear_add(a, rom)
    bank = parse_int(a.bank) if a.bank else None
    if a.header == "auto":
        header = snes_header_size(rom) if mapping in ("lorom", "hirom") else \
            (16 if rom[:4] == b"NES\x1a" else 0)
    else:
        header = parse_int(a.header)
    # header also shifts where the pointer table itself is, if the user gave
    # a "clean" (headerless) offset with --ptr-headerless
    if a.ptr_headerless:
        pstart += header

    if a.count:
        count = parse_int(a.count)
    elif a.ptr_end:
        count = (parse_int(a.ptr_end) - pstart) // stride
    else:
        # auto: stop at the first pointer whose target falls outside the ROM,
        # or at the lowest target seen (text usually follows the table)
        count = None

    records = []
    seen: Dict[int, int] = {}
    k = 0
    lowest_target = len(rom)
    while True:
        if count is not None and k >= count:
            break
        pat = pstart + k * stride
        if pat + size > len(rom):
            break
        if count is None and pat >= lowest_target:
            break
        ptr = int.from_bytes(rom[pat:pat + size], a.endian)
        try:
            off = map_pointer(ptr, mapping, add, header, bank)
        except ValueError:
            off = -1
        if not (0 <= off < len(rom)):
            if count is None:
                break
            records.append(dict(index=k, offset=off, pointer_at=pat, pointer=ptr,
                                length=0, text="<BAD POINTER>"))
            k += 1
            continue
        lowest_target = min(lowest_target, off)
        if off in seen and a.dedupe:
            text = f"<SAME AS [{seen[off]:04d}]>"
            length = 0
        else:
            seen.setdefault(off, k)
            limit = min(len(rom), off + a.max_len)
            text, nxt = tbl.decode(rom, off, limit, stop_at_end=True)
            length = nxt - off
        records.append(dict(index=k, offset=off, pointer_at=pat, pointer=ptr,
                            length=length, text=text))
        k += 1
    write_records(records, a.format, a.out)
    print(f"{len(records)} pointers read", file=sys.stderr)


def cmd_findptr(a) -> None:
    """Search a file for pointers to given offsets (finds pointer tables)."""
    data = read_rom(a.file)
    mapping, add = linear_add(a, data)
    if mapping != "none":
        sys.exit("findptr supports linear mappings: none, psx, psxexe, gba")
    size = a.ptr_size
    for t in a.targets:
        off = parse_int(t)
        value = off - add
        if not (0 <= value < 256 ** size):
            print(f"0x{off:X}: pointer value out of range for {size}-byte pointers")
            continue
        needle = value.to_bytes(size, a.endian)
        hits, i = [], data.find(needle)
        while i != -1 and len(hits) < 100:
            if i % a.align == 0:
                hits.append(i)
            i = data.find(needle, i + 1)
        where = ", ".join(f"0x{h:X}" for h in hits) or "none"
        print(f"target 0x{off:X} (pointer value 0x{value:0{size*2}X}): {where}")


def cmd_ptrscan(a) -> None:
    """Find runs of 32-bit pointers that land inside this file - pointer
    tables for text usually show up as long runs. With --mapping none the
    values are taken as plain file offsets (plus --add)."""
    data = read_rom(a.file)
    mapping, add = linear_add(a, data)
    n = len(data)
    runs, k = [], 0
    while k + 4 <= n:
        j, targets = k, []
        while j + 4 <= n:
            v = int.from_bytes(data[j:j + 4], "little")
            off = v + add
            if not (0 <= off < n) or v == 0:
                break
            targets.append(off)
            j += 4
        if len(targets) >= a.min:
            ordered = sum(1 for x, y in zip(targets, targets[1:]) if y >= x)
            runs.append((k, len(targets), min(targets), max(targets),
                         ordered / max(1, len(targets) - 1)))
            k = j
        else:
            k += 4
    runs.sort(key=lambda r: -r[1])
    if not runs:
        print("no pointer runs found (check --mapping / --load, or lower --min)")
        return
    print(f"{'table at':>10}  {'count':>6}  {'targets':>23}  ascending")
    for start, cnt, lo, hi, asc in runs[:a.top]:
        print(f"0x{start:08X}  {cnt:>6}  0x{lo:08X}-0x{hi:08X}  {asc:>8.0%}")
    print("\nTry: ptrdump FILE --sjis -m <same mapping> -p <table at> -c <count>")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="romscript",
        description="Pull the script (text/dialogue) out of game ROMs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Table (.tbl)" + __doc__.split("Table (.tbl)")[1] if __doc__ else None)
    sub = p.add_subparsers(dest="cmd", required=True)

    def table_opts(sp):
        sp.add_argument("-t", "--table", help=".tbl file")
        sp.add_argument("--sjis", action="store_true",
                        help="decode Shift-JIS for anything the table doesn't "
                             "cover (alone: plain Shift-JIS, 00 ends a string)")
        sp.add_argument("--end-code", action="append", metavar="HEX",
                        help="extra end-of-string code, e.g. 00 or FFFF (repeatable)")

    def mapping_opts(sp, choices):
        sp.add_argument("-m", "--mapping", choices=choices, default="none",
                        help="how pointer values map to file offsets. psxexe = "
                             "a PS-X EXE (SLPS_xxx.xx); psx = other file loaded "
                             "at --load")
        sp.add_argument("--load", help="RAM load address for --mapping psx")
        sp.add_argument("--add", help="value added to every mapped offset")

    def outputs(sp):
        sp.add_argument("-o", "--out", help="output file (default: stdout)")
        sp.add_argument("-f", "--format", choices=["txt", "json", "csv"],
                        default="txt")

    s = sub.add_parser("info", help="show ROM size/header/guessed system")
    s.add_argument("rom")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("strings", help="find plain ASCII / Shift-JIS text")
    s.add_argument("rom")
    s.add_argument("-e", "--encoding", choices=["ascii", "sjis"], default="ascii")
    s.add_argument("-n", "--min", type=int, default=6, help="min chars (default 6)")
    s.add_argument("--start")
    s.add_argument("--end")
    outputs(s)
    s.set_defaults(func=cmd_strings)

    s = sub.add_parser("relsearch",
                       help="find text in an unknown encoding, build a starter .tbl")
    s.add_argument("rom")
    s.add_argument("text", help="a word you know appears in the game, e.g. 'potion' "
                               "or 'あくま'. Use ? as a wildcard character")
    s.add_argument("-w", "--width", type=int, choices=[1, 2], default=1,
                   help="bytes per character (default 1)")
    s.add_argument("--endian", choices=["little", "big"], default="little")
    s.add_argument("--align", type=int, default=1)
    s.add_argument("--max", type=int, default=50, help="max hits to list")
    s.add_argument("-t", "--make-tbl", metavar="FILE",
                   help="write a starter table from the best match")
    s.set_defaults(func=cmd_relsearch)

    s = sub.add_parser("dump", help="decode a byte range with a .tbl file")
    s.add_argument("rom")
    table_opts(s)
    s.add_argument("-s", "--start", required=True, help="start offset, e.g. 0x1A000")
    s.add_argument("-e", "--end", help="end offset (exclusive); default end of file")
    s.add_argument("--skip-empty", action="store_true",
                   help="drop strings that are only an end code")
    outputs(s)
    s.set_defaults(func=cmd_dump)

    s = sub.add_parser("ptrdump", help="follow a pointer table and decode each string")
    s.add_argument("rom")
    table_opts(s)
    s.add_argument("-p", "--ptr-start", required=True,
                   help="file offset of the pointer table")
    g = s.add_mutually_exclusive_group()
    g.add_argument("-c", "--count", help="number of pointers")
    g.add_argument("--ptr-end", help="file offset where the pointer table ends")
    s.add_argument("--ptr-size", type=int, choices=[2, 3, 4],
                   help="bytes per pointer (default 4 for psx/psxexe/gba, else 2)")
    s.add_argument("--stride", type=int,
                   help="bytes between pointers if entries hold extra data")
    s.add_argument("--endian", choices=["little", "big"], default="little")
    mapping_opts(s, ["none", "psx", "psxexe", "gba", "lorom", "hirom", "nes"])
    s.add_argument("--bank", help="bank byte for 2-byte SNES/NES pointers")
    s.add_argument("--header", default="auto",
                   help="header bytes to skip: auto (SNES 512 / iNES 16) or a number")
    s.add_argument("--ptr-headerless", action="store_true",
                   help="--ptr-start is given without the header (shift it)")
    s.add_argument("--max-len", type=int, default=4096,
                   help="give up on a string after this many bytes")
    s.add_argument("--no-dedupe", dest="dedupe", action="store_false",
                   help="decode repeated targets again instead of referencing")
    outputs(s)
    s.set_defaults(func=cmd_ptrdump)

    s = sub.add_parser("disc", help="list / extract files from a PS1 .bin or .iso")
    s.add_argument("action", choices=["list", "extract"])
    s.add_argument("image", help="disc image (.bin, not .cue)")
    s.add_argument("files", nargs="*", help="only these files (default: all)")
    s.add_argument("-o", "--out", default="disc_files", help="extract folder")
    s.set_defaults(func=cmd_disc)

    s = sub.add_parser("ptrscan", help="find PS1 pointer tables (runs of 32-bit pointers)")
    s.add_argument("file")
    mapping_opts(s, ["none", "psx", "psxexe"])
    s.add_argument("--min", type=int, default=8, help="shortest run to report")
    s.add_argument("--top", type=int, default=30)
    s.set_defaults(func=cmd_ptrscan)

    s = sub.add_parser("findptr", help="find pointers to given string offsets")
    s.add_argument("file")
    s.add_argument("targets", nargs="+", help="file offsets of strings")
    mapping_opts(s, ["none", "psx", "psxexe", "gba"])
    s.add_argument("--ptr-size", type=int, choices=[2, 3, 4], default=4)
    s.add_argument("--endian", choices=["little", "big"], default="little")
    s.add_argument("--align", type=int, default=1)
    s.set_defaults(func=cmd_findptr)
    return p


def _fix_negative_values(argv: List[str]) -> List[str]:
    """Let users write `--add -0x8000` (argparse would treat -0x8000 as a flag)."""
    out, i = [], 0
    takes_number = {"--add", "--bank", "--header", "--load"}
    while i < len(argv):
        tok = argv[i]
        if tok in takes_number and i + 1 < len(argv) and argv[i + 1].startswith("-"):
            out.append(f"{tok}={argv[i + 1]}")
            i += 2
            continue
        out.append(tok)
        i += 1
    return out


def main(argv: Optional[List[str]] = None) -> None:
    argv = _fix_negative_values(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except BrokenPipeError:          # e.g. piped into `head`
        sys.stderr.close()
        os._exit(0)


if __name__ == "__main__":
    main()
