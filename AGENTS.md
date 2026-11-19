# galley: instructions for agents

If you are asked to change galley's own code and a `CLAUDE.md` file is present in this folder, read it first and follow it: it holds the maintainer's rules for developing galley. Otherwise see the "Development" section of `README.md` and `CONTRIBUTING.md`.

Read `README.md` too; this file adds what an agent needs to install galley for someone, set it up for a document, and help when something goes wrong.

galley is a small local web app (Python 3.9+ standard library, plus the MathJax copy in `vendor/mathjax/`) that renders a LaTeX document in the browser and passes the person's questions and edit requests to an agent command-line tool (Codex `codex exec` or Claude Code `claude -p`) running **read-only** in the document's folder. galley itself applies edits only when the person accepts them, and never commits or pushes.

## Installing galley for someone

1. Ask where they keep tools; default to `~/tools/galley`. Clone the repository there (`git clone https://github.com/hermele/galley.git ~/tools/galley`), or unzip a release bundle there. Do not put it inside their document's folder or repository unless they want it shared with collaborators.
2. Check `python3 --version` (3.9 or newer).
3. Link the launcher onto their PATH: `mkdir -p ~/.local/bin && ln -s <galley folder>/galley ~/.local/bin/galley`. If `~/.local/bin` is not on PATH, add `export PATH="$HOME/.local/bin:$PATH"` to their shell startup file (`~/.zshrc` on macOS) **only with their agreement**, and tell them to open a new terminal.
4. Check which agent CLIs they have: `which codex claude`. galley offers whichever exist. Each must be signed in once by the person (`codex`, or `claude`, run interactively); never handle their credentials.
5. Check `latexmk` if they want galley's **Build PDF**. It is optional.
6. Run `galley --version`.

Do not change the person's global Git, shell or editor settings beyond step 3, and do not install Python packages; galley needs none.

## Setting galley up for a document

- Start it with `galley path/to/main.tex`, or from the document's folder with plain `galley` (it looks for the single `.tex` file containing `\documentclass`). On first run it asks the person's name (stored in `~/.config/galley/config.json`) and creates `galley/` next to the main file.
- **Instructions for agents** (`galley/instructions.md`) are included in every request. galley's first-run banner offers to have an agent draft them. If the person asks *you* to write them, cover: what the document is; authors and status; notation and conventions (point to the macro file); related sources and where they are; how agents should work on it (for example, which parts may be edited, house style, whether to mark agent text). Keep it short (under about 60 lines). Do not invent facts; ask the person for what you cannot find. Questions for the person go in a "## Questions for <person>" section right after the title, followed by galley's note that they are open issues (galley adds the note if it is missing). Agent drafts made in galley are written straight to the file.
- If the document lives in a project that already has `AGENTS.md`/`CLAUDE.md` instructions, galley's agents also see those (Codex reads `AGENTS.md` files from the repository root down to the folder it starts in; Claude Code reads `CLAUDE.md` files). galley's instructions should not duplicate them; refer to them instead.
- `galley/settings.json` holds per-document settings:
  - `"build"`: the build command, with `{main}` for the main file. A command other than galley's default, or a `latexmkrc` in the folder, is shown to the person for approval before the first build; never approve it for them;
  - `"agent_cwd"`: the folder agents start in, relative to the document folder (default `.`); it must lie inside the document folder or its repository, otherwise galley uses the document folder;
  - `"mark_agent_text"`: whether agents wrap their text in `\AGTADD{...}`.

  Edit these only on request.
- If the document is in a Git repository shared with others, `galley/` (except `build/` and `state/`) is meant to be committed, so collaborators share notes, records and instructions. Ask before committing anything.

## How galley calls agents

For each request, galley runs one of these commands in the document folder (or `agent_cwd`):

- Codex: `codex exec --skip-git-repo-check --cd <folder> --sandbox read-only -m <model> -c model_reasoning_effort="<effort>" --output-last-message <file> -`
- Claude Code: `claude -p [--model <model>] --allowedTools Read,Grep,Glob --output-format text`

The prompt arrives on standard input. It contains:

- galley's context: the document's files, where records and notes are, and the instructions file;
- the passage's LaTeX, with its file and line range (or a section's ranges, or "the whole document");
- the request, and any earlier questions and answers in the same thread.

