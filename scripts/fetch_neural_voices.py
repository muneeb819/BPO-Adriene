#!/usr/bin/env python3
"""Fetch the local neural voices (Piper/ONNX) used by the offline TTS server.

The three voices are ~60 MB each, so they are deliberately **not** committed to
git and **not** vendored in any wheel. This script pulls them out of public GitHub
repositories (streamed, so only the two files per voice ever touch the disk),
verifies their SHA-256, and drops them in ``data/models/piper``.

    python scripts/fetch_neural_voices.py            # all three voices
    python scripts/fetch_neural_voices.py --voice en_US-amy-low
    python scripts/fetch_neural_voices.py --list

Why these hosts: the project runs in environments where huggingface.co and GitHub
*release* assets are unreachable, but ``codeload.github.com`` repo tarballs are
not. Each entry below is a repository that has the real 60 MB model committed
(not a Git-LFS pointer).

Afterwards:

    pip install piper-tts onnxruntime          # if not already installed
    python scripts/neural_tts_server.py --port 8880 --warm
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import tarfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_DIR = os.path.join("data", "models", "piper")

#: voice id -> sources. ``members`` maps the archive path to the local file name.
VOICES: "dict[str, dict]" = {
    "en_GB-alan-medium": {
        "title": "Alan — British English male, medium quality",
        "url": "https://codeload.github.com/KessiaTheCreator/recovery-robot/tar.gz/refs/heads/main",
        "members": {
            "recovery-robot-main/en_GB-alan-medium.onnx.1": "en_GB-alan-medium.onnx",
            "recovery-robot-main/en_GB-alan-medium.onnx.json": "en_GB-alan-medium.onnx.json",
        },
        "sha256": {
            "en_GB-alan-medium.onnx":
                "0a309668932205e762801f1efc2736cd4b0120329622adf62be09e56339d3330",
            "en_GB-alan-medium.onnx.json":
                "c0f0d124e5895c00e7c03b35dcc8287f319a6998a365b182deb5c8e752ee8c1e",
        },
    },
    "en_US-amy-low": {
        "title": "Amy — US English female, low quality (16 kHz)",
        "url": "https://codeload.github.com/Sparky4567/holocron/tar.gz/refs/heads/master",
        "members": {
            "holocron-master/amy/en_US-amy-low.onnx": "en_US-amy-low.onnx",
            "holocron-master/amy/en_US-amy-low.onnx.json": "en_US-amy-low.onnx.json",
        },
        "sha256": {
            "en_US-amy-low.onnx":
                "a5a91abb7de0f104358a25aded480ddacf1ff0762886325886ec406a2e86aab3",
            "en_US-amy-low.onnx.json":
                "6b3081b541cf36a457dd8ca37c0dfb6b36db893cd56e54518503483c92df7264",
        },
    },
    "en_US-lessac-medium": {
        "title": "Lessac — US English male, medium quality",
        "url": "https://codeload.github.com/Guimli/NaomiDIAG/tar.gz/refs/heads/main",
        "members": {
            "NaomiDIAG-main/tools/voices/en_US-lessac-medium.onnx": "en_US-lessac-medium.onnx",
            "NaomiDIAG-main/tools/voices/en_US-lessac-medium.onnx.json": "en_US-lessac-medium.onnx.json",
        },
        "sha256": {
            "en_US-lessac-medium.onnx":
                "5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f",
            "en_US-lessac-medium.onnx.json":
                "efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0",
        },
    },
}

HELP_BLOCKED = """
  If this host is blocked in your environment, copy the two files per voice by
  hand into {target}:
      <voice>.onnx        the ONNX model (~60 MB)
      <voice>.onnx.json   its config (a few KB)
  Any Piper voice works — drop it in and it shows up in the studio. Voices are
  published by the Piper project (rhasspy/piper-voices); the URLs above are just
  repositories that mirror the same files where release downloads are blocked.
