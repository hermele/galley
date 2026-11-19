"""Parse a LaTeX document into anchored blocks for galley.

Part of galley by Michael Hermele. Copyright (C) 2026 The Regents of the University of Colorado.
SPDX-License-Identifier: AGPL-3.0-or-later (with the additional term in NOTICE); see LICENSE.

Standard library only. The parser is deliberately approximate: it renders
text-mode LaTeX to HTML and leaves mathematics to MathJax. Every block
records the source file and line range it came from, so that a click in the
viewer can be turned into a precise reference for an agent.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import unicodedata
from dataclasses import dataclass, field

MATH_ENVS = {
    "equation", "equation*", "align", "align*", "gather", "gather*",
    "multline", "multline*", "flalign", "flalign*", "displaymath",
}
NUMBERED_MATH_ENVS = {"equation", "align", "gather", "multline", "flalign"}
DIAGRAM_RENDERER = "dvisvgm-driver-1"  # part of each diagram's cache key; change it to recompile all
SECTION_LEVELS = {"section": 1, "subsection": 2, "subsubsection": 3}
LIST_ENVS = {"enumerate": "ol", "itemize": "ul", "description": "ul"}
IGNORED_COMMANDS = {
    "noindent", "par", "small", "footnotesize", "normalsize", "large",
    "begingroup", "endgroup", "raggedright", "centering", "medskip",
    "smallskip", "bigskip", "newline", "maketitle", "tableofcontents",
    "protect", "relax", "nobreak", "allowbreak", "clearpage", "newpage",
    "phantomsection",
}
SIMPLE_SYMBOLS = {
    "ldots": "\u2026", "dots": "\u2026", "S": "\u00a7", "%": "%", "&": "&amp;",
    "_": "_", "#": "#", "{": "{", "}": "}", "$": "$", ",": "\u2009",
    ";": " ", ":": " ", "quad": "\u2003", "qquad": "\u2003\u2003",
    " ": " ", "-": "", "/": "", "textendash": "\u2013", "textemdash": "\u2014",
    "LaTeX": "LaTeX", "TeX": "TeX", "o": "\u00f8", "ss": "\u00df",
}
FONT_SWITCHES = {  # old-style and declaration forms: (tag, inline style)
    "bf": ("strong", ""), "bfseries": ("strong", ""),
    "it": ("em", ""), "itshape": ("em", ""), "em": ("em", ""), "sl": ("em", ""), "slshape": ("em", ""),
    "rm": ("span", "font-style:normal;font-weight:normal"), "rmfamily": ("span", ""),
    "upshape": ("span", "font-style:normal"), "mdseries": ("span", "font-weight:normal"),
    "normalfont": ("span", "font-style:normal;font-weight:normal"),
    "sf": ("span", "font-family:var(--sans)"), "sffamily": ("span", "font-family:var(--sans)"),
    "tt": ("code", ""), "ttfamily": ("code", ""),
    "sc": ("span", "font-variant:small-caps"), "scshape": ("span", "font-variant:small-caps"),
}
STYLE_COMMANDS = {
    "emph": "em", "textit": "em", "textbf": "strong", "texttt": "code",
    "textsf": "span", "textrm": "span", "textup": "span", "textsc": "span",
    "mbox": "span", "text": "span", "textnormal": "span", "underline": "u",
}
# Text-mode letters and symbols. Unlike SIMPLE_SYMBOLS they give way to a document's own
# definition of the same name (physicists often redefine \O, for instance). Also used
# inside \text{...} in mathematics, where MathJax does not know them.
TEXT_SYMBOLS = {
    "AA": "Å", "aa": "å", "AE": "Æ", "ae": "æ", "OE": "Œ", "oe": "œ",
    "O": "Ø", "L": "Ł", "l": "ł", "i": "ı", "j": "ȷ",
    "dag": "†", "ddag": "‡", "P": "¶", "copyright": "©", "pounds": "£",
    "textbackslash": "\\", "textasciitilde": "~", "textasciicircum": "^", "textbar": "|",
    "textless": "&lt;", "textgreater": "&gt;", "textdegree": "°", "textregistered": "®",
    "texttrademark": "™", "textbullet": "•", "textperiodcentered": "·",
    "textquoteleft": "‘", "textquoteright": "’", "textquotedblleft": "“",
    "textquotedblright": "”", "textellipsis": "…", "slash": "/", "textmu": "µ",
    "textonehalf": "½", "texttimes": "×", "textpm": "±", "checkmark": "✓",
}
# Text accents: \'e, \"o, \^{o}, \c{c}, ... (combining characters, then normalized).
ACCENTS = {
    "'": "́", "`": "̀", "^": "̂", '"': "̈", "~": "̃", "=": "̄",
    ".": "̇", "u": "̆", "v": "̌", "H": "̋", "c": "̧", "k": "̨",
    "r": "̊", "d": "̣", "b": "̱",
}
# Text letters that MathJax does not know; replaced by their characters in mathematics.
MATH_TEXT_LETTERS = {n: TEXT_SYMBOLS.get(n) or SIMPLE_SYMBOLS[n]
                     for n in ("AA", "aa", "AE", "ae", "OE", "oe", "O", "o", "L", "l", "ss")}
TEXT_ARG_IN_MATH = ("text", "textrm", "textit", "textbf", "textsf", "texttt", "textup",
                    "textnormal", "mbox", "hbox")


# ---------------------------------------------------------------------------
# Source reading


@dataclass
class Line:
    text: str  # with comments removed
    file: str  # path relative to the document directory
    lineno: int  # 1-based
    color: str = ""  # LaTeX colour of a {\color{...} ...} region spanning whole lines


def strip_comment(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(s[i : i + 2])
            i += 2
            continue
        if c == "%":
            break
        out.append(c)
        i += 1
    return "".join(out)


INPUT_RE = re.compile(r"\\(?:input|include)\{([^}]+)\}")


def read_expanded(note_dir: str, rel: str, depth: int = 0) -> list[Line]:
    if depth > 10:
        raise RuntimeError("\\input nesting too deep")
    path = os.path.join(note_dir, rel)
    if not os.path.exists(path) and not rel.endswith(".tex"):
        rel = rel + ".tex"
        path = os.path.join(note_dir, rel)
    lines: list[Line] = []
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, start=1):
            raw = raw.rstrip("\n")
            text = strip_comment(raw)
            if raw.strip() and not text.strip():
                # A line holding only a comment: in LaTeX the % also swallows the line break, so
                # the line is not a blank line (no paragraph break, no \par inside math). Drop it.
                continue
            m = INPUT_RE.search(text)
            if m and text.strip() == m.group(0):
                lines.extend(read_expanded(note_dir, m.group(1), depth + 1))
                continue
            lines.append(Line(text, rel, n))
    if depth == 0:
        mark_color_regions(lines)
    return lines


COLOR_OPEN_RE = re.compile(r"^(\s*)\{\\color\{([^}]*)\}")
TEXTCOLOR_OPEN_RE = re.compile(r"^(\s*)\\textcolor\{([^}]*)\}\{")


def mark_color_regions(lines: list) -> None:
    r"""Find colour groups that start a line and span several lines, sections or
    environments, e.g. {\color{blue}\section{...} ... }. Remove their braces so the
    structure is parsed normally, and record the colour on each line."""
    i = 0
    while i < len(lines):
        m = COLOR_OPEN_RE.match(lines[i].text) or TEXTCOLOR_OPEN_RE.match(lines[i].text)
        if not m:
            i += 1
            continue
        color = m.group(2).strip()
        depth, j, pos, found = 1, i, m.end(), None
        while j < len(lines) and lines[j].file == lines[i].file and found is None:
            t = lines[j].text
            k = pos
            while k < len(t):
                c = t[k]
                if c == "\\":
                    k += 2
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        found = (j, k)
                        break
                k += 1
            if found is None:
                j += 1
                pos = 0
        if found is None:
            i += 1
            continue
        if found[0] == i:
            # One-line group: unwrap it only around a heading, e.g. {\color{x}\subsection{...}};
            # other one-line groups are handled by the inline renderer.
            t = lines[i].text
            inner = t[m.end():found[1]]
            if re.match(r"\s*\\(sub)*section\*?\s*[\[{]", inner):
                lines[i].text = m.group(1) + inner + t[found[1] + 1:]
                lines[i].color = lines[i].color or color
            i += 1
            continue
        j, k = found
        lines[i].text = m.group(1) + lines[i].text[m.end():]
        t = lines[j].text
        lines[j].text = t[:k] + t[k + 1:]
        for line in lines[i : j + 1]:
            if not line.color:
                line.color = color
        i += 1


# ---------------------------------------------------------------------------
# Small scanning helpers


def read_group(s: str, i: int, open_c: str = "{", close_c: str = "}"):
    """s[i] must be open_c. Return (content, index after closing brace)."""
    assert s[i] == open_c
    depth = 0
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return s[i + 1 : j], j + 1
        j += 1
    return s[i + 1 :], len(s)


def skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\n":
        i += 1
    return i


def read_optional(s: str, i: int):
    j = skip_ws(s, i)
    if j < len(s) and s[j] == "[":
        content, k = read_group(s, j, "[", "]")
        return content, k
    return None, i


def read_arg(s: str, i: int):
    j = skip_ws(s, i)
    if j < len(s) and s[j] == "{":
        return read_group(s, j)
    if j < len(s) and s[j] == "\\":
        m = re.match(r"\\[A-Za-z]+|\\.", s[j:])
        if m:
            return m.group(0), j + len(m.group(0))
    if j < len(s):
        return s[j], j + 1
    return "", j


CMD_RE = re.compile(r"[A-Za-z]+\*?|.")


# ---------------------------------------------------------------------------
# Preamble: macros, theorem environments, title


@dataclass
class TheoremEnv:
    name: str
    display: str
    counter: str  # name of the counter it shares
    style: str  # plain | definition | remark
    numbered: bool = True
    within: str = ""  # e.g. "section" when numbered within sections


@dataclass
class Preamble:
    macros: dict = field(default_factory=dict)  # MathJax macro table
    theorems: dict = field(default_factory=dict)
    title: str = ""
    date: str = ""
    text_macros: dict = field(default_factory=dict)
    eq_within: str = ""  # from \numberwithin{equation}{section}
    float_within: dict = field(default_factory=dict)  # \numberwithin{figure}{section}, same for table
    colors: dict = field(default_factory=dict)  # \definecolor names -> CSS colours


def define_color_css(model: str, spec: str) -> str:
    try:
        if model == "rgb":
            r, g, b = (float(x) for x in spec.split(","))
            return "rgb(%d,%d,%d)" % (round(r * 255), round(g * 255), round(b * 255))
        if model == "RGB":
            r, g, b = (int(float(x)) for x in spec.split(","))
            return "rgb(%d,%d,%d)" % (r, g, b)
        if model == "HTML":
            return "#" + spec if re.fullmatch(r"[0-9A-Fa-f]{6}", spec) else ""
        if model == "gray":
            v = round(float(spec) * 255)
            return "rgb(%d,%d,%d)" % (v, v, v)
    except ValueError:
        pass
    return ""


def parse_preamble(lines: list[Line]) -> Preamble:
    pre = Preamble()
    text = "\n".join(l.text for l in lines)
    style = "plain"
    i = 0
    while i < len(text):
        if text[i] != "\\":
            i += 1
            continue
        m = CMD_RE.match(text, i + 1)
        if not m:
            i += 1
            continue
        name = m.group(0)
        j = m.end()
        if name in ("newcommand", "renewcommand", "providecommand", "newcommand*"):
            target, j = read_arg(text, j)
            target = target.strip().lstrip("\\")
            nargs, j = read_optional(text, j)
            default, j = read_optional(text, j)
            body, j = read_arg(text, j)
            if nargs:
                pre.macros[target] = [body, int(nargs)]
            else:
                pre.macros[target] = body
            pre.text_macros[target] = (body, int(nargs) if nargs else 0)
        elif name in ("DeclareMathOperator", "DeclareMathOperator*"):
            target, j = read_arg(text, j)
            body, j = read_arg(text, j)
            pre.macros[target.strip().lstrip("\\")] = "\\operatorname{%s}" % body
        elif name == "DeclarePairedDelimiter":
            target, j = read_arg(text, j)
            left, j = read_arg(text, j)
            right, j = read_arg(text, j)
            pre.macros[target.strip().lstrip("\\")] = [
                "\\left%s #1 \\right%s" % (left, right), 1]
        elif name == "theoremstyle":
            arg, j = read_arg(text, j)
            style = arg.strip()
        elif name in ("newtheorem", "newtheorem*"):
            env, j = read_arg(text, j)
            shared, j = read_optional(text, j)
            display, j = read_arg(text, j)
            within, j = read_optional(text, j)
            env = env.strip()
            pre.theorems[env] = TheoremEnv(
                name=env,
                display=display.strip(),
                counter=(shared or env).strip(),
                style=style,
                numbered=not name.endswith("*"),
                within=(within or "").strip(),
            )
        elif name == "definecolor":
            cname, j = read_arg(text, j)
            model, j = read_arg(text, j)
            spec, j = read_arg(text, j)
            pre.colors[cname.strip()] = define_color_css(model.strip(), spec.strip())
        elif name == "colorlet":
            cname, j = read_arg(text, j)
            base, j = read_arg(text, j)
            pre.colors[cname.strip()] = base.strip()
        elif name == "numberwithin":
            ctr, j = read_arg(text, j)
            parent, j = read_arg(text, j)
            if ctr.strip() == "equation":
                pre.eq_within = parent.strip()
            elif ctr.strip() in ("figure", "table"):
                pre.float_within[ctr.strip()] = parent.strip()
        elif name == "title":
            arg, j = read_arg(text, j)
            pre.title = arg
        elif name == "date":
            arg, j = read_arg(text, j)
            pre.date = arg
        i = j
    return pre


# ---------------------------------------------------------------------------
# Aux files


NEWLABEL_RE = re.compile(r"\\newlabel\{([^}]*)\}\{\{([^}]*)\}")
BIBCITE_RE = re.compile(r"\\bibcite\{([^}]*)\}\{([^}]*)\}")


TOC_RE = re.compile(r"\\contentsline\s*\{(section|subsection|subsubsection)\}\{\\numberline\s*\{([^}]*)\}")


def read_aux(path: str, prefix: str = "", toc: dict | None = None):
    """Labels and citation numbers from a LaTeX .aux file; optionally the numbering of
    sections in order (from the table-of-contents lines), keyed by level."""
    labels, cites = {}, {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = NEWLABEL_RE.search(line)
                if m:
                    labels[prefix + m.group(1)] = m.group(2).replace("\\,", " ").strip()
                m = BIBCITE_RE.search(line)
                if m:
                    cites[m.group(1)] = m.group(2).lstrip("{").strip()
                if toc is not None:
                    m = TOC_RE.search(line)
                    if m and m.group(2).strip():  # skip unnumbered entries such as "Contents"
                        toc.setdefault(SECTION_LEVELS[m.group(1)], []).append(m.group(2).strip())
    return labels, cites


# ---------------------------------------------------------------------------
# Rendering state


class Numbering:
    """Tracks LaTeX counters.

    The document's last build (.aux file) is authoritative where it has an answer: section
    numbers come from its table of contents, and labelled statements and equations take
    the number recorded there, which also resynchronizes the running counters. Unlabelled
    or new material is numbered by counting, using the preamble's numbering scheme.
    """

    def __init__(self, theorems: dict, eq_within: str = "", toc: dict | None = None,
                 float_within: dict | None = None):
        self.theorems = theorems
        self.eq_within = eq_within
        self.float_within = float_within or {}
        self.floats = {"figure": 0, "table": 0}
        self.toc = toc or {}
        self.counts = {1: 0, 2: 0, 3: 0}  # numbered sections seen at each level
        self.labels = {1: "", 2: "", 3: ""}
        self.appendix = False
        self.app_count = 0
        self.counters: dict[str, int] = {}
        self.equation = 0
        # Which counters reset with sections.
        self.within: dict[str, str] = {}
        for t in theorems.values():
            if t.within:
                self.within[t.counter] = t.within

    def bump_section(self, level: int) -> str:
        self.counts[level] += 1
        for deeper in range(level + 1, 4):
            self.labels[deeper] = ""
        if level == 1:
            if self.appendix:
                self.app_count += 1
            for ctr, w in self.within.items():
                if w == "section":
                    self.counters[ctr] = 0
            if self.eq_within == "section":
                self.equation = 0
            for kind, w in self.float_within.items():
                if w == "section":
                    self.floats[kind] = 0
        listed = self.toc.get(level, [])
        idx = self.counts[level] - 1
        if idx < len(listed):
            label = listed[idx]
        elif level == 1:
            label = chr(ord("A") + self.app_count - 1) if self.appendix else str(self.counts[1] - self.app_count)
        else:
            parent = self.labels[level - 1] or "?"
            n = self._own_sub(level)
            label = "%s.%d" % (parent, n)
        self.labels[level] = label
        return label

    def _own_sub(self, level: int) -> int:
        # Count of numbered subsections under the current parent, for documents without an aux file.
        key = "_sub%d_%s" % (level, self.labels[level - 1])
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def section_prefix(self) -> str:
        return self.labels[1] or "0"

    def bump_theorem(self, env: str) -> str | None:
        t = self.theorems.get(env)
        if not t or not t.numbered:
            return None
        self.counters[t.counter] = self.counters.get(t.counter, 0) + 1
        n = self.counters[t.counter]
        if self.within.get(t.counter) == "section":
            return "%s.%d" % (self.section_prefix(), n)
        return str(n)

    def sync_theorem(self, env: str, number: str):
        t = self.theorems.get(env)
        m = re.search(r"(\d+)\s*$", number or "")
        if t and m:
            self.counters[t.counter] = int(m.group(1))

    def bump_equation(self) -> str:
        self.equation += 1
        if self.eq_within == "section":
            return "%s.%d" % (self.section_prefix(), self.equation)
        return str(self.equation)

    def bump_float(self, kind: str) -> str:
        self.floats[kind] += 1
        if self.float_within.get(kind) == "section":
            return "%s.%d" % (self.section_prefix(), self.floats[kind])
        return str(self.floats[kind])

    def sync_float(self, kind: str, number: str):
        m = re.search(r"(\d+)\s*$", number or "")
        if m:
            self.floats[kind] = int(m.group(1))

    def sync_equation(self, number: str):
        m = re.search(r"(\d+)\s*$", number or "")
        if m:
            self.equation = int(m.group(1))


class Renderer:
    def __init__(self, pre: Preamble, aux_labels: dict, cites: dict, toc: dict | None = None):
        self.pre = pre
        self.aux_labels = aux_labels
        self.cites = cites
        self.own_labels: dict[str, str] = {}
        self.label_block: dict[str, str] = {}
        self.num = Numbering(pre.theorems, pre.eq_within, toc, pre.float_within)
        self.float_stack: list[dict] = []  # open figure/table environments
        self.anchored: set = set()  # labels whose anchor is already placed (at the top of a float)
        self.label_kind: dict[str, str] = {}
        self.authors: list = []
        self.warnings: list[str] = []
        self.current_block = None  # id of block being rendered
        self.labels_in_block: list[str] = []
        self.pending_refs: list[tuple[str, str]] = []  # (placeholder, label)
        self.diagram_key = ""  # preamble the diagrams are compiled with; part of their hash
        self.diagrams: dict[str, str] = {}  # hash -> LaTeX source of a display with a tikzcd diagram
        self._expanding: list[str] = []  # text macros being expanded (recursion guard)

    def add_diagram(self, src: str) -> str:
        """Register a display containing a tikzcd diagram for compiling; returns its hash."""
        def ref(m):
            n = self.aux_labels.get(m.group(2), "??")
            return "(%s)" % n if m.group(1) == "eqref" else n
        src = re.sub(r"\\(ref\*?|eqref|cref|Cref|autoref)\{([^}]*)\}", ref, src.strip())
        # No empty lines: one would be a paragraph break inside math, which LaTeX rejects. galley
        # makes them, e.g. where it removed a \label that stood on its own line.
        src = "\n".join(l for l in src.split("\n") if l.strip())
        h = hashlib.sha1((DIAGRAM_RENDERER + self.diagram_key + "\n%%\n" + src).encode("utf-8")).hexdigest()[:16]
        self.diagrams[h] = src
        return h

    def diagram_html(self, h: str) -> str:
        return ('<div class="diagram" data-diagram="%s" title="Click to see the LaTeX source">'
                '<span class="diagram-wait">tikzcd diagram: compiling\u2026</span></div>' % h)

    def css_color(self, name: str) -> str:
        name = name.strip()
        seen = 0
        while name in self.pre.colors and seen < 5:
            name = self.pre.colors[name]
            seen += 1
        # Only plain colour values reach a style attribute (no "red;background:url(...)").
        if re.fullmatch(r"#[0-9A-Fa-f]{3,8}|rgb\(\d{1,3},\d{1,3},\d{1,3}\)", name):
            return name
        if name.startswith(("rgb(", "#")):
            return "inherit"
        base = name.split("!")[0].strip()  # xcolor mixes like blue!50!black: keep the base colour
        return re.sub(r"[^A-Za-z]", "", base) or "inherit"

    # -- references -------------------------------------------------------

    def ref_number(self, label: str) -> str:
        if label in self.own_labels:
            return self.own_labels[label]
        if label in self.aux_labels:
            return self.aux_labels[label]
        return "??"

    def ref_placeholder(self, label: str, kind: str) -> str:
        token = "\u0000REF%d\u0000" % len(self.pending_refs)
        self.pending_refs.append((token, label, kind))
        return token

    def resolve_refs(self, s: str) -> str:
        for token, label, kind in self.pending_refs:
            if token not in s:
                continue
            n = self.ref_number(label)
            if kind == "math":
                rep = "\\text{%s}" % n
            elif kind == "matheq":
                rep = "\\text{(%s)}" % n
            elif label not in self.own_labels and label in self.aux_labels and ":" in label:
                # A label from another document (e.g. via the xr package): show its number, no link.
                text = "(%s)" % n if kind == "eq" else n
                rep = '<span class="extref" title="Reference to another document (%s)">%s</span>' % (
                    html.escape(label, quote=True), html.escape(text))
            else:
                text = "(%s)" % n if kind == "eq" else n
                if kind == "named":
                    kind_name = self.label_kind.get(label, "")
                    text = ("Eq. (%s)" % n) if kind_name == "equation" else ("%s %s" % (kind_name, n)).strip()
                rep = '<a class="ref" href="#lbl-%s" data-label="%s">%s</a>' % (
                    html.escape(label, quote=True), html.escape(label, quote=True), html.escape(text))
            s = s.replace(token, rep)
        return s

    def define_label(self, label: str, number: str | None, kind: str = ""):
        if kind:
            self.label_kind[label] = kind
        if number is not None:
            self.own_labels[label] = number
            aux = self.aux_labels.get(label)
            if aux is not None and aux != number:
                self.warnings.append(
                    "numbering differs for %s: counted %s, last build %s" % (label, number, aux))
        if self.current_block:
            self.label_block[label] = self.current_block
        self.labels_in_block.append(label)

    # -- math -------------------------------------------------------------

    def expand_ref_macros(self, m: str) -> str:
        """Expand one-argument preamble macros whose body is a reference, e.g. \\eqnum{x}."""
        for name, (body, nargs) in self.pre.text_macros.items():
            if nargs == 1 and re.search(r"\\(ref|eqref|cref|Cref|autoref)", body) and ("\\" + name) in m:
                m = re.sub(r"\\%s\s*\{([^}]*)\}" % re.escape(name),
                           lambda mm, b=body: b.replace("#1", mm.group(1)), m)
        return m

    def math_text_symbols(self, m: str) -> str:
        r"""Text-mode letters in mathematics, e.g. $5.4\,\text{\AA}$ or $1\,\AA$: MathJax knows
        neither, so put in the characters (inside \text{...}) or \text{<character>} (elsewhere).
        Names the document defines itself are left alone."""
        names = [n for n in MATH_TEXT_LETTERS if n not in self.pre.macros]
        if not any("\\" + n in m for n in names):
            return m
        pat = re.compile(r"\\(%s)(?![A-Za-z])\s*(?:\{\})?" % "|".join(sorted(names, key=len, reverse=True)))
        def chars(t):
            return pat.sub(lambda mm: MATH_TEXT_LETTERS[mm.group(1)], t)
        out, i = [], 0
        arg_re = re.compile(r"\\(%s)\s*\{" % "|".join(TEXT_ARG_IN_MATH))
        while True:
            mm = arg_re.search(m, i)
            if not mm:
                break
            content, k = read_group(m, mm.end() - 1)
            out.append(pat.sub(lambda x: "\\text{%s}" % MATH_TEXT_LETTERS[x.group(1)], m[i:mm.start()]))
            out.append(m[mm.start():mm.end()] + chars(content) + "}")
            i = k
        out.append(pat.sub(lambda x: "\\text{%s}" % MATH_TEXT_LETTERS[x.group(1)], m[i:]))
        return "".join(out)

    def math_refs(self, m: str) -> str:
        m = self.math_text_symbols(m)
        m = self.expand_ref_macros(m)
        def sub(match):
            cmd, label = match.group(1), match.group(2)
            kind = "matheq" if cmd == "eqref" else "math"
            return self.ref_placeholder(label, kind)
        return re.sub(r"\\(ref\*?|eqref|cref|Cref|autoref)\{([^}]*)\}", sub, m)

    def equation_number(self, labels: list, counted: str) -> str:
        for lab in labels:
            if lab in self.aux_labels:
                self.num.sync_equation(self.aux_labels[lab])
                return self.aux_labels[lab]
        return counted

    def render_display(self, env: str | None, body: str) -> str:
        """Render a display-math environment; returns HTML."""
        # tikzcd displays are compiled to SVG by galley (see diagrams.py); other TikZ pictures
        # cannot be drawn by MathJax: keep the equation (and its number), show a placeholder.
        has_cd = "\\begin{tikzcd}" in body
        body = re.sub(r"\\begin\{(tikzpicture|xymatrix)\}.*?\\end\{\1\}",
                      r"\\boxed{\\text{diagram (\1): see the PDF}}", body, flags=re.S)
        anchors = []
        numbered = env in NUMBERED_MATH_ENVS
        base = (env or "").rstrip("*")
        if env in ("align", "align*", "gather", "gather*", "flalign", "flalign*"):
            rows = split_rows(body)
            out_rows = []
            for row in rows:
                labels = re.findall(r"\\label\{([^}]*)\}", row)
                row = re.sub(r"\\label\{[^}]*\}", "", row)
                nonum = bool(re.search(r"\\(nonumber|notag)\b", row))
                row = re.sub(r"\\(nonumber|notag)\b", "", row)
                if numbered and not nonum and row.strip():
                    n = self.num.bump_equation()
                    n = self.equation_number(labels, n)
                    row = row + "\\tag{%s}" % n
                    for lab in labels:
                        self.define_label(lab, n, "equation")
                        anchors.append(lab)
                else:
                    for lab in labels:
                        self.define_label(lab, None)
                        anchors.append(lab)
                out_rows.append(row)
            body = "\\\\".join(out_rows)
            inner = "\\begin{%s*}%s\\end{%s*}" % (base, body, base)
        else:
            labels = re.findall(r"\\label\{([^}]*)\}", body)
            body = re.sub(r"\\label\{[^}]*\}", "", body)
            if numbered and not re.search(r"\\(nonumber|notag)\b", body):
                n = self.num.bump_equation()
                n = self.equation_number(labels, n)
                body = body + "\\tag{%s}" % n
                for lab in labels:
                    self.define_label(lab, n, "equation")
                    anchors.append(lab)
            else:
                for lab in labels:
                    self.define_label(lab, None)
                    anchors.append(lab)
            body = re.sub(r"\\(nonumber|notag)\b", "", body)
            if base in ("multline",):
                inner = "\\begin{multline*}%s\\end{multline*}" % body
            else:
                inner = "\\begin{equation*}%s\\end{equation*}" % body
        spans = "".join('<span class="lbl" id="lbl-%s"></span>' % html.escape(a, quote=True) for a in anchors)
        if has_cd:
            tags = re.findall(r"\\tag\{([^}]*)\}", inner)
            h = self.add_diagram(re.sub(r"\\tag\{[^}]*\}", "", inner))
            eqno = '<span class="diagram-eqno">%s</span>' % html.escape(", ".join("(%s)" % t for t in tags)) if tags else ""
            return '%s<div class="diagram-display">%s%s</div>' % (spans, self.diagram_html(h), eqno)
        inner = self.math_refs(inner)
        return '%s<div class="math display">%s</div>' % (spans, html.escape(inner, quote=False))

    # -- text -------------------------------------------------------------

    def render(self, s: str) -> str:
        """Render text-mode LaTeX (possibly containing math and lists)."""
        out: list[str] = []
        list_stack: list[dict] = []
        i = 0
        n = len(s)
        while i < n:
            c = s[i]
            if c == "$":
                if s.startswith("$$", i):
                    end = s.find("$$", i + 2)
                    end = n if end < 0 else end
                    out.append(self.render_display(None, s[i + 2 : end]))
                    i = end + 2
                else:
                    end = i + 1
                    while end < n and not (s[end] == "$" and s[end - 1] != "\\"):
                        end += 1
                    out.append("\\(" + html.escape(self.math_refs(s[i + 1 : end]), quote=False) + "\\)")
                    i = end + 1
                continue
            if c == "\\":
                if s.startswith("\\(", i):
                    end = s.find("\\)", i)
                    end = n if end < 0 else end
                    out.append("\\(" + html.escape(self.math_refs(s[i + 2 : end]), quote=False) + "\\)")
                    i = end + 2
                    continue
                if s.startswith("\\[", i):
                    end = s.find("\\]", i)
                    end = n if end < 0 else end
                    out.append(self.render_display(None, s[i + 2 : end]))
                    i = end + 2
                    continue
                if s.startswith("\\\\", i):
                    out.append("<br>")
                    i += 2
                    _, i = read_optional(s, i)
                    continue
                m = CMD_RE.match(s, i + 1)
                if not m:
                    i += 1
                    continue
                name = m.group(0)
                j = m.end()
                html_piece, i = self.render_command(name, s, j, list_stack)
                out.append(html_piece)
                continue
            if c == "{":
                # Render a group on its own so a \color inside it ends with the group.
                content, i = read_group(s, i)
                out.append(self.render(content))
                continue
            if c == "}":
                i += 1
                continue
            if c == "~":
                out.append("\u00a0")
                i += 1
                continue
            if s.startswith("---", i):
                out.append("\u2014")
                i += 3
                continue
            if s.startswith("--", i):
                out.append("\u2013")
                i += 2
                continue
            if s.startswith("``", i):
                out.append("\u201c")
                i += 2
                continue
            if s.startswith("''", i):
                out.append("\u201d")
                i += 2
                continue
            if c == "`":
                out.append("\u2018")
                i += 1
                continue
            if c == "\n":
                out.append(" ")
                i += 1
                continue
            out.append(html.escape(c, quote=False))
            i += 1
        while list_stack:
            top = list_stack.pop()
            if top["open_item"]:
                out.append("</li>")
            out.append("</%s>" % top["tag"])
        return "".join(out)

    def render_command(self, name: str, s: str, j: int, list_stack: list):
        star = name.endswith("*")
        base = name.rstrip("*")
        if base == "begin":
            env, k = read_arg(s, j)
            env = env.strip()
            if env in MATH_ENVS:
                end_tag = "\\end{%s}" % env
                end = find_env_end(s, k, env)
                return self.render_display(env, s[k:end]), end + len(end_tag)
            if env == "tikzcd":
                end = find_env_end(s, k, env)
                h = self.add_diagram("\\begin{tikzcd}%s\\end{tikzcd}" % s[k:end])
                return '<div class="diagram-display">%s</div>' % self.diagram_html(h), end + len("\\end{tikzcd}")
            if env in ("tikzpicture", "pgfpicture", "picture", "xy"):
                end = find_env_end(s, k, env)
                return '<div class="figure-placeholder">[%s figure: see the PDF]</div>' % html.escape(env), end + len("\\end{%s}" % env)
            if env in TABLE_ENVS:
                return self.render_table(env, s, k)
            if env in FLOAT_ENVS:
                _, k = read_optional(s, k)
                return self.open_float(FLOAT_ENVS[env], env, s, k), k
            if env == "center":
                return '<div class="figure">', k
            if env in LIST_ENVS:
                opt, k = read_optional(s, k)
                tag = LIST_ENVS[env]
                cls = ""
                if opt and "roman" in opt:
                    cls = ' class="roman"'
                elif opt and "alph" in opt:
                    cls = ' class="alpha"'
                list_stack.append({"tag": tag, "open_item": False})
                return "<%s%s>" % (tag, cls), k
            if env in ("pmatrix", "bmatrix", "matrix", "cases", "array", "aligned", "gathered"):
                # math env appearing in text mode: hand to MathJax as display
                end = find_env_end(s, k, env)
                return self.render_display(None, "\\begin{%s}%s\\end{%s}" % (env, s[k:end], env)), end + len("\\end{%s}" % env)
            # unknown text env: render contents
            return '<div class="env env-%s">' % html.escape(env), k
        if base == "end":
            env, k = read_arg(s, j)
            env = env.strip()
            if env in LIST_ENVS and list_stack:
                top = list_stack.pop()
                closing = ("</li>" if top["open_item"] else "") + "</%s>" % top["tag"]
                return closing, k
            if env in FLOAT_ENVS and self.float_stack:
                self.float_stack.pop()
            return "</div>", k
        if base == "item":
            opt, k = read_optional(s, j)
            prefix = ""
            if list_stack:
                top = list_stack[-1]
                if top["open_item"]:
                    prefix = "</li>"
                top["open_item"] = True
            lab = ""
            if opt is not None:
                lab = '<span class="itemlabel">%s</span> ' % self.render(opt)
                return prefix + '<li class="labelled">' + lab, k
            return prefix + "<li>", k
        if base in STYLE_COMMANDS:
            arg, k = read_arg(s, j)
            tag = STYLE_COMMANDS[base]
            return "<%s>%s</%s>" % (tag, self.render(arg), tag), k
        if base in ("ref", "pageref"):
            arg, k = read_arg(s, j)
            return self.ref_placeholder(arg.strip(), "ref"), k
        if base in ("autoref", "cref", "Cref", "nameref"):
            arg, k = read_arg(s, j)
            labels = [x.strip() for x in arg.split(",") if x.strip()]
            return ", ".join(self.ref_placeholder(l, "named") for l in labels), k
        if base == "eqref":
            arg, k = read_arg(s, j)
            return self.ref_placeholder(arg.strip(), "eq"), k
        if base == "color":
            # \color{c} colours the rest of the current group (render() scopes groups).
            arg, k = read_arg(s, j)
            return '<span style="color:%s">%s</span>' % (html.escape(self.css_color(arg), quote=True),
                                                        self.render(s[k:])), len(s)
        if base in FONT_SWITCHES:
            # {\bf ...}, \itshape, ...: style the rest of the current group (render() scopes groups).
            k = j
            while k < len(s) and s[k] in " \t\n":
                k += 1
            tag, style = FONT_SWITCHES[base]
            return '<%s%s>%s</%s>' % (tag, ' style="%s"' % style if style else "", self.render(s[k:]), tag), len(s)
        if base == "textcolor":
            arg, k = read_arg(s, j)
            text, k = read_arg(s, k)
            return '<span style="color:%s">%s</span>' % (html.escape(self.css_color(arg), quote=True),
                                                        self.render(text)), k
        if base in ("title", "author", "affiliation", "email", "date", "thanks", "keywords", "pacs"):
            _, k = read_optional(s, j)
            arg, k = read_arg(s, k)
            if base == "title" and not self.pre.title:
                self.pre.title = arg
            elif base == "date" and not self.pre.date:
                self.pre.date = arg
            elif base == "author":
                self.authors.append(arg)
            return "", k
        if base in ("newcommand", "renewcommand", "providecommand", "DeclareMathOperator", "DeclareRobustCommand"):
            # A definition in the document body: register it for later text and MathJax.
            k = j
            target, k = read_arg(s, k)
            name = target.strip().lstrip("\\")
            if base == "DeclareMathOperator":
                body, k = read_arg(s, k)
                self.pre.macros[name] = "\\operatorname{%s}" % body
                return "", k
            nargs, k = read_optional(s, k)
            _, k = read_optional(s, k)
            body, k = read_arg(s, k)
            self.pre.macros[name] = [body, int(nargs)] if nargs else body
            self.pre.text_macros[name] = (body, int(nargs) if nargs else 0)
            return "", k
        if base == "texorpdfstring":
            a, k = read_arg(s, j)
            _, k = read_arg(s, k)
            return self.render(a), k
        if base in ("path", "nolinkurl"):
            arg, k = read_arg(s, j)
            return "<code>%s</code>" % html.escape(arg), k
        if base in ("rowcolor", "cellcolor", "columncolor"):  # table colours: not shown
            _, k = read_optional(s, j)
            _, k = read_arg(s, k)
            return "", k
        if base in ("setcounter", "addtocounter"):
            _, k = read_arg(s, j)
            _, k = read_arg(s, k)
            return "", k
        if base in ("bibliography", "bibliographystyle"):
            _, k = read_arg(s, j)
            return "", k
        if base == "AGTADD":
            # Agent-written text (violet in the PDF); styled by the viewer, switchable.
            arg, k = read_arg(s, j)
            return '<span class="agent">%s</span>' % self.render(arg), k
        if base == "includegraphics":
            _, k = read_optional(s, j)
            arg, k = read_arg(s, k)
            path = arg.strip()
            if re.search(r"\.(png|jpe?g|gif|svg)$", path, re.I) or not os.path.splitext(path)[1]:
                return '<img class="figure-img" src="/asset?path=%s" alt="%s">' % (
                    html.escape(path, quote=True), html.escape(path, quote=True)), k
            return '<a class="figure-file" href="/asset?path=%s" target="_blank">[figure: %s]</a>' % (
                html.escape(path, quote=True), html.escape(path)), k
        if base in ("caption", "captionof"):
            kind = None
            if base == "captionof":
                kind, j = read_arg(s, j)
                kind = kind.strip().rstrip("*")
            _, k = read_optional(s, j)
            arg, k = read_arg(s, k)
            return self.caption(arg, star, s, k, kind), k
        if base in ("bibinfo", "bibfield"):
            _, k = read_arg(s, j)
            arg, k = read_arg(s, k)
            return self.render(arg), k
        if base in ("BibitemShut", "natexlab", "BibitemOpen", "citenamefont", "bibnamefont", "Eprint"):
            if base == "Eprint":
                url, k = read_arg(s, j)
                text, k = read_arg(s, k)
                return link_html(url, self.render(text)), k
            if base in ("citenamefont", "bibnamefont"):
                arg, k = read_arg(s, j)
                return self.render(arg), k
            if base in ("BibitemShut", "natexlab"):
                _, k = read_arg(s, j)
                return "", k
            return "", j
        if base == "label":
            arg, k = read_arg(s, j)
            label = arg.strip()
            if label not in self.own_labels and label not in self.labels_in_block:
                self.define_label(label, None)
            if label in self.anchored:
                return "", k
            return '<span class="lbl" id="lbl-%s"></span>' % html.escape(label, quote=True), k
        if base in ("cite", "citep", "citet"):
            opt, k = read_optional(s, j)
            arg, k = read_arg(s, k)
            parts = []
            for key in [x.strip() for x in arg.split(",") if x.strip()]:
                num = self.cites.get(key, key)
                parts.append('<a class="cite" href="#bib-%s" title="%s">%s</a>' % (
                    html.escape(key, quote=True), html.escape(key, quote=True), html.escape(num)))
            inner = ", ".join(parts)
            if opt:
                inner += ", " + self.render(opt)
            return "[%s]" % inner, k
        if base == "href":
            url, k = read_arg(s, j)
            text, k = read_arg(s, k)
            return link_html(url, self.render(text)), k
        if base == "url":
            url, k = read_arg(s, j)
            return link_html(url, html.escape(url)), k
        if base == "paragraph":
            arg, k = read_arg(s, j)
            return "<strong>%s</strong> " % self.render(arg), k
        if base == "footnote":
            arg, k = read_arg(s, j)
            return '<span class="footnote">(%s)</span>' % self.render(arg), k
        if base in IGNORED_COMMANDS:
            return "", j
        if base in ("hspace", "vspace", "setlength", "addtolength"):
            _, k = read_arg(s, j)
            if base in ("setlength", "addtolength"):
                _, k = read_arg(s, k)
            return " ", k
        if name in ACCENTS and not name.isalpha():
            return self.accent(name, s, j)
        if name in SIMPLE_SYMBOLS:
            return SIMPLE_SYMBOLS[name], j
        if base == "ensuremath":
            arg, k = read_arg(s, j)
            return "\\(" + html.escape(self.math_refs(arg), quote=False) + "\\)", k
        if base in self.pre.text_macros:
            body, nargs = self.pre.text_macros[base]
            if nargs == 0:
                return self.expand_text_macro(base, body), j
            args = []
            k = j
            for _ in range(nargs):
                a, k = read_arg(s, k)
                args.append(a)
            expanded = body
            for idx, a in enumerate(args, start=1):
                expanded = expanded.replace("#%d" % idx, a)
            return self.guarded_render(base, expanded), k
        if name in TEXT_SYMBOLS:
            if name.isalpha():  # TeX drops the spaces after a control word: "\AA ngstr" -> no gap
                j = skip_ws(s, j)
            return TEXT_SYMBOLS[name], j
        if name in ACCENTS:
            return self.accent(name, s, j)
        return '<span class="unknown" title="Unrendered LaTeX command">\\%s</span>' % html.escape(name), j

    # -- figures, tables and captions ----------------------------------------

    def float_number(self, kind: str, label: str | None) -> str:
        """Number of a new figure or table: the last build's, when its label is in the .aux file,
        else the next by counting."""
        if label and label in self.aux_labels:
            number = self.aux_labels[label]
            self.num.sync_float(kind, number)
            return number
        return self.num.bump_float(kind)

    def open_float(self, kind: str, env: str, s: str, k: int) -> str:
        """Start of a figure or table: number its (first) caption now, so that its label can
        be anchored at the top of the float, where a reference should land."""
        end = find_env_end(s, k, env)
        body = s[k:end]
        ctx = {"kind": kind, "first": None}
        self.float_stack.append(ctx)
        m = re.search(r"\\caption(\*?)\s*[\[{]", body)
        if not m or m.group(1):
            return '<div class="figure float-%s">' % kind
        nxt = re.search(r"\\caption\s*[\[{]", body[m.end():])
        scope = body[m.start(): m.end() + nxt.start()] if nxt else body[m.start():]
        lm = re.search(r"\\label\{([^}]*)\}", scope)
        label = lm.group(1).strip() if lm else None
        number = self.float_number(kind, label)
        ctx["first"] = number
        spans = ""
        if label:
            self.define_label(label, number, kind.capitalize())
            self.anchored.add(label)
            spans = '<span class="lbl" id="lbl-%s"></span>' % html.escape(label, quote=True)
        return '<div class="figure float-%s">%s' % (kind, spans)

    def caption(self, arg: str, star: bool, s: str, k: int, kind: str | None = None) -> str:
        """A caption, numbered "Figure N:" / "Table N:" inside a figure or table (or with
        \\captionof); \\caption* and captions elsewhere stay unnumbered."""
        ctx = self.float_stack[-1] if self.float_stack else None
        kind = kind or (ctx["kind"] if ctx else None)
        if star or kind not in ("figure", "table"):
            return '<div class="caption">%s</div>' % self.render(arg)
        if ctx and ctx["first"] is not None and ctx["kind"] == kind:
            number, ctx["first"] = ctx["first"], None
        else:
            rest = s[k:]
            stop = re.search(r"\\caption|\\end\{", rest)
            lm = re.search(r"\\label\{([^}]*)\}", arg + (rest[: stop.start()] if stop else rest))
            label = lm.group(1).strip() if lm else None
            number = self.float_number(kind, label)
            if label:
                self.define_label(label, number, kind.capitalize())
            if ctx:
                ctx["first"] = None
        name = "Figure" if kind == "figure" else "Table"
        return '<div class="caption"><span class="caption-label">%s %s:</span> %s</div>' % (
            name, html.escape(number), self.render(arg))

    def render_table(self, env: str, s: str, k: int):
        """tabular, tabular*, tabularx, tabulary, longtable -> an HTML table."""
        width = None
        if env in ("tabular*", "tabularx", "tabulary"):
            width, k = read_arg(s, k)
        _, k = read_optional(s, k)
        spec, k = read_arg(s, k)
        end = find_env_end(s, k, env)
        after = end + len("\\end{%s}" % env)
        body = s[k:end]
        cols = parse_colspec(spec)
        longtable = env == "longtable"
        if longtable:
            body = arrange_longtable(body)
            self.float_stack.append({"kind": "table", "first": None})
        try:
            rows_html = self.table_rows(body, cols)
        finally:
            if longtable:
                self.float_stack.pop()
        full = width is not None and re.search(r"\\(linewidth|textwidth|columnwidth|hsize)", width)
        style = ' style="width:100%"' if full else ""
        return ('<div class="table-wrap"><table class="latex-table"%s>%s</table></div>'
                % (style, rows_html)), after

    def table_rows(self, body: str, cols: list) -> str:
        out, captions = [], []
        pending: list = []  # rules waiting to go above the next row
        rows = []  # (rules above, cells)
        for raw in split_top(body, "row"):
            rules, rest = take_rules(raw)
            pending += rules
            rest = rest.strip()
            if not rest:
                continue
            if re.match(r"\\caption\b|\\label\{", rest):
                # longtable: a caption (and label) row
                captions.append(self.render(rest))
                continue
            rows.append((pending, split_top(rest, "cell")))
            pending = []
        ncols = max([len(cols)] + [sum(cell_span(c) for c in cells) for _, cells in rows])
        for r, (rules, cells) in enumerate(rows):
            below = pending if r == len(rows) - 1 else []
            tds, col = [], 0
            for cell in cells:
                span, content, cspec = cell_parts(cell)
                info = (parse_colspec(cspec)[:1] or [None])[0] if cspec is not None else (
                    cols[col] if col < len(cols) else {"align": "left", "wrap": False, "lbar": False, "rbar": False})
                cls = ["al-" + info["align"]]
                if info["wrap"]:
                    cls.append("wrap")
                if info["lbar"]:
                    cls.append("bl")
                if info["rbar"]:
                    cls.append("br")
                cls += rule_classes(rules, col, span, "t") + rule_classes(below, col, span, "b")
                tds.append('<td%s class="%s">%s</td>' % (
                    ' colspan="%d"' % span if span > 1 else "", " ".join(cls), self.render(content.strip())))
                col += span
            while col < ncols and cells:
                tds.append('<td class="%s"></td>' % " ".join(
                    rule_classes(rules, col, 1, "t") + rule_classes(below, col, 1, "b")))
                col += 1
            out.append("<tr>%s</tr>" % "".join(tds))
        cap = "".join(captions)
        return ('<caption>%s</caption>' % cap if cap else "") + "".join(out)

    def accent(self, name: str, s: str, j: int):
        """\\'e, \\"{o}, \\c c, ...: the letter with a combining accent, normalized."""
        if name.isalpha() and j < len(s) and s[j] not in " \t\n{\\":
            return '<span class="unknown" title="Unrendered LaTeX command">\\%s</span>' % html.escape(name), j
        arg, k = read_arg(s, j)
        if re.fullmatch(r"\\[A-Za-z]+", arg):
            k = skip_ws(s, k)  # spaces after a control word, e.g. na\"\i ve
        if arg.strip() in ("\\i", "\\j"):
            arg = arg.strip()[1]
        text = self.render(arg)
        if not text or text.startswith("<") or text.startswith("&"):
            return text, k
        return unicodedata.normalize("NFC", text[0] + ACCENTS[name] + text[1:]), k

    def guarded_render(self, name: str, body: str) -> str:
        """Render a macro's expansion in text mode, refusing runaway recursion."""
        depth = self._expanding.count(name)
        if depth >= 3 or len(self._expanding) > 30:
            return '<span class="unknown" title="Recursive macro">\\%s</span>' % html.escape(name)
        self._expanding.append(name)
        try:
            return self.render(body)
        finally:
            self._expanding.pop()

    def expand_text_macro(self, name: str, body: str) -> str:
        r"""A preamble macro without arguments used in running text, e.g. \cmpd -> CuO$_2$.

        LaTeX expands it in text mode, so galley does too. A body that only makes sense in
        mathematics (an unbalanced ^ or _, or commands the text renderer does not know, outside
        any $...$) is the rare case of a math macro written in text without $; it is still
        handed to MathJax, as before.
        """
        outside = re.sub(r"\$[^$]*\$|\\\(.*?\\\)|\\ensuremath\s*\{[^{}]*\}", "", body)
        if re.search(r"(?<!\\)[\^_]", outside):
            return "\\(" + html.escape("\\" + name, quote=False) + "\\)"
        rendered = self.guarded_render(name, body)
        if 'class="unknown"' in rendered and "$" not in body and "\\ensuremath" not in body:
            return "\\(" + html.escape("\\" + name, quote=False) + "\\)"
        return rendered


