#!/usr/bin/env python3
"""Fail unless tvmate.py and version.txt describe the same complete release."""

import hashlib
import pathlib
import re
import runpy
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tvmate.py"
MANIFEST = ROOT / "version.txt"


def main():
    raw = SCRIPT.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if len(raw) < 500_000:
        raise SystemExit(f"tvmate.py is unexpectedly small ({len(raw)} bytes)")
    text = raw.decode("utf-8")
    compile(text, str(SCRIPT), "exec")
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js is required to validate embedded browser JavaScript")
    page = str(runpy.run_path(str(SCRIPT), run_name="release_verify").get("PAGE") or "")
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", page, re.DOTALL | re.IGNORECASE)
    if not scripts:
        raise SystemExit("No embedded browser JavaScript found")
    parsed = subprocess.run([node, "--check"], input="\n".join(scripts), text=True,
                            capture_output=True, check=False)
    if parsed.returncode:
        raise SystemExit("Embedded browser JavaScript is invalid:\n" + parsed.stderr.strip())
    match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    lines = [line.strip() for line in MANIFEST.read_text("utf-8").splitlines()
             if line.strip()]
    if not match or len(lines) != 2:
        raise SystemExit("VERSION or two-line version.txt manifest is invalid")
    digest = hashlib.sha256(raw).hexdigest()
    failures = []
    if match.group(1) != lines[0]:
        failures.append(f"version mismatch: script={match.group(1)} manifest={lines[0]}")
    if digest != lines[1].lower():
        failures.append(f"checksum mismatch: calculated={digest} manifest={lines[1]}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Release verified: {lines[0]} · {len(raw)} bytes · {digest} · browser JS parsed")


if __name__ == "__main__":
    main()
