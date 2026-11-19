// galley client. Part of galley by Michael Hermele.
// Copyright (C) 2026 The Regents of the University of Colorado.
// SPDX-License-Identifier: AGPL-3.0-or-later (with the additional term in NOTICE); see LICENSE.
"use strict";

const S = {
  doc: null,
  blocks: new Map(),       // id -> {block, section, env}
  notesByBlock: new Map(), // id -> [note]
  docNotes: [],            // notes on the whole document
  annotations: [],
  current: null,           // block id open in the panel, or "document"
  currentAnchor: null,
  selection: "",
  threads: new Map(),      // anchor key -> [message]
  proofsHidden: false,
  filtered: false,
  showArchived: false,
  showViolet: true,
  collapsed: new Set(),
  mathjaxReady: null,
  fingerprint: null,
  editing: null,           // {original, dirty} while editing source in the panel
  sourceText: "",
};

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// The key of this galley run (see make_handler in galley_server.py): POSTs must carry it.
const GALLEY_KEY = document.querySelector('meta[name="galley-key"]')?.content || "";

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Galley-Key": GALLEY_KEY }, body: JSON.stringify(body) };
  try {
    const r = await fetch(path, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok && !data.error) data.error = r.status + " " + r.statusText;
    return data;
  } catch (e) {
    return { error: "galley is not responding (is the server still running?)" };
  }
}

function toast(msg, ms = 2800) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

// Messages that stay until dismissed (wrap-up results, warnings about the document's settings).
function notice(msg, { kind = "info", action = "", onAction = null, key = "" } = {}) {
  if (key) { S.noticeKeys = S.noticeKeys || new Set(); if (S.noticeKeys.has(key)) return; S.noticeKeys.add(key); }
  const box = document.createElement("div");
  box.className = "notice " + kind;
  box.setAttribute("role", kind === "error" ? "alert" : "status");
  box.innerHTML = `<span class="notice-text"></span>${action ? '<button class="notice-action"></button>' : ""}<button class="notice-close" title="Dismiss">&times;</button>`;
  box.querySelector(".notice-text").textContent = msg;
  if (action) {
    const b = box.querySelector(".notice-action");
    b.textContent = action;
    b.onclick = () => { box.remove(); onAction && onAction(); };
  }
  box.querySelector(".notice-close").onclick = () => box.remove();
  $("#notices").appendChild(box);
  return box;
}

function stored(key, fallback) {
  try { const v = localStorage.getItem("galley-" + key); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; }
}
function store(key, value) {
  try { localStorage.setItem("galley-" + key, JSON.stringify(value)); } catch (e) {}
}

// ---------------------------------------------------------------- MathJax

// MathJax's HTML commands could turn mathematics in a document or an agent's answer into a
// javascript: link or a style that loads an outside URL. Keep their contents and drop the effect.
// (\class stays: galley uses it for violet agent text; the page's security policy is a second line.)
const MATH_NEUTRALIZED = { href: ["#2", 2], style: ["#2", 2], cssId: ["#2", 2] };
function safeMacros(macros) { return Object.assign({}, macros || {}, MATH_NEUTRALIZED); }

function initMathJax(macros) {
  const key = JSON.stringify(macros || {});
  if (S.mathjaxReady) {
    // The document's macros changed while the page was open (e.g. a new \newcommand in the
    // macro file): give MathJax the new table and rebuild its TeX input.
    if (key !== S.macroKey) {
      S.macroKey = key;
      S.mathjaxReady = S.mathjaxReady.then(() => {
        MathJax.config.tex.macros = safeMacros(macros);
        MathJax.startup.getComponents();
      });
    }
    return S.mathjaxReady;
  }
  S.macroKey = key;
  window.MathJax = {
    tex: {
      inlineMath: [["\\(", "\\)"]],
      displayMath: [["\\[", "\\]"]],
      processEscapes: false,
      processEnvironments: true,
      processRefs: false,
      tags: "ams",
      macros: safeMacros(macros),
    },
    options: { skipHtmlTags: ["script", "noscript", "style", "textarea", "pre", "code"], ignoreHtmlClass: "no-math" },
    startup: { typeset: false },
    chtml: { fontURL: "/mathjax/output/chtml/fonts/woff-v2" },
  };
  S.mathjaxReady = new Promise((resolve) => {
    window.MathJax.startup.ready = () => { window.MathJax.startup.defaultReady(); resolve(); };
    const s = document.createElement("script");
    s.src = "/mathjax/tex-chtml-full.js";
    s.async = true;
    document.head.appendChild(s);
  });
  return S.mathjaxReady;
}

async function typeset(el) {
  await S.mathjaxReady;
  try { MathJax.typesetClear([el]); await MathJax.typesetPromise([el]); } catch (e) { console.warn("MathJax", e); }
}

// ---------------------------------------------------------------- anchors

const DOC_ANCHOR = () => ({ kind: "document", file: S.doc?.main_file, hash: "document" });

function anchorOf(id) {
  if (id === "document") return DOC_ANCHOR();
  const info = S.blocks.get(id);
  if (!info) return null;
  const b = info.block;
  const a = { ...b.anchor, kind: b.kind, labels: b.labels || [] };
  delete a.color;
  if (b.kind === "env") { a.display = b.display; a.number = b.number || ""; }
  if (b.kind === "section") { a.number = b.number || ""; a.title = b.title_text; a.span = b.span || []; }
  if (info.env && b.kind !== "env") a.within = `${info.env.display} ${info.env.number || ""}`.trim();
  if (info.section && b.kind !== "section") a.section = info.section.number || info.section.title_text;
  return a;
}

// Live threads are keyed by source hash, so they follow a passage when lines above it change;
// a passage whose source occurs more than once also needs its line to tell the copies apart.
function anchorKey(a) {
  if (!a) return "";
  if (a.kind === "document") return "document";
  const k = `${a.file}#${a.hash}`;
  return (S.hashCount?.get(k) || 0) > 1 ? `${k}@${a.start}` : k;
}

function index(blocks, section, env) {
  for (const b of blocks) {
    S.blocks.set(b.id, { block: b, section, env });
    if (b.children) index(b.children, b.kind === "section" ? b : section, b.kind === "env" ? b : env);
  }
}

// ---------------------------------------------------------------- rendering

function lblSpans(b) {
  return (b.labels || []).map((l) => `<span class="lbl" id="lbl-${esc(l)}"></span>`).join("");
}

function colorStyle(b) {
  return b.anchor && b.anchor.color ? ` style="color:${esc(b.anchor.color)}"` : "";
}

function renderBlocks(blocks) { return blocks.map(renderBlock).join(""); }

function renderBlock(b) {
  if (b.kind === "section") {
    const h = "h" + Math.min(4, b.level + 1);
    const key = b.number || b.title_text;
    const collapsed = S.collapsed.has(key) ? " collapsed" : "";
    return `<section class="sec${collapsed}" id="${b.id}" data-key="${esc(key)}">${lblSpans(b)}
      <${h} class="sec-head" data-sec="${b.id}"${colorStyle(b)}><button class="gutter" data-ask="${b.id}" title="Discuss this whole section">+</button><span class="caret">&#9662;</span>${b.number ? `<span class="num">${esc(b.number)}</span>` : ""}<span>${b.title_html}</span></${h}>
      <div class="sec-body">${renderBlocks(b.children || [])}</div></section>`;
  }
  if (b.kind === "env") {
    const isProof = b.env === "proof";
    const head = isProof
      ? `<span class="caret">&#9662;</span>${b.title_html ? `Proof of ${b.title_html}` : "Proof"}.`
      : `${esc(b.display)}${b.number ? " " + esc(b.number) : ""}${b.title_html ? ` <span class="title">(${b.title_html})</span>` : ""}.`;
    const collapsed = isProof && S.proofsHidden ? " collapsed" : "";
    return `<div class="env kind-${isProof ? "proof" : esc(b.env)} style-${esc(b.style || "plain")}${collapsed}" id="${b.id}" data-blk="${b.id}"${colorStyle(b)}>${lblSpans(b)}
      <div class="env-head blk" data-blk="${b.id}"><button class="gutter" data-ask="${b.id}" title="Ask about this whole ${isProof ? "proof" : esc(b.display || b.env).toLowerCase()}">+</button>${head}</div>
      <div class="env-body">${renderBlocks(b.children || [])}</div></div>`;
  }
  if (b.kind === "bibitem") {
    return `<div class="blk para bibitem" id="${b.id}" data-blk="${b.id}"><button class="gutter" data-ask="${b.id}">+</button><div class="content"><span class="bibnum">[${esc(b.number)}]</span>${b.html}</div></div>`;
  }
  return `<div class="blk para" id="${b.id}" data-blk="${b.id}"${colorStyle(b)}><button class="gutter" data-ask="${b.id}" title="Ask about this paragraph">+</button><div class="content">${b.html}</div></div>`;
}

