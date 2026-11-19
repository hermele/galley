"""Shared helpers for galley's tests: a sample document in a temporary folder, a galley server
running in this process, and an HTTP client that behaves like galley's page (or not)."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import galley_server  # noqa: E402

SAMPLES = os.path.join(HERE, "samples")


class TempHome:
    """A temporary folder used as the config home, so tests never touch the real one."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="galley-test-")
        self.saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.dir, "config")
        os.environ["GIT_CONFIG_GLOBAL"] = os.path.join(self.dir, "gitconfig")
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"

    def close(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)


def copy_sample(name: str, dest_parent: str) -> str:
    dest = os.path.join(dest_parent, name)
    shutil.copytree(os.path.join(SAMPLES, name), dest)
    return dest


def write_config(extra: dict | None = None):
    cfg = {"person": "Tester",
           "agents": {"mock": {"label": "Mock", "command": [sys.executable, os.path.join(HERE, "mock_agent.py")],
                               "result": "stdout", "model": "", "efforts": []}},
           "default_agent": "mock"}
    cfg.update(extra or {})
    path = galley_server.user_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)


class Server:
    """galley serving a document from this process."""

    def __init__(self, main_path: str):
        self.doc = galley_server.Document(main_path)
        self.cfg = galley_server.load_user_config(self.doc.dir, interactive=False)
        self.cfg["default_agent"] = "mock" if "mock" in self.cfg["agents"] else self.cfg["default_agent"]
        self.app = galley_server.App(self.doc, self.cfg)
        self.app.doc_json()
        self.httpd = galley_server.ThreadingHTTPServer(("127.0.0.1", 0), None)
        self.port = self.httpd.server_address[1]
        self.app.port = self.port
        self.httpd.RequestHandlerClass = galley_server.make_handler(self.app)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = "galley_%d=%s" % (self.port, self.app.token)
        self.origin = "http://127.0.0.1:%d" % self.port

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method: str, path: str, body=None, headers: dict | None = None, page: bool = True):
        """page=True: send what galley's own page sends (cookie, key header, JSON, own origin)."""
        h = {"Host": "127.0.0.1:%d" % self.port}
        if page:
            h["Cookie"] = self.cookie
            if method == "POST":
                h.update({"X-Galley-Key": self.app.token, "Content-Type": "application/json", "Origin": self.origin})
        h.update(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
            raw = r.read()
            return r.status, dict(r.getheaders()), raw
        finally:
            conn.close()

    def get_json(self, path: str, **kw):
        status, headers, raw = self.request("GET", path, **kw)
        return status, json.loads(raw or b"{}")

    def post_json(self, path: str, body: dict, **kw):
        status, headers, raw = self.request("POST", path, body, **kw)
        return status, json.loads(raw or b"{}")

    def wait_job(self, jid: str, timeout: float = 30) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            status, j = self.get_json("/api/job/" + jid)
            if j.get("status") != "running":
                return j
            time.sleep(0.1)
        raise AssertionError("job %s did not finish" % jid)


class GalleyTestCase(unittest.TestCase):
    """A fresh copy of a sample document and config home per test."""

    sample = "basic"

    def setUp(self):
        self.home = TempHome()
        write_config()
        self.work = tempfile.mkdtemp(prefix="galley-doc-")
        self.docdir = copy_sample(self.sample, self.work)
        self.main = os.path.join(self.docdir, "main.tex")
        self.server = None

    def start(self) -> Server:
        self.server = Server(self.main)
        return self.server

    def tearDown(self):
        if self.server:
            self.server.close()
        shutil.rmtree(self.work, ignore_errors=True)
        self.home.close()
