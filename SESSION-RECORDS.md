# Session records

galley follows these conventions; agents working on a document outside galley should follow them too.

Much research work now happens in conversations with agents. A session record keeps what a polished document leaves out: motivations and intuition (especially moves between physics and mathematics that reshape a question), what was tried and failed and why, which assumptions were in force, who decided what, and what each person seemed to take away. Records are written for people and agents who weren't there, including yourself later.

## Rules for agents

- Keep one record per session in the `galley/records/` folder next to the document's main file (or a `records/` folder next to the work), named `YYYY-MM-DD-<person>-<slug>.md`.
- Start early and update at checkpoints: a human decision, a result, a dead end, an artifact produced, or when asked to "log this" or "wrap up". Don't wait for the end of the session.
- Report aims and decisions only as firmly as the session supports them. Note uncertainty, tentativeness and shifts in direction; don't assign anyone a goal the session doesn't show.
- Record what the human seemed to value (marked as inferred, with the evidence), not only explicit decisions.
- Record physical and mathematical intuition, and who offered it, even when informal or later abandoned.
- For each claim, say what was actually done to check it (agent argument, agent check and how, computation, confirmed by a named person) and against which file and version.
- Records are append-only. Correct an earlier record with a new entry that says what it supersedes. Summaries go at the top.
- Keep it short. No boilerplate caveats; the status of each claim carries that.
- If the human says "off the record", write nothing to shared files.
- At the start of a session, read the recent records for the document.

## Template

```markdown
# <short title of what the session worked on>

- Date: YYYY-MM-DD
- Human: <name>
- Agent: <tool (model, thinking level)>
- Work area: <folder or document>
- Continues: <previous record in this thread, if any>

## Goal
What the human seemed to be after, and why, as far as the session shows.

## What happened
The main moves. Name the source of key ideas where it matters.

## What the human took from it
Explicit decisions, quoted briefly; what they seemed to find valuable (inferred, with evidence).

## Intuition and motivation
Physical pictures, heuristics, mathematical intuitions; moves between physics and mathematics.

## Results and their status
Each claim with what was done to check it. Note assumptions.

## Dead ends
What was tried and failed, and why.

## Open threads
Questions left open, next steps, things to ask others.

## Artifacts
Files produced or changed, with commit or version.
```

Omit empty headings.