function renderToc(blocks) {
  const out = [];
  const walk = (bs) => {
    for (const b of bs) {
      if (b.kind !== "section") continue;
      out.push(`<a href="#${b.id}" class="l${b.level}"><span class="num">${esc(b.number || "")}</span>${b.title_html}</a>`);
      walk(b.children || []);
    }
  };
  walk(blocks);
  $("#toc").innerHTML = out.join("");
}

function visibleNotes(list) { return S.showArchived ? list : list.filter((x) => !x.archived); }

function matchAnnotations() {
  S.notesByBlock = new Map();
  S.docNotes = [];
  const byHash = new Map();
  const byLabel = new Map();
  const leaves = [];
  for (const [id, info] of S.blocks) {
    const b = info.block;
    if (!b.anchor) continue;
    const key = `${b.anchor.file}#${b.anchor.hash}`;
    if (!byHash.has(key)) byHash.set(key, []);
    byHash.get(key).push(id);
    for (const l of b.labels || []) byLabel.set(l, id);
    leaves.push([id, b]);
  }
  for (const a of S.annotations) {
    const an = a.anchor || {};
    if (an.kind === "document") { S.docNotes.push(a); continue; }
    let id = nearestBlock(byHash.get(`${an.file}#${an.hash}`), an);
    let stale = false;
    if (!id) {
      stale = true;
      for (const l of an.labels || []) if (byLabel.has(l)) { id = byLabel.get(l); break; }
    }
    if (!id) {
      let best = null;
      for (const [bid, b] of leaves) {
        if (b.anchor.file !== an.file) continue;
        if ((b.kind === "section") !== (an.kind === "section")) continue;
        if (b.anchor.start <= an.end && b.anchor.end >= an.start) {
          // Most lines in common with the note's passage (e.g. the paragraph it was merged into),
          // then the shortest block.
          const overlap = Math.min(b.anchor.end, an.end) - Math.max(b.anchor.start, an.start);
          const span = b.anchor.end - b.anchor.start;
          if (!best || overlap > best[2] || (overlap === best[2] && span < best[1])) best = [bid, span, overlap];
        }
      }
      if (best) id = best[0];
    }
    if (!id) continue;
    a._stale = stale;
    if (!S.notesByBlock.has(id)) S.notesByBlock.set(id, []);
    S.notesByBlock.get(id).push(a);
  }
  document.querySelectorAll("#doc .badge").forEach((e) => e.remove());
  document.querySelectorAll(".has-notes").forEach((e) => e.classList.remove("has-notes"));
  for (const [id, all] of S.notesByBlock) {
    const el = document.getElementById(id);
    if (!el) continue;
    const list = visibleNotes(all);
    if (!list.length) continue;
    const host = el.classList.contains("env") ? el.querySelector(".env-head") : el.tagName === "SECTION" ? el.querySelector(".sec-head") : el;
    const allStale = list.every((x) => x._stale);
    const allArchived = list.every((x) => x.archived);
    const badge = document.createElement("span");
    badge.className = "badge" + (allArchived ? " archived" : allStale ? " stale" : "");
    badge.textContent = list.length;
    badge.title = `${list.length} saved item(s)${allArchived ? ", archived" : allStale ? " — the passage has changed since" : ""}`;
    badge.dataset.ask = id;
    host.appendChild(badge);
    el.classList.add("has-notes");
  }
  const n = visibleNotes(S.docNotes).length;
  const b = $("#doc-badge");
  b.textContent = n || "";
  b.hidden = !n;
}

async function loadDoc(keep) {
  const scrollY = window.scrollY;
  const [doc, ann] = await Promise.all([api("/api/doc"), api("/api/annotations")]);
  if (doc.error) { $("#doc").innerHTML = `<pre class="problem">${esc(doc.error)}</pre>`; return; }
  S.doc = doc;
  S.fingerprint = doc.fingerprint;
  S.annotations = ann.items || [];
  S.blocks = new Map();
  index(doc.blocks, null, null);
  S.hashCount = new Map();
  for (const { block: b } of S.blocks.values()) {
    if (b.anchor) { const k = `${b.anchor.file}#${b.anchor.hash}`; S.hashCount.set(k, (S.hashCount.get(k) || 0) + 1); }
  }
  document.title = doc.title + " — galley";
  $("#doc-title").textContent = doc.title;
  const changed = doc.git.changed.length;
  const where = doc.git.repo ? `${doc.doc_dir} @ ${doc.git.commit}` : doc.doc_dir;
  $("#git-status").textContent = `${where}${changed ? ` · ${changed} uncommitted change${changed > 1 ? "s" : ""}` : ""}`;
  $("#git-status").className = changed ? "dirty small" : "muted small";
  $("#git-status").title = changed ? doc.git.changed.join("\n") : "";
  if (doc.git.repo && !doc.git.status_checked) $("#git-status").textContent += " · changes not checked";
  for (const n of doc.notices || []) notice(n, { kind: "warn", key: n });
  setupAgents(doc);
  updateMenuState();
  initMathJax(doc.macros);
  const page = `<div class="page">${setupBanner(doc)}<div class="doc-title">${doc.title_html}</div><div class="doc-date">${doc.date_html || esc(doc.date)}</div>${renderBlocks(doc.blocks)}</div>`;
  const main = $("#doc");
  main.innerHTML = page;
  main.classList.toggle("filtered", S.filtered);
  renderToc(doc.blocks);
  matchAnnotations();
  if (doc.warnings.length) console.info("galley parser notes:", doc.warnings);
  await typeset(main);
  await typeset($("#toc"));
  loadDiagrams();
  if (keep) window.scrollTo(0, scrollY);
  else if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
  if (keep && S.current) {
    if (S.current === "document") openPanel("document", S.selection, true);
    else {
      const id = findBlockByAnchor(S.currentAnchor);
      if (id) openPanel(id, S.selection, true); else closePanel();
    }
  }
}

// Several blocks can have the same source (two identical headings or paragraphs), hence the
// same hash: take the one whose lines are closest to the anchor's recorded lines.
function nearestBlock(ids, a) {
  if (!ids || !ids.length) return null;
  if (ids.length === 1 || a.start == null) return ids[0];
  let best = null;
  for (const id of ids) {
    const b = S.blocks.get(id).block.anchor;
    const d = Math.abs(b.start - a.start) + Math.abs(b.end - (a.end ?? a.start));
    if (!best || d < best[1]) best = [id, d];
  }
  return best[0];
}

function findBlockByAnchor(a) {
  if (!a) return null;
  let fallback = null;
  const same = [];
  for (const [id, info] of S.blocks) {
    const b = info.block;
    if (!b.anchor || (b.kind === "section") !== (a.kind === "section")) continue;
    if (b.anchor.file === a.file && b.anchor.hash === a.hash) { same.push(id); continue; }
    if (!fallback && b.anchor.file === a.file && b.anchor.start === a.start) fallback = id;
    if (!fallback && (a.labels || []).some((l) => (b.labels || []).includes(l))) fallback = id;
  }
  return nearestBlock(same, a) || fallback;
}