SAFE_LINK_RE = re.compile(r"(https?|mailto|ftp):", re.I)


def link_html(url: str, text_html: str) -> str:
    """A link from the document: only web, mail and ftp addresses become links (never javascript:
    or data:); anything else is shown as text, with the address on hover."""
    url = url.strip()
    if SAFE_LINK_RE.match(url):
        return '<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>' % (html.escape(url, quote=True), text_html)
    return '<span class="locallink" title="%s">%s</span>' % (html.escape(url, quote=True), text_html)


def find_env_end(s: str, k: int, env: str) -> int:
    depth = 1
    pat = re.compile(r"\\(begin|end)\{%s\}" % re.escape(env))
    for m in pat.finditer(s, k):
        depth += 1 if m.group(1) == "begin" else -1
        if depth == 0:
            return m.start()
    return len(s)


def split_rows(body: str) -> list[str]:
    rows, depth, cur, i = [], 0, [], 0
    while i < len(body):
        if body.startswith("\\begin{", i):
            depth += 1
        elif body.startswith("\\end{", i):
            depth -= 1
        if depth == 0 and body.startswith("\\\\", i):
            rows.append("".join(cur))
            cur = []
            i += 2
            continue
        cur.append(body[i])
        i += 1
    rows.append("".join(cur))
    return rows


