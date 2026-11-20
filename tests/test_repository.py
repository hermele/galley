"""The repository itself: included MathJax unchanged, license notices present, nothing left from
the workspace galley was first written in."""
import hashlib
import os
import re
import unittest

from helpers import ROOT, galley_server

SOURCES = ["galley_server.py", "latex_doc.py", "diagrams.py", "figures.py", "static/app.js", "static/app.css", "make_bundle.sh"]


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class Repository(unittest.TestCase):
    def test_mathjax_checksums(self):
        base = os.path.join(ROOT, "vendor", "mathjax")
        listed = {}
        for line in read("vendor/mathjax/SHA256SUMS").splitlines():
            digest, name = line.split(None, 1)
            listed[name.strip()] = digest
        self.assertIn("tex-chtml-full.js", listed)
        self.assertEqual(len([n for n in listed if n.endswith(".woff")]), 23)
        for name, digest in listed.items():
            with open(os.path.join(base, name), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), digest, name)
        on_disk = {os.path.relpath(os.path.join(d, f), base) for d, _, fs in os.walk(base) for f in fs
                   if f != ".DS_Store"}  # left by macOS Finder; Git-ignored
        self.assertEqual(on_disk - set(listed), {"README.md", "SHA256SUMS"})

    def test_license_and_notice(self):
        lic = read("LICENSE")
        self.assertTrue(lic.lstrip().startswith("GNU AFFERO GENERAL PUBLIC LICENSE"))
        self.assertIn("Version 3, 19 November 2007", lic)
        notice = read("NOTICE")
        self.assertIn("Copyright (C) 2026 The Regents of the University of Colorado", notice)
        self.assertIn('"Based on galley by Michael Hermele"', notice)
        self.assertIn("section 7(b)", notice)

    def test_source_headers(self):
        for rel in SOURCES:
            head = read(rel)[:1200]
            self.assertIn("AGPL-3.0-or-later", head, rel)
            self.assertIn("Regents of the University of Colorado", head, rel)

    def test_about_and_readme_attribution(self):
        self.assertTrue("galley by Michael Hermele" in read("static/app.js"))
        self.assertTrue("galley by Michael Hermele" in read("README.md"))
        self.assertTrue("Version %s" % galley_server.VERSION in read("README.md"))

    def test_no_outside_hosts_or_old_headers(self):
        """No script host in the code or docs, and no per-request authorship lines in headers.
        (The maintainer also checks for private names with a separate, private list.)"""
        bad = re.compile(r"cdn\.jsdelivr|unpkg\.com|Cowork\) at", re.I)
        for d, dirs, files in os.walk(ROOT):
            dirs[:] = [x for x in dirs if x not in (".git", "vendor", "__pycache__", "internal")]
            for f in files:
                if f.endswith((".py", ".js", ".css", ".html", ".md", ".sh", ".tex", ".yml")) or f in ("galley", "NOTICE"):
                    rel = os.path.relpath(os.path.join(d, f), ROOT)
                    if rel in ("CLAUDE.md", os.path.join("tests", "test_repository.py")):  # CLAUDE.md: private, not committed
                        continue
                    m = bad.search(read(rel))
                    self.assertIsNone(m, "%s: %s" % (rel, m and m.group(0)))


if __name__ == "__main__":
    unittest.main()
