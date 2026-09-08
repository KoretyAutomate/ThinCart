#!/usr/bin/env python3
"""publish_apk.py — make a CI-built APK the one the app offers as an update.

    python3 server/publish_apk.py /path/to/app-debug.apk [--notes "what changed"]

Copies the APK into dist/ and writes dist/version.json, which GET /version
serves. The version numbers are read from INSIDE the APK's binary
AndroidManifest.xml, not from build.gradle: the working tree moves on after a
build, and publishing a previously-built APK would otherwise claim whatever
the tree happened to say — an update the phone installs and then still sees
as old.

SIGNING GATE: an APK signed with a different key cannot update the installed
app; Android refuses it and the only way forward is uninstall-first, which
wipes the saved server address and any queued offline ops. This refuses to
publish an APK whose signing cert is not the persistent one the CI gate
asserts (.github/workflows/build-apk.yml).

Ported from OutfitAdvisor's publish_apk.py.
"""

import argparse
import datetime as dt
import hashlib
import json
import shutil
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# The persistent debug key every build is signed with — the same value the CI
# cert-drift gate asserts.
EXPECTED_CERT = "067bcb5f27b863b77d75e8dff39b9dc78b7d5998735ece815eb7cf6decd84113"


def _axml_strings(blob: bytes, off: int) -> list[str]:
    """Decode an AXML string-pool chunk into a list of strings."""
    count = struct.unpack_from("<I", blob, off + 8)[0]
    flags = struct.unpack_from("<I", blob, off + 16)[0]
    strings_start = struct.unpack_from("<I", blob, off + 20)[0]
    utf8 = bool(flags & (1 << 8))
    out = []
    for i in range(count):
        so = struct.unpack_from("<I", blob, off + 28 + i * 4)[0]
        p = off + strings_start + so
        if utf8:
            # two varint-ish lengths (char count, then byte count), then bytes
            n = blob[p]
            p += 2 if n & 0x80 else 1
            n = blob[p]
            if n & 0x80:
                n = ((n & 0x7F) << 8) | blob[p + 1]
                p += 2
            else:
                p += 1
            out.append(blob[p : p + n].decode("utf-8", "replace"))
        else:
            n = struct.unpack_from("<H", blob, p)[0]
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", blob, p + 2)[0]
                p += 4
            else:
                p += 2
            out.append(blob[p : p + n * 2].decode("utf-16-le", "replace"))
    return out


def apk_version(apk: Path) -> tuple[int, str]:
    """versionCode / versionName straight out of the APK's binary manifest.

    Minimal AXML walk: find the string pool, then the <manifest> START_ELEMENT,
    then its android:versionCode / android:versionName attributes. We only need
    two attributes off one element, so this stays far short of a full parser.
    """
    with zipfile.ZipFile(apk) as z:
        blob = z.read("AndroidManifest.xml")

    CHUNK_STRINGS, CHUNK_RESMAP, CHUNK_START_ELEM = 0x0001, 0x0180, 0x0102
    # AAPT2 does not store the NAME of a framework attribute as a string — the
    # string-pool slot is empty and the real identity is a resource id in the
    # resource-map chunk, parallel-indexed to the pool. Matching on the string
    # alone finds nothing, which is exactly how the first attempt failed.
    ATTR_VERSION_CODE, ATTR_VERSION_NAME = 0x0101021B, 0x0101021C

    strings: list[str] = []
    resmap: list[int] = []
    off, end = 8, len(blob)  # skip the 8-byte file header
    while off + 8 <= end:
        ctype, hsize, csize = (
            struct.unpack_from("<H", blob, off)[0],
            struct.unpack_from("<H", blob, off + 2)[0],
            struct.unpack_from("<I", blob, off + 4)[0],
        )
        if csize <= 0 or off + csize > end:
            break
        if ctype == CHUNK_STRINGS:
            strings = _axml_strings(blob, off)
        elif ctype == CHUNK_RESMAP:
            n = (csize - hsize) // 4
            resmap = list(struct.unpack_from(f"<{n}I", blob, off + hsize))
        elif ctype == CHUNK_START_ELEM and strings:
            name_i = struct.unpack_from("<I", blob, off + 20)[0]
            if name_i < len(strings) and strings[name_i] == "manifest":
                attr_start = struct.unpack_from("<H", blob, off + 24)[0]
                attr_size = struct.unpack_from("<H", blob, off + 26)[0]
                attr_count = struct.unpack_from("<H", blob, off + 28)[0]
                code, vname = None, None
                for i in range(attr_count):
                    # attributeStart counts from the START of the attrExt struct,
                    # which begins after the 16-byte node header — not from the
                    # chunk start. Using the chunk start reads garbage.
                    a = off + 16 + attr_start + i * attr_size
                    a_name = struct.unpack_from("<I", blob, a + 4)[0]
                    raw = struct.unpack_from("<i", blob, a + 8)[0]
                    data = struct.unpack_from("<I", blob, a + 16)[0]
                    res_id = resmap[a_name] if a_name < len(resmap) else 0
                    key = strings[a_name] if a_name < len(strings) else ""
                    if res_id == ATTR_VERSION_CODE or key == "versionCode":
                        code = data
                    elif res_id == ATTR_VERSION_NAME or key == "versionName":
                        vname = strings[raw] if 0 <= raw < len(strings) else str(data)
                if code is None:
                    sys.exit("versionCode not found in the APK manifest")
                return code, vname or "?"
        off += csize
    sys.exit("could not locate <manifest> in the APK's AndroidManifest.xml")