# ---------------------------------------------------------------------------
# Tables

TABLE_ENVS = {"tabular", "tabular*", "tabularx", "tabulary", "longtable"}
FLOAT_ENVS = {"figure": "figure", "figure*": "figure", "table": "table", "table*": "table"}
RULE_RE = re.compile(
    r"\s*\\(toprule|midrule|bottomrule|hline|cline|cmidrule|addlinespace|specialrule|morecmidrules"
    r"|hhline|noalign)(?![A-Za-z])")


def parse_colspec(spec: str) -> list:
    """Column specification -> [{align, wrap, lbar, rbar}]. Understands l c r, p{} m{} b{},
    X (tabularx), L C R J (tabulary), S (siunitx), |, @{} !{} >{} <{} and *{n}{...}."""
    # *{n}{cols}
    for _ in range(20):
        m = re.search(r"\*\s*\{(\d+)\}\s*\{", spec)
        if not m:
            break
        inner, end = read_group(spec, m.end() - 1)
        spec = spec[: m.start()] + inner * int(m.group(1)) + spec[end:]
    cols, i, bar = [], 0, False
    while i < len(spec):
        c = spec[i]
        if c in " \t\n":
            i += 1
            continue
        if c == "|":
            if cols:
                cols[-1]["rbar"] = True
            else:
                bar = True
            i += 1
            continue
        if c in "@!<>":
            _, i = read_arg(spec, i + 1)
            continue
        if c == "{":  # stray group
            _, i = read_group(spec, i)
            continue
        if c in "pmb":
            _, i = read_arg(spec, i + 1)
            col = {"align": "left", "wrap": True}
        elif c in "lcr":
            col = {"align": {"l": "left", "c": "center", "r": "right"}[c], "wrap": False}
            i += 1
        elif c in "LCRJ":
            col = {"align": {"L": "left", "C": "center", "R": "right", "J": "justify"}[c], "wrap": True}
            i += 1
        elif c == "X":
            col = {"align": "left", "wrap": True}
            i += 1
        elif c == "S":
            col = {"align": "center", "wrap": False}
            i += 1
            if i < len(spec) and spec[i] == "[":
                _, i = read_group(spec, i, "[", "]")
        elif c.isalpha():
            col = {"align": "left", "wrap": False}
            i += 1
        else:
            i += 1
            continue
        col.update(lbar=bar, rbar=False)
        bar = False
        cols.append(col)
    return cols


