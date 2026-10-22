#!/usr/bin/env python3
"""Mark each success case with whether any of its frames holds a Compressed
block. Frames built only from Raw and RLE blocks need no entropy decoding,
so the reward gives them no weight.

usage: tag_compressed.py CASE_DIR ...      (rewrites CASE_DIR/manifest.json)
"""
import json
import sys


def has_compressed(b):
    """Walk the block headers of a well-formed stream; True on the first
    Compressed block. Skippable frames are stepped over."""
    i = 0
    while i < len(b):
        magic = int.from_bytes(b[i:i + 4], "little")
        if 0x184D2A50 <= magic <= 0x184D2A5F:
            i += 8 + int.from_bytes(b[i + 4:i + 8], "little")
            continue
        if magic != 0xFD2FB528:
            raise ValueError(f"bad magic {magic:#x} at {i}")
        fhd = b[i + 4]
        single = (fhd >> 5) & 1
        i += 5 + (0 if single else 1) + [0, 1, 2, 4][fhd & 3] + [single, 2, 4, 8][fhd >> 6]
        while True:
            h = int.from_bytes(b[i:i + 3], "little")
            typ, size = (h >> 1) & 3, h >> 3
            if typ == 2:
                return True
            if typ == 3:
                raise ValueError(f"reserved block type at {i}")
            i += 3 + (1 if typ == 1 else size)
            if h & 1:
                break
        i += 4 if (fhd >> 2) & 1 else 0
    if i != len(b):
        raise ValueError("stream does not end on a frame boundary")
    return False


def main():
    for d in sys.argv[1:]:
        path = f"{d}/manifest.json"
        doc = json.load(open(path))
        n = 0
        for c in doc["cases"]:
            if c["expect"] == "ok":
                c["compressed"] = has_compressed(open(f"{d}/cases/{c['id']}.zst", "rb").read())
                n += c["compressed"]
        with open(path, "w") as f:
            json.dump(doc, f, indent=0)
        print(f"{path}: {n} success cases with a Compressed block")


if __name__ == "__main__":
    main()
