"""Layouts that real papers use: environments that share a line with other text, references
that look external, title data, PDF figures, xymatrix diagrams, text commands (galley 1.0.1)."""
import os
import re
import shutil
import tempfile
import time
import unittest

from helpers import SAMPLES, GalleyTestCase, galley_server  # noqa: F401  (sets up the import path)
import figures
import latex_doc


def walk(blocks):
    for b in blocks:
        yield b
        yield from walk(b.get("children", []))


def parse_text(tex: str) -> dict:
    d = tempfile.mkdtemp(prefix="galley-tex-")
    try:
        with open(os.path.join(d, "main.tex"), "w") as fh:
            fh.write(tex)
        return latex_doc.parse_document(d, "main.tex", None, None)
    finally:
        shutil.rmtree(d, ignore_errors=True)


class Structure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = latex_doc.parse_document(os.path.join(SAMPLES, "structure"), "main.tex", None, None)
        cls.blocks = list(walk(cls.d["blocks"]))
        cls.html = "\n".join(b.get("html", "") for b in cls.blocks)

    def test_all_sections_survive(self):
        self.assertEqual([b.get("number") for b in self.d["blocks"] if b["kind"] == "section"], ["1", "2", "3"])
        self.assertNotIn("\\section", self.html)

    def test_proof_after_end_on_same_line(self):
        envs = [(b.get("env"), b["anchor"]["start"]) for b in self.blocks if b["kind"] == "env"]
        self.assertIn(("proposition", 16), envs)
        self.assertIn(("proof", 18), envs)  # \end{proposition}\begin{proof} share line 18

    def test_environment_after_text_and_text_after_end(self):
        ex = [b for b in self.blocks if b.get("env") == "example"]
        self.assertEqual(len(ex), 1)
        self.assertEqual(ex[0]["labels"], ["ex:mid:line"])
        self.assertIn("Some text before an example.", self.html)
        self.assertIn("Text after the example on the same line.", self.html)

    def test_reference_with_colon_is_internal(self):
        self.assertIn('href="#lbl-ex:mid:line"', self.html)
        self.assertNotIn("extref", self.html)

    def test_xymatrix_and_tikzpicture_are_diagrams(self):
        sources = sorted(self.d["diagrams"].values())
        self.assertEqual(len(sources), 2)
        self.assertTrue(any("\\xymatrix" in s for s in sources))
        self.assertTrue(any(s.startswith("\\begin{tikzpicture}") for s in sources))
        self.assertNotIn("see the PDF", self.html)
        self.assertIn("Figure 2:", self.html)

    def test_mathjax_macros_for_arrows_and_breaks(self):
        m = self.d["macros"]
        self.assertEqual(m["xlongrightarrow"], ["\\xrightarrow[#1]{#2}", 2, ""])
        self.assertEqual(m["xLongleftarrow"], ["\\xLeftarrow[#1]{#2}", 2, ""])
        self.assertEqual(m["allowbreak"], "")
        self.assertEqual(m["qedhere"], "")

    def test_title_authors_date(self):
        self.assertEqual(self.d["authors_html"], "First Author, Second Author")
        self.assertRegex(self.d["date_html"], r"^[A-Z][a-z]+ \d{1,2}, \d{4}$")

    def test_spacing_commands_leave_no_trace(self):
        self.assertNotIn("unknown", self.html)
        self.assertNotIn("linespread", self.html)
        self.assertNotIn("1.08", self.html)

    def test_pdf_figure_is_an_image(self):
        self.assertIn('<img class="figure-img" src="/figure?path=square.pdf"', self.html)


