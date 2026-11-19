"""The server's protections: the key of each run, Host and Origin checks, JSON-only POSTs, the
page's Content-Security-Policy, and what may be read or written."""
import os
import unittest

from helpers import GalleyTestCase


class KeyAndOrigin(GalleyTestCase):
    def setUp(self):
        super().setUp()
        self.s = self.start()

    def test_page_needs_key(self):
        status, headers, raw = self.s.request("GET", "/", page=False)
        self.assertEqual(status, 403)
        self.assertIn(b"needs its key", raw)

    def test_link_with_key_sets_cookie(self):
        status, headers, raw = self.s.request("GET", "/?t=" + self.s.app.token, page=False)
        self.assertEqual(status, 303)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(headers.get("Location"), "/")

    def test_wrong_key_in_link(self):
        status, _, raw = self.s.request("GET", "/?t=wrong", page=False)
        self.assertEqual(status, 403)

    def test_page_with_cookie_and_policy(self):
        status, headers, raw = self.s.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'name="galley-key"', raw)
        csp = headers["Content-Security-Policy"]
        self.assertIn("script-src 'self';", csp)
        self.assertNotIn("http", csp)  # no outside hosts at all (MathJax is included)
        self.assertIn("default-src 'none'", csp)

    def test_api_needs_key(self):
        status, _, _ = self.s.request("GET", "/api/doc", page=False)
        self.assertEqual(status, 403)
        status, _ = self.s.get_json("/api/doc")
        self.assertEqual(status, 200)

    def test_whoami_is_open(self):
        status, j = self.s.get_json("/api/whoami", page=False)
        self.assertEqual(j["app"], "galley")

    def test_foreign_host_refused(self):
        status, _, _ = self.s.request("GET", "/api/doc", headers={"Host": "evil.example:%d" % self.s.port})
        self.assertEqual(status, 403)

    def test_post_from_other_origin_refused(self):
        status, j = self.s.post_json("/api/settings", {"mark_agent_text": True}, headers={"Origin": "http://127.0.0.2:8000"})
        self.assertEqual(status, 403)
        self.assertFalse(self.s.doc.settings["mark_agent_text"])

    def test_post_without_key_header_refused(self):
        status, j = self.s.post_json("/api/settings", {"mark_agent_text": True}, headers={"X-Galley-Key": "nope"})
        self.assertEqual(status, 403)

    def test_post_needs_json(self):
        status, _, _ = self.s.request("POST", "/api/settings", '{"mark_agent_text": true}',
                                      headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertFalse(self.s.doc.settings["mark_agent_text"])

    def test_post_from_own_page_works(self):
        status, j = self.s.post_json("/api/settings", {"mark_agent_text": True})
        self.assertEqual(status, 200)
        self.assertTrue(j["settings"]["mark_agent_text"])

    def test_mathjax_served_locally(self):
        status, headers, raw = self.s.request("GET", "/mathjax/tex-chtml-full.js")
        self.assertEqual(status, 200)
        self.assertGreater(len(raw), 1000000)
        status, headers, raw = self.s.request("GET", "/mathjax/output/chtml/fonts/woff-v2/MathJax_Main-Regular.woff")
        self.assertEqual(status, 200)
        status, headers, raw = self.s.request("GET", "/mathjax/../galley_server.py")
        self.assertNotEqual(status, 200)
        status, headers, raw = self.s.request("GET", "/mathjax/a11y/explorer.js")  # not included: no redirect elsewhere
        self.assertEqual(status, 404)
        self.assertNotIn("Location", headers)

    def test_json_responses_are_sandboxed(self):
        status, headers, raw = self.s.request("GET", "/api/doc")
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")


class Paths(GalleyTestCase):
    def setUp(self):
        super().setUp()
        self.s = self.start()

    def test_asset_outside_refused(self):
        status, j = self.s.get_json("/asset?path=../../../../etc/passwd")
        self.assertEqual(status, 500)
        self.assertIn("outside", j["error"])

    def test_symlink_out_refused(self):
        os.symlink("/etc/hostname", os.path.join(self.docdir, "link.txt"))
        status, j = self.s.get_json("/asset?path=link.txt")
        self.assertIn("outside", j.get("error", ""))

    def test_source_of_document_file(self):
        status, j = self.s.get_json("/api/source?file=main.tex&start=1&end=1")
        self.assertEqual(j["text"], "\\documentclass{article}")

    def test_hand_edit_of_settings_refused(self):
        before = open(os.path.join(self.docdir, "galley", "settings.json")).read()
        status, j = self.s.post_json("/api/edit", {"file": "galley/settings.json", "start": 1, "end": 1,
                                                   "original": "{", "text": "{\"x\": 1,"})
        self.assertIn("not one of the document's source files", j.get("error", ""))
        self.assertEqual(before, open(os.path.join(self.docdir, "galley", "settings.json")).read())

    def test_hand_edit_of_source(self):
        status, j = self.s.post_json("/api/edit", {"file": "sections/results.tex", "start": 1, "end": 1,
                                                   "original": "\\section{Results}", "text": "\\section{Main results}"})
        self.assertTrue(j.get("changed"), j)
        self.assertIn("Main results", open(os.path.join(self.docdir, "sections", "results.tex")).read())


if __name__ == "__main__":
    unittest.main()