function setupBanner(doc) {
  if (doc.instructions_exist || doc.settings.instructions_skipped) return "";
  return `<div class="setup-banner"><div><strong>Set up instructions for agents.</strong> A short <code>galley/instructions.md</code>
    tells agents what this document is, its conventions and related sources, and is included in every request.</div>
    <div class="actions"><button class="primary" data-instr="draft">Draft with agent</button>
    <button data-instr="write">Write my own</button><button data-instr="skip">Not now</button></div></div>`;
}

// ---------------------------------------------------------------- agents and settings

function setupAgents(doc) {
  const sel = $("#agent-select");
  if (sel.options.length) return;
  for (const [k, label] of Object.entries(doc.agents)) {
    const st = doc.agent_settings[k] || {};
    const opt = new Option(`${label}${st.model ? " · " + st.model : ""}${st.available === false ? " (not installed)" : ""}`, k);
    sel.add(opt);
  }
  const saved = stored("agent", null);
  sel.value = saved && doc.agents[saved] ? saved : doc.default_agent;
  updateEffort();
}

function updateEffort() {
  const agent = $("#agent-select").value;
  const st = S.doc?.agent_settings?.[agent];
  const wrap = $("#effort-wrap");
  const sel = $("#effort-select");
  if (!st || !st.efforts.length) { wrap.hidden = true; return; }
  wrap.hidden = false;
  if (sel.dataset.agent !== agent) {
    sel.innerHTML = "";
    for (const e of st.efforts) sel.add(new Option(e[0].toUpperCase() + e.slice(1), e));
    sel.value = st.default_effort || st.efforts[0];
    sel.dataset.agent = agent;
  }
  wrap.title = `${st.model ? "Model " + st.model + "; r" : "R"}easoning effort passed explicitly on every call`;
}

function currentEffort() { return $("#effort-wrap").hidden ? "" : $("#effort-select").value; }
function agentChoice() { return { agent: $("#agent-select").value, effort: currentEffort() }; }

function updateMenuState() {
  const s = S.doc?.settings || {};
  $("#m-marking").textContent = (s.mark_agent_text ? "✓ " : "") + "Mark agent-written text in violet";
  $("#m-violet").textContent = (S.showViolet ? "✓ " : "") + "Show violet text";
  $("#m-annotated").textContent = (S.filtered ? "✓ " : "") + "Highlight passages with notes";
  document.body.classList.toggle("hide-violet", !S.showViolet);
}

// ---------------------------------------------------------------- markdown