After the instructions, galley adds a standing note: questions listed in the instructions are open issues about the document's setup, not requests; agents keep them in mind but do not assume answers or act on them unless asked.

Edit proposals must come back as blocks:

```
FILE: <path relative to the repository root, or the document folder if not in a repository>
<<<<<<< ORIGINAL
<exact existing lines>
=======
<replacement lines>
>>>>>>> REVISED
```

galley checks that each ORIGINAL occurs exactly once in one of the document's source files (the `.tex` files it includes and the `.bib` files it names; never `galley/` files, `latexmkrc` or anything else), shows a diff, and writes the files only when the person accepts. A proposal that touches any other file is refused as a whole and shown as refused.

The commands can be overridden in `~/.config/galley/config.json` under `"agents"` (same keys as in `galley_server.py`'s `DEFAULT_AGENTS`). The Codex model comes from the top-level `model = "..."` in `~/.codex/config.toml` unless set there.

## Session records

galley writes one record per session in `galley/records/` and the full text of every interaction in `galley/notes.jsonl` (append-only JSON lines; archived items are hidden, not deleted). Wrap-up summaries go at the top of a record. When you work on the same document **outside** galley, follow `SESSION-RECORDS.md` and put your record in the same `galley/records/` folder, named `YYYY-MM-DD-<person>-<slug>.md`. At the start of a session, read the recent records there.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| "codex/claude was not found on your PATH" | galley was started from a terminal where the command isn't on PATH. Fix PATH, then restart galley. |
| Codex: "Not inside a trusted directory and --skip-git-repo-check was not specified" | A custom `agents.codex.command` in galley's config without `--skip-git-repo-check`: Codex runs only in a Git repository or a folder trusted in `~/.codex/config.toml`. Add `--skip-git-repo-check` to the command (galley runs Codex in a read-only sandbox anyway), or add `[projects."<folder>"]` with `trust_level = "trusted"` to `~/.codex/config.toml`. |
| An agent fails with "Operation not permitted" (macOS) | The terminal galley was started from has no access to the folder (Desktop, Documents, Downloads and iCloud folders are protected). Allow the terminal app under System Settings → Privacy & Security → Files and Folders (or Full Disk Access) and restart galley. |
| Agent error about the model | Codex: no `model = "..."` at the top of `~/.codex/config.toml`, or an unknown model; set `agents.codex.model` in galley's config. |
| References show `??` | The label is new since the last build: run **Build PDF** (or the person's usual build). |
| Odd rendering of a construct | The renderer is approximate; check the PDF. Unknown commands show in orange. Persistent problems can be reported as an issue at https://github.com/hermele/galley (with a small example the person is happy to share publicly). |
| **Build PDF** asks to approve a command | The document's `galley/settings.json` sets a build command other than galley's default, or the folder has a `latexmkrc` (or the command names a script). Let the person read it and decide; approvals are stored in `~/.config/galley/approvals.json`. |
| An edit proposal says "edit refused" | The agent proposed a change to a file that is not one of the document's sources. galley never applies such edits; ask for the change to the `.tex` or `.bib` file instead, or make other changes outside galley. |
| The top bar says "changes not checked" | The repository's own Git settings define a filter program, so galley does not run `git status` there (it could run that program). |
| Mathematics shows as raw TeX | `vendor/mathjax/` is missing or incomplete (for example a partial copy of galley). Restore it from the repository or a release. |
| "changed since this edit was proposed" | The file changed after the agent proposed the edit; ask again. |
| A `tikzcd` diagram shows "could not be compiled" | "Why" under the placeholder shows the LaTeX error. galley compiles with the document's preamble using `latex` (DVI mode) and `dvisvgm`; check that both are on PATH (galley also looks in `/Library/TeX/texbin`) and that the preamble works with `latex`. Failures are retried when galley restarts; the cache is `galley/build/diagrams/`. |
| The page says "This galley window needs its key", or actions fail with "galley refused this request" | The browser tab is not the one galley opened in this run (a bookmark, or a tab from before galley was restarted). Open the link galley printed (it ends in `?t=...`), or run `galley` again for the document. |
| Port already in use | galley tries the next ports automatically; `--port N` picks one. |

Do not edit files inside `galley/records/` or `notes.jsonl` by hand except on the person's explicit request: they are the history of the work.
