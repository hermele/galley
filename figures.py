"""Show PDF figures in galley.

Part of galley by Michael Hermele. Copyright (C) 2026 The Regents of the University of Colorado.
SPDX-License-Identifier: AGPL-3.0-or-later (with the additional term in NOTICE); see LICENSE.

Browsers cannot show a PDF as an inline image (Safari aside), so galley converts the first
page of a PDF figure to PNG with a tool that is usually already there: poppler's pdftocairo
or pdftoppm (common on Linux, and with Homebrew's poppler on macOS), else macOS's built-in
Quick Look (qlmanage), which draws the page at the requested size, and as a last resort
macOS's sips (which renders at screen resolution, so its images look soft). Ghostscript is
not used: it has a poor record with untrusted files. Results are cached in
galley/build/figures/ under a hash of the PDF's content, so each figure is converted once.
If no converter works, galley shows a link that opens the PDF instead.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

DPI = 216  # three times the PDF's own 72 points per inch: sharp on high-resolution displays
CACHE_VERSION = "r2"  # part of each cached file's name; change it to convert all figures again


def converters() -> list:
    """(name, path) of the converters found on this computer, in order of preference."""
    found = []
    for name in ("pdftocairo", "pdftoppm"):
        p = shutil.which(name) or next((c for c in ("/opt/homebrew/bin/" + name, "/usr/local/bin/" + name)
                                        if os.access(c, os.X_OK)), None)
        if p:
            found.append((name, p))
    if sys.platform == "darwin":
        for name in ("qlmanage", "sips"):
            if os.access("/usr/bin/" + name, os.X_OK):
                found.append((name, "/usr/bin/" + name))
    return found


class FigureConverter:
    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        self.tools = converters()
        self.lock = threading.Lock()
        self.failed: set = set()  # content hashes that no converter could handle (this run)

    def png_for(self, pdf_path: str) -> str | None:
        """Path of a PNG of the PDF's first page, converting it if needed; None if that fails."""
        with open(pdf_path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()[:20]
        out = os.path.join(self.cache_dir, "%s-%s.png" % (digest, CACHE_VERSION))
        if os.path.exists(out):
            return out
        if digest in self.failed or not self.tools:
            return None
        with self.lock:  # one conversion at a time; a second request for the same figure waits
            if os.path.exists(out):
                return out
            os.makedirs(self.cache_dir, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.cache_dir) as work:
                src = os.path.join(work, "in.pdf")
                shutil.copyfile(pdf_path, src)
                for name, tool in self.tools:
                    png = self._run(name, tool, src, work)
                    if png:
                        os.replace(png, out)
                        return out
            self.failed.add(digest)
            return None

    def _run(self, name: str, tool: str, src: str, work: str) -> str | None:
        base = os.path.join(work, "out")
        png = base + ".png"
        if name in ("pdftocairo", "pdftoppm"):
            cmd = [tool, "-png", "-singlefile", "-f", "1", "-l", "1", "-r", str(DPI), src, base]
        elif name == "qlmanage":
            # Quick Look thumbnail: -s is the larger side in pixels; it writes <file>.png into -o.
            w, h = page_size(src)
            size = max(600, min(4000, round(max(w, h) * DPI / 72)))
            cmd = [tool, "-t", "-s", str(size), "-o", work, src]
            png = src + ".png"
        else:  # sips
            cmd = [tool, "-s", "format", "png", "-s", "dpiWidth", str(DPI), "-s", "dpiHeight", str(DPI),
                   src, "--out", base + ".png"]
        try:
            subprocess.run(cmd, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return png if os.path.exists(png) and os.path.getsize(png) > 0 else None


def page_size(pdf_path: str) -> tuple:
    """Width and height in points of the PDF's first page (its first /MediaBox), or a default."""
    try:
        with open(pdf_path, "rb") as fh:
            data = fh.read(4_000_000)
        m = re.search(rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]", data)
        if m:
            x0, y0, x1, y1 = (float(v) for v in m.groups())
            if x1 - x0 > 0 and y1 - y0 > 0:
                return x1 - x0, y1 - y0
    except (OSError, ValueError):
        pass
    return 600.0, 600.0
