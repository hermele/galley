"""tikzcd diagrams are compiled automatically, so LaTeX must not run commands or touch files
outside its work folder, whatever the document's preamble says. Needs latex, dvisvgm, tikz-cd."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from helpers import GalleyTestCase, galley_server  # noqa: F401  (sets up the import path)
import diagrams

HAVE_TEX = bool(diagrams.find_tool("latex") and diagrams.find_tool("dvisvgm")) and subprocess.run(
    ["kpsewhich", "tikz-cd.sty"], capture_output=True, text=True).stdout.strip() != "" if shutil.which("kpsewhich") else False

SQUARE = "\\begin{tikzcd}\nA \\arrow[r] & B\n\\end{tikzcd}"


@unittest.skipUnless(HAVE_TEX, "latex, dvisvgm or tikz-cd not installed")
class Diagrams(GalleyTestCase):
    sample = "diagrams"

    def compile(self, preamble, source=SQUARE, h="0123456789abcdef"):
        cache = os.path.join(self.docdir, "galley", "build", "diagrams")
        dc = diagrams.DiagramCompiler(cache, self.docdir)
        dc.request(preamble, {h: source})
        end = time.time() + 120
        while time.time() < end:
            st = dc.status([h])[h]
            if st["state"] != "pending":
                return st
            time.sleep(0.2)
        raise AssertionError("diagram not compiled")

    def test_sample_document_diagram(self):
        s = self.start()
        status, d = s.get_json("/api/doc")
        self.assertEqual(len(d["diagrams"]), 1)
        h = d["diagrams"][0]
        end = time.time() + 120
        while time.time() < end:
            status, st = s.post_json("/api/diagrams", {"hashes": [h]})
            if st[h]["state"] != "pending":
                break
            time.sleep(0.3)
        self.assertEqual(st[h]["state"], "done", st)
        status, headers, raw = s.request("GET", "/api/diagram/%s.svg" % h)
        self.assertIn(b"<svg", raw)
        self.assertIn("sandbox", headers["Content-Security-Policy"])

    def test_xymatrix_diagram(self):
        if not shutil.which("kpsewhich") or not subprocess.run(["kpsewhich", "xy.sty"], capture_output=True,
                                                                  text=True).stdout.strip():
            self.skipTest("xy package not installed")
        pre = "\\documentclass{article}\n\\usepackage[all]{xy}\n"
        st = self.compile(pre, "\\[\n\\xymatrix{ A \\ar[r]^f & B }\n\\]", h="00aa11bb22cc33dd")
        self.assertEqual(st["state"], "done", st)

    def test_tikzpicture(self):
        pre = "\\documentclass{article}\n\\usepackage{tikz}\n"
        src = "\\begin{tikzpicture}\n\\draw[blue] (0,0) rectangle (1,1);\n\\node at (0.5,0.5) {$X$};\n\\end{tikzpicture}"
        st = self.compile(pre, src, h="11aa22bb33cc44dd")
        self.assertEqual(st["state"], "done", st)

    def test_input_from_document_folder_still_works(self):
        with open(os.path.join(self.docdir, "mymacros.tex"), "w") as fh:
            fh.write("\\newcommand{\\obj}{X}\n")
        pre = "\\documentclass{article}\n\\usepackage{tikz-cd}\n\\input{mymacros}\n"
        st = self.compile(pre, "\\begin{tikzcd}\n\\obj \\arrow[r] & B\n\\end{tikzcd}")
        self.assertEqual(st["state"], "done", st)

    def test_no_shell_escape(self):
        target = os.path.join(self.work, "shell-ran")
        pre = ("\\documentclass{article}\n\\usepackage{tikz-cd}\n"
               "\\immediate\\write18{touch %s}\n" % target)
        self.compile(pre)
        self.assertFalse(os.path.exists(target))

    def test_no_writing_outside(self):
        target = os.path.join(self.work, "written.tex")
        pre = ("\\documentclass{article}\n\\usepackage{tikz-cd}\n"
               "\\newwrite\\gw\\immediate\\openout\\gw=%s\\immediate\\write\\gw{x}\\immediate\\closeout\\gw\n" % target)
        self.compile(pre)
        self.assertFalse(os.path.exists(target))
        pre2 = ("\\documentclass{article}\n\\usepackage{tikz-cd}\n"
                "\\newwrite\\gw\\immediate\\openout\\gw=../../../../escaped.tex\\immediate\\closeout\\gw\n")
        self.compile(pre2, h="fedcba9876543210")
        self.assertFalse(os.path.exists(os.path.join(self.docdir, "escaped.tex")))

    def test_no_reading_outside(self):
        secret = os.path.join(self.work, "secret.txt")
        with open(secret, "w") as fh:
            fh.write("secret\n")
        pre = ("\\documentclass{article}\n\\usepackage{tikz-cd}\n"
               "\\newread\\gr\\openin\\gr=%s\n\\ifeof\\gr\\else\\errmessage{READ-SUCCEEDED}\\fi\n" % secret)
        st = self.compile(pre)
        self.assertNotIn("READ-SUCCEEDED", st.get("error", ""))

    def test_control_plain_latex_reads_absolute_paths(self):
        """Shows test_no_reading_outside can fail: plain latex reads the file."""
        secret = os.path.join(self.work, "secret.txt")
        with open(secret, "w") as fh:
            fh.write("secret\n")
        work = tempfile.mkdtemp(dir=self.work)
        with open(os.path.join(work, "t.tex"), "w") as fh:
            fh.write("\\newread\\gr\\openin\\gr=%s\n\\ifeof\\gr\\else\\message{READ-SUCCEEDED}\\fi\\end\n" % secret)
        env = dict(os.environ, openin_any="a")
        r = subprocess.run(["tex", "-interaction=nonstopmode", "t.tex"], cwd=work, env=env, capture_output=True, text=True)
        self.assertIn("READ-SUCCEEDED", r.stdout)


if __name__ == "__main__":
    unittest.main()