def split_top(body: str, kind: str) -> list:
    """Split a table body into rows (at \\\\ or \\tabularnewline) or a row into cells (at &),
    never inside braces, nested environments or mathematics."""
    parts, cur, depth, env, math, i, n = [], [], 0, 0, False, 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            if body.startswith("\\begin{", i):
                env += 1
            elif body.startswith("\\end{", i):
                env -= 1
            if kind == "row" and depth == 0 and env == 0 and not math:
                m = re.match(r"\\\\\*?|\\tabularnewline(?![A-Za-z])", body[i:])
                if m:
                    parts.append("".join(cur))
                    cur = []
                    i += len(m.group(0))
                    _, i = read_optional(body, i)
                    continue
            if body.startswith("\\(", i) or body.startswith("\\)", i):
                math = body[i + 1] == "("
            cur.append(body[i: i + 2])
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "$":
            math = not math
        elif c == "&" and kind == "cell" and depth == 0 and env == 0 and not math:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


def take_rules(row: str):
    """Strip the rules at the start of a row: ([(name, first col, last col)], rest).
    Columns are 0-based; None means the full width."""
    rules = []
    while True:
        m = RULE_RE.match(row)
        if not m:
            return rules, row
        name, k = m.group(1), m.end()
        a = b = None
        if name == "cmidrule":
            k = skip_ws(row, k)
            if k < len(row) and row[k] == "(":
                k = row.index(")", k) + 1 if ")" in row[k:] else k
            _, k = read_optional(row, k)
        if name in ("cline", "cmidrule", "hhline", "noalign"):
            arg, k = read_arg(row, k)
            r = re.match(r"\s*(\d+)\s*-\s*(\d+)", arg)
            if r:
                a, b = int(r.group(1)) - 1, int(r.group(2)) - 1
        elif name in ("toprule", "midrule", "bottomrule", "addlinespace"):
            _, k = read_optional(row, k)
        elif name == "specialrule":
            for _ in range(3):
                _, k = read_arg(row, k)
        if name not in ("addlinespace", "morecmidrules", "hhline", "noalign"):
            weight = "heavy" if name in ("toprule", "bottomrule", "specialrule") else "light"
            rules.append((weight, a, b))
        row = row[k:]