class Commands(unittest.TestCase):
    def test_mathbbm_and_text_commands(self):
        d = parse_text("\\documentclass{article}\n\\begin{document}\n"
                       "Unit $\\mathbbm{1}$. \\fbox{boxed}\\nopagebreak[4] and \\leavevmode done.\n"
                       "\\acknowledgments We thank X.\n\\end{document}\n")
        self.assertEqual(d["macros"]["mathbbm"], ["\\mathbb{#1}", 1])
        html = "\n".join(b.get("html", "") for b in walk(d["blocks"]))
        self.assertIn('<span class="fbox">boxed</span>', html)
        self.assertIn("<strong>Acknowledgments.</strong>", html)
        self.assertNotIn("unknown", html)
        self.assertNotIn("[4]", html)

    def test_unmatched_end_does_not_swallow_the_document(self):
        d = parse_text("\\documentclass{article}\n\\begin{document}\n\\section{A}\nText.\n\\end{proof}\n\n"
                       "More.\n\\section{B}\nLast.\n\\end{document}\n")
        self.assertEqual([b.get("number") for b in d["blocks"] if b["kind"] == "section"], ["1", "2"])

    def test_external_document_labels(self):
        tmp = tempfile.mkdtemp(prefix="galley-xr-")
        try:
            with open(os.path.join(tmp, "supp.aux"), "w") as fh:
                fh.write("\\newlabel{lem:far}{{S3}{1}}\n")
            with open(os.path.join(tmp, "main.tex"), "w") as fh:
                fh.write("\\documentclass{article}\n\\usepackage{xr}\n\\externaldocument[S-]{supp}\n\\begin{document}\n"
                         "See Lemma~\\ref{S-lem:far}.\n\\end{document}\n")
            d = latex_doc.parse_document(tmp, "main.tex", None, None)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        html = "\n".join(b.get("html", "") for b in walk(d["blocks"]))
        self.assertIn('class="extref"', html)
        self.assertIn("S3", html)


class QuickLook(unittest.TestCase):
    """The qlmanage path, with a stand-in for macOS's qlmanage (it writes <file>.png into -o)."""

    def test_qlmanage_command_and_output(self):
        work = tempfile.mkdtemp(prefix="galley-ql-")
        try:
            fake = os.path.join(work, "qlmanage")
            log = os.path.join(work, "args.txt")
            with open(fake, "w") as fh:
                fh.write("#!/bin/sh\necho \"$@\" > '%s'\n"
                         "while [ $# -gt 0 ]; do case $1 in -o) out=$2; shift;; -s|-t) [ $1 = -s ] && shift;; *) src=$1;; esac; shift; done\n"
                         "printf '\\211PNG fake' > \"$out/$(basename \"$src\").png\"\n" % log)
            os.chmod(fake, 0o755)
            conv = figures.FigureConverter(os.path.join(work, "cache"))
            conv.tools = [("qlmanage", fake)]
            png = conv.png_for(os.path.join(SAMPLES, "structure", "square.pdf"))
            self.assertTrue(png and png.endswith("-%s.png" % figures.CACHE_VERSION), png)
            args = open(log).read().split()
            self.assertEqual(args[:3], ["-t", "-s", "600"])  # 100 pt page at 216 dpi = 300 px, raised to the minimum
            self.assertEqual(figures.page_size(os.path.join(SAMPLES, "structure", "square.pdf")), (100.0, 100.0))
        finally:
            shutil.rmtree(work, ignore_errors=True)


class PdfFigures(GalleyTestCase):
    sample = "structure"

    def test_pdf_figure_served_as_png_or_refused(self):
        s = self.start()
        status, headers, raw = s.request("GET", "/figure?path=square.pdf")
        if figures.converters():
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "image/png")
            self.assertTrue(raw.startswith(b"\x89PNG"))
            cache = os.path.join(self.docdir, "galley", "build", "figures")
            self.assertEqual(len([f for f in os.listdir(cache) if f.endswith("-%s.png" % figures.CACHE_VERSION)]), 1)
        else:
            self.assertEqual(status, 404)  # the page then shows a link to the PDF

    def test_no_converter_gives_404(self):
        s = self.start()
        s.app.figures.tools = []
        status, _, _ = s.request("GET", "/figure?path=square.pdf")
        self.assertEqual(status, 404)

    def test_figure_path_outside_refused(self):
        s = self.start()
        status, _, _ = s.request("GET", "/figure?path=../../etc/passwd")
        self.assertNotEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