"""


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _already_ok(target_dir: str, voice: str) -> bool:
    spec = VOICES[voice]
    for local, want in spec["sha256"].items():
        path = os.path.join(target_dir, local)
        if not os.path.isfile(path) or _sha256(path) != want:
            return False
    return True


def fetch(voice: str, target_dir: str, *, force: bool = False) -> bool:
    """Stream the repo tarball and extract this voice's two files."""
    spec = VOICES[voice]
    os.makedirs(target_dir, exist_ok=True)
    if not force and _already_ok(target_dir, voice):
        print(f"  {voice:24} already present and verified — skipping")
        return True
    print(f"  {voice:24} {spec['title']}")
    print(f"    from {spec['url']}")
    wanted = {src: os.path.join(target_dir, dst) for src, dst in spec["members"].items()}
    extracted: "list[str]" = []
    try:
        request = urllib.request.Request(spec["url"], headers={"User-Agent": "bpo-adriene"})
        with urllib.request.urlopen(request, timeout=300) as response:
            total = int(response.headers.get("Content-Length") or 0)
            size = f"{total / 1048576:.0f} MB" if total else "a tarball of unknown size"
            print(f"    downloading {size} (only the voice files are kept)…")
            buffer = io.BytesIO()
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                buffer.write(block)
        buffer.seek(0)
        with tarfile.open(fileobj=buffer, mode="r:gz") as archive:
            for member in archive:
                destination = wanted.get(member.name)
                if not destination:
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                with open(destination, "wb") as out:
                    while True:
                        block = source.read(1 << 20)
                        if not block:
                            break
                        out.write(block)
                extracted.append(destination)
                print(f"    wrote {os.path.basename(destination)} "
                      f"({os.path.getsize(destination) / 1048576:.1f} MB)")
    except Exception as exc:                                    # noqa: BLE001
        print(f"    FAILED: {type(exc).__name__}: {exc}")
        print(HELP_BLOCKED.format(target=f"'{target_dir}'"))
        return False
    missing = [os.path.basename(p) for p in wanted.values() if not os.path.isfile(p)]
    if len(extracted) != len(wanted) or missing:
        print(f"    FAILED: the archive did not contain every file (missing {missing})")
        print(HELP_BLOCKED.format(target=f"'{target_dir}'"))
        return False
    bad = []
    for local, want in spec["sha256"].items():
        got = _sha256(os.path.join(target_dir, local))
        if got != want:
            bad.append((local, got))
    if bad:
        for local, got in bad:
            print(f"    CHECKSUM MISMATCH {local}: got {got}")
        print("    The download is corrupt — delete the files and run again.")
        return False
    print(f"    verified {len(spec['sha256'])} file(s) against their SHA-256")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fetch local neural voices for the studio")
    parser.add_argument("--voice", action="append", default=[],
                        help="only this voice (repeatable); default: all")
    parser.add_argument("--dir", default=DEFAULT_DIR, help=f"target directory ({DEFAULT_DIR})")
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    parser.add_argument("--list", action="store_true", help="show the voices and exit")
    args = parser.parse_args(argv)

    if args.list:
        print("Local neural voices available to fetch:")
        for voice, spec in VOICES.items():
            present = "installed" if _already_ok(args.dir, voice) else "not installed"
            print(f"  {voice:24} {present:14} {spec['title']}")
        return 0

    wanted = args.voice or list(VOICES)
    unknown = [v for v in wanted if v not in VOICES]
    if unknown:
        print(f"unknown voice(s): {', '.join(unknown)}\nknown: {', '.join(VOICES)}")
        return 2
    print(f"fetching {len(wanted)} voice(s) into {os.path.abspath(args.dir)}")
    ok = all(fetch(voice, args.dir, force=args.force) for voice in wanted)
    print("\nnext steps")
    print("  pip install piper-tts onnxruntime     # if the import below fails")
    print("  python scripts/neural_tts_server.py --port 8880 --warm")
    print("  python -c \"from agent import neural_tts as nt; print(nt.health())\"")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
