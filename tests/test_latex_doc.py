"""The LaTeX renderer on the sample documents: structure, numbering, tables, macros, links."""
import os
import unittest

from helpers import SAMPLES, galley_server  # noqa: F401  (sets up the import path)
import latex_doc


def walk(blocks):
    for b in blocks:
        yield b
        yield from walk(b.get("children", []))


class Basic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = latex_doc.parse_document(os.path.join(SAMPLES, "basic"), "main.tex", None, None)
        cls.blocks = list(walk(cls.d["blocks"]))
        cls.html = "\n".join(b.get("html", "") for b in cls.blocks)

    def test_files_and_title(self):
        self.assertEqual(self.d["files"], ["main.tex", "sections/results.tex"])
        self.assertEqual(self.d["title"], "A Sample Document for galley's Tests")

    def test_sections_and_environments(self):
        titles = [b.get("title") for b in self.d["blocks"] if b["kind"] == "section"]
        self.assertEqual(len(titles), 3)
        envs = [b.get("env") for b in self.blocks if b["kind"] == "env"]
        self.assertIn("theorem", envs)
        self.assertIn("proof", envs)

    def test_anchors_have_lines(self):
        for b in self.blocks:
            if "anchor" in b:
                a = b["anchor"]
                self.assertIn(a["file"], self.d["files"])
                self.assertLessEqual(a["start"], a["end"])

    def test_comment_line_does_not_split_paragraph(self):
        intro = [b for b in self.blocks if b["kind"] == "para" and "sample about" in b.get("html", "")]
        self.assertEqual(len(intro), 1)
        self.assertIn("It continues here", intro[0]["html"])

    def test_text_macro_expanded(self):
        self.assertIn("Galley\\(_2\\)", self.html)

    def test_links(self):
        self.assertIn('href="https://example.org"', self.html)
        self.assertNotIn("javascript:", self.html.replace("title=\"javascript:", ""))
        self.assertNotIn('href="javascript', self.html)

    def test_table(self):
        self.assertIn("<table", self.html)
        self.assertIn('colspan="2"', self.html)
        self.assertIn("Table 1:", self.html)

    def test_figure_numbered_and_referenced(self):
        self.assertIn("Figure 1:", self.html)
        self.assertIn('href="#lbl-fig:empty"', self.html)
        self.assertIn('href="#lbl-tab:small"', self.html)

    def test_accents(self):
        for word in ("étude", "naïve", "Ångström"):
            self.assertIn(word, self.html)

    def test_citation(self):
        self.assertIn('class="cite"', self.html)

    def test_macros_for_mathjax(self):
        self.assertIn("R", self.d["macros"])


class DiagramSource(unittest.TestCase):
    def test_tikzcd_found(self):
        d = latex_doc.parse_document(os.path.join(SAMPLES, "diagrams"), "main.tex", None, None)
        self.assertEqual(len(d["diagrams"]), 1)
        src = list(d["diagrams"].values())[0]
        self.assertIn("\\begin{tikzcd}", src)
        self.assertIn("tikz-cd", d["preamble_tex"])


if __name__ == "__main__":
    unittest.main()
