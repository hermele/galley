#!/usr/bin/env python3
"""galley: read a LaTeX document in your browser and work on it with AI agents.

galley by Michael Hermele. Copyright (C) 2026 The Regents of the University of Colorado.
This program is free software: you can redistribute it and/or modify it under the terms of
the GNU Affero General Public License as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version, with the additional term
under section 7(b) given in NOTICE. It is distributed WITHOUT ANY WARRANTY; see LICENSE.
SPDX-License-Identifier: AGPL-3.0-or-later

Standard library only; Python 3.9 or newer. Binds to 127.0.0.1.

    galley [main.tex] [--port N] [--no-browser]

galley renders the document, sends questions and edit requests about a passage, a
section or the whole document to an agent command-line tool (Codex or Claude Code)
running read-only, shows proposed edits for review, applies them only when accepted,
lets you edit the LaTeX directly, and keeps per-passage notes and session records in a
`galley/` folder next to the main file. It never commits or pushes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import hmac
import secrets
import threading
import time
import traceback
import urllib.request
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import latex_doc  # noqa: E402
import diagrams  # noqa: E402

VERSION = "1.0.0"
SOURCE_URL = "https://github.com/hermele/galley"
MAX_BODY = 20 * 1024 * 1024  # largest request body accepted (bytes)
FOLDER = "galley"  # per-document folder, next to the main .tex file

DEFAULT_AGENTS = {
    "codex": {
        "label": "Codex",
        # --skip-git-repo-check: Codex otherwise refuses folders that are neither a Git repository
        # nor trusted in ~/.codex/config.toml; galley's read-only sandbox is the protection that matters.
        "command": ["codex", "exec", "--skip-git-repo-check", "--cd", "{cwd}", "--sandbox", "read-only", "-m", "{model}",
                    "-c", "model_reasoning_effort=\"{effort}\"", "--output-last-message", "{outfile}", "-"],
        "result": "outfile",
        "model": "",
        "model_from": "~/.codex/config.toml",
        "efforts": ["low", "medium", "high"],
        "default_effort": "medium",
    },
    "claude": {
        "label": "Claude Code",
        "command": ["claude", "-p", "--allowedTools", "Read,Grep,Glob", "--output-format", "text"],
        "model_args": ["--model", "{model}"],
        "result": "stdout",
        "model": "",
        "efforts": [],
    },
}

DEFAULT_BUILD = ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-outdir=" + FOLDER + "/build", "{main}"]

AGTADD_PREAMBLE = r"""% Added by galley: agent-written text is wrapped in \AGTADD{...} and shown in violet.
% To print it in the normal colour, change \galleyshowagenttrue to \galleyshowagentfalse.
%XCOLOR%\newif\ifgalleyshowagent
\galleyshowagenttrue
\DeclareRobustCommand{\AGTADD}[1]{\ifgalleyshowagent{\color{violet}#1}\else{#1}\fi}
"""


# ---------------------------------------------------------------------------
# Small helpers


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def sha12(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# A folder someone else made (for example a zip that includes its .git directory) can carry Git
# settings that make git run a program: core.fsmonitor on almost any command, and filter drivers
# ("clean") when `git status` compares files. galley runs only read-only git commands, always with
# fsmonitor and hooks switched off and no pager, and runs `git status` only when the repository's
# own configuration defines no filter program (see git_status).
GIT_SAFE_ARGS = ["-c", "core.fsmonitor=", "-c", "core.hooksPath=" + os.devnull, "--no-pager"]
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0"}
GIT_PROGRAM_KEY = re.compile(r"^filter\..+\.(clean|smudge|process)$|^core\.fsmonitor$", re.I)


def git(args: list, cwd: str) -> str:
    """Output of a read-only git command (empty on any failure)."""
    try:
        return subprocess.run(["git", *GIT_SAFE_ARGS, *args], cwd=cwd, capture_output=True, text=True,
                              timeout=30, stdin=subprocess.DEVNULL, env=dict(os.environ, **GIT_ENV)).stdout
    except Exception:
        return ""


def git_repo_programs(cwd: str) -> list:
    """Settings in the repository's own configuration (scope local or worktree, including files
    it includes) that name a program git could run. None if git cannot tell (git < 2.26)."""
    out = git(["config", "--list", "--show-scope"], cwd)
    if not out.strip():
        return None
    found = []
    for line in out.splitlines():
        scope, _, entry = line.partition("\t")
        key = entry.split("=", 1)[0]
        if scope in ("local", "worktree") and GIT_PROGRAM_KEY.match(key):
            found.append(key)
    return found


def git_status(cwd: str, path: str):
    """`git status --porcelain` for path, or None when the repository's own settings define a
    program that status could run (it is then not checked)."""
    risky = git_repo_programs(cwd)
    if risky is None or any(k.lower() != "core.fsmonitor" for k in risky):
        return None
    return git(["status", "--porcelain", "--ignore-submodules=all", "--", path], cwd)


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write_text(path: str, text: str):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def excerpt(text: str, limit: int = 700) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut[-80:]:
        cut = cut[: cut.rfind(" ")]
    return cut + " \u2026"


def quote_md(text: str) -> str:
    return "\n".join("> " + l if l.strip() else ">" for l in (text or "").strip().splitlines())


AGENT_ERROR_HINTS = [
    (r"not inside a trusted directory|skip-git-repo-check",
     "Codex runs only in a Git repository or a folder it trusts. Add \"--skip-git-repo-check\" to the "
     "Codex command in ~/.config/galley/config.json (galley 0.4 and later do this by default), or mark the "
     "folder trusted in ~/.codex/config.toml."),
    (r"operation not permitted",
     "macOS may be blocking access to this folder: allow your terminal app under System Settings > "
     "Privacy & Security > Files and Folders (or Full Disk Access), then restart galley."),
    (r"not logged in|log ?in|unauthori[sz]ed|authenticat|api key|401",
     "The agent may need you to sign in again: run it once in a terminal (\"codex\" or \"claude\")."),
    (r"model .*(not found|not exist|unknown|unsupported)|unknown model|invalid model",
     "Check the model name in ~/.codex/config.toml or in galley's config (agents.<name>.model)."),
]


def agent_error(label: str, code, output: str) -> str:
    """An agent's failure, with its own error message first (not buried in its output) and a hint
    for the common causes."""
    lines = [l.strip() for l in (output or "").splitlines() if l.strip()]
    key = next((l for l in reversed(lines) if re.search(r"error|not inside|not permitted|denied|"
                                                        r"not logged|unauthori|not found|invalid", l, re.I)),
               lines[-1] if lines else "")
    msg = "%s stopped with an error (exit status %s): %s" % (label, code, key or "no output")
    for pat, hint in AGENT_ERROR_HINTS:
        if re.search(pat, output or "", re.I):
            msg += "\n\n" + hint
            break
    tail = "\n".join(lines[-15:])
    if tail and tail != key:
        msg += "\n\nEnd of the agent's output:\n" + tail
    return msg


def said(person: str, text: str) -> str:
    """'Ada: text' for a record; a message with line breaks is quoted so they survive Markdown."""
    text = (text or "").strip()
    if "\n" not in text:
        return "%s: %s" % (person, text)
    lines = text.splitlines()
    # Two trailing spaces: a Markdown line break, so the lines stay separate when rendered.
    hard = [l + "  " if l.strip() and k + 1 < len(lines) and lines[k + 1].strip() else l
            for k, l in enumerate(lines)]
    return "%s:\n\n%s" % (person, quote_md("\n".join(hard)))


_append_lock = threading.Lock()


def append_to(path: str, text: str):
    with _append_lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n\n")


# ---------------------------------------------------------------------------
# User configuration (~/.config/galley/config.json)


def user_config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "galley", "config.json")


def guess_person(doc_dir: str) -> str:
    name = git(["config", "user.name"], doc_dir).strip()
    if name:
        return name.split()[0]
    return getpass.getuser()


def load_user_config(doc_dir: str, interactive: bool) -> dict:
    path = user_config_path()
    cfg = {}
    if os.path.exists(path):
        try:
            cfg = json.loads(read_text(path))
        except json.JSONDecodeError as exc:
            sys.exit("galley: cannot read %s: %s" % (path, exc))
    if not cfg.get("person"):
        guess = guess_person(doc_dir)
        person = guess
        if interactive and sys.stdin.isatty():
            try:
                answer = input("galley: your name for session records [%s]: " % guess).strip()
                person = answer or guess
            except EOFError:
                pass
        cfg["person"] = person
        os.makedirs(os.path.dirname(path), exist_ok=True)
        saved = {"person": person,
                 "_comment": "galley user settings. Add an 'agents' object to override the defaults; see the README."}
        write_text(path, json.dumps(saved, indent=2) + "\n")
        print("galley: saved your name in %s" % path)
    agents = json.loads(json.dumps(DEFAULT_AGENTS))
    for name, spec in (cfg.get("agents") or {}).items():
        agents.setdefault(name, {}).update(spec)
    for name, spec in agents.items():
        if not spec.get("model") and spec.get("model_from"):
            spec["model"] = model_from_toml(spec["model_from"])
            spec["model_source"] = spec["model_from"]
        else:
            spec["model_source"] = "galley config" if spec.get("model") else ""
        spec["available"] = bool(shutil.which(spec["command"][0]))
    cfg["agents"] = agents
    cfg.setdefault("default_agent", "codex")
    return cfg


def approvals_path() -> str:
    return os.path.join(os.path.dirname(user_config_path()), "approvals.json")


def load_approvals() -> dict:
    """Build commands the person approved: main file -> fingerprints (see App.build_plan). Kept in
    the user's own config folder, never in the document, so a shared document cannot approve itself."""
    try:
        data = json.loads(read_text(approvals_path()))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_approval(main_path: str, fingerprint: str):
    data = load_approvals()
    items = [f for f in data.get(main_path, []) if f != fingerprint] + [fingerprint]
    data[main_path] = items[-20:]
    os.makedirs(os.path.dirname(approvals_path()), exist_ok=True)
    write_text(approvals_path(), json.dumps(data, indent=2) + "\n")


def shell_quote(arg: str) -> str:
    return arg if re.fullmatch(r"[A-Za-z0-9_./=:@%+,-]+", arg) else "'" + arg.replace("'", "'\\''") + "'"


def model_from_toml(path: str) -> str:
    """The top-level `model = "..."` of a TOML file such as ~/.codex/config.toml."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return ""
    for line in read_text(path).splitlines():
        if line.strip().startswith("["):
            break
        m = re.match(r'\s*model\s*=\s*"([^"]+)"', line)
        if m:
            return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# Document and its galley/ folder


def find_main(arg: str | None) -> str:
    if arg and os.path.isfile(arg):
        return os.path.abspath(arg)
    d = os.path.abspath(arg or ".")
    if not os.path.isdir(d):
        sys.exit("galley: %s not found" % arg)
    settings = os.path.join(d, FOLDER, "settings.json")
    if os.path.exists(settings):
        main = json.loads(read_text(settings)).get("main")
        if main and os.path.exists(os.path.join(d, main)):
            return os.path.join(d, main)
    candidates = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".tex"):
            try:
                head = read_text(os.path.join(d, name))
            except (OSError, UnicodeDecodeError):
                continue
            if re.search(r"^\s*\\documentclass", head, re.M):
                candidates.append(name)
    if len(candidates) == 1:
        return os.path.join(d, candidates[0])
    if not candidates:
        sys.exit("galley: no .tex file with \\documentclass in %s; give the main file explicitly." % d)
    sys.exit("galley: several main files in %s (%s); give one explicitly." % (d, ", ".join(candidates)))


BIB_RE = re.compile(r"\\(?:bibliography|addbibresource)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}")

FOLDER_README = """# galley folder

Created by galley for the document `{main}`. galley is a local viewer for reading this
document and working on it with AI agents.

- `records/`: session records, one per galley session, with summaries at the top.
- `notes.jsonl`: questions, answers, edit proposals and comments attached to passages
  (append-only; archived items are hidden in galley, not deleted).
- `instructions.md`: what agents should know about this document; included in every request.
- `settings.json`: settings for this document.
- `build/`, `state/`: local build output and scratch files (not shared).
"""


def within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class Document:
    def __init__(self, main_path: str):
        self.main_path = main_path
        self.dir = os.path.dirname(main_path)
        self.main = os.path.basename(main_path)
        self.stem = os.path.splitext(self.main)[0]
        self.folder = os.path.join(self.dir, FOLDER)
        self.repo = git(["rev-parse", "--show-toplevel"], self.dir).strip() or None
        self.base = self.repo or self.dir
        self.first_run = not os.path.exists(os.path.join(self.folder, "settings.json"))
        self.ensure_folder()
        self.settings = self.load_settings()
        self.files: list = [self.main]

    def ensure_folder(self):
        os.makedirs(os.path.join(self.folder, "records"), exist_ok=True)
        os.makedirs(os.path.join(self.folder, "state"), exist_ok=True)
        defaults = {
            "README.md": FOLDER_README.format(main=self.main),
            ".gitignore": "build/\nstate/\n",
            # notes.jsonl is append-only, so concurrent additions merge cleanly.
            ".gitattributes": "notes.jsonl merge=union\n",
        }
        for name, text in defaults.items():
            p = os.path.join(self.folder, name)
            if not os.path.exists(p):
                write_text(p, text)

    def load_settings(self) -> dict:
        p = os.path.join(self.folder, "settings.json")
        s = {}
        if os.path.exists(p):
            s = json.loads(read_text(p))
        changed = not s
        defaults = {
            "main": self.main,
            "mark_agent_text": False,
            "agent_cwd": ".",
            "build": list(DEFAULT_BUILD),
            "instructions_skipped": False,
        }
        for k, v in defaults.items():
            if k not in s:
                s[k] = v
                changed = True
        if changed:
            write_text(p, json.dumps(s, indent=2) + "\n")
        return s

    def save_settings(self):
        write_text(os.path.join(self.folder, "settings.json"), json.dumps(self.settings, indent=2) + "\n")

    # -- paths --------------------------------------------------------------

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.base)

    def abs_in_doc(self, rel_file: str) -> str:
        """A file in the document folder, for reading. Symlinks are resolved: the target must lie in
        the document folder or (for documents in a repository) in that repository."""
        path = os.path.normpath(os.path.join(self.dir, rel_file))
        if not (path == self.dir or path.startswith(self.dir + os.sep)):
            raise ValueError("path outside the document folder: %s" % rel_file)
        real = os.path.realpath(path)
        if not any(within(real, os.path.realpath(root)) for root in (self.dir, self.base)):
            raise ValueError("path leads outside the document folder: %s" % rel_file)
        return path

    def source_file_for_write(self, rel_file: str) -> str:
        """One of the document's own source files (as parsed, or a .bib file it names), for writing;
        never inside galley/, and never a symlink leading out of the document folder."""
        rel = os.path.normpath(rel_file)
        if rel not in self.editable_files():
            raise ValueError("not one of the document's source files: %s" % rel_file)
        path = os.path.normpath(os.path.join(self.dir, rel))
        real = os.path.realpath(path)
        if not within(real, os.path.realpath(self.dir)) or within(real, os.path.realpath(self.folder)):
            raise ValueError("refusing to write outside the document's sources: %s" % rel_file)
        return path

    @property
    def agent_cwd(self) -> str:
        """Where agents start: settings' agent_cwd if it is a folder inside the document folder or
        its repository (a shared settings.json must not send agents elsewhere); else the document folder."""
        return self.dir if self.agent_cwd_problem() else self._agent_cwd_wanted()

    def _agent_cwd_wanted(self) -> str:
        return os.path.normpath(os.path.join(self.dir, str(self.settings.get("agent_cwd") or ".")))

    def agent_cwd_problem(self) -> str:
        want = self._agent_cwd_wanted()
        real = os.path.realpath(want)
        roots = [self.dir] + ([self.repo] if self.repo else [])
        if not os.path.isdir(want):
            return "agent_cwd in %s/settings.json is not a folder (%s); agents start in the document folder." % (FOLDER, want)
        if not any(within(real, os.path.realpath(r)) for r in roots):
            return ("agent_cwd in %s/settings.json points outside the document folder%s (%s); agents start in "
                    "the document folder instead." % (FOLDER, " and its repository" if self.repo else "", want))
        return ""

    def bib_files(self) -> list:
        """The .bib files the document names with \\bibliography or \\addbibresource, if they are
        in the document folder."""
        out = []
        for rel in self.files:
            try:
                text = read_text(os.path.join(self.dir, rel))
            except (OSError, UnicodeDecodeError):
                continue
            text = re.sub(r"(?<!\\)%.*", "", text)
            for m in BIB_RE.finditer(text):
                for name in m.group(1).split(","):
                    name = name.strip()
                    if not name or "\\" in name:
                        continue
                    if not name.endswith(".bib"):
                        name += ".bib"
                    path = os.path.normpath(os.path.join(self.dir, name))
                    rel_bib = os.path.relpath(path, self.dir)
                    if within(path, self.dir) and os.path.isfile(path) and rel_bib not in out:
                        out.append(rel_bib)
        return out

    def editable_files(self) -> set:
        """Files galley may change for the person (hand edits, accepted agent edits): the document's
        .tex sources as parsed and the .bib files it names. Never anything in galley/."""
        return {os.path.normpath(f) for f in list(self.files) + self.bib_files()}

    @property
    def notes_path(self) -> str:
        return os.path.join(self.folder, "notes.jsonl")

    @property
    def instructions_path(self) -> str:
        return os.path.join(self.folder, "instructions.md")

    def newest(self, names: list) -> str | None:
        found = [p for p in names if os.path.exists(p)]
        return max(found, key=os.path.getmtime) if found else None

    def build_file(self, ext: str) -> str | None:
        name = self.stem + ext
        return self.newest([os.path.join(self.folder, "build", name), os.path.join(self.dir, name),
                            os.path.join(self.dir, "build", name), os.path.join(self.dir, "out", name)])

    def instructions(self) -> str:
        p = self.instructions_path
        return read_text(p) if os.path.exists(p) else ""

    def parse(self) -> dict:
        d = latex_doc.parse_document(self.dir, self.main, self.build_file(".aux"), self.build_file(".bbl"))
        self.files = d["files"]
        return d

    def fingerprint(self) -> str:
        parts = []
        for rel in self.files + [os.path.join(FOLDER, "notes.jsonl"), os.path.join(FOLDER, "instructions.md")]:
            p = os.path.join(self.dir, rel)
            if os.path.exists(p):
                st = os.stat(p)
                parts.append("%s:%d:%d" % (rel, st.st_mtime_ns, st.st_size))
        aux = self.build_file(".aux")
        if aux:
            parts.append("aux:%d" % os.stat(aux).st_mtime_ns)
        return sha12("|".join(parts))


# ---------------------------------------------------------------------------
# Session record


INTERACTION_HEAD = re.compile(r"^### \d\d:\d\d \u00b7 ", re.M)


class SessionRecord:
    def __init__(self, doc: Document, person: str):
        self.doc = doc
        self.person = person
        self.path: str | None = None
        self.lock = threading.Lock()
        self.started = now()

    def _ensure(self):
        if self.path:
            return
        stamp = self.started.strftime("%Y-%m-%d")
        name = "%s-%s-galley-%s.md" % (stamp, re.sub(r"\W+", "-", self.person.lower()), self.started.strftime("%H%M"))
        self.path = os.path.join(self.doc.folder, "records", name)
        commit = git(["rev-parse", "--short", "HEAD"], self.doc.dir).strip() if self.doc.repo else ""
        header = [
            "# galley session: %s" % getattr(self.doc, "title", self.doc.main),
            "",
            "- Date: %s" % stamp,
            "- Human: %s" % self.person,
            "- Agent: via galley; the agent, model and thinking level of each interaction are listed below",
            "- Work area: %s" % (self.doc.rel(self.doc.dir) if self.doc.repo else self.doc.dir),
        ]
        if commit:
            header.append("- Started at commit: %s" % commit)
        header += [
            "- Written by: galley logs each interaction as it happens. Summaries, if present, appear "
            "above the interaction log, were written by an agent at wrap-up, and are marked.",
            "",
            "## Interactions",
            "",
            "",
        ]
        write_text(self.path, "\n".join(header))

    def append(self, text: str):
        with self.lock:
            self._ensure()
            append_to(self.path, text)

    def read(self) -> str:
        return read_text(self.path) if self.path and os.path.exists(self.path) else ""

    def rel(self) -> str | None:
        return self.doc.rel(self.path) if self.path else None


def insert_summary(path: str, title: str, body: str):
    """Put a wrap-up summary at the top of a record, newest first, headings demoted."""
    body = re.sub(r"^#{1,2} ", "### ", body.strip(), flags=re.M)
    block = "## %s\n\n%s\n\n" % (title, body)
    with _append_lock:
        text = read_text(path)
        m = re.search(r"^## Summary \(agent-written", text, re.M) or re.search(r"^## Interactions", text, re.M)
        text = (text[: m.start()] + block + text[m.start():]) if m else (text.rstrip() + "\n\n" + block)
        write_text(path, text)


def list_records(doc: Document, current: str | None) -> list:
    d = os.path.join(doc.folder, "records")
    out = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d), reverse=True):
        if not name.endswith(".md"):
            continue
        text = read_text(os.path.join(d, name))
        n = len(re.findall(r"annotation `a-[0-9a-f]+`", text)) + len(re.findall(r"\u00b7 (Comment|Edited by hand) \u00b7", text))
        out.append({"name": name, "path": doc.rel(os.path.join(d, name)),
                    "current": bool(current) and os.path.basename(current) == name,
                    "interactions": n, "has_summary": "## Summary (agent-written" in text})
    return out


def anchor_desc(a: dict) -> str:
    a = a or {}
    if a.get("kind") == "document":
        return "whole document"
    parts = []
    if a.get("kind") == "section":
        parts.append("section %s" % (a.get("number") or "") + (" \u201c%s\u201d" % a["title"] if a.get("title") else ""))
    elif a.get("display"):
        parts.append(" ".join(x for x in (a.get("display"), a.get("number") or "") if x))
    elif a.get("kind") == "para":
        parts.append("paragraph")
    if a.get("within"):
        parts.append("in " + a["within"])
    if a.get("labels"):
        parts.append("[" + ", ".join(a["labels"]) + "]")
    if a.get("file"):
        parts.append("%s:%s\u2013%s" % (a.get("file"), a.get("start"), a.get("end")))
    if a.get("section") and a.get("kind") != "section":
        parts.append("in \u00a7" + str(a["section"]))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Notes (per-passage history, append-only JSON lines)


class Notes:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()

    def append(self, entry: dict):
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def all(self) -> list:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        items = {e["id"]: e for e in out if e.get("type") != "outcome"}
        for e in out:
            if e.get("type") == "outcome" and e.get("ref") in items:
                item = items[e["ref"]]
                if e.get("outcome") in ("archived", "unarchived"):
                    item["archived"] = e["outcome"] == "archived"
                else:
                    item.setdefault("outcomes", []).append(e)
        return list(items.values())

    def set_archived(self, ids: list, archived: bool, person: str) -> int:
        current = {a["id"]: a for a in self.all()}
        n = 0
        for i in ids:
            a = current.get(i)
            if not a or bool(a.get("archived")) == archived:
                continue
            self.append({"id": "o-" + uuid.uuid4().hex[:8], "type": "outcome", "ref": i,
                         "outcome": "archived" if archived else "unarchived", "person": person,
                         "time": now().isoformat(timespec="seconds")})
            n += 1
        return n


# ---------------------------------------------------------------------------
# Prompts


DISPLAY_NOTE = (
    "Your reply is shown in a narrow sidebar. It renders Markdown paragraphs, headings, bulleted "
    "and numbered lists, bold and italic, inline code, code blocks, small pipe tables and web links. "
    "Mathematics in $...$ and $$...$$ is rendered by MathJax, with the document's own LaTeX macros. "
    "Raw HTML, images and links to local files do not render: refer to files as `path:line` in backticks."
)

MARKING_NOTE = (
    "This document marks agent-written text: wrap every piece of text you add or rewrite in "
    "\\AGTADD{...} so it shows in violet, including displayed equations. Wrap each paragraph "
    "separately (never across a blank line), and keep \\section commands and the \\begin/\\end lines "
    "of theorem-like environments outside it (wrap the contents instead). Leave existing \\AGTADD "
    "markup as it is unless asked to change it."
)

MODE_INSTRUCTIONS = {
    "explain": (
        "Answer the question. Be precise and concise. Ground statements in the document's source and "
        "the results it cites, and say plainly when an argument is your own rather than stated in a "
        "source. If you think something is wrong, unclear, or missing a hypothesis, say so and why. "
        "Do not modify any files."
    ),
    "suggest": (
        "Propose an edit to the LaTeX source that addresses the request. Do not modify any files "
        "yourself. Reply with a short explanation of the change and its reasoning, followed by one or "
        "more edit blocks in exactly this format:\n\n"
        "FILE: <path relative to {base_desc}>\n"
        "<<<<<<< ORIGINAL\n<exact existing lines, copied verbatim from the file>\n=======\n"
        "<replacement lines>\n>>>>>>> REVISED\n\n"
        "Only edit files in the document's folder ({doc_dir}). The ORIGINAL text must match the file "
        "exactly and occur once; include enough lines to make it unique. Keep changes minimal and "
        "preserve the authors' wording outside them. Follow the document's existing notation."
    ),
}

WRAPUP_PROMPT = """You are writing the summary part of a session record for {person}'s work in galley,
following the session-record protocol. Below is the interaction log galley wrote during the session.
Write ONLY the following Markdown sections, omitting any that would be empty:

## Goal
What {person} seemed to be after, and why, as far as the log shows. Say where the aim was unclear,
still forming, or shifted. Quote rather than infer a crisp goal.

## What {person} took from it
Explicit decisions (quote briefly), and what {person} seemed to find valuable: ideas they followed up,
built on, accepted, or rejected. Mark inferences as inferred and point to the interaction that
supports each one. Keep decisions, leanings and inferred takeaways distinct.

## Intuition and motivation
Physical pictures, heuristics and mathematical intuitions that came up, especially moves between
physics and mathematics that reshaped a question.

## Results and their status
Claims made in answers or edits, each with what was actually done to check it (an agent argument
is unchecked unless something more was done).

## Dead ends
What was tried and did not work, and why.

## Open threads
Questions left open and natural next steps.

Be concise; the whole summary should usually be under 40 lines. Do not modify any files.

Interaction log:

{log}

Full text of the questions, answers and edit proposals referred to in the log (the log itself
shows only excerpts):

{details}
"""

INSTRUCTIONS_PROMPT = """Draft the file {path}: instructions that galley will include in every request
an AI agent receives about the LaTeX document {main} (in {doc_dir}). Agents use it to understand the
document and work on it well.

Look at the document (its main file and the files it inputs), its folder, and nearby README, AGENTS.md
or CLAUDE.md files, and anything they point to that helps. Do not modify any files. Then write concise
Markdown (usually under 60 lines) with these sections, after the questions described below:

## What this document is
## Authors and status
## Notation and conventions
## Related sources
## How agents should work on it

Put a section "## Questions for {person}" first, right after the level-one heading: up to five short
questions whose answers would most improve these instructions, followed by this note, verbatim:

{note}

Only state what you can support from what you read, and say where you inferred something. Where the
authors need to decide or supply something, say so (as a question if it matters).

Reply with the file's content only, starting with a level-one heading."""

INSTRUCTIONS_REVISE = """Below is {path}, which {person} may have edited and annotated with answers to the
questions in it. Revise it into a clean version that incorporates the edits and answers. Remove questions
that have been answered; keep the ones that remain open, unchanged, in the section "## Questions for
{person}" right after the level-one heading, followed by this note, verbatim:

{note}

If no questions remain, drop that section and the note. Keep the other sections. Do not modify any files.
Reply with the file's content only.

{text}"""

QUESTIONS_NOTE = ("_Note for agents: the questions above are open issues about how this document is set up, and "
                  "{person} may leave them unanswered. Keep them in mind, but do not assume answers to them, and do "
                  "not act on them or answer them unless asked._")

QUESTIONS_STANDING = ("Any questions listed in these instructions (for example under \"Questions for ...\") are open "
                      "issues about the document's setup, not requests to you: keep them in mind, do not assume "
                      "answers to them, and do not act on them or answer them unless %s asks.")


def normalize_instructions(text: str, person: str) -> str:
    """Tidy an agent's draft: no code fence, questions section first, with the note for agents."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text).strip()
    parts = re.split(r"(?m)^(?=## )", text)
    head, sections = parts[0], parts[1:]
    qs = [p for p in sections if re.match(r"## Questions\b", p)]
    if not qs:
        return text + "\n"
    rest = [p for p in sections if p not in qs]
    q = "\n\n".join(p.strip() for p in qs)
    if "Note for agents: the questions above" not in q:
        q += "\n\n" + QUESTIONS_NOTE.format(person=person)
    return "\n\n".join([head.strip(), q.strip()] + [p.strip() for p in rest]) + "\n"


# ---------------------------------------------------------------------------
# Edit proposals


EDIT_RE = re.compile(
    r"FILE:\s*(?P<file>\S+)\s*\n<<<<<<< ORIGINAL\n(?P<orig>.*?)\n=======\n(?P<new>.*?)\n?>>>>>>> REVISED", re.S)


def parse_edits(text: str) -> list:
    return [{"file": m.group("file").strip().strip("`"), "original": m.group("orig"),
             "replacement": m.group("new")} for m in EDIT_RE.finditer(text)]


def strip_edits(text: str) -> str:
    return EDIT_RE.sub("", text).replace("```\n\n```", "").strip()


def locate(content: str, original: str):
    """(start, end) of the unique match of original, tolerating trailing spaces; else None."""
    idx = content.find(original)
    if idx >= 0 and content.find(original, idx + 1) < 0:
        return idx, idx + len(original)
    if idx >= 0:
        return None
    want = [l.rstrip() for l in original.split("\n")]
    lines = content.split("\n")
    hits = [s for s in range(len(lines) - len(want) + 1)
            if [l.rstrip() for l in lines[s : s + len(want)]] == want]
    if len(hits) != 1:
        return None
    start = sum(len(l) + 1 for l in lines[: hits[0]])
    return start, start + len("\n".join(lines[hits[0] : hits[0] + len(want)]))


def resolve_edit_path(doc: Document, given: str) -> str | None:
    """Map a path from an edit block to one of the document's editable files (its .tex sources and
    the .bib files it names; see Document.editable_files), or None: agents may propose changes to
    nothing else (not latexmkrc, scripts, settings or galley/ files)."""
    given = given.strip()
    candidates = []
    if os.path.isabs(given):
        candidates.append(given)
    else:
        candidates += [os.path.join(doc.base, given), os.path.join(doc.dir, given)]
    editable = doc.editable_files()
    for c in candidates:
        c = os.path.normpath(c)
        if not within(c, doc.dir) or not os.path.isfile(c):
            continue
        if os.path.relpath(c, doc.dir) not in editable:
            continue
        try:
            return doc.source_file_for_write(os.path.relpath(c, doc.dir))
        except ValueError:
            continue
    return None


def prepare_proposal(doc: Document, edits: list) -> dict:
    result = {"ok": True, "problems": [], "files": {}, "diff": "", "refused": []}
    old, new = {}, {}
    for e in edits:
        path = resolve_edit_path(doc, e["file"])
        if not path:
            result["ok"] = False
            result["problems"].append(
                "edit refused: %s is not one of the document's source files (galley applies agent edits only to "
                "the .tex files the document includes and the .bib files it names)" % e["file"])
            result["refused"].append({"file": e["file"], "original": e["original"][:4000],
                                      "replacement": e["replacement"][:4000]})
            continue
        rel = doc.rel(path)
        if rel not in old:
            old[rel] = read_text(path)
            new[rel] = old[rel]
        span = locate(new[rel], e["original"])
        if not span:
            result["ok"] = False
            result["problems"].append("could not find a unique match for an ORIGINAL block in %s" % rel)
            continue
        a, b = span
        new[rel] = new[rel][:a] + e["replacement"] + new[rel][b:]
    diffs = []
    for rel in old:
        diffs.extend(difflib.unified_diff(old[rel].splitlines(keepends=True), new[rel].splitlines(keepends=True),
                                          fromfile="a/" + rel, tofile="b/" + rel, n=2))
        result["files"][rel] = {"before_hash": sha12(old[rel]), "after": new[rel]}
    result["diff"] = "".join(diffs)
    if result["ok"] and not result["diff"]:
        result["ok"] = False
        result["problems"].append("the proposed edits make no change")
    return result


def unwrap_agtadd(text: str) -> tuple:
    """Remove \\AGTADD{...} wrappers, keeping their contents. Returns (text, count)."""
    out, i, n = [], 0, 0
    key = "\\AGTADD"
    while True:
        j = text.find(key, i)
        if j < 0:
            out.append(text[i:])
            break
        k = j + len(key)
        while k < len(text) and text[k] in " \t":
            k += 1
        if k >= len(text) or text[k] != "{":
            out.append(text[i:k])
            i = k
            continue
        inner, end = latex_doc.read_group(text, k)
        out.append(text[i:j])
        inner2, m = unwrap_agtadd(inner)
        out.append(inner2)
        n += 1 + m
        i = end
    return "".join(out), n


# ---------------------------------------------------------------------------
# Agent jobs


class Jobs:
    def __init__(self, app: "App"):
        self.app = app
        self.jobs: dict = {}
        self.lock = threading.Lock()

    def get(self, jid: str) -> dict | None:
        with self.lock:
            j = self.jobs.get(jid)
            return None if not j else {k: v for k, v in j.items() if k not in ("proc", "prompt", "proposal_files", "req")}

    def running(self, kind: str) -> list:
        with self.lock:
            return [{"id": j["id"], "kind": j["kind"], "record": j.get("record", "")}
                    for j in self.jobs.values() if j["kind"] == kind and j["status"] == "running"]

    def settings_for(self, agent: str, effort: str | None) -> dict:
        spec = self.app.cfg["agents"].get(agent) or {}
        efforts = spec.get("efforts") or []
        if efforts:
            effort = effort if effort in efforts else spec.get("default_effort", efforts[0])
        else:
            effort = ""
        return {"model": spec.get("model", ""), "effort": effort}

    def label(self, agent: str, st: dict) -> str:
        spec = self.app.cfg["agents"].get(agent) or {}
        bits = [x for x in (st.get("model"), st.get("effort")) if x]
        return "%s (%s)" % (spec.get("label", agent), ", ".join(bits)) if bits else spec.get("label", agent)

    def start(self, kind: str, req: dict, prompt: str) -> str:
        jid = uuid.uuid4().hex[:10]
        agent = req.get("agent") or self.app.cfg["default_agent"]
        st = self.settings_for(agent, req.get("effort"))
        job = {"id": jid, "kind": kind, "mode": req.get("mode"), "agent": agent, "settings": st,
               "agent_label": self.label(agent, st), "status": "running", "progress": "", "answer": "",
               "error": "", "started": now().isoformat(timespec="seconds"), "anchor": req.get("anchor"),
               "question": req.get("question", ""), "selection": req.get("selection", ""),
               "prompt": prompt, "proposal": None, "req": req,
               "record": os.path.basename(req.get("record_path") or "")}
        with self.lock:
            self.jobs[jid] = job
        threading.Thread(target=self._run, args=(jid,), daemon=True).start()
        return jid

    def _run(self, jid: str):
        job = self.jobs[jid]
        app = self.app
        spec = app.cfg["agents"].get(job["agent"])
        try:
            if not spec:
                raise RuntimeError("unknown agent %r" % job["agent"])
            if not shutil.which(spec["command"][0]):
                raise RuntimeError("%s was not found on your PATH. Install it, or start galley from a "
                                   "terminal where it works." % spec["command"][0])
            st = job["settings"]
            if "{model}" in " ".join(spec["command"]) and not st["model"]:
                raise RuntimeError("no model set for %s: add one to %s" % (spec.get("label"), user_config_path()))
            outfile = os.path.join(app.doc.folder, "state", "out-%s.txt" % jid)
            command = list(spec["command"])
            if st["model"] and spec.get("model_args"):
                command = command[:2] + spec["model_args"] + command[2:]
            cmd = [a.replace("{cwd}", app.doc.agent_cwd).replace("{outfile}", outfile)
                   .replace("{model}", st["model"]).replace("{effort}", st["effort"]) for a in command]
            proc = subprocess.Popen(cmd, cwd=app.doc.agent_cwd, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            job["proc"] = proc
            proc.stdin.write(job["prompt"])
            proc.stdin.close()
            chunks = []
            timer = threading.Timer(app.cfg.get("timeout_seconds", 1800), proc.kill)
            timer.start()
            for line in proc.stdout:
                chunks.append(line)
                job["progress"] = "".join(chunks)[-6000:]
            proc.wait()
            timer.cancel()
            stdout = "".join(chunks)
            if proc.returncode != 0:
                raise RuntimeError(agent_error(spec.get("label") or job["agent"], proc.returncode, stdout))
            if spec.get("result") == "outfile":
                if not os.path.exists(outfile):
                    raise RuntimeError("agent produced no final message:\n%s" % stdout[-3000:])
                answer = read_text(outfile)
                os.remove(outfile)
            else:
                answer = stdout
            if not answer.strip():
                raise RuntimeError("agent returned an empty reply")
            job["answer"] = answer.strip()
            self._finish(job)
            job["status"] = "done"
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
            traceback.print_exc()
            if job["kind"] == "ask":
                app.record.append("### %s \u00b7 %s \u00b7 %s \u00b7 %s \u00b7 failed\n\nError: %s" % (
                    now().strftime("%H:%M"), job["mode"], anchor_desc(job["anchor"]), job["agent_label"],
                    excerpt(str(exc), 300)))

    def _finish(self, job: dict):
        app = self.app
        label = job["agent_label"]
        req = job["req"]
        if job["kind"] == "wrapup":
            insert_summary(req["record_path"], "Summary (agent-written at wrap-up by %s, %s)" % (
                label, now().strftime("%Y-%m-%d %H:%M")), job["answer"])
            return
        if job["kind"] == "instructions":
            # Written straight to the file, so a draft is never lost with the browser page.
            job["answer"] = normalize_instructions(job["answer"], app.person)
            app.save_instructions(job["answer"], by="%s by %s" % ("revised" if req.get("revise") else "drafted", label))
            return
        anchor = job["anchor"] or {}
        commit = git(["rev-parse", "--short", "HEAD"], app.doc.dir).strip() if app.doc.repo else ""
        entry = {"id": "a-" + job["id"], "type": job["mode"], "time": now().isoformat(timespec="seconds"),
                 "person": app.person, "agent": label, "model": job["settings"]["model"],
                 "effort": job["settings"]["effort"], "anchor": anchor, "commit": commit,
                 "question": job["question"], "selection": job["selection"], "thread": req.get("thread")}
        head = "### %s \u00b7 %s \u00b7 %s \u00b7 %s" % (
            now().strftime("%H:%M"), {"explain": "Question", "suggest": "Edit request"}.get(job["mode"], job["mode"]),
            anchor_desc(anchor), label)
        body = []
        if job["selection"]:
            body.append("Highlighted: " + excerpt(job["selection"], 200).replace("\n", " "))
        body.append(said(app.person, job["question"] or "(default request)"))
        if job["mode"] == "suggest":
            edits = parse_edits(job["answer"])
            explanation = strip_edits(job["answer"])
            prop = prepare_proposal(app.doc, edits) if edits else {
                "ok": False, "problems": ["no edit blocks found in the reply"], "diff": "", "files": {}}
            job["answer"] = explanation
            job["proposal"] = {"ok": prop["ok"], "problems": prop["problems"], "diff": prop["diff"],
                               "refused": prop.get("refused", []), "state": "pending"}
            write_text(os.path.join(app.doc.folder, "state", "proposal-%s.json" % job["id"]),
                       json.dumps({"files": prop["files"], "diff": prop["diff"], "ok": prop["ok"],
                                   "problems": prop["problems"]}))
            entry.update(answer=explanation, diff=prop["diff"], proposal_ok=prop["ok"], problems=prop["problems"],
                         refused=[r["file"] for r in prop.get("refused", [])])
            body.append("Agent's explanation (excerpt): " + excerpt(explanation, 500))
            body.append("Proposed change: %s (decision pending)" % (
                "diff ready for review" if prop["ok"] else "; ".join(prop["problems"])))
        else:
            entry["answer"] = job["answer"]
            body.append("Answer (excerpt):\n\n" + quote_md(excerpt(job["answer"], 700)))
        body.append("Full text: annotation `%s` in `%s`." % (entry["id"], app.doc.rel(app.doc.notes_path)))
        app.notes.append(entry)
        app.record.append(head + "\n\n" + "\n\n".join(body))

    def decide(self, jid: str, accept: bool, note: str = "") -> dict:
        app = self.app
        path = os.path.join(app.doc.folder, "state", "proposal-%s.json" % jid)
        if not os.path.exists(path):
            return {"ok": False, "error": "no such proposal"}
        prop = json.loads(read_text(path))
        job = self.jobs.get(jid)
        if accept:
            if prop.get("ok") is False:
                return {"ok": False, "error": "This proposal cannot be accepted: " + "; ".join(prop.get("problems") or [])}
            for rel in prop["files"]:
                try:
                    app.doc.source_file_for_write(os.path.relpath(os.path.join(app.doc.base, rel), app.doc.dir))
                except ValueError as exc:
                    return {"ok": False, "error": str(exc)}
            for rel, info in prop["files"].items():
                if sha12(read_text(os.path.join(app.doc.base, rel))) != info["before_hash"]:
                    return {"ok": False, "error": "%s changed since this edit was proposed; ask again." % rel}
            for rel, info in prop["files"].items():
                write_text(os.path.join(app.doc.base, rel), info["after"])
        state = "accepted" if accept else "rejected"
        if job and job.get("proposal"):
            job["proposal"]["state"] = state
        app.notes.append({"id": "o-" + uuid.uuid4().hex[:8], "type": "outcome", "ref": "a-" + jid,
                          "outcome": state, "note": note, "person": app.person,
                          "time": now().isoformat(timespec="seconds")})
        msg = "### %s \u00b7 Edit %s \u00b7 %s\n\n%s %s the edit proposed by %s (annotation `a-%s`)." % (
            now().strftime("%H:%M"), state, anchor_desc(job["anchor"] if job else {}), app.person, state,
            job["agent_label"] if job else "the agent", jid)
        if accept:
            msg += " Files changed in the working tree, not committed: %s." % ", ".join("`%s`" % f for f in prop["files"])
            msg += "\n\n```diff\n%s```" % prop["diff"]
        if note:
            msg += "\n\nComment: " + note
        app.record.append(msg)
        return {"ok": True, "state": state}


# ---------------------------------------------------------------------------
# Application


class App:
    def __init__(self, doc: Document, cfg: dict):
        self.doc = doc
        self.cfg = cfg
        self.person = cfg["person"]
        self.record = SessionRecord(doc, self.person)
        self.notes = Notes(doc.notes_path)
        self.jobs = Jobs(self)
        self.build_lock = threading.Lock()
        self.mathjax_dir = self.find_mathjax()
        self.token = secrets.token_urlsafe(24)  # key for this run; see make_handler
        self.port = 0
        self.diagrams = diagrams.DiagramCompiler(os.path.join(doc.folder, "build", "diagrams"), doc.dir)

    def find_mathjax(self) -> str | None:
        """MathJax 3 (es5): the copy in vendor/mathjax, or another one named by GALLEY_MATHJAX."""
        for d in (os.environ.get("GALLEY_MATHJAX"), os.path.join(HERE, "vendor", "mathjax")):
            if d and os.path.isfile(os.path.join(d, "tex-chtml-full.js")):
                return os.path.normpath(d)
        return None

    # -- document --------------------------------------------------------

    def doc_json(self) -> dict:
        d = self.doc.parse()
        self.doc.title = d["title"]
        self.diagrams.request(d.pop("preamble_tex"), d["diagrams"])
        d["diagrams"] = sorted(d["diagrams"])
        def walk(blocks):
            for b in blocks:
                if "anchor" in b:
                    b["anchor"]["path"] = self.doc.rel(os.path.join(self.doc.dir, b["anchor"]["file"]))
                walk(b.get("children", []))
        walk(d["blocks"])
        changed = []
        commit = ""
        status_checked = False
        if self.doc.repo:
            status = git_status(self.doc.dir, self.doc.dir)
            status_checked = status is not None
            changed = [l[3:] for l in (status or "").splitlines() if l.strip()]
            commit = git(["rev-parse", "--short", "HEAD"], self.doc.dir).strip()
        d["git"] = {"repo": bool(self.doc.repo), "commit": commit, "changed": changed, "status_checked": status_checked}
        d["notices"] = [w for w in (self.doc.agent_cwd_problem(),) if w]
        if self.doc.repo and not status_checked:
            d["notices"].append("galley does not run git status in this repository, because its own Git settings "
                                 "define a filter program (or git is older than 2.26), so uncommitted changes are not shown.")
        d["about"] = {"version": VERSION, "source": SOURCE_URL}
        d["agents"] = {k: v.get("label", k) for k, v in self.cfg["agents"].items()}
        d["agent_settings"] = {k: {"model": v.get("model", ""), "efforts": v.get("efforts") or [],
                                   "default_effort": v.get("default_effort", ""), "available": v.get("available", True)}
                               for k, v in self.cfg["agents"].items()}
        d["default_agent"] = self.cfg["default_agent"]
        d["person"] = self.person
        d["record"] = self.record.rel()
        d["doc_dir"] = self.doc.rel(self.doc.dir) if self.doc.repo else self.doc.dir
        d["has_pdf"] = bool(self.doc.build_file(".pdf"))
        d["settings"] = self.doc.settings
        d["instructions_exist"] = os.path.exists(self.doc.instructions_path)
        d["marking_defined"] = self.marking_defined()
        d["fingerprint"] = self.doc.fingerprint()
        d["version"] = VERSION
        return d

    def source(self, rel_file: str, start: int, end: int) -> str:
        lines = read_text(self.doc.abs_in_doc(rel_file)).split("\n")
        return "\n".join(lines[max(0, start - 1) : end])

    def passage_text(self, a: dict, limit: int = 40000) -> str:
        kind = a.get("kind")
        if kind == "document":
            return ""
        if kind == "section":
            parts, total = [], 0
            for sp in a.get("span") or []:
                t = self.source(sp["file"], int(sp["start"]), int(sp["end"]))
                parts.append("%% --- %s, lines %s-%s ---\n%s" % (sp["file"], sp["start"], sp["end"], t))
                total += len(t)
            text = "\n".join(parts)
            if total > limit:
                return ""  # too long to include: the agent reads the files itself
            return text
        return self.source(a["file"], int(a["start"]), int(a["end"]))

    # -- prompts -----------------------------------------------------------

    def preamble_lines(self) -> list:
        doc = self.doc
        lines = [
            "You are assisting %s, who is working on a LaTeX document in galley, a local viewer that "
            "lets people read a document and ask agents about it. You are running read-only in %s; files "
            "change only when %s accepts an edit you propose." % (self.person, doc.agent_cwd, self.person),
            "",
            "The document's main file is %s in the folder %s; these files belong to it: %s." % (
                doc.main, doc.dir, ", ".join(doc.files)),
            "galley's folder for this document is %s: session records are in %s/records/ (read recent ones "
            "if earlier context would help), and questions, answers and comments on passages are in %s/notes.jsonl."
            % (os.path.join(doc.dir, FOLDER), FOLDER, FOLDER),
        ]
        instr = doc.instructions().strip()
        if instr:
            lines += ["", "Instructions for this document (from %s/instructions.md):" % FOLDER, "", instr,
                      "", QUESTIONS_STANDING % self.person]
        if doc.settings.get("mark_agent_text"):
            lines += ["", MARKING_NOTE]
        return lines

    def build_prompt(self, req: dict) -> str:
        a = req["anchor"]
        mode = req.get("mode", "explain")
        lines = self.preamble_lines() + [""]
        kind = a.get("kind")
        if kind == "document":
            lines.append("The request concerns the whole document. Read the files you need.")
        else:
            text = self.passage_text(a)
            lines.append("The request concerns this %s (%s):" % (
                "section" if kind == "section" else "passage", anchor_desc(a)))
            if text:
                lines += ["```latex", text.rstrip(), "```"]
            else:
                lines.append("(It is long: read it from the files and line ranges given.)")
        if req.get("selection"):
            lines += ["", "The reader highlighted this text within it (as rendered):", quote_md(req["selection"])]
        history = req.get("history") or []
        if history:
            lines += ["", "Earlier in this thread:"]
            for h in history[-6:]:
                lines += ["", "%s asked: %s" % (self.person, h.get("question", "")),
                          "Answer: %s" % excerpt(h.get("answer", ""), 2500)]
        question = (req.get("question") or "").strip()
        if not question:
            question = {"explain": "Explain this: what it says, why it is true, and how it fits into the document.",
                        "suggest": "Suggest an improvement."}.get(mode, "")
        base_desc = "the repository root (%s)" % self.doc.base if self.doc.repo else "the document folder (%s)" % self.doc.dir
        lines += ["", "Request from %s: %s" % (self.person, question), "",
                  MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS["explain"]).format(base_desc=base_desc, doc_dir=self.doc.dir),
                  "", DISPLAY_NOTE]
        return "\n".join(lines)

    # -- actions -------------------------------------------------------------

    def ask(self, req: dict) -> dict:
        return {"job": self.jobs.start("ask", req, self.build_prompt(req))}

    def full_texts(self, ids: list) -> str:
        items = {a["id"]: a for a in self.notes.all()}
        parts = []
        for i in ids:
            a = items.get(i)
            if not a:
                continue
            block = ["### %s (%s, %s)" % (i, a.get("type"), anchor_desc(a.get("anchor"))),
                     "%s: %s" % (a.get("person", ""), a.get("question", ""))]
            if a.get("selection"):
                block.append("Highlighted: " + a["selection"][:500])
            block.append("Answer:\n" + (a.get("answer") or "")[:8000])
            if a.get("diff"):
                block.append("Proposed diff:\n" + a["diff"][:4000])
            for o in a.get("outcomes", []):
                block.append("Outcome: %s %s" % (o.get("outcome"), o.get("note") or ""))
            parts.append("\n\n".join(block))
        return "\n\n".join(parts)

    def wrapup(self, req: dict) -> dict:
        name = req.get("record")
        path = os.path.join(self.doc.folder, "records", os.path.basename(name)) if name else self.record.path
        if not path or not os.path.exists(path):
            return {"error": "Nothing has been recorded in this session yet."}
        for j in self.jobs.running("wrapup"):  # one wrap-up per record at a time
            if j["record"] == os.path.basename(path):
                return {"job": j["id"], "record": self.doc.rel(path), "already": True}
        log = read_text(path)
        details = self.full_texts(re.findall(r"annotation `(a-[0-9a-f]+)`", log))
        prompt = WRAPUP_PROMPT.format(person=self.person, log=log, details=details or "(none)")
        jid = self.jobs.start("wrapup", {"agent": req.get("agent"), "effort": req.get("effort"), "mode": "wrapup",
                                         "anchor": {}, "record_path": path}, prompt)
        return {"job": jid, "record": self.doc.rel(path)}

    def draft_instructions(self, req: dict) -> dict:
        path = os.path.join(FOLDER, "instructions.md")
        note = QUESTIONS_NOTE.format(person=self.person)
        text = req.get("text") or ""
        if text.strip():
            if text.strip() != self.doc.instructions().strip():
                self.save_instructions(text)  # keep the person's edits before the agent revises them
            prompt = INSTRUCTIONS_REVISE.format(path=path, person=self.person, text=text, note=note)
        else:
            prompt = INSTRUCTIONS_PROMPT.format(path=path, main=self.doc.main, doc_dir=self.doc.dir,
                                                person=self.person, note=note)
        return {"job": self.jobs.start("instructions", {"agent": req.get("agent"), "effort": req.get("effort"),
                                                        "mode": "instructions", "anchor": {},
                                                        "revise": bool(text.strip())}, prompt)}

    def save_instructions(self, text: str, skipped: bool = False, by: str = "") -> dict:
        if skipped:
            self.doc.settings["instructions_skipped"] = True
            self.doc.save_settings()
            return {"ok": True}
        new = not os.path.exists(self.doc.instructions_path)
        old = self.doc.instructions()
        write_text(self.doc.instructions_path, text.rstrip() + "\n")
        diff = "".join(difflib.unified_diff(old.splitlines(keepends=True), (text.rstrip() + "\n").splitlines(keepends=True),
                                            fromfile="a/instructions.md", tofile="b/instructions.md", n=1))
        if by:
            what = "Instructions %s" % by
            who = "The agent's text was written to `%s`." % self.doc.rel(self.doc.instructions_path)
        else:
            what = "Instructions %s" % ("created" if new else "edited")
            who = "%s %s `%s`." % (self.person, "saved" if new else "edited", self.doc.rel(self.doc.instructions_path))
        if not new and not diff:
            return {"ok": True}
        self.record.append("### %s \u00b7 %s\n\n%s%s" % (
            now().strftime("%H:%M"), what, who, "" if new else "\n\n```diff\n%s```" % diff))
        return {"ok": True}

    def edit_source(self, req: dict) -> dict:
        """A hand edit of a passage's LaTeX from the browser."""
        path = self.doc.source_file_for_write(req["file"])
        start, end = int(req["start"]), int(req["end"])
        lines = read_text(path).split("\n")
        current = "\n".join(lines[start - 1 : end])
        if current != req["original"]:
            return {"ok": False, "error": "This passage changed in the file since you opened it. Reload and try again."}
        new_text = req["text"]
        if new_text == current:
            return {"ok": True, "changed": False}
        lines[start - 1 : end] = new_text.split("\n")
        write_text(path, "\n".join(lines))
        rel = self.doc.rel(path)
        diff = "".join(difflib.unified_diff((current + "\n").splitlines(keepends=True), (new_text + "\n").splitlines(keepends=True),
                                            fromfile="a/" + rel, tofile="b/" + rel, n=1))
        self.record.append("### %s \u00b7 Edited by hand \u00b7 %s\n\n%s edited the source directly (not committed).\n\n```diff\n%s```" % (
            now().strftime("%H:%M"), anchor_desc(req.get("anchor") or {"file": req["file"], "start": start, "end": end}),
            self.person, diff))
        return {"ok": True, "changed": True}

    def marking_defined(self) -> bool:
        for rel in self.doc.files:
            try:
                t = read_text(os.path.join(self.doc.dir, rel))
            except OSError:
                continue
            if re.search(r"\\(newcommand|renewcommand|providecommand|DeclareRobustCommand|def)\s*\{?\\AGTADD", t):
                return True
        return False

    def setup_marking(self) -> dict:
        if self.marking_defined():
            return {"ok": True, "changed": False}
        path = self.doc.main_path
        text = read_text(path)
        m = re.search(r"^[ \t]*\\begin\{document\}", text, re.M)
        if not m:
            return {"ok": False, "error": "could not find \\begin{document} in %s" % self.doc.main}
        has_color = any(re.search(r"\\usepackage(\[[^\]]*\])?\{[^}]*\b(x?color)\b", read_text(os.path.join(self.doc.dir, r)))
                        for r in self.doc.files if os.path.exists(os.path.join(self.doc.dir, r)))
        block = AGTADD_PREAMBLE.replace("%XCOLOR%", "" if has_color else "\\usepackage{xcolor}\n")
        write_text(path, text[: m.start()] + block + text[m.start():])
        self.record.append("### %s \u00b7 Marking set up\n\n%s turned on marking of agent-written text; galley added "
                           "the \\AGTADD definition before \\begin{document} in `%s`." % (
                               now().strftime("%H:%M"), self.person, self.doc.rel(path)))
        return {"ok": True, "changed": True}

    def clear_marking(self, req: dict) -> dict:
        """Remove \\AGTADD wrappers in one passage or the whole document, keeping the text."""
        total, files = 0, []
        if req.get("scope") == "document":
            targets = [(rel, None, None) for rel in self.doc.files]
        else:
            targets = [(req["file"], int(req["start"]), int(req["end"]))]
        for rel, start, end in targets:
            path = self.doc.source_file_for_write(rel)
            text = read_text(path)
            if start is None:
                new, n = unwrap_agtadd(text)
            else:
                lines = text.split("\n")
                seg, n = unwrap_agtadd("\n".join(lines[start - 1 : end]))
                lines[start - 1 : end] = seg.split("\n")
                new = "\n".join(lines)
            if n:
                write_text(path, new)
                total += n
                files.append(rel)
        if total:
            where = "the whole document" if req.get("scope") == "document" else anchor_desc(req.get("anchor") or req)
            self.record.append("### %s \u00b7 Marking cleared \u00b7 %s\n\n%s removed %d \\AGTADD marking%s (text kept) in %s." % (
                now().strftime("%H:%M"), where, self.person, total, "" if total == 1 else "s",
                ", ".join("`%s`" % f for f in files)))
        return {"ok": True, "removed": total}

    def build_plan(self) -> dict:
        """What Build PDF would run, and whether the person must approve it first. galley's default
        command needs no approval; another command (galley/settings.json travels with a shared
        document), or files the build reads as code (latexmkrc, which latexmk runs as Perl; a script
        or Makefile the command names) need approval once per content, remembered per user."""
        main = self.doc.main if not self.doc.main.startswith("-") else "./" + self.doc.main
        raw = self.doc.settings.get("build")
        if not (isinstance(raw, list) and raw and all(isinstance(a, str) for a in raw)):
            raw = DEFAULT_BUILD
        cmd = [a.replace("{main}", main) for a in raw]
        names = ["latexmkrc", ".latexmkrc"]
        if os.path.basename(cmd[0]) in ("make", "gmake"):
            names += ["GNUmakefile", "makefile", "Makefile"]
        for a in cmd:
            if not a.startswith("-") and not a.endswith(".tex"):
                names.append(a)
        files = []
        for name in dict.fromkeys(names):
            path = os.path.normpath(os.path.join(self.doc.dir, name))
            if within(path, self.doc.dir) and os.path.isfile(path) and not within(path, self.doc.folder):
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                files.append({"name": os.path.relpath(path, self.doc.dir), "text": text})
        needs = raw != DEFAULT_BUILD or bool(files)
        fp = hashlib.sha256(json.dumps({"main": self.doc.main_path, "cmd": cmd, "files": files},
                                       sort_keys=True).encode("utf-8")).hexdigest()
        return {"command": cmd, "files": files, "needs_approval": needs and fp not in load_approvals().get(
            self.doc.main_path, []), "fingerprint": fp}

    def build(self, approve: str = "") -> dict:
        plan = self.build_plan()
        cmd = plan["command"]
        if plan["needs_approval"]:
            if not approve or not hmac.compare_digest(approve, plan["fingerprint"]):
                return {"ok": False, "needs_approval": {
                    "fingerprint": plan["fingerprint"], "command": " ".join(shell_quote(a) for a in cmd),
                    "default": " ".join(shell_quote(a.replace("{main}", self.doc.main)) for a in DEFAULT_BUILD),
                    "files": [{"name": f["name"], "text": f["text"][:20000]} for f in plan["files"]]}}
            save_approval(self.doc.main_path, plan["fingerprint"])
            self.record.append("### %s \u00b7 Build approved\n\n%s approved the build command `%s`%s." % (
                now().strftime("%H:%M"), self.person, " ".join(shell_quote(a) for a in cmd),
                (" and " + ", ".join("`%s`" % f["name"] for f in plan["files"])) if plan["files"] else ""))
        if not shutil.which(cmd[0]) and not os.path.isfile(os.path.join(self.doc.dir, cmd[0])):
            return {"ok": False, "log": "%s not found on PATH" % cmd[0]}
        with self.build_lock:
            p = subprocess.run(cmd, cwd=self.doc.dir, capture_output=True, text=True, timeout=900,
                               stdin=subprocess.DEVNULL)
        return {"ok": p.returncode == 0, "log": (p.stdout + p.stderr)[-4000:]}