def rule_classes(rules: list, col: int, span: int, side: str) -> list:
    out = []
    for weight, a, b in rules:
        if a is None or (col <= b and col + span - 1 >= a):
            cls = "r%s-%s" % (side, weight)
            if cls not in out:
                out.append(cls)
    if ("r%s-heavy" % side) in out and ("r%s-light" % side) in out:
        out.remove("r%s-light" % side)
    return out


MULTICOL_RE = re.compile(r"\s*\\multicolumn(?![A-Za-z])")


def cell_parts(cell: str):
    """(columns spanned, content, column spec or None) for a table cell."""
    m = MULTICOL_RE.match(cell)
    if not m:
        mr = re.match(r"\s*\\multirow(?![A-Za-z])", cell)
        if mr:  # \multirow{n}[..]{width}[..]{text}: show the text in its first row
            k = mr.end()
            _, k = read_arg(cell, k)
            _, k = read_optional(cell, k)
            _, k = read_arg(cell, k)
            _, k = read_optional(cell, k)
            text, k = read_arg(cell, k)
            return 1, text + cell[k:], None
        return 1, cell, None
    n, k = read_arg(cell, m.end())
    spec, k = read_arg(cell, k)
    text, k = read_arg(cell, k)
    try:
        span = max(1, int(n.strip()))
    except ValueError:
        span = 1
    return span, text + cell[k:], spec