function md(text) {
  const math = [];
  const keep = (s) => `\u0000${math.push(s) - 1}\u0000`;
  let t = String(text || "");
  t = t.replace(/```([\w-]*)\n([\s\S]*?)```/g, (_, lang, code) => keep(`<pre><code>${esc(code)}</code></pre>`));
  t = t.replace(/\$\$([\s\S]+?)\$\$/g, (_, m) => keep(`\\[${esc(m)}\\]`));
  t = t.replace(/\\\[([\s\S]+?)\\\]/g, (_, m) => keep(`\\[${esc(m)}\\]`));
  t = t.replace(/\\\(([\s\S]+?)\\\)/g, (_, m) => keep(`\\(${esc(m)}\\)`));
  t = t.replace(/(^|[^\\$])\$([^$\n]+?)\$/g, (_, pre, m) => pre + keep(`\\(${esc(m)}\\)`));
  t = t.replace(/`([^`\n]+)`/g, (_, c) => keep(`<code>${esc(c)}</code>`));
  t = esc(t);
  const lines = t.split("\n");
  const out = [];
  let list = null, para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${para.join(" ")}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map((i) => `<li>${i}</li>`).join("")}</${list.tag}>`); list = null; } };
  const isRow = (l) => /^\s*\|.*\|\s*$/.test(l || "");
  const isSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l || "");
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  for (let li = 0; li < lines.length; li++) {
    const raw = lines[li];
    const line = raw.trimEnd();
    let m;
    if (!line.trim()) { flushPara(); flushList(); continue; }
    if (isRow(line) && isSep(lines[li + 1])) {
      // Pipe table: header row, separator, body rows.
      flushPara(); flushList();
      const head = cells(line);
      const body = [];
      li += 2;
      while (li < lines.length && isRow(lines[li])) { body.push(cells(lines[li])); li++; }
      li--;
      out.push(`<div class="mdtable-wrap"><table class="mdtable"><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${
        body.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { flushPara(); flushList(); out.push(`<h3>${m[2]}</h3>`); continue; }
    if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { flushPara(); if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; } list.items.push(m[1]); continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flushPara(); if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; } list.items.push(m[1]); continue; }
    if ((m = line.match(/^&gt;\s?(.*)$/))) { flushPara(); flushList(); out.push(`<blockquote>${m[1]}</blockquote>`); continue; }
    if (list && /^\s{2,}\S/.test(raw)) { list.items[list.items.length - 1] += " " + line.trim(); continue; }
    flushList();
    para.push(line);
  }
  flushPara(); flushList();
  let html = out.join("");
  // Links: web links open in a new tab; local paths are shown as plain text with the path on hover.
  html = html.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_, label, href) =>
    /^https?:\/\//.test(href) ? `<a href="${href}" target="_blank" rel="noopener">${label}</a>` : `<span class="locallink" title="${href}">${label}</span>`);
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, "$1<em>$2</em>");
  html = html.replace(/\u0000(\d+)\u0000/g, (_, i) => math[+i]);
  return html;
}

// Edits an agent proposed to files galley does not let agents change (anything but the document's
// .tex sources and the .bib files it names): shown so the person sees what was attempted.
function renderRefused(list) {
  if (!list || !list.length) return "";
  return list.map((r) => `<div class="refused"><strong>Refused:</strong> the agent proposed a change to <code>${esc(r.file)}</code>,
    which is not one of the document's source files. galley does not apply it, so this proposal cannot be accepted.
    Be careful with any advice that came with it.
    <details><summary>what the agent proposed for ${esc(r.file)}</summary><pre class="no-math">${esc(r.original.split("\n").map((l) => "- " + l).join("\n"))}
${esc(r.replacement.split("\n").map((l) => "+ " + l).join("\n"))}</pre></details></div>`).join("");
}

function renderDiff(diff) {
  return `<div class="diff no-math">${diff.split("\n").map((l) => {
    const cls = l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : "";
    return cls ? `<span class="${cls}">${esc(l)}</span>` : esc(l) + "\n";
  }).join("")}</div>`;
}


// ---------------------------------------------------------------- panel

function blockTitle(id) {
  if (id === "document") return "Whole document";
  const info = S.blocks.get(id);
  const b = info.block;
  if (b.kind === "section") return `Section ${b.number || ""} ${b.title_text ? "· " + b.title_text : ""}`.trim();
  if (b.kind === "env") return b.env === "proof" ? `Proof${info.section ? " in §" + (info.section.number || "") : ""}` : `${b.display} ${b.number || ""}`.trim();
  if (b.kind === "bibitem") return `Reference [${b.number}]`;
  const where = info.env ? `${info.env.env === "proof" ? "proof" : info.env.display + " " + (info.env.number || "")}` : (info.section ? "§" + (info.section.number || info.section.title_text) : "");
  return `Paragraph${where ? " in " + where : ""}`;
}

// ---------------------------------------------------------------- reading position
// When the document column changes width (sidebar opened or closed, contents column toggled,
// window resized), the text reflows and the same scroll offset shows other text. galley keeps
// a reference point (a block and how far through it the top of the window is) up to date while
// you scroll, and after a width change scrolls so that point is back where it was.

const READ_SEL = ".blk, .sec-head, .env-head, .doc-title, .doc-date, .setup-banner";

function readLine() { return ($("#topbar")?.offsetHeight || 49) + 8; }

function captureReading() {
  const doc = $("#doc");
  if (!doc) return null;
  const dr = (doc.querySelector(".page") || doc).getBoundingClientRect();  // the text column, not its margins
  const x = dr.left + Math.min(dr.width / 2, 300);
  const line = readLine();
  for (let y = line; y < Math.min(window.innerHeight, line + 400); y += 16) {
    const hit = document.elementFromPoint(x, y);
    const el = hit && hit.closest && doc.contains(hit) ? hit.closest(READ_SEL) : null;
    if (el) {
      const r = el.getBoundingClientRect();
      if (r.top >= line) return { el, frac: 0, line: r.top };  // just below the top: keep its own position
      return { el, frac: r.height ? Math.min(1, (line - r.top) / r.height) : 0, line };
    }
  }
  return null;
}

// Keep this block where it is on screen: the passage under discussion stays put when the sidebar
// opens or closes, if it is on screen; otherwise the top of the window is the reference.
function pinReading(el) {
  if (!el) return;
  const r = el.getBoundingClientRect();
  if (r.bottom < readLine() || r.top > window.innerHeight) return;  // not on screen: keep the usual reference
  S.reading = { el, frac: 0, line: r.top };
  S.pinnedAt = performance.now();
}

function restoreReading() {
  const a = S.reading;
  if (!a || !a.el.isConnected) return;
  const r = a.el.getBoundingClientRect();
  const delta = r.top + a.frac * r.height - a.line;
  if (Math.abs(delta) > 1) window.scrollBy(0, delta);
}

function watchReading() {
  let pending = false;
  window.addEventListener("scroll", () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      if (performance.now() - (S.pinnedAt || 0) < 400) return;  // a passage was just pinned: keep it
      S.reading = captureReading();
    });
  }, { passive: true });
  let width = $("#doc").getBoundingClientRect().width;
  const onResize = () => {
    const w = $("#doc").getBoundingClientRect().width;
    if (Math.abs(w - width) < 1) return;
    width = w;
    restoreReading();
    S.pinnedAt = 0;
    S.reading = captureReading();
  };
  if (window.ResizeObserver) new ResizeObserver(onResize).observe($("#doc"));
  else window.addEventListener("resize", onResize);
}

// "Show in text": bring the passage the sidebar is discussing into view.
function blockElement(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  if (el.tagName === "SECTION") return el.querySelector(":scope > .sec-head") || el;
  return el;
}

function locateCurrent() {
  if (!S.current || S.current === "document" || $("#panel").hidden) return;
  const el = blockElement(S.current);
  if (!el) return;
  let p = el.parentElement;  // open collapsed sections and proofs around it
  while (p) {
    if (p.classList?.contains("collapsed")) {
      p.classList.remove("collapsed");
      if (p.dataset.key) S.collapsed.delete(p.dataset.key);
    }
    p = p.parentElement;
  }
  const r = el.getBoundingClientRect();
  const line = readLine();
  const room = window.innerHeight - line;
  const offset = r.height > room * 0.66 ? 8 : room / 3 - Math.min(r.height / 2, room / 6);
  window.scrollTo({ top: window.scrollY + r.top - line - offset + 8, behavior: "smooth" });
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
}

async function openPanel(id, selection = "", keepThread = false) {
  if (S.editing && S.editing.dirty && !confirm("Discard your unsaved source edit?")) return;
  S.editing = null;
  document.querySelectorAll(".selected").forEach((e) => e.classList.remove("selected"));
  if (id !== "document") {
    const el = document.getElementById(id);
    (el?.classList.contains("env") ? el.querySelector(".env-head") : el?.tagName === "SECTION" ? el.querySelector(".sec-head") : el)?.classList.add("selected");
  }
  S.current = id;
  S.currentAnchor = anchorOf(id);
  S.selection = selection;
  const a = S.currentAnchor;
  if ($("#panel").hidden && id !== "document") pinReading(blockElement(id));
  $("#panel").hidden = false;
  $("#panel-title").textContent = blockTitle(id);
  $("#panel-locate").hidden = id === "document";
  const isDoc = a.kind === "document";
  const isSection = a.kind === "section";
  $("#panel-anchor").textContent = isDoc ? `${S.doc.main_file} and ${S.doc.files.length - 1} input file(s)`
    : isSection ? (a.span || []).map((s) => `${s.file}:${s.start}–${s.end}`).join(", ")
    : `${a.path}:${a.start}–${a.end}${a.labels.length ? "  [" + a.labels.join(", ") + "]" : ""}`;
  const selBox = $("#panel-selection");
  if (selection) {
    selBox.hidden = false;
    selBox.innerHTML = `<span class="x" title="Clear">&times;</span>${esc(selection.length > 400 ? selection.slice(0, 400) + "…" : selection)}`;
    selBox.querySelector(".x").onclick = () => { S.selection = ""; selBox.hidden = true; };
  } else selBox.hidden = true;
  const srcBox = $("#panel-source");
  srcBox.hidden = isDoc || isSection;
  $("#panel-edit").hidden = true;
  if (!srcBox.hidden) {
    const src = await api(`/api/source?file=${encodeURIComponent(a.file)}&start=${a.start}&end=${a.end}`);
    S.sourceText = src.text ?? "";
    $("#panel-source pre").textContent = src.text || src.error || "";
    $("#clear-marking").hidden = !/\\AGTADD\s*\{/.test(S.sourceText);
  }
  $("#compose-text").placeholder = isDoc ? "Discuss the whole document, e.g. a global change of notation…"
    : isSection ? "Ask about this section, or describe an edit to it…" : "Ask about this passage, or describe the edit you want…";
  renderHistory();
  renderThread();
  $("#panel").scrollTop = 0;
  if (!keepThread) $("#compose-text").focus({ preventScroll: true });
}

function closePanel() {
  if (S.editing && S.editing.dirty && !confirm("Discard your unsaved source edit?")) return;
  S.editing = null;
  if (!$("#panel").hidden && S.current && S.current !== "document") pinReading(blockElement(S.current));
  $("#panel").hidden = true;
  S.current = null;
  document.querySelectorAll(".selected").forEach((e) => e.classList.remove("selected"));
}

function notesForCurrent() {
  if (S.current === "document") return S.docNotes;
  return S.notesByBlock.get(S.current) || [];
}

function renderHistory() {
  const items = notesForCurrent().slice().sort((x, y) => (x.time < y.time ? -1 : 1));
  const live = new Set((S.threads.get(anchorKey(S.currentAnchor)) || []).map((m) => "a-" + m.job));
  const box = $("#panel-history");
  const saved = items.filter((a) => !live.has(a.id));
  const active = saved.filter((a) => !a.archived);
  const archived = saved.filter((a) => a.archived);
  const shown = S.showArchived ? saved : active;
  if (!saved.length) { box.innerHTML = ""; return; }
  const links = [];
  if (active.length > 1) links.push(`<a href="#" data-archive-here>archive all here</a>`);
  if (archived.length) links.push(`<a href="#" data-toggle-archived>${S.showArchived ? "hide" : "show"} ${archived.length} archived</a>`);
  box.innerHTML = `<div class="history-head"><span>Saved here</span><span class="history-links">${links.join(" · ")}</span></div>` + shown.map((a) => {
    const when = (a.time || "").replace("T", " ").slice(0, 16);
    const outcome = (a.outcomes || []).map((o) => o.outcome).join(", ");
    const kind = a.type === "comment" ? "Comment" : a.type === "suggest" ? `Edit proposal${outcome ? " · " + outcome : " · no decision"}` : "Question";
    const action = a.archived ? `<a href="#" data-unarchive="${esc(a.id)}">unarchive</a>` : `<a href="#" data-archive="${esc(a.id)}" title="Hide this item; it stays in the notes file">archive</a>`;
    return `<div class="msg old${a._stale ? " stale" : ""}${a.archived ? " archived" : ""}"><div class="meta"><span>${kind} · ${esc(a.person || "")}${a.agent ? " · " + esc(a.agent) : ""} · ${esc(when)}${a.archived ? " · archived" : ""}</span>${action}</div>
      ${a.selection ? `<div class="small muted">“${esc(a.selection.slice(0, 200))}”</div>` : ""}
      ${a.question ? `<div class="msg-text"><strong>${esc(a.question)}</strong></div>` : ""}
      ${a.answer ? `<div class="answer">${md(a.answer)}</div>` : ""}
      ${a.refused && a.refused.length ? `<div class="problem">Refused: changes to ${a.refused.map((f) => `<code>${esc(f)}</code>`).join(", ")}, which are not source files of the document.</div>` : ""}
      ${a.diff ? `<details><summary>proposed diff</summary>${renderDiff(a.diff)}</details>` : ""}</div>`;
  }).join("");
  typeset(box);
}

function renderThread() {
  const box = $("#panel-thread");
  const msgs = S.threads.get(anchorKey(S.currentAnchor)) || [];
  box.innerHTML = msgs.map((m) => {
    const agentLabel = m.agentLabel || S.doc.agents[m.agent] || m.agent;
    let body = "";
    if (m.status === "running") {
      body = `<div class="muted"><span class="spinner"></span>${esc(agentLabel)} is working…</div>` +
        (m.progress ? `<details><summary>agent output</summary><pre>${esc(m.progress.slice(-3000))}</pre></details>` : "");
    } else if (m.status === "error") {
      body = `<div class="problem">${esc(m.error)}</div>`;
    } else if (m.status === "done") {
      body = `<div class="answer">${md(m.answer)}</div>`;
      if (m.proposal) {
        const p = m.proposal;
        const problems = (p.problems || []).filter((x) => !x.startsWith("edit refused:"));
        body += problems.length ? `<div class="problem">${esc(problems.join("; "))}</div>` : "";
        body += renderRefused(p.refused);
        if (p.diff) body += renderDiff(p.diff);
        if (p.ok && p.state === "pending") {
          body += `<div class="proposal-actions"><button class="primary" data-accept="${m.job}">Accept edit</button><button data-reject="${m.job}">Reject</button><span class="muted small">Accepting changes the files; nothing is committed.</span></div>`;
        } else if (p.state && p.state !== "pending") {
          body += `<div class="muted small">Edit ${esc(p.state)}.</div>`;
        }
      }
    }
    const label = m.mode === "suggest" ? "Edit request" : "Question";
    return `<div class="msg q"><div class="meta"><span>${label} · ${esc(S.doc.person)} → ${esc(agentLabel)}</span></div>
      ${m.selection ? `<div class="small muted">“${esc(m.selection.slice(0, 200))}”</div>` : ""}
      <div class="msg-text"><strong>${esc(m.question || (m.mode === "suggest" ? "(suggest an improvement)" : "(explain this)"))}</strong></div>${body}</div>`;
  }).join("");
  typeset(box);
}

const WRAP_PHRASE = /^\s*(please\s+)?(log (this|that|it)|wrap(\s+it)?[\s-]*up)\s*(please)?\s*[.!]?\s*$/i;

async function send(mode) {
  if (!S.current) return;
  const text = $("#compose-text").value.trim();
  if (mode === "explain" && WRAP_PHRASE.test(text)) {
    $("#compose-text").value = "";
    return wrapUpCurrent();
  }
  if (mode === "comment") {
    if (!text) return toast("Write the comment first.");
    const r = await api("/api/note", { anchor: S.currentAnchor, text });
    if (r.error) return toast(r.error);
    $("#compose-text").value = "";
    await refreshAnnotations();
    return toast("Comment saved.");
  }
  if (mode === "suggest" && !text) return toast("Describe the edit you want.");
  const key = anchorKey(S.currentAnchor);
  const thread = S.threads.get(key) || [];
  const history = thread.filter((m) => m.status === "done").map((m) => ({ question: m.question, answer: m.answer }));
  const { agent, effort } = agentChoice();
  const r = await api("/api/ask", { anchor: S.currentAnchor, mode, question: text, selection: S.selection, agent, effort, history, thread: key });
  if (r.error) return toast(r.error, 5000);
  thread.push({ job: r.job, mode, question: text, selection: S.selection, agent, status: "running", progress: "" });
  S.threads.set(key, thread);
  $("#compose-text").value = "";
  renderThread();
  poll(r.job, key);
}

async function poll(job, key) {
  const msg = (S.threads.get(key) || []).find((m) => m.job === job);
  if (!msg) return;
  const j = await api(`/api/job/${job}`);
  if (j.error && !j.status) { msg.status = "error"; msg.error = j.error; }
  else { msg.agentLabel = j.agent_label; msg.status = j.status; msg.progress = j.progress; msg.answer = j.answer; msg.error = j.error; msg.proposal = j.proposal; }
  if (anchorKey(S.currentAnchor) === key) {
    if (msg.status !== "running" || msg._lastProgress !== msg.progress) renderThread();
    msg._lastProgress = msg.progress;
  }
  if (msg.status === "running") setTimeout(() => poll(job, key), 1500);
  else if (msg.status === "done") { await refreshAnnotations(); if (anchorKey(S.currentAnchor) !== key) toast("An answer arrived for another passage."); }
}

async function refreshAnnotations() {
  const ann = await api("/api/annotations");
  S.annotations = ann.items || [];
  matchAnnotations();
  if (S.current) renderHistory();
}

async function decide(job, accept) {
  const r = await api("/api/decide", { job, accept });
  if (!r.ok) return toast(r.error || "Failed.", 5000);
  for (const msgs of S.threads.values()) for (const m of msgs) if (m.job === job && m.proposal) m.proposal.state = r.state;
  if (accept) {
    const oldKey = anchorKey(S.currentAnchor);
    const msgs = S.threads.get(oldKey);
    await loadDoc(true);
    const newKey = anchorKey(S.currentAnchor);
    if (msgs && newKey && newKey !== oldKey) { S.threads.set(newKey, msgs); S.threads.delete(oldKey); }
    renderThread();
    toast("Edit applied.");
  } else {
    renderThread();
    toast("Edit rejected.");
  }
}

// ---------------------------------------------------------------- source editing

function startEdit() {
  const a = S.currentAnchor;
  if (!a || a.kind === "document" || a.kind === "section") return;
  S.editing = { original: S.sourceText, dirty: false };
  const box = $("#panel-edit");
  box.hidden = false;
  const ta = $("#edit-text");
  ta.value = S.sourceText;
  ta.rows = Math.min(24, Math.max(6, S.sourceText.split("\n").length + 1));
  ta.focus();
}

async function saveEdit() {
  const a = S.currentAnchor;
  const text = $("#edit-text").value;
  const r = await api("/api/edit", { file: a.file, start: a.start, end: a.end, original: S.editing.original, text, anchor: a });
  if (!r.ok) return toast(r.error || "Could not save.", 5000);
  S.editing = null;
  $("#panel-edit").hidden = true;
  if (!r.changed) return toast("No change.");
  // Re-render now; the file watcher would also notice.
  const hashBefore = S.currentAnchor.hash;
  await loadDoc(true);
  toast("Saved to " + a.file + ".");
  if (S.currentAnchor && S.currentAnchor.hash === hashBefore) closePanel();
}

// ---------------------------------------------------------------- wrap-up, records, notes housekeeping

// Wrap-ups run in the background. While any runs, the Wrap up button shows a spinner (and how
// many run) and is disabled; when one ends, a notice stays until dismissed.
S.wrapups = new Map(); // job id -> record name

function updateWrapButton() {
  const b = $("#btn-wrapup");
  const n = S.wrapups.size;
  b.disabled = n > 0;
  b.classList.toggle("busy", n > 0);
  b.innerHTML = n ? `<span class="spinner"></span>Wrapping up${n > 1 ? ` (${n})` : ""}…` : "Wrap up";
  b.title = n ? `Summarizing ${n === 1 ? "a record" : n + " records"} in the background: ${[...S.wrapups.values()].join(", ")}`
    : "Summarize this session's record in the background";
}

function viewRecord(name) {
  return api(`/api/record?name=${encodeURIComponent(name)}`).then((rec) => {
    if (rec.error) return toast(rec.error, 4000);
    showModal(`Session record — ${rec.path}`, `<pre class="no-math">${esc(rec.text)}</pre>`);
  });
}

// Follow a wrap-up job until it ends. Returns true if it succeeded.
async function trackWrapup(job, record, { quiet = false } = {}) {
  if (S.wrapups.has(job)) return S.wrapupDone?.get(job);
  S.wrapups.set(job, record);
  updateWrapButton();
  const done = (async () => {
    let j;
    for (;;) {
      await new Promise((res) => setTimeout(res, 2500));
      j = await api(`/api/job/${job}`);
      if (j.status === "running") continue;
      if (!j.status && !j.error) continue;
      break;
    }
    S.wrapups.delete(job);
    updateWrapButton();
    if (j.status === "done") {
      if (!quiet) notice(`Session summarized: ${record}`, { action: "View record", onAction: () => viewRecord(record) });
      return true;
    }
    notice(`Wrap-up of ${record} failed: ${(j.error || "galley lost track of the job").slice(0, 400)}`, { kind: "error" });
    return false;
  })();
  S.wrapupDone = S.wrapupDone || new Map();
  S.wrapupDone.set(job, done);
  return done;
}

async function startWrapup(record, opts) {
  const r = await api("/api/wrapup", { ...agentChoice(), record });
  if (r.error) { notice(r.error, { kind: "error" }); return false; }
  const name = (r.record || record || "").split("/").pop();
  return trackWrapup(r.job, name, opts);
}

async function wrapUpCurrent() {
  if (S.wrapups.size) return;
  const r = await api("/api/records");
  const cur = (r.items || []).find((x) => x.current);
  if (!cur) return toast("Nothing has been recorded in this session yet.", 3500);
  return startWrapup(cur.name);
}

async function wrapUpAll(names) {
  let done = 0;
  for (const name of names) if (await startWrapup(name, { quiet: true })) done++;
  notice(`Summarized ${done} of ${names.length} records.`, { kind: done === names.length ? "info" : "warn",
    action: "Show records", onAction: showRecords });
}

// After a reload, pick up wrap-ups that are still running.
async function resumeWrapups() {
  const r = await api("/api/jobs/running");
  for (const j of r.items || []) trackWrapup(j.id, j.record);
}

function notesSection() {
  const active = S.annotations.filter((x) => !x.archived).length;
  const archived = S.annotations.length - active;
  return `<div class="notes-housekeeping"><div><strong>Saved notes:</strong> ${active} shown, ${archived} archived.
    Archiving hides items; they stay in the notes file and can be unarchived.</div>
    <div class="actions">${active ? `<button data-archive-all>Archive all ${active}</button>` : ""}
    ${archived ? `<button data-toggle-archived>${S.showArchived ? "Hide" : "Show"} archived</button>` : ""}</div></div>`;
}

async function showRecords() {
  const r = await api("/api/records");
  const items = (r.items || []).filter((x) => x.interactions > 0 || x.current);
  const pending = items.filter((x) => !x.has_summary && x.interactions > 0).map((x) => x.name);
  const allBtn = pending.length > 1 ? `<p><button class="primary" data-wrap-all="${esc(pending.join("|"))}">Wrap up all ${pending.length} records without a summary</button></p>` : "";
  const rows = items.map((x) => `<tr><td>${esc(x.name)}${x.current ? ' <span class="muted small">(this session)</span>' : ""}</td>
      <td class="small">${x.interactions} interaction${x.interactions === 1 ? "" : "s"}</td>
      <td class="small">${x.has_summary ? "summarized" : "<strong>no summary yet</strong>"}</td>
      <td><button data-view="${esc(x.name)}">View</button> ${[...S.wrapups.values()].includes(x.name)
        ? `<span class="muted small"><span class="spinner"></span>wrapping up…</span>`
        : `<button data-wrap="${esc(x.name)}">${x.has_summary ? "Summarize again" : "Wrap up"}</button>`}</td></tr>`).join("");
  showModal("Session records and notes", notesSection() + allBtn +
    (items.length ? `<p class="muted small">Wrap up runs in the background and puts a summary at the top of the record.</p><table class="records">${rows}</table>`
      : `<p class="muted">No session records yet. A record starts with the first question, edit or comment.</p>`));
}

function confirmInline(btn, label) {
  if (btn.dataset.armed) return true;
  btn.dataset.armed = "1";
  btn.textContent = label;
  btn.classList.add("primary");
  return false;
}

async function setArchived(body, archived) {
  const r = await api("/api/archive", { ...body, archived });
  if (r.error) return toast(r.error, 4000);
  await refreshAnnotations();
  if (r.changed) toast(`${archived ? "Archived" : "Unarchived"} ${r.changed} item${r.changed === 1 ? "" : "s"}.`);
}

// ---------------------------------------------------------------- tikzcd diagrams

// galley compiles each display with a tikzcd diagram to SVG in the background (diagrams.py).
async function loadDiagrams() {
  clearTimeout(S.diagramTimer);
  const els = [...document.querySelectorAll(".diagram[data-diagram]:not(.ready)")];
  if (!els.length) return;
  const hashes = [...new Set(els.map((e) => e.dataset.diagram))];
  const st = await api("/api/diagrams", { hashes });
  if (st.error) return;
  let pending = false;
  // Match the diagram's text size to the page: the SVG is at the document's own font size.
  const ref = document.querySelector("#doc .para .content") || document.querySelector("#doc");
  const scale = parseFloat(getComputedStyle(ref).fontSize) / ((S.doc.font_pt || 10) * 96 / 72);
  for (const el of els) {
    const s = st[el.dataset.diagram];
    if (!s || s.state === "pending") { pending = true; continue; }
    el.classList.add("ready");
    if (s.state === "done") {
      el.innerHTML = `<img src="/api/diagram/${el.dataset.diagram}.svg" alt="tikzcd diagram">`;
      const img = el.firstElementChild;
      img.onload = () => { img.style.width = Math.round(img.naturalWidth * scale) + "px"; };
    } else {
      el.classList.add("failed");
      el.innerHTML = `<span class="figure-placeholder">[tikzcd diagram: could not be compiled; see the PDF]</span>
        <details class="diagram-error"><summary>Why</summary><pre>${esc(s.error || "")}</pre></details>`;
    }
  }
  if (pending) S.diagramTimer = setTimeout(loadDiagrams, 1500);
}

// ---------------------------------------------------------------- instructions

async function openInstructions(draft) {
  const r = await api("/api/instructions");
  showModal("Instructions for agents", `<p class="muted small">Saved as <code>${esc(r.path)}</code> and included in every request about this document.</p>
    <textarea id="instr-text" rows="22" placeholder="What this document is, authors and status, notation and conventions, related sources, how agents should work on it…">${esc(r.text)}</textarea>
    <div id="instr-status" class="muted small"></div>
    <div class="modal-actions"><button class="primary" id="instr-save">Save</button>
    <button id="instr-draft">${r.text ? "Revise with agent" : "Draft with agent"}</button>
    <button id="instr-cancel">Cancel</button></div>`);
  $("#instr-save").onclick = async () => {
    const res = await api("/api/instructions", { text: $("#instr-text").value });
    if (res.error) return toast(res.error);
    $("#modal").hidden = true;
    toast("Instructions saved.");
    loadDoc(true);
  };
  $("#instr-cancel").onclick = () => ($("#modal").hidden = true);
  $("#instr-draft").onclick = () => draftInstructions($("#instr-text").value.trim());
  if (draft) draftInstructions("");
}

async function draftInstructions(text) {
  const status = $("#instr-status");
  const btns = document.querySelectorAll(".modal-actions button");
  btns.forEach((b) => (b.disabled = true));
  const { agent } = agentChoice();
  status.innerHTML = `<span class="spinner"></span>${esc(S.doc.agents[agent] || agent)} is ${text ? "revising" : "reading the document and drafting"} the instructions. This can take a few minutes; you can close this window and come back.`;
  const before = $("#instr-text") ? $("#instr-text").value : null;
  const j = await runInBackground("/api/instructions/draft", { ...agentChoice(), text }, "", "");
  btns.forEach((b) => (b.disabled = false));
  if (!j) { if (status.isConnected) status.textContent = "The draft failed; see the message at the bottom of the window."; return; }
  // The server has already written the agent's text to galley/instructions.md.
  const r = await api("/api/instructions");
  const ta = $("#instr-text");
  const open = ta && !$("#modal").hidden && status.isConnected;
  if (open && ta.value === before) {
    ta.value = r.text;
    status.textContent = `Saved to ${r.path}. Edit it here and Save, or answer the questions at the top and “Revise with agent”.`;
    $("#instr-draft").textContent = "Revise with agent";
  } else if (open) {
    status.textContent = `The agent's version is saved in ${r.path}, but you have edited this window since; Cancel and reopen to see it.`;
  } else {
    toast(`The agent's instructions are saved in ${r.path} (More → Instructions for agents).`, 6000);
  }
  loadDoc(true);
}

// ---------------------------------------------------------------- violet marking

async function toggleMarking() {
  const on = !S.doc.settings.mark_agent_text;
  if (on && !S.doc.marking_defined) {
    showModal("Mark agent-written text", `<p>Agents will wrap text they add or rewrite in <code>\\AGTADD{…}</code>, shown in violet here and in the PDF.
      To make the PDF build, galley needs to add these lines before <code>\\begin{document}</code> in <code>${esc(S.doc.main_file)}</code>:</p>
      <pre class="no-math">% Added by galley: agent-written text is wrapped in \\AGTADD{...} and shown in violet.
% To print it in the normal colour, change \\galleyshowagenttrue to \\galleyshowagentfalse.
\\newif\\ifgalleyshowagent
\\galleyshowagenttrue
\\DeclareRobustCommand{\\AGTADD}[1]{\\ifgalleyshowagent{\\color{violet}#1}\\else{#1}\\fi}</pre>
      <p class="muted small">(plus <code>\\usepackage{xcolor}</code> if the document doesn't load it already).</p>
      <div class="modal-actions"><button class="primary" id="mark-yes">Add the lines and turn marking on</button><button id="mark-no">Cancel</button></div>`);
    $("#mark-no").onclick = () => ($("#modal").hidden = true);
    $("#mark-yes").onclick = async () => {
      const r = await api("/api/marking/setup", {});
      if (!r.ok) return toast(r.error || "Failed.", 5000);
      await api("/api/settings", { mark_agent_text: true });
      $("#modal").hidden = true;
      await loadDoc(true);
      toast("Marking on: agents will mark their text in violet.");
    };
    return;
  }
  await api("/api/settings", { mark_agent_text: on });
  S.doc.settings.mark_agent_text = on;
  updateMenuState();
  toast(on ? "Marking on: agents will mark their text in violet." : "Marking off for new edits (existing violet text stays).");
}

async function clearMarking(scope) {
  const body = scope === "document" ? { scope } : { file: S.currentAnchor.file, start: S.currentAnchor.start, end: S.currentAnchor.end, anchor: S.currentAnchor };
  const r = await api("/api/marking/clear", body);
  if (!r.ok) return toast(r.error || "Failed.", 5000);
  toast(r.removed ? `Removed ${r.removed} violet marking${r.removed === 1 ? "" : "s"}; the text stays.` : "No violet marking found.");
  if (r.removed) await loadDoc(true);
}

// ---------------------------------------------------------------- file watching

async function watch() {
  try {
    const r = await api("/api/version");
    if (r.fingerprint && S.fingerprint && r.fingerprint !== S.fingerprint) {
      if (S.editing && S.editing.dirty) {
        $("#watch-banner").hidden = false;
      } else {
        S.fingerprint = r.fingerprint;
        await loadDoc(true);
        if (S.current && S.current !== "document") renderHistory();
      }
    }
  } catch (e) {}
  setTimeout(watch, 2000);
}

// ---------------------------------------------------------------- events

function fitTopbar() {
  document.documentElement.style.setProperty("--topbar-h", $("#topbar").offsetHeight + "px");
}

function wire() {
  fitTopbar();
  if (window.ResizeObserver) new ResizeObserver(fitTopbar).observe($("#topbar"));
  else window.addEventListener("resize", fitTopbar);
  S.showViolet = stored("showViolet", true);

  $("#doc").addEventListener("click", (ev) => {
    const instr = ev.target.closest("[data-instr]");
    if (instr) {
      const what = instr.dataset.instr;
      if (what === "skip") return api("/api/instructions", { skip: true }).then(() => loadDoc(true));
      return openInstructions(what === "draft");
    }
    const diagram = ev.target.closest(".diagram");
    if (diagram && !ev.target.closest("details")) {
      const blk = diagram.closest("[data-blk]");
      if (blk) return openPanel(blk.dataset.blk).then(() => { const d = $("#panel-source details"); if (d) d.open = true; });
    }
    const ask = ev.target.closest("[data-ask]");
    if (ask) {
      ev.preventDefault();
      ev.stopPropagation();
      if (S.current === ask.dataset.ask && !$("#panel").hidden) return closePanel();
      return openPanel(ask.dataset.ask);
    }
    const h = ev.target.closest("[data-sec]");
    if (h && !ev.target.closest("a")) {
      const sec = document.getElementById(h.dataset.sec);
      sec.classList.toggle("collapsed");
      const key = sec.dataset.key;
      if (sec.classList.contains("collapsed")) S.collapsed.add(key); else S.collapsed.delete(key);
      return;
    }
    const proofHead = ev.target.closest(".kind-proof > .env-head");
    if (proofHead && !ev.target.closest("a")) { proofHead.parentElement.classList.toggle("collapsed"); return; }
    const ref = ev.target.closest("a.ref, a.cite");
    if (ref) {
      const target = document.getElementById(ref.getAttribute("href").slice(1));
      if (target) {
        ev.preventDefault();
        let p = target.parentElement;
        while (p) { if (p.classList?.contains("collapsed")) p.classList.remove("collapsed"); p = p.parentElement; }
        const blk = target.closest(".blk, .env, section") || target;
        blk.scrollIntoView({ behavior: "smooth", block: "center" });
        blk.classList.remove("flash"); void blk.offsetWidth; blk.classList.add("flash");
        history.pushState(null, "", "#" + target.id);
      }
    }
  });

  document.addEventListener("mouseup", (ev) => {
    const pop = $("#selection-pop");
    if (ev.target.closest("#selection-pop")) return;
    setTimeout(() => {
      const sel = window.getSelection();
      const text = sel && !sel.isCollapsed ? sel.toString().trim() : "";
      if (!text || !sel.anchorNode) { pop.hidden = true; return; }
      const el = (sel.anchorNode.nodeType === 1 ? sel.anchorNode : sel.anchorNode.parentElement)?.closest("[data-blk]");
      if (!el || !$("#doc").contains(el)) { pop.hidden = true; return; }
      const rect = sel.getRangeAt(0).getBoundingClientRect();
      pop.style.left = `${rect.left + window.scrollX}px`;
      pop.style.top = `${rect.bottom + window.scrollY + 6}px`;
      pop.hidden = false;
      pop.dataset.blk = el.dataset.blk;
      pop.dataset.text = text;
    }, 0);
  });
  $("#pop-ask").addEventListener("click", () => {
    const pop = $("#selection-pop");
    pop.hidden = true;
    openPanel(pop.dataset.blk, pop.dataset.text);
  });

  $("#btn-document").onclick = () => (S.current === "document" && !$("#panel").hidden ? closePanel() : openPanel("document"));
  $("#panel-close").onclick = closePanel;
  $("#panel-locate").onclick = locateCurrent;
  // The shortcut, written the way this system writes it, on the button and in its tooltip.
  const mac = /mac|iphone|ipad/i.test(navigator.userAgentData?.platform || navigator.platform || navigator.userAgent);
  const keys = mac ? "\u21e7\u2318L" : "Ctrl+Shift+L";
  $("#locate-key").textContent = keys;
  $("#panel-locate").title = `Scroll the document to this passage (${mac ? "Shift-Command-L" : "Ctrl+Shift+L"})`;
  $("#send-explain").onclick = () => send("explain");
  $("#send-suggest").onclick = () => send("suggest");
  $("#send-comment").onclick = () => send("comment");
  $("#compose-text").addEventListener("keydown", (ev) => { if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) send("explain"); });
  $("#edit-source").onclick = startEdit;
  $("#edit-save").onclick = saveEdit;
  $("#edit-cancel").onclick = () => { S.editing = null; $("#panel-edit").hidden = true; };
  $("#edit-text").addEventListener("input", () => { if (S.editing) S.editing.dirty = true; });
  $("#edit-text").addEventListener("keydown", (ev) => { if (ev.key === "s" && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); saveEdit(); } });
  $("#clear-marking").onclick = () => clearMarking("passage");
  $("#watch-reload").onclick = () => { $("#watch-banner").hidden = true; S.editing = null; $("#panel-edit").hidden = true; loadDoc(true); };

  $("#panel").addEventListener("click", (ev) => {
    const arc = ev.target.closest("[data-archive], [data-unarchive], [data-archive-here], [data-toggle-archived]");
    if (arc) {
      ev.preventDefault();
      if (arc.dataset.archive) return setArchived({ ids: [arc.dataset.archive] }, true);
      if (arc.dataset.unarchive) return setArchived({ ids: [arc.dataset.unarchive] }, false);
      if (arc.hasAttribute("data-archive-here")) return setArchived({ ids: notesForCurrent().filter((x) => !x.archived).map((x) => x.id) }, true);
      S.showArchived = !S.showArchived;
      matchAnnotations();
      return renderHistory();
    }
    const a = ev.target.closest("[data-accept]");
    if (a) return decide(a.dataset.accept, true);
    const r = ev.target.closest("[data-reject]");
    if (r) return decide(r.dataset.reject, false);
    const ref = ev.target.closest("a.ref");
    if (ref) { ev.preventDefault(); document.getElementById(ref.getAttribute("href").slice(1))?.scrollIntoView({ behavior: "smooth", block: "center" }); }
  });

  $("#agent-select").onchange = (ev) => { store("agent", ev.target.value); updateEffort(); };
  $("#toggle-toc").onclick = () => $("#toc").classList.toggle("hidden");
  $("#btn-proofs").onclick = (ev) => {
    S.proofsHidden = !S.proofsHidden;
    document.querySelectorAll(".env.kind-proof").forEach((e) => e.classList.toggle("collapsed", S.proofsHidden));
    ev.target.textContent = S.proofsHidden ? "Show proofs" : "Hide proofs";
  };
  $("#btn-wrapup").onclick = wrapUpCurrent;

  // "More" menu
  const menu = $("#menu");
  $("#btn-more").onclick = (ev) => { ev.stopPropagation(); menu.hidden = !menu.hidden; };
  document.addEventListener("click", (ev) => { if (!ev.target.closest("#menu")) menu.hidden = true; });
  menu.addEventListener("click", async (ev) => {
    const item = ev.target.closest("[data-m]");
    if (!item) return;
    menu.hidden = true;
    const m = item.dataset.m;
    if (m === "reload") return loadDoc(true).then(() => toast("Reloaded from source."));
    if (m === "pdf") return window.open("/pdf", "_blank");
    if (m === "build") return buildPdf("");
    if (m === "about") return showAbout();
    if (m === "records") return showRecords();
    if (m === "instructions") {
      await openInstructions(false);
      return;
    }
    if (m === "marking") return toggleMarking();
    if (m === "violet") { S.showViolet = !S.showViolet; store("showViolet", S.showViolet); return updateMenuState(); }
    if (m === "clear-all") {
      if (!confirm("Remove all violet marking in the document? The text stays; only the \\AGTADD wrappers are removed.")) return;
      return clearMarking("document");
    }
    if (m === "annotated") { S.filtered = !S.filtered; $("#doc").classList.toggle("filtered", S.filtered); return updateMenuState(); }
  });

  $("#modal-body").addEventListener("click", async (ev) => {
    if (ev.target.closest("[data-archive-all]")) {
      const n = S.annotations.filter((x) => !x.archived).length;
      if (!confirmInline(ev.target, `Archive all ${n}?`)) return;
      await setArchived({ all: true }, true);
      return showRecords();
    }
    if (ev.target.closest("[data-toggle-archived]")) {
      S.showArchived = !S.showArchived;
      matchAnnotations();
      if (S.current) renderHistory();
      return showRecords();
    }
    const all = ev.target.closest("[data-wrap-all]");
    if (all) { $("#modal").hidden = true; return wrapUpAll(all.dataset.wrapAll.split("|")); }
    const w = ev.target.closest("[data-wrap]");
    if (w) { $("#modal").hidden = true; return startWrapup(w.dataset.wrap); }
    const v = ev.target.closest("[data-view]");
    if (v) return viewRecord(v.dataset.view);
    const ab = ev.target.closest("[data-approve-build]");
    if (ab) { $("#modal").hidden = true; return buildPdf(ab.dataset.approveBuild); }
    if (ev.target.closest("[data-cancel]")) { $("#modal").hidden = true; return; }
  });
  $("#modal-close").onclick = () => ($("#modal").hidden = true);
  $("#modal").addEventListener("click", (ev) => { if (ev.target.id === "modal") $("#modal").hidden = true; });
  document.addEventListener("keydown", (ev) => {
    if ((ev.metaKey || ev.ctrlKey) && ev.shiftKey && ev.key.toLowerCase() === "l") {
      // Works in the compose box too: it has the focus whenever the sidebar opens, and the
      // combination does nothing there. It does not type anything or move the cursor.
      if (S.current && S.current !== "document" && !$("#panel").hidden) { ev.preventDefault(); locateCurrent(); }
      return;
    }
    if (ev.key === "Escape") { if (!$("#modal").hidden) $("#modal").hidden = true; else if (!$("#menu").hidden) $("#menu").hidden = true; else closePanel(); }
  });
}