# ---------------------------------------------------------------------------
# HTTP


LOCKED_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>galley</title>
<style>body{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:38em;margin:15vh auto;padding:0 1em;color:#333}</style>
</head><body><h2>This galley window needs its key</h2>
<p>For safety, galley only answers the browser window it opened itself. Open the link galley printed
in the terminal (it ends in <code>?t=...</code>), or run <code>galley</code> again for this document:
it opens a window with the key.</p></body></html>"""


def content_security_policy() -> str:
    """What galley's page may load: only galley's own files (MathJax is included in vendor/). No
    inline scripts, so javascript: links cannot run; no images, styles or fonts from other hosts,
    so rendered text cannot make the browser contact them."""
    return ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


# Anything else galley serves (images, diagrams, JSON) is not a page: if it is opened directly,
# it may not run scripts or load anything.
PLAIN_CSP = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"


def make_handler(app: App):
    """HTTP handler. galley binds to 127.0.0.1, but any web page open in the same browser can still
    send requests there, and DNS rebinding can make another site's name point at it. So: the Host
    header must be galley's own; every request needs the key of this run (a cookie, set when the
    browser opens the link with ?t=<key>); POSTs must also carry it in a header, be JSON and come
    from galley's own origin."""
    static_dir = os.path.join(HERE, "static")
    cookie_name = "galley_%d" % app.port
    hosts = {"127.0.0.1:%d" % app.port, "localhost:%d" % app.port}
    origins = {"http://" + h for h in hosts}
    csp = content_security_policy()
    types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
             ".css": "text/css; charset=utf-8", ".woff": "font/woff", ".pdf": "application/pdf",
             ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".svg": "image/svg+xml"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            path = str(args[0]) if args else ""  # send_error passes the status code here
            if any(x in path for x in ("/api/job/", "/api/version", "/mathjax/")):
                return
            sys.stderr.write("[galley] " + (fmt % args) + "\n")

        def send_json(self, obj, status=200):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", PLAIN_CSP)
            self.end_headers()
            self.wfile.write(data)

        def send_file(self, path):
            if not path or not os.path.isfile(path):
                return self.send_error(HTTPStatus.NOT_FOUND)
            with open(path, "rb") as fh:
                data = fh.read()
            ext = os.path.splitext(path)[1].lower()
            self.send_response(200)
            self.send_header("Content-Type", types.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if ext not in (".pdf", ".js", ".css", ".woff", ".woff2"):  # PDFs open in the browser's viewer
                self.send_header("Content-Security-Policy", PLAIN_CSP)
            self.end_headers()
            self.wfile.write(data)

        def send_page(self, html_text: str, status=200, extra=None):
            data = html_text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or []):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def refuse(self, why: str, status=HTTPStatus.FORBIDDEN):
            sys.stderr.write("[galley] refused %s %s: %s\n" % (self.command, urlparse(self.path).path, why))
            self.send_json({"error": "galley refused this request (%s). If you restarted galley, open the link "
                                     "it printed in the terminal, or run galley again." % why}, status)

        def host_ok(self) -> bool:
            return (self.headers.get("Host") or "") in hosts

        def has_key(self) -> bool:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == cookie_name and hmac.compare_digest(value, app.token):
                    return True
            return False

        def body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            url = urlparse(self.path)
            p = url.path
            q = parse_qs(url.query)
            if not self.host_ok():
                return self.refuse("unexpected Host header")
            try:
                if p in ("/", "/index.html"):
                    key = (q.get("t") or [""])[0]
                    if key and hmac.compare_digest(key, app.token):
                        # Opened from galley's link: keep the key in a cookie and drop it from the address bar.
                        cookie = "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (cookie_name, app.token)
                        self.send_response(303)
                        self.send_header("Location", "/")
                        self.send_header("Set-Cookie", cookie)
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if not self.has_key():
                        return self.send_page(LOCKED_PAGE, 403)
                    page = read_text(os.path.join(static_dir, "index.html")).replace(
                        "<head>", '<head>\n<meta name="galley-key" content="%s">' % app.token, 1)
                    return self.send_page(page)
                if p == "/api/whoami":  # lets a second "galley" command find this one (no key needed)
                    return self.send_json({"app": "galley", "main": app.doc.main_path})
                if not self.has_key():
                    return self.refuse("missing or wrong key")
                if p.startswith("/static/"):
                    target = os.path.normpath(os.path.join(static_dir, p[len("/static/"):]))
                    return self.send_file(target if target.startswith(static_dir + os.sep) else None)
                if p.startswith("/mathjax/"):
                    rest = p[len("/mathjax/"):]
                    if app.mathjax_dir:
                        target = os.path.normpath(os.path.join(app.mathjax_dir, rest))
                        if target.startswith(app.mathjax_dir + os.sep) and os.path.isfile(target):
                            return self.send_file(target)
                    return self.send_error(HTTPStatus.NOT_FOUND)
                if p == "/pdf":
                    return self.send_file(app.doc.build_file(".pdf"))
                if p == "/asset":
                    path = app.doc.abs_in_doc(q["path"][0])
                    if not os.path.splitext(path)[1]:
                        for ext in (".png", ".pdf", ".jpg"):
                            if os.path.exists(path + ext):
                                path += ext
                                break
                    return self.send_file(path)
                if p == "/api/doc":
                    return self.send_json(app.doc_json())
                if p == "/api/version":
                    return self.send_json({"fingerprint": app.doc.fingerprint()})
                if p == "/api/annotations":
                    return self.send_json({"items": app.notes.all()})
                if p == "/api/source":
                    return self.send_json({"text": app.source(q["file"][0], int(q["start"][0]), int(q["end"][0]))})
                if p == "/api/jobs/running":
                    return self.send_json({"items": app.jobs.running("wrapup")})
                if p.startswith("/api/job/"):
                    j = app.jobs.get(p.rsplit("/", 1)[1])
                    return self.send_json(j or {"error": "no such job"}, 200 if j else 404)
                if p == "/api/records":
                    return self.send_json({"items": list_records(app.doc, app.record.path)})
                if p == "/api/record":
                    name = os.path.basename(q.get("name", [""])[0])
                    if name and (not app.record.path or name != os.path.basename(app.record.path)):
                        path = os.path.join(app.doc.folder, "records", name)
                        if not os.path.isfile(path):
                            return self.send_json({"error": "no such record"}, 404)
                        return self.send_json({"path": app.doc.rel(path), "text": read_text(path)})
                    return self.send_json({"path": app.record.rel(), "text": app.record.read()})
                if p.startswith("/api/diagram/") and p.endswith(".svg"):
                    h = p[len("/api/diagram/"):-4]
                    if not re.fullmatch(r"[0-9a-f]{8,64}", h):
                        return self.send_error(HTTPStatus.NOT_FOUND)
                    return self.send_file(app.diagrams.svg_path(h))
                if p == "/api/instructions":
                    return self.send_json({"text": app.doc.instructions(), "exists": os.path.exists(app.doc.instructions_path),
                                           "path": app.doc.rel(app.doc.instructions_path)})
                self.send_error(HTTPStatus.NOT_FOUND)
            except Exception as exc:
                traceback.print_exc()
                self.send_json({"error": str(exc)}, 500)

        def do_POST(self):
            p = urlparse(self.path).path
            if not self.host_ok():
                return self.refuse("unexpected Host header")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in origins:
                return self.refuse("request from another site")
            if not self.has_key() or not hmac.compare_digest(self.headers.get("X-Galley-Key") or "", app.token):
                return self.refuse("missing or wrong key")
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.refuse("not a JSON request", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            try:
                req = self.body()
                if p == "/api/ask":
                    return self.send_json(app.ask(req))
                if p == "/api/decide":
                    return self.send_json(app.jobs.decide(req["job"], bool(req.get("accept")), req.get("note", "")))
                if p == "/api/wrapup":
                    return self.send_json(app.wrapup(req))
                if p == "/api/build":
                    return self.send_json(app.build(str(req.get("approve") or "")))
                if p == "/api/edit":
                    return self.send_json(app.edit_source(req))
                if p == "/api/settings":
                    for k in ("mark_agent_text",):
                        if k in req:
                            app.doc.settings[k] = bool(req[k])
                    app.doc.save_settings()
                    return self.send_json({"ok": True, "settings": app.doc.settings})
                if p == "/api/marking/setup":
                    return self.send_json(app.setup_marking())
                if p == "/api/marking/clear":
                    return self.send_json(app.clear_marking(req))
                if p == "/api/instructions":
                    return self.send_json(app.save_instructions(req.get("text", ""), bool(req.get("skip"))))
                if p == "/api/diagrams":
                    return self.send_json(app.diagrams.status(list(req.get("hashes") or [])))
                if p == "/api/instructions/draft":
                    return self.send_json(app.draft_instructions(req))
                if p == "/api/archive":
                    archived = bool(req.get("archived", True))
                    ids = [a["id"] for a in app.notes.all() if not a.get("archived")] if req.get("all") else list(req.get("ids") or [])
                    n = app.notes.set_archived(ids, archived, app.person)
                    if n:
                        what = ("all %d saved items" % n) if req.get("all") else (
                            "%d saved item%s (%s)" % (n, "" if n == 1 else "s", ", ".join("`%s`" % i for i in ids[:8])))
                        app.record.append("### %s \u00b7 %s\n\n%s %s %s. Archived items are hidden in galley, not deleted." % (
                            now().strftime("%H:%M"), "Archived" if archived else "Unarchived", app.person,
                            "archived" if archived else "unarchived", what))
                    return self.send_json({"ok": True, "changed": n})
                if p == "/api/note":
                    entry = {"id": "c-" + uuid.uuid4().hex[:8], "type": "comment", "person": app.person,
                             "time": now().isoformat(timespec="seconds"), "anchor": req["anchor"],
                             "question": req.get("text", ""), "answer": ""}
                    app.notes.append(entry)
                    app.record.append("### %s \u00b7 Comment \u00b7 %s\n\n%s" % (
                        now().strftime("%H:%M"), anchor_desc(req["anchor"]), said(app.person, req.get("text", ""))))
                    return self.send_json({"ok": True})
                self.send_error(HTTPStatus.NOT_FOUND)
            except Exception as exc:
                traceback.print_exc()
                self.send_json({"error": str(exc)}, 500)

    return Handler


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def running_galley(port: int) -> str | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/whoami" % port, timeout=1) as r:
            info = json.loads(r.read().decode("utf-8"))
            return info.get("main") if info.get("app") == "galley" else None
    except Exception:
        return None


def key_path(doc_folder: str) -> str:
    return os.path.join(doc_folder, "state", "server.json")


def save_key(doc: Document, port: int, token: str):
    """Keep this run's key where only this user can read it (galley/state/ is not shared), so that a
    second "galley" command for the same document can open a window with it."""
    path = key_path(doc.folder)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"port": port, "key": token, "pid": os.getpid()}, fh)
    os.chmod(path, 0o600)


