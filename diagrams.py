"""Compile tikzcd diagrams to SVG for galley.

Part of galley by Michael Hermele. Copyright (C) 2026 The Regents of the University of Colorado.
SPDX-License-Identifier: AGPL-3.0-or-later (with the additional term in NOTICE); see LICENSE.

MathJax cannot draw tikzcd diagrams, so galley compiles each display that
contains one with the document's own preamble (latex, then dvisvgm) and shows
the SVG. Results are cached in galley/build/diagrams/ under a hash of the
preamble and the display's source, so each diagram is compiled once.
All diagrams waiting at a time are compiled in one LaTeX run; if that run
fails, each is compiled separately so one bad diagram does not hide the rest.

This happens without a click, on a preamble that may come from someone else, so LaTeX runs
without shell escape and with kpathsea's paranoid file access (openout_any=p, openin_any=p):
it cannot run commands, and it can neither write nor read files outside its work folder and
the search path (no absolute paths elsewhere, no "..", no dot files).
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import threading


# kpathsea settings for compiling a preamble galley did not write (environment overrides texmf.cnf).
LATEX_SAFE_ENV = {"openout_any": "p", "openin_any": "p", "shell_escape": "f"}


def find_tool(name: str) -> str | None:
    """Find a TeX program, also in the usual MacTeX/TeX Live places if PATH lacks them."""
    p = shutil.which(name)
    if p:
        return p
    for d in ["/Library/TeX/texbin", "/usr/local/texlive/bin", "/opt/homebrew/bin", "/usr/local/bin"]:
        for c in [os.path.join(d, name)] + glob.glob(os.path.join(d, "*", name)):
            if os.access(c, os.X_OK):
                return c
    return None


def log_error(log: str) -> str:
    """The first LaTeX error in a log, with a little context."""
    lines = log.splitlines()
    for i, l in enumerate(lines):
        if l.startswith("!"):
            keep = [x for x in lines[i : i + 8] if not x.startswith(("Type X", "or enter new", "Enter file", "?"))]
            return "\n".join(keep[:6]).strip()
    return log[-1500:].strip() or "LaTeX failed without a message"


class DiagramCompiler:
    def __init__(self, cache_dir: str, doc_dir: str):
        self.cache_dir = cache_dir
        self.doc_dir = doc_dir
        self.latex = find_tool("latex")
        self.dvisvgm = find_tool("dvisvgm")
        self.cond = threading.Condition()
        self.queue: dict = {}  # hash -> (preamble, source)
        self.running: set = set()
        # Failures are retried once per galley run (a missing package may have been installed since).
        for e in glob.glob(os.path.join(cache_dir, "*.err")):
            try:
                os.remove(e)
            except OSError:
                pass
        threading.Thread(target=self._worker, daemon=True).start()

    # -- interface ---------------------------------------------------------

    def available(self) -> str:
        """Empty if diagrams can be compiled, otherwise why not."""
        missing = [n for n, p in (("latex", self.latex), ("dvisvgm", self.dvisvgm)) if not p]
        return ("not found: " + ", ".join(missing)) if missing else ""

    def svg_path(self, h: str) -> str:
        return os.path.join(self.cache_dir, h + ".svg")

    def err_path(self, h: str) -> str:
        return os.path.join(self.cache_dir, h + ".err")

    def request(self, preamble: str, diagrams: dict):
        """Queue the diagrams (hash -> source) that are not compiled yet."""
        if self.available():
            return
        with self.cond:
            for h, src in diagrams.items():
                if os.path.exists(self.svg_path(h)) or os.path.exists(self.err_path(h)) or h in self.running:
                    continue
                self.queue[h] = (preamble, src)
            if self.queue:
                self.cond.notify()

    def status(self, hashes: list) -> dict:
        why = self.available()
        out = {}
        for h in hashes:
            if not re.fullmatch(r"[0-9a-f]{8,64}", h):
                continue
            if os.path.exists(self.svg_path(h)):
                out[h] = {"state": "done"}
            elif os.path.exists(self.err_path(h)):
                with open(self.err_path(h), encoding="utf-8", errors="replace") as f:
                    out[h] = {"state": "failed", "error": f.read()}
            elif why:
                out[h] = {"state": "failed", "error": "Cannot compile diagrams: LaTeX tools " + why + "."}
            else:
                out[h] = {"state": "pending"}
        return out

    # -- compiling -----------------------------------------------------------

    def _worker(self):
        while True:
            with self.cond:
                while not self.queue:
                    self.cond.wait()
                batch = dict(self.queue)
                self.queue.clear()
                self.running.update(batch)
            try:
                by_pre: dict = {}
                for h, (pre, src) in batch.items():
                    by_pre.setdefault(pre, []).append((h, src))
                for pre, items in by_pre.items():
                    self._compile(pre, items)
            except Exception as exc:  # never let the worker die
                for h in batch:
                    if not os.path.exists(self.svg_path(h)):
                        self._write(self.err_path(h), "galley error: %s" % exc)
            finally:
                with self.cond:
                    self.running.difference_update(batch)

    def _write(self, path: str, text: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

    def _compile(self, preamble: str, items: list):
        ok, err = self._run(preamble, items, "batch")
        if not ok and re.search(r"File `[^']*\.cls' not found", err):
            # The document class is not installed here: try the standard article class instead.
            fallback = re.sub(r"\\documentclass\s*(\[[^\]]*\])?\s*\{[^}]*\}", r"\\documentclass{article}", preamble, count=1)
            if fallback != preamble:
                preamble = fallback
                ok, err = self._run(preamble, items, "batch")
        if ok or len(items) == 1:
            if not ok:
                self._write(self.err_path(items[0][0]), err)
            return
        for h, src in items:  # find out which ones fail
            ok, err = self._run(preamble, [(h, src)], h)
            if not ok:
                self._write(self.err_path(h), err)

    def _run(self, preamble: str, items: list, name: str):
        work = os.path.join(self.cache_dir, "work-" + name)
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        try:
            body = "\n\\clearpage\n".join("%s\n" % src for _, src in items)
            # TikZ's default (dvips) driver draws with PostScript, which dvisvgm can only convert
            # with Ghostscript; without it arrows vanish and labels pile up. The dvisvgm driver
            # writes SVG directly.
            tex = ("\\def\\pgfsysdriver{pgfsys-dvisvgm.def}\n" + preamble.rstrip() + "\n\\pagestyle{empty}\n\\begin{document}\n\\pagestyle{empty}\\thispagestyle{empty}\n"
                   + body + "\n\\end{document}\n")
            with open(os.path.join(work, "d.tex"), "w", encoding="utf-8") as f:
                f.write(tex)
            env = dict(os.environ)
            env["TEXINPUTS"] = self.doc_dir + os.pathsep + env.get("TEXINPUTS", "")
            env.update(LATEX_SAFE_ENV)
            try:
                r = subprocess.run([self.latex, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "d.tex"], cwd=work, env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
            except subprocess.TimeoutExpired:
                return False, "LaTeX took longer than 3 minutes."
            logp = os.path.join(work, "d.log")
            if os.path.exists(logp):
                with open(logp, encoding="utf-8", errors="replace") as fh:
                    log = fh.read()
            else:
                log = r.stdout.decode(errors="replace")
            if r.returncode != 0 or not os.path.exists(os.path.join(work, "d.dvi")):
                return False, log_error(log)
            m = re.search(r"Output written on d\.dvi \((\d+) pages?", log)
            if not m or int(m.group(1)) != len(items):
                return False, "expected %d page(s), LaTeX wrote %s" % (len(items), m.group(1) if m else "none")
            try:
                r = subprocess.run([self.dvisvgm, "--no-fonts", "--exact-bbox", "--page=1-", "--output=p%p.svg", "d.dvi"],
                                   cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
            except subprocess.TimeoutExpired:
                return False, "dvisvgm took longer than 3 minutes."
            pages = {}
            for p in glob.glob(os.path.join(work, "p*.svg")):
                n = re.search(r"p0*(\d+)\.svg$", p)
                if n:
                    pages[int(n.group(1))] = p
            if len(pages) != len(items):
                return False, "dvisvgm failed:\n" + r.stdout.decode(errors="replace")[-1500:]
            for i, (h, _) in enumerate(items, 1):
                os.makedirs(self.cache_dir, exist_ok=True)
                os.replace(pages[i], self.svg_path(h))
            return True, ""
        finally:
            shutil.rmtree(work, ignore_errors=True)