def apk_cert(apk: Path) -> str | None:
    """SHA-256 of the APK's signing certificate, read from the APK Signing Block
    (scheme v2/v3) — the same digest `apksigner verify --print-certs` prints,
    without running apksigner: the block sits just before the zip's central
    directory, and the first certificate in the first signer is the one that
    matters. None when the APK has no v2/v3 block, which no CI build lacks.
    """
    data = apk.read_bytes()
    eocd = data.rfind(b"PK\x05\x06")
    if eocd < 0:
        return None
    cd_off = struct.unpack_from("<I", data, eocd + 16)[0]
    if data[cd_off - 16: cd_off] != b"APK Sig Block 42":
        return None
    size = struct.unpack_from("<Q", data, cd_off - 24)[0]
    pos, end = cd_off - 8 - size + 8, cd_off - 24
    while pos + 12 <= end:
        ln = struct.unpack_from("<Q", data, pos)[0]
        pid = struct.unpack_from("<I", data, pos + 8)[0]
        if pid in (0x7109871A, 0xF05368C0):        # v2, v3
            return _first_cert_sha256(data[pos + 12: pos + 8 + ln])
        pos += 8 + ln
    return None


def _first_cert_sha256(block: bytes) -> str | None:
    """signers -> signer -> signed data -> (digests, certificates): the first
    certificate, DER, hashed. v2 and v3 share this prefix of the layout."""
    def u32(o: int) -> int:
        return struct.unpack_from("<I", block, o)[0]

    try:
        p = 4                       # past the signers-sequence length
        p += 4                      # past the first signer's length
        signed_len = u32(p)
        p += 4
        sd = block[p: p + signed_len]
        q = 4 + struct.unpack_from("<I", sd, 0)[0]          # skip digests
        q += 4                                              # certificates-sequence length
        cert_len = struct.unpack_from("<I", sd, q)[0]
        q += 4
        return hashlib.sha256(sd[q: q + cert_len]).hexdigest()
    except struct.error:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("apk", type=Path)
    ap.add_argument("--notes", default="")
    ap.add_argument("--allow-unsigned-check", action="store_true",
                    help="publish even if the signing cert cannot be verified locally")
    args = ap.parse_args()
    if not args.apk.is_file():
        sys.exit(f"no such file: {args.apk}")

    cert = apk_cert(args.apk)
    if cert is None:
        if not args.allow_unsigned_check:
            sys.exit("no v2/v3 signing block — cannot verify the signing key. Re-run with "
                     "--allow-unsigned-check only if you are certain this APK came from the CI pipeline.")
        print("WARNING: signing cert NOT verified (no signing block)")
    elif cert != EXPECTED_CERT:
        sys.exit(f"REFUSING TO PUBLISH: signed with {cert}, expected {EXPECTED_CERT}.\n"
                 "An APK with a different key cannot update the installed app.")
    else:
        print(f"signing cert OK ({cert[:16]}…)")

    code, vname = apk_version(args.apk)
    DIST.mkdir(exist_ok=True)
    name = "thincart.apk"
    shutil.copy2(args.apk, DIST / name)
    data = (DIST / name).read_bytes()
    meta = {
        "versionCode": code, "versionName": vname, "file": name,
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "publishedAt": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "notes": args.notes,
    }
    (DIST / "version.json").write_text(json.dumps(meta, indent=2))
    print(f"published v{vname} (code {code}), {len(data)/1e6:.1f} MB -> {DIST / name}")


if __name__ == "__main__":
    main()