def saved_key(main_path: str, port: int) -> str | None:
    try:
        with open(key_path(os.path.join(os.path.dirname(main_path), FOLDER)), encoding="utf-8") as fh:
            info = json.load(fh)
        return info.get("key") if info.get("port") == port else None
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(prog="galley", description="Read a LaTeX document and work on it with AI agents.")
    ap.add_argument("main", nargs="?", help="the document's main .tex file (default: the one in this folder)")
    ap.add_argument("--port", type=int, default=8765, help="first port to try (default 8765)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    ap.add_argument("--version", action="version", version="galley " + VERSION)
    args = ap.parse_args()
    main_path = find_main(args.main)

    # Reuse a galley already serving this document; otherwise find a free port.
    port = args.port
    for _ in range(30):
        if not port_in_use(port):
            break
        if running_galley(port) == main_path:
            url = "http://127.0.0.1:%d/" % port
            key = saved_key(main_path, port)
            if key:
                url += "?t=" + key
            print("galley is already running for this document: %s" % url)
            if not args.no_browser:
                webbrowser.open(url)
            return
        port += 1

    doc = Document(main_path)
    cfg = load_user_config(doc.dir, interactive=True)
    app = App(doc, cfg)
    try:
        app.doc_json()
    except Exception as exc:
        sys.exit("galley: could not read %s: %s" % (main_path, exc))
    app.port = port
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    save_key(doc, port, app.token)
    url = "http://127.0.0.1:%d/?t=%s" % (port, app.token)
    print("galley %s: %s" % (VERSION, url))
    print("Document: %s  (galley folder: %s)" % (doc.rel(doc.main_path), doc.rel(doc.folder)))
    for name, spec in cfg["agents"].items():
        state = "" if spec.get("available") else "  [not found on PATH]"
        extra = ""
        if spec.get("efforts"):
            extra = ", model %s, thinking %s by default" % (spec.get("model") or "NOT SET", spec.get("default_effort"))
        print("  agent %s%s%s" % (spec.get("label", name), extra, state))
    if not app.mathjax_dir:
        print("  MathJax not found in %s (or GALLEY_MATHJAX): mathematics will not be rendered."
              % os.path.join(HERE, "vendor", "mathjax"))
    for w in [doc.agent_cwd_problem()]:
        if w:
            print("  Note: " + w)
    print("Stop with Ctrl-C.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
