# galley

galley by Michael Hermele. Version 1.0.0.

galley lets you read a LaTeX document in your web browser and work on it with an AI agent (Codex or Claude Code) at your side. You can:

- **read** the document with collapsible sections and proofs, a contents sidebar and clickable references;
- **ask** about any paragraph, theorem, proof or section (the **+** in the margin, or select text), or about the **whole document** (the top bar), for example to change notation globally;
- **get edits proposed** by the agent as a diff, which change your files only when you accept them;
- **edit the LaTeX directly** in the sidebar;
- see agent-written text **in violet**, if you want, in galley and in the PDF;
- keep **notes and session records**: every question, answer, edit and comment is saved next to your document, and a summary of each session can be added at the top of its record.

galley runs entirely on your own computer. The agents run read-only, so files change only through edits you accept or make yourself. galley never commits or pushes anything.

You are encouraged to modify and distribute your own custom version(s) of galley for a range of tasks as you see fit, following the applicable terms (see below as well as `LICENSE` and `NOTICE`, including the attribution term).

galley was written by Claude, Anthropic's AI model (working in Claude Cowork), under the direction of Michael Hermele, who uses it in his research group.

## Requirements

- **macOS or Linux**, with **Python 3.9 or newer** (`python3 --version`). No Python packages are needed. Windows is untested.
- At least one agent command-line tool, installed and signed in:
  - **Codex**: the `codex` command ([OpenAI Codex CLI](https://github.com/openai/codex)).
  - **Claude Code**: the `claude` command ([Claude Code](https://code.claude.com/docs/en/overview)); run `claude` once to sign in.
- **LaTeX** (MacTeX or TeX Live): `latexmk` for galley's **Build PDF** (galley can also use a PDF you build yourself), and `latex` and `dvisvgm` to draw `tikzcd` diagrams. Without them galley still works and shows a placeholder for each diagram.
- A web browser. galley includes MathJax, so it needs no internet connection of its own (the agents do).
- Git is optional: galley works on a document in any folder. In a Git repository it also shows uncommitted changes and helps collaborators share notes.

## Install

1. Get galley, either by cloning the repository

   ```sh
   git clone https://github.com/hermele/galley.git ~/tools/galley
   ```

   or by downloading the zip of a release from the repository's **Releases** page and unpacking it, e.g. to `~/tools/galley`. To update a clone later: `git -C ~/tools/galley pull`.
2. Make the `galley` command available everywhere by linking it into a folder on your PATH:

   ```sh
   mkdir -p ~/.local/bin
   ln -s ~/tools/galley/galley ~/.local/bin/galley
   ```

   If `~/.local/bin` is not on your PATH yet (`echo $PATH`), add it:
   `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc` (or `~/.bashrc`) and open a new terminal.
3. Check: `galley --version`.

## Use

```sh
galley path/to/paper.tex      # or: cd into the document's folder and run  galley
```

galley opens the document in your browser (at `http://127.0.0.1:8765/`, or the next free port). Stop it with Ctrl-C. Running `galley` again for a document that is already open just brings it up in the browser. For safety, galley answers only the window it opened: the link it opens (and prints) carries a key for this run. A bookmark or an old tab from before a restart shows "needs its key"; run `galley` again, or open the printed link. Run it from a terminal where `codex` and `claude` work, since galley uses that terminal's PATH.

**The first time**, galley asks for your name (used in session records) and creates a `galley/` folder next to your main `.tex` file. It then offers to **set up instructions for agents**: a short file saying what the document is, its authors, conventions and related sources, which is included in every request. "Draft with agent" lets the agent read the document and its surroundings and write `galley/instructions.md`, starting with a few questions for you. The draft is saved to the file as soon as it is ready. Edit it in galley (More → Instructions for agents) and Save, or answer some of the questions and click "Revise with agent". You don't have to answer them: agents are told that unanswered questions are open issues, not requests.

**Day to day**

| To do this | In galley |
| --- | --- |
| Ask about a passage | Hover in the margin and click **+**, or select text and click **Ask about selection**; type and press **Ask** |
| Discuss a section | **+** next to the section heading |
| Discuss the whole document | **Discuss document** in the top bar |
| Get an edit proposed | Type the request and press **Suggest edit**; then **Accept edit** or **Reject** |
| Edit the LaTeX yourself | Open a passage, **Edit source**, then **Save to file** (Cmd/Ctrl-S) |
| Save a note without an agent | **Comment** |
| Find the passage the sidebar is discussing | **Show in text** in the sidebar header, or Cmd-Shift-L (Ctrl-Shift-L; shown on the button) |
| Choose the agent and how hard it thinks | **Agent** and **Thinking** in the top bar |
| Summarize the session | **Wrap up** (runs in the background; the button shows a spinner until it is done), or type "log this" or "wrap up" |
| See records, summarize older ones, archive old notes | **More → Session records and notes** |
| Edit the instructions for agents | **More → Instructions for agents** |
| Mark agent-written text in violet | **More → Mark agent-written text in violet** |
| Hide violet text (show it in normal colour) | **More → Show violet text** |
| Remove violet marking after review | **Clear violet marking here** in the sidebar, or **More → Clear all violet marking** |
| Rebuild the PDF / open it | **More → Build PDF** / **Open PDF** |
| Version, license and source | **More → About galley** |

Opening or closing the sidebar, hiding the contents (☰) or resizing the window changes the width of the text, so it reflows; galley keeps your place, holding the passage under discussion (or else the text at the top of the window) where it was.

If you edit the `.tex` files in another editor while galley is open, galley notices within a couple of seconds and updates the page.

### Commutative diagrams

galley draws `tikzcd` diagrams by compiling each display that contains one with your document's own preamble (`latex`, then `dvisvgm`) in the background, the first time it is shown. The pictures are cached in `galley/build/diagrams/`, so a diagram is compiled again only when it or the preamble changes. Equation numbers are kept, and clicking a diagram opens its LaTeX source in the sidebar. If a diagram does not compile, galley shows a placeholder with the LaTeX error under "Why"; it tries again the next time galley starts.

### Violet marking

When marking is on, agents wrap text they add or rewrite in `\AGTADD{...}`. galley shows it in violet, and so does the PDF. Turning marking on the first time adds a few lines before `\begin{document}` in your main file, after asking:

```latex
\newif\ifgalleyshowagent
\galleyshowagenttrue
\DeclareRobustCommand{\AGTADD}[1]{\ifgalleyshowagent{\color{violet}#1}\else{#1}\fi}
```

Change `\galleyshowagenttrue` to `\galleyshowagentfalse` to print agent text in the normal colour without removing the marking. Once you have reviewed agent text and made it your own, clear its marking; the text stays.

## What galley writes

Everything goes into the `galley/` folder next to your main file, except edits to your `.tex` and `.bib` files that you accept or make.

| File | What it is | Share it? |
| --- | --- | --- |
| `galley/records/YYYY-MM-DD-<name>-galley-HHMM.md` | One record per galley session: every interaction as it happened, with any summary at the top | yes |
| `galley/notes.jsonl` | Questions, answers, edit proposals and comments, attached to passages | yes |
| `galley/instructions.md` | Instructions for agents about this document | yes |
| `galley/settings.json` | Settings for this document (marking, build command) | yes |
| `galley/build/`, `galley/state/` | PDF builds and scratch files | no (ignored by Git) |

If your document is in a Git repository, galley adds a `.gitattributes` line so that `notes.jsonl`, which is append-only, merges cleanly when several people use galley on the same document.

Your personal settings are in `~/.config/galley/config.json`: your name, and optionally your agents' settings. Build commands you have approved are remembered in `~/.config/galley/approvals.json`. galley takes Codex's model from `~/.codex/config.toml` and always passes the model and thinking level explicitly, so records show exactly what was used. To change them, add for example:

```json
{
  "person": "Ada",
  "agents": {
    "codex": { "model": "<a Codex model name>", "efforts": ["low", "medium", "high"], "default_effort": "medium" },
    "claude": { "model": "<a Claude model name or alias>" }
  }
}
```

## Security

galley runs only on your computer and is built to be safe to point at a document someone else wrote.

- **Only galley's own window can use it.** galley listens on 127.0.0.1 only. Every request must carry the key of the current run (from the link galley opens), come from galley's own page and, for changes, be a JSON request; requests from other web pages, other host names (DNS rebinding) or without the key are refused.
- **Nothing in a document can run code in your browser or contact other sites.** galley's page may load scripts, styles, fonts and images only from galley itself (MathJax is included). Document links work only for web and mail addresses, and MathJax's `\href`, `\style` and `\cssId` show their contents without the effect.
- **Agents run read-only.** Codex runs in its read-only sandbox; Claude Code may only read, search and list files. Agents start in the document's folder (or a folder inside it or its repository, if `galley/settings.json` says so).
- **Files change only when you say so, and only the document's sources.** Edits, by hand or proposed by an agent and accepted, can change only the `.tex` files the document includes and the `.bib` files it names: never `galley/` files, `latexmkrc`, scripts or anything else, and never through a symlink pointing out of the document folder. An agent's proposal that touches any other file is refused as a whole and shown in red, so you can see what was attempted.
- **galley asks before building with a command it has not seen.** `galley/settings.json` travels with a shared document and sets the command **Build PDF** runs, and `latexmk` runs a `latexmkrc` in the document's folder as Perl code. Before running a command other than galley's default, or with such a file present, galley shows them and asks; your approval is remembered in your own settings (not in the document) until the command or the files change.
- **Diagrams are compiled safely.** galley compiles `tikzcd` diagrams with the document's preamble automatically, so it runs `latex` without shell escape and with restricted file access: no commands, no reading or writing files outside its work folder and TeX's search path.
- **Git is used read-only and cannot run programs.** galley runs only read-only `git` commands, with the repository's `core.fsmonitor` and hooks switched off. If the repository's own settings define a filter program, galley does not run `git status` there.

**What remains up to you:** a document someone shares with you can contain instructions aimed at the agent, in its text, in `galley/instructions.md` or in its notes ("prompt injection"). The agent cannot change files or reach the network on its own, but its answers are shown to you: treat unexpected links, commands or advice in an answer with care, and read proposed diffs before accepting them.

To report a security problem, see `SECURITY.md`.

## Session records

Each galley session writes a record as it goes, so nothing depends on remembering to save. **Wrap up** asks the agent to add a summary at the top: what you seemed to be after, what you seemed to take from the session (marked as inferred), intuition and motivation, results and how well they were checked, dead ends and open threads. See `SESSION-RECORDS.md` for the conventions, which also work for agent sessions outside galley.

## Limitations

- The rendering is approximate. `tikzcd` diagrams are drawn by LaTeX (see above), but other TikZ pictures appear as placeholders, unusual commands show in orange, and the PDF remains authoritative. Tables (`tabular`, `tabularx`, `longtable`, ...) are drawn as plain HTML tables: alignment, rules and `\multicolumn` are kept, but not colours, widths of `p{}` columns or `\multirow` spans. References and numbering come from the last LaTeX build where available, so rebuild after adding labels.
- Each question starts a fresh agent call. Follow-ups in the same sidebar include the earlier questions and answers, but the agent does not keep a running conversation.
- galley is meant for one person per computer, on their own copy of the document. Several people can use it on a shared document through Git.

For agents: see `AGENTS.md` for how to install galley for someone, set it up for a document and troubleshoot it.

## Development

galley is a few Python files (standard library only) and a static page: `galley_server.py` (server, agents, records), `latex_doc.py` (the LaTeX renderer), `diagrams.py` (tikzcd diagrams), `static/` (the page) and `vendor/mathjax/` (MathJax 3.2.2, see its README). The tests use sample documents in `tests/samples/` and run with

```sh
python3 -m unittest discover -s tests
```

Diagram tests are skipped where `latex`, `dvisvgm` or `tikz-cd` are missing. Bug reports and suggestions are welcome as issues; pull requests are not accepted for now (see `CONTRIBUTING.md`).

## License

galley by Michael Hermele. Copyright (C) 2026 The Regents of the University of Colorado.

galley is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version, with an additional term under section 7(b): modified versions, and works based on galley, must keep the attribution "Based on galley by Michael Hermele" in their README (or equivalent documentation) and in their About text (or equivalent legal notices). galley comes with no warranty. See `LICENSE` and `NOTICE`.

galley includes MathJax 3.2.2 (Copyright The MathJax Consortium, Apache License 2.0); see `vendor/mathjax/`.
