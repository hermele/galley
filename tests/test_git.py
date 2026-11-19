"""galley's git calls never run programs named by a repository's own settings."""
import os
import shutil
import subprocess
import unittest

from helpers import GalleyTestCase, galley_server


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitHardening(GalleyTestCase):
    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.docdir, check=True, capture_output=True)

    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("-c", "user.name=T", "-c", "user.email=t@example.org", "add", "-A")
        self.git("-c", "user.name=T", "-c", "user.email=t@example.org", "commit", "-qm", "sample")
        self.ran = os.path.join(self.work, "ran")
        self.script = os.path.join(self.work, "evil.sh")
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\necho \"$0 $*\" >> '%s'\ncat\n" % self.ran)
        os.chmod(self.script, 0o755)

    def touch_sources(self):
        # Make the index's stat information stale, so `git status` has to look at file contents.
        for name in ("main.tex", "refs.bib"):
            p = os.path.join(self.docdir, name)
            st = os.stat(p)
            os.utime(p, (st.st_atime + 5, st.st_mtime + 5))

    def test_fsmonitor_not_run(self):
        self.git("config", "core.fsmonitor", self.script)
        self.touch_sources()
        s = self.start()
        status, d = s.get_json("/api/doc")
        self.assertTrue(d["git"]["repo"])
        self.assertTrue(d["git"]["status_checked"])
        self.assertFalse(os.path.exists(self.ran), open(self.ran).read() if os.path.exists(self.ran) else "")

    def test_clean_filter_not_run(self):
        self.git("config", "filter.evil.clean", self.script)
        with open(os.path.join(self.docdir, ".gitattributes"), "w") as fh:
            fh.write("* filter=evil\n")
        self.touch_sources()
        s = self.start()
        status, d = s.get_json("/api/doc")
        self.assertFalse(d["git"]["status_checked"])
        self.assertTrue(any("git status" in n for n in d["notices"]))
        self.assertFalse(os.path.exists(self.ran), open(self.ran).read() if os.path.exists(self.ran) else "")

    def test_included_filter_detected(self):
        inc = os.path.join(self.work, "inc.cfg")
        with open(inc, "w") as fh:
            fh.write("[filter \"evil\"]\n\tclean = %s\n" % self.script)
        self.git("config", "include.path", inc)
        self.assertIn("filter.evil.clean", galley_server.git_repo_programs(self.docdir))
        self.assertIsNone(galley_server.git_status(self.docdir, self.docdir))

    def test_control_status_works_without_risky_settings(self):
        with open(self.main, "a") as fh:
            fh.write("% changed\n")
        s = self.start()
        status, d = s.get_json("/api/doc")
        self.assertTrue(d["git"]["status_checked"])
        self.assertIn("main.tex", " ".join(d["git"]["changed"]))

    def test_control_plain_git_would_run_them(self):
        """Shows the tests above can fail: without galley's settings, git runs both programs."""
        self.git("config", "core.fsmonitor", self.script)
        self.git("config", "filter.evil.clean", self.script)
        with open(os.path.join(self.docdir, ".gitattributes"), "w") as fh:
            fh.write("* filter=evil\n")
        self.touch_sources()
        subprocess.run(["git", "status", "--porcelain"], cwd=self.docdir, capture_output=True, stdin=subprocess.DEVNULL)
        self.assertTrue(os.path.exists(self.ran))


if __name__ == "__main__":
    unittest.main()