def cell_span(cell: str) -> int:
    return cell_parts(cell)[0]


def arrange_longtable(body: str) -> str:
    r"""longtable: keep the first header (\endfirsthead, else \endhead), then the body, then the
    last footer (\endlastfoot, else \endfoot); the repeated page headers and footers are dropped."""
    pieces = re.split(r"\\(endfirsthead|endhead|endfoot|endlastfoot)(?![A-Za-z])", body)
    if len(pieces) == 1:
        return body
    parts = {}
    for idx in range(1, len(pieces), 2):
        parts[pieces[idx]] = pieces[idx - 1]
    main = pieces[-1]
    head = parts.get("endfirsthead", parts.get("endhead", ""))
    foot = parts.get("endlastfoot", parts.get("endfoot", ""))
    return head + main + foot


# ---------------------------------------------------------------------------
# Block segmentation


SECTION_RE = re.compile(r"^\s*\\(section|subsection|subsubsection)(\*?)\s*(\[|\{)")
BEGIN_RE = re.compile(r"^\s*\\begin\{([^}]+)\}")


def src_hash(lines: list[Line]) -> str:
    text = "\n".join(l.text.strip() for l in lines if l.text.strip())
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


class Builder:
    def __init__(self, renderer: Renderer):
        self.r = renderer
        self.counter = 0
        self.root = {"kind": "root", "children": []}
        self.stack = [(0, self.root)]

    def new_id(self) -> str:
        self.counter += 1
        return "b%d" % self.counter

    def anchor(self, lines: list[Line]) -> dict:
        files = {l.file for l in lines}
        first, last = lines[0], lines[-1]
        a = {"file": first.file, "start": first.lineno, "end": last.lineno,
             "hash": src_hash(lines)}
        if first.color:
            a["color"] = self.r.css_color(first.color)
        if len(files) > 1:
            a["note"] = "spans several files"
        return a

    def add(self, block: dict):
        self.stack[-1][1]["children"].append(block)

    def finish_html(self, block: dict, html_text: str):
        block["html"] = html_text  # references are resolved after the full pass

    # --------------------------------------------------------------------

    def build(self, body: list[Line]):
        i = 0
        n = len(body)
        while i < n:
            line = body[i]
            t = line.text.strip()
            if not t:
                i += 1
                continue
            if t == "\\appendix":
                self.r.num.appendix = True
                i += 1
                continue
            if t in ("\\maketitle", "\\tableofcontents") or t.startswith("\\bibliographystyle"):
                i += 1
                continue
            if t.startswith("\\bibliography{"):
                self.bibtex(line)
                i += 1
                continue
            m = SECTION_RE.match(line.text)
            if m:
                i = self.section(body, i, m)
                continue
            m = BEGIN_RE.match(line.text)
            if m:
                env = m.group(1)
                if env in self.r.pre.theorems or env in ("proof", "abstract", "thebibliography"):
                    i = self.environment(body, i, env)
                    continue
            i = self.paragraph(body, i, self.stack[-1][1]["children"])

    def section(self, body, i, m):
        level = SECTION_LEVELS[m.group(1)]
        starred = m.group(2) == "*"
        line = body[i]
        s = line.text
        j = m.end() - 1
        short, j = (read_optional(s, j) if s[j] == "[" else (None, j))
        title, j = read_arg(s, j)
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        bid = self.new_id()
        self.r.current_block = bid
        self.r.labels_in_block = []
        number = None if starred else self.r.num.bump_section(level)
        consumed = [line]
        rest = s[j:]
        k = i + 1
        # A label on the following line belongs to the heading.
        labels = re.findall(r"\\label\{([^}]*)\}", rest)
        while k < len(body) and re.match(r"^\s*\\label\{[^}]*\}\s*$", body[k].text):
            labels += re.findall(r"\\label\{([^}]*)\}", body[k].text)
            consumed.append(body[k])
            k += 1
        for lab in labels:
            # Sections are numbered from the last build's table of contents; references to them
            # use the build's own label text (e.g. "II A" in REVTeX), which can differ in form.
            self.r.define_label(lab, self.r.aux_labels.get(lab, number), "Section")
        block = {
            "id": bid, "kind": "section", "level": level, "number": number,
            "title_html": "", "labels": labels, "anchor": self.anchor(consumed),
            "children": [],
        }
        block["title_html"] = self.r.render(title)
        block["title_text"] = plain_title(title, self.r.pre.text_macros)
        self.stack[-1][1]["children"].append(block)
        self.stack.append((level, block))
        return k

    def environment(self, body, i, env):
        depth = 0
        k = i
        pat = re.compile(r"\\(begin|end)\{%s\}" % re.escape(env))
        while k < len(body):
            for mm in pat.finditer(body[k].text):
                depth += 1 if mm.group(1) == "begin" else -1
            if depth <= 0:
                break
            k += 1
        lines = body[i : k + 1]
        bid = self.new_id()
        self.r.current_block = bid
        self.r.labels_in_block = []
        first = lines[0].text
        m = re.search(r"\\begin\{%s\}" % re.escape(env), first)
        after = first[m.end():]
        opt, pos = read_optional(after, 0)
        if env == "thebibliography":
            _, pos = read_arg(after, pos)
        head_rest = after[pos:]
        last = lines[-1].text
        endm = list(re.finditer(r"\\end\{%s\}" % re.escape(env), last))
        inner_lines = [Line(head_rest, lines[0].file, lines[0].lineno)] + lines[1:]
        drop_head = len(lines) > 1 and not head_rest.strip()
        if len(lines) == 1:
            # begin and end on one line
            e = head_rest.rfind("\\end{%s}" % env)
            inner_lines = [Line(head_rest[:e], lines[0].file, lines[0].lineno)]
        elif endm:
            inner_lines[-1] = Line(last[: endm[-1].start()], lines[-1].file, lines[-1].lineno)
        all_inner = list(inner_lines)
        if drop_head:
            inner_lines = inner_lines[1:]
        # A label alone on the line after \begin belongs to the environment, not its first paragraph.
        while inner_lines and re.match(r"^\s*\\label\{[^}]*\}\s*$", inner_lines[0].text):
            inner_lines = inner_lines[1:]
        block = {"id": bid, "kind": "env", "env": env, "anchor": self.anchor(lines),
                 "children": [], "labels": []}
        if env in self.r.pre.theorems:
            th = self.r.pre.theorems[env]
            number = self.r.num.bump_theorem(env)
            env_labels = [lab for l in all_inner for lab in re.findall(r"\\label\{([^}]*)\}", l.text)
                          if not self.in_math(all_inner, lab) and not self.in_list(all_inner, lab)]
            for lab in env_labels:
                if lab in self.r.aux_labels and number is not None:
                    number = self.r.aux_labels[lab]
                    self.r.num.sync_theorem(env, number)
                    break
            block.update(display=th.display, number=number, style=th.style,
                         title_html=self.r.render(opt) if opt else "")
            for lab in env_labels:
                self.r.define_label(lab, number, th.display)
                block["labels"].append(lab)
        elif env == "proof":
            block.update(display="Proof", title_html=self.r.render(opt) if opt else "")
        elif env == "abstract":
            block.update(display="Abstract", style="definition")
        elif env == "thebibliography":
            block.update(display="References")
        self.stack[-1][1]["children"].append(block)
        if env == "thebibliography":
            self.bibliography(inner_lines, block)
        else:
            p = 0
            while p < len(inner_lines):
                if not inner_lines[p].text.strip():
                    p += 1
                    continue
                p = self.paragraph(inner_lines, p, block["children"])
        return k + 1

    def in_list(self, lines, label) -> bool:
        text = "\n".join(l.text for l in lines)
        before = text[: text.find("\\label{%s}" % label)]
        return any(before.count("\\begin{%s}" % e) > before.count("\\end{%s}" % e) for e in LIST_ENVS)

    def in_math(self, lines, label) -> bool:
        text = "\n".join(l.text for l in lines)
        idx = text.find("\\label{%s}" % label)
        before = text[:idx]
        for env in MATH_ENVS:
            if before.count("\\begin{%s}" % env) > before.count("\\end{%s}" % env):
                return True
        if before.count("\\[") > before.count("\\]"):
            return True
        return False

    def bibtex(self, line):
        """\\bibliography{...}: show the entries from the last build's .bbl file."""
        bid = self.new_id()
        block = {"id": bid, "kind": "env", "env": "thebibliography", "display": "References",
                 "anchor": self.anchor([line]), "children": [], "labels": []}
        self.stack[-1][1]["children"].append(block)
        bbl = getattr(self, "bbl_lines", [])
        text = [l for l in bbl]
        start = next((i for i, l in enumerate(text) if "\\begin{thebibliography}" in l.text), None)
        if start is None:
            self.finish_html(block, "")
            block["children"].append({"id": self.new_id(), "kind": "para", "anchor": self.anchor([line]),
                                      "labels": [], "html": '<span class="muted">Bibliography: build the PDF to see the entries.</span>'})
            return
        stop = next((i for i, l in enumerate(text) if "\\end{thebibliography}" in l.text), len(text))
        self.bibliography(text[start + 1 : stop], block, generated=True)

    def bibliography(self, lines, block, generated=False):
        text_lines = [l for l in lines]
        items = []
        cur = None
        for l in text_lines:
            m = re.search(r"\\bibitem(\[[^\]]*\])?\{([^}]*)\}", l.text)
            if m:
                if cur:
                    items.append(cur)
                cur = {"key": m.group(2), "lines": [Line(l.text[m.end():], l.file, l.lineno)]}
            elif cur:
                cur["lines"].append(l)
        if cur:
            items.append(cur)
        for it in items:
            bid = self.new_id()
            self.r.current_block = bid
            num = self.r.cites.get(it["key"], it["key"])
            h = self.r.render("\n".join(l.text for l in it["lines"]))
            anchor = self.anchor(it["lines"])
            if generated:
                anchor = dict(block["anchor"], generated=True)  # entries come from the .bbl, not the source
            child = {"id": bid, "kind": "bibitem", "key": it["key"], "number": num,
                     "anchor": anchor, "html": ""}
            self.finish_html(child, '<span class="lbl" id="bib-%s"></span>%s' % (html.escape(it["key"], quote=True), h))
            block["children"].append(child)

    def paragraph(self, lines, i, target):
        """Collect a paragraph starting at lines[i]; append it; return next index."""
        depth = 0
        k = i
        collected = []
        while k < len(lines):
            line = lines[k]
            t = line.text.strip()
            if depth == 0 and k > i:
                if not t:
                    break
                if SECTION_RE.match(line.text) or t == "\\appendix" or t.startswith("\\bibliography"):
                    break
                mm = BEGIN_RE.match(line.text)
                if mm and (mm.group(1) in self.r.pre.theorems or mm.group(1) in ("proof", "abstract", "thebibliography")):
                    break
            collected.append(line)
            depth += len(re.findall(r"\\begin\{", line.text)) - len(re.findall(r"\\end\{", line.text))
            depth += len(re.findall(r"(?<!\\)\\\[", line.text)) - len(re.findall(r"(?<!\\)\\\]", line.text))
            k += 1
        bid = self.new_id()
        prev_block = self.r.current_block
        prev_labels = self.r.labels_in_block
        self.r.current_block = bid
        self.r.labels_in_block = []
        self.r.float_stack = []  # a float never spans paragraphs; don't let a malformed one leak
        text = "\n".join(l.text for l in collected)
        rendered = self.r.render(text)
        block = {"id": bid, "kind": "para", "anchor": self.anchor(collected),
                 "labels": list(self.r.labels_in_block)}
        self.finish_html(block, rendered)
        target.append(block)
        self.r.current_block = prev_block
        self.r.labels_in_block = prev_labels
        return k


