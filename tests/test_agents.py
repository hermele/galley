"""Agents: edits apply only to the document's sources (.tex files and named .bib files), only
when accepted; agents start inside the document folder; wrap-ups run once per record."""
import json
import os
import time
import unittest

from helpers import GalleyTestCase

PASSAGE = {"kind": "block", "file": "main.tex", "start": 16, "end": 19, "hash": "x"}


class Edits(GalleyTestCase):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.docdir, "latexmkrc"), "w") as fh:
            fh.write("$pdf_mode = 1;\n")
        self.s = self.start()

    def suggest(self, marker):
        status, r = self.s.post_json("/api/ask", {"mode": "suggest", "question": marker, "anchor": PASSAGE})
        return r["job"], self.s.wait_job(r["job"])

    def test_tex_edit_applies_only_when_accepted(self):
        jid, j = self.suggest("EDIT_TEX")
        self.assertEqual(j["status"], "done", j)
        self.assertTrue(j["proposal"]["ok"], j["proposal"])
        main = os.path.join(self.docdir, "main.tex")
        self.assertIn("It continues here", open(main).read())
        status, r = self.s.post_json("/api/decide", {"job": jid, "accept": True})
        self.assertTrue(r["ok"], r)
        self.assertIn("It goes on here", open(main).read())

    def test_bib_edit_allowed(self):
        self.assertIn("refs.bib", self.s.doc.editable_files())
        jid, j = self.suggest("EDIT_BIB")
        self.assertTrue(j["proposal"]["ok"], j["proposal"])
        status, r = self.s.post_json("/api/decide", {"job": jid, "accept": True})
        self.assertTrue(r["ok"], r)
        self.assertIn("2001", open(os.path.join(self.docdir, "refs.bib")).read())

    def test_latexmkrc_edit_refused_and_flagged(self):
        jid, j = self.suggest("EDIT_RC")
        p = j["proposal"]
        self.assertFalse(p["ok"])
        self.assertEqual([r["file"] for r in p["refused"]], ["latexmkrc"])
        self.assertIn("system(", p["refused"][0]["replacement"])
        self.assertTrue(any("edit refused: latexmkrc" in x for x in p["problems"]))
        status, r = self.s.post_json("/api/decide", {"job": jid, "accept": True})
        self.assertFalse(r["ok"])
        self.assertEqual(open(os.path.join(self.docdir, "latexmkrc")).read(), "$pdf_mode = 1;\n")
        self.assertIn("\\AA ngstr", open(os.path.join(self.docdir, "main.tex")).read())  # nothing applied
        notes = [json.loads(l) for l in open(os.path.join(self.docdir, "galley", "notes.jsonl"))]
        self.assertEqual(notes[-1]["refused"], ["latexmkrc"])

    def test_galley_folder_edit_refused(self):
        jid, j = self.suggest("EDIT_GALLEY")
        self.assertFalse(j["proposal"]["ok"])
        self.assertEqual(j["proposal"]["refused"][0]["file"], "galley/settings.json")


class AgentFolder(GalleyTestCase):
    def test_agent_cwd_outside_falls_back(self):
        os.makedirs(os.path.join(self.docdir, "galley"), exist_ok=True)
        with open(os.path.join(self.docdir, "galley", "settings.json"), "w") as fh:
            json.dump({"main": "main.tex", "agent_cwd": "../.."}, fh)
        s = self.start()
        self.assertEqual(s.doc.agent_cwd, s.doc.dir)
        status, d = s.get_json("/api/doc")
        self.assertTrue(any("agent_cwd" in n for n in d["notices"]))
        log = os.path.join(self.work, "agent.log")
        os.environ["GALLEY_TEST_AGENT_LOG"] = log
        try:
            status, r = s.post_json("/api/ask", {"mode": "explain", "question": "hi", "anchor": PASSAGE})
            s.wait_job(r["job"])
        finally:
            del os.environ["GALLEY_TEST_AGENT_LOG"]
        self.assertEqual(os.path.realpath(open(log).read().strip()), os.path.realpath(self.docdir))

    def test_agent_cwd_subfolder_allowed(self):
        with open(os.path.join(self.docdir, "galley.tmp"), "w"):
            pass
        os.makedirs(os.path.join(self.docdir, "galley"), exist_ok=True)
        with open(os.path.join(self.docdir, "galley", "settings.json"), "w") as fh:
            json.dump({"main": "main.tex", "agent_cwd": "sections"}, fh)
        s = self.start()
        self.assertEqual(s.doc.agent_cwd, os.path.join(self.docdir, "sections"))
        self.assertEqual(s.doc.agent_cwd_problem(), "")


class Wrapup(GalleyTestCase):
    def test_wrapup_once_per_record_and_listed(self):
        s = self.start()
        status, r = s.post_json("/api/note", {"anchor": PASSAGE, "text": "a comment"})
        name = os.path.basename(s.app.record.path)
        os.environ["GALLEY_TEST_AGENT_DELAY"] = "1.5"
        try:
            status, a = s.post_json("/api/wrapup", {"record": name})
            status, b = s.post_json("/api/wrapup", {"record": name})
            self.assertEqual(a["job"], b["job"])
            self.assertTrue(b.get("already"))
            status, running = s.get_json("/api/jobs/running")
            self.assertEqual([(x["id"], x["record"]) for x in running["items"]], [(a["job"], name)])
            j = s.wait_job(a["job"])
        finally:
            del os.environ["GALLEY_TEST_AGENT_DELAY"]
        self.assertEqual(j["status"], "done")
        status, running = s.get_json("/api/jobs/running")
        self.assertEqual(running["items"], [])
        self.assertIn("A canned answer", open(s.app.record.path).read())


if __name__ == "__main__":
    unittest.main()
