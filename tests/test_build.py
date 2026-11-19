"""Build PDF: galley's default command runs without asking; any other command, or a latexmkrc
(or a script or Makefile the command names), needs the person's approval first, remembered in
the user's config and invalidated when the command or those files change."""
import json
import os
import sys
import unittest

from helpers import GalleyTestCase, galley_server


class BuildApproval(GalleyTestCase):
    def set_build(self, cmd):
        os.makedirs(os.path.join(self.docdir, "galley"), exist_ok=True)
        with open(os.path.join(self.docdir, "galley", "settings.json"), "w") as fh:
            json.dump({"main": "main.tex", "build": cmd}, fh)

    def marker(self):
        return os.path.join(self.docdir, "built.txt")

    def custom_command(self):
        return [sys.executable, "-c", "open('built.txt', 'a').write('x')", "{main}"]

    def test_custom_command_needs_approval(self):
        self.set_build(self.custom_command())
        s = self.start()
        status, r = s.post_json("/api/build", {})
        self.assertFalse(r["ok"])
        a = r["needs_approval"]
        self.assertIn("built.txt", a["command"])
        self.assertFalse(os.path.exists(self.marker()))
        # A wrong fingerprint does not count as approval.
        status, r = s.post_json("/api/build", {"approve": "0" * 64})
        self.assertIn("needs_approval", r)
        self.assertFalse(os.path.exists(self.marker()))
        status, r = s.post_json("/api/build", {"approve": a["fingerprint"]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(open(self.marker()).read(), "x")
        # Remembered (outside the document), so the next build runs directly.
        status, r = s.post_json("/api/build", {})
        self.assertTrue(r["ok"], r)
        approvals = json.load(open(galley_server.approvals_path()))
        self.assertIn(s.doc.main_path, approvals)
        self.assertFalse(any("approv" in name for name in os.listdir(os.path.join(self.docdir, "galley"))))

    def test_changed_command_asks_again(self):
        self.set_build(self.custom_command())
        s = self.start()
        a = s.post_json("/api/build", {})[1]["needs_approval"]
        s.post_json("/api/build", {"approve": a["fingerprint"]})
        s.doc.settings["build"] = [sys.executable, "-c", "open('other.txt', 'w')"]
        status, r = s.post_json("/api/build", {})
        self.assertIn("needs_approval", r)
        self.assertFalse(os.path.exists(os.path.join(self.docdir, "other.txt")))

    def test_latexmkrc_needs_approval_and_change_asks_again(self):
        rc = os.path.join(self.docdir, "latexmkrc")
        with open(rc, "w") as fh:
            fh.write("$pdf_mode = 1;\n")
        s = self.start()
        plan = s.app.build_plan()
        self.assertTrue(plan["needs_approval"])
        self.assertEqual([f["name"] for f in plan["files"]], ["latexmkrc"])
        galley_server.save_approval(s.doc.main_path, plan["fingerprint"])
        self.assertFalse(s.app.build_plan()["needs_approval"])
        with open(rc, "a") as fh:
            fh.write("system('touch /tmp/x');\n")
        plan = s.app.build_plan()
        self.assertTrue(plan["needs_approval"])
        self.assertIn("system(", plan["files"][0]["text"])

    def test_script_named_by_command_is_covered(self):
        with open(os.path.join(self.docdir, "build.sh"), "w") as fh:
            fh.write("echo one\n")
        self.set_build(["sh", "build.sh"])
        s = self.start()
        plan = s.app.build_plan()
        self.assertEqual([f["name"] for f in plan["files"]], ["build.sh"])
        galley_server.save_approval(s.doc.main_path, plan["fingerprint"])
        with open(os.path.join(self.docdir, "build.sh"), "w") as fh:
            fh.write("echo two\n")
        self.assertTrue(s.app.build_plan()["needs_approval"])

    def test_default_command_needs_no_approval(self):
        s = self.start()
        plan = s.app.build_plan()
        self.assertFalse(plan["needs_approval"])
        self.assertEqual(plan["command"][0], "latexmk")
        self.assertEqual(plan["command"][-1], "main.tex")

    def test_main_file_starting_with_dash(self):
        os.rename(self.main, os.path.join(self.docdir, "-shell-escape.tex"))
        self.main = os.path.join(self.docdir, "-shell-escape.tex")
        s = self.start()
        self.assertEqual(s.app.build_plan()["command"][-1], "./-shell-escape.tex")

    def test_approval_by_other_site_refused(self):
        self.set_build(self.custom_command())
        s = self.start()
        a = s.post_json("/api/build", {})[1]["needs_approval"]
        status, r = s.post_json("/api/build", {"approve": a["fingerprint"]}, headers={"Origin": "http://127.0.0.2:9000"})
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(self.marker()))


if __name__ == "__main__":
    unittest.main()