def plain_title(t: str, text_macros: dict | None = None) -> str:
    # \texorpdfstring{tex}{plain}: keep the plain version.
    while "\\texorpdfstring" in t:
        i = t.index("\\texorpdfstring")
        try:
            a, k = read_arg(t, i + len("\\texorpdfstring"))
            b, k = read_arg(t, k)
        except Exception:
            break
        t = t[:i] + b + t[k:]
    for _ in range(3):  # macros without arguments, e.g. \cmpd -> CuO$_2$
        for name, (body, nargs) in (text_macros or {}).items():
            if nargs == 0 and "\\" + name in t:
                t = re.sub(r"\\%s(?![A-Za-z])\s*" % re.escape(name), lambda m, b=body: b, t)
    t = re.sub(r"\\\\(\[[^\]]*\])?", " ", t)  # line breaks in titles
    t = t.replace("---", "\u2014").replace("--", "\u2013").replace("~", " ")
    t = re.sub(r"\\(textcolor|color)\{[^}]*\}", "", t)
    t = re.sub(r"\$([^$]*)\$", r"\1", t)
    t = re.sub(r"_\{?(\d+)\}?", lambda m: m.group(1).translate(SUBSCRIPT_DIGITS), t)  # CuO_2 -> CuO₂
    t = re.sub(r"\\[A-Za-z]+\*?", "", t)
    return re.sub(r"[{}]", "", t).strip()