// Build PDF. galley asks before running a build command or latexmkrc it has not seen for this
// document (galley/settings.json travels with shared documents); the server enforces this too.
async function buildPdf(approve) {
  toast("Building the PDF…", 60000);
  const r = await api("/api/build", approve ? { approve } : {});
  if (r.needs_approval) {
    $("#toast").hidden = true;
    const a = r.needs_approval;
    const files = a.files.map((f) => `<p>It also runs <code>${esc(f.name)}</code>${/latexmkrc$/.test(f.name) ? " (latexmk runs this file as Perl code)" : ""}:</p>
      <pre class="no-math approve-code">${esc(f.text)}</pre>`).join("");
    showModal("Approve the build command", `<p>This document asks galley to build its PDF with a command, or files, that you have not approved yet.
      A document from someone else can run any program this way, so check it first.</p>
      <p>Command, run in the document's folder:</p><pre class="no-math approve-code">${esc(a.command)}</pre>
      ${a.command === a.default ? "" : `<p class="muted small">galley's default command is <code>${esc(a.default)}</code>; it is set in <code>galley/settings.json</code>.</p>`}
      ${files}
      <p class="muted small">galley remembers your approval for this document (in your own settings, not in the document) until the command or these files change.</p>
      <div class="modal-actions"><button class="primary" data-approve-build="${esc(a.fingerprint)}">Approve and build</button><button data-cancel>Cancel</button></div>`);
    return;
  }
  if (r.ok) { toast("PDF built; numbering refreshed."); loadDoc(true); }
  else showModal("Build failed", `<pre class="no-math">${esc(r.log || r.error)}</pre>`);
}

// About galley: the program's legal notices (the AGPL's "Appropriate Legal Notices").
function showAbout() {
  const a = (S.doc && S.doc.about) || {};
  showModal("About galley", `<p><strong>galley ${esc(a.version || "")}</strong></p>
    <p>galley by Michael Hermele</p>
    <p>Copyright (C) 2026 The Regents of the University of Colorado</p>
    <p>galley is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License,
    version 3 or (at your option) any later version, with an additional term under section 7(b): modified versions must keep the
    attribution “Based on galley by Michael Hermele”. There is no warranty. See the files LICENSE and NOTICE that come with galley.</p>
    <p>Source code: <a href="${esc(a.source || "")}" target="_blank" rel="noopener noreferrer">${esc(a.source || "")}</a></p>
    <p class="muted small">Includes MathJax 3.2.2 (Copyright The MathJax Consortium; Apache License 2.0).</p>`);
}

function showModal(title, html) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $("#modal").hidden = false;
}

wire();
watchReading();
loadDoc(false).then(() => { setTimeout(watch, 2000); resumeWrapups(); });