SUBSCRIPT_DIGITS = str.maketrans("0123456789", "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089")


# ---------------------------------------------------------------------------
# Entry point


def find_bbl_items(path: str) -> list:
    """Lines of a BibTeX-generated .bbl file, as Line objects."""
    out = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for n, raw in enumerate(fh, start=1):
                out.append(Line(strip_comment(raw.rstrip("\n")), os.path.basename(path), n))
    return out


def section_spans(blocks):
    """Give each section the per-file line ranges it covers (heading through its last block)."""
    def collect(b, acc):
        a = b.get("anchor")
        if a and not a.get("generated"):
            lo, hi = acc.get(a["file"], (a["start"], a["end"]))
            acc[a["file"]] = (min(lo, a["start"]), max(hi, a["end"]))
        for c in b.get("children", []):
            collect(c, acc)
        return acc
    for b in blocks:
        if b.get("kind") == "section":
            acc = collect(b, {})
            b["span"] = [{"file": f, "start": lo, "end": hi} for f, (lo, hi) in acc.items()]
            section_spans(b.get("children", []))


def font_size_pt(preamble: str) -> float:
    """The document's base font size, from the class options (LaTeX's default is 10pt)."""
    m = re.search(r"\\documentclass\s*\[([^\]]*)\]", preamble)
    n = re.search(r"\b(\d+(?:\.\d+)?)pt\b", m.group(1)) if m else None
    return float(n.group(1)) if n else 10.0


def raw_preamble(main_path: str) -> str:
    """The main file up to \\begin{document}, as written (\\input left for TeX to resolve)."""
    with open(main_path, encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")
    out = []
    for l in lines:
        k = strip_comment(l).find("\\begin{document}")
        if k >= 0:
            out.append(l[:k])
            break
        out.append(l)
    return "\n".join(out)


def parse_document(doc_dir: str, main_file: str, aux_path: str | None = None,
                   bbl_path: str | None = None) -> dict:
    """Parse the document whose main file is doc_dir/main_file.

    aux_path and bbl_path are absolute paths to the last build's .aux and .bbl, if any.
    """
    lines = read_expanded(doc_dir, main_file)
    begin = next((i for i, l in enumerate(lines) if "\\begin{document}" in l.text), None)
    if begin is None:
        raise ValueError("%s has no \\begin{document}" % main_file)
    end = max(i for i, l in enumerate(lines) if "\\end{document}" in l.text)
    pre = parse_preamble(lines[:begin])
    toc: dict = {}
    labels, cites = read_aux(aux_path or "", toc=toc)
    # \externaldocument[prefix]{name} (xr package): labels of another document.
    for m in re.finditer(r"\\externaldocument\[([^\]]*)\]\{([^}]*)\}", "\n".join(l.text for l in lines[:begin])):
        l2, _ = read_aux(os.path.join(doc_dir, m.group(2) + ".aux"), m.group(1))
        labels.update(l2)
    r = Renderer(pre, labels, cites, toc)
    preamble_tex = raw_preamble(os.path.join(doc_dir, main_file))
    # The hash covers the expanded preamble, so editing an \input macro file recompiles diagrams.
    r.diagram_key = preamble_tex + "\n".join(l.text for l in lines[:begin])
    b = Builder(r)
    b.bbl_lines = find_bbl_items(bbl_path)
    body = list(lines[begin + 1 : end])
    b.build(body)
    def walk(blocks):
        for blk in blocks:
            for key in ("html", "title_html"):
                if blk.get(key):
                    blk[key] = r.resolve_refs(blk[key])
            walk(blk.get("children", []))
    walk(b.root["children"])
    section_spans(b.root["children"])
    unresolved = sorted({lab for _, lab, kind in r.pending_refs if lab and r.ref_number(lab) == "??"})
    if unresolved:
        r.warnings.append("unresolved references (rebuild the PDF if they are new): " + ", ".join(unresolved))
    macros = {k: v for k, v in pre.macros.items()
              if not (isinstance(v, list) and re.search(r"\\(ref|eqref|cref)", v[0]))
              and not (isinstance(v, str) and re.search(r"\\(ref|eqref|cref)", v))}
    # Agent-written text inside mathematics: a class the viewer can color or not.
    macros["AGTADD"] = ["\\class{agent}{#1}", 1]
    macros.setdefault("ensuremath", ["#1", 1])  # macros written with \ensuremath, used in math
    title = plain_title(pre.title, pre.text_macros) or os.path.splitext(os.path.basename(main_file))[0]
    return {
        "title": title,
        "title_html": r.render(pre.title) if pre.title else html.escape(title),
        "date": plain_title(pre.date),
        "date_html": r.render(pre.date),
        "main_file": main_file,
        "files": sorted({l.file for l in lines}),
        "macros": macros,
        "blocks": b.root["children"],
        "label_block": r.label_block,
        "labels": r.own_labels,
        "warnings": r.warnings,
        "diagrams": r.diagrams,
        "font_pt": font_size_pt(preamble_tex),
        "preamble_tex": preamble_tex,
    }


# Backward-compatible name used by the first pilot.
def parse_note(note_dir: str, main_file: str, aux_file: str | None = None, extra_aux=None) -> dict:
    return parse_document(note_dir, main_file, os.path.join(note_dir, aux_file) if aux_file else None)


if __name__ == "__main__":  # quick manual check
    import json
    import sys
    d = parse_document(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None,
                       sys.argv[4] if len(sys.argv) > 4 else None)
    print(json.dumps({"warnings": d["warnings"], "nblocks": len(d["blocks"])}, indent=1))
