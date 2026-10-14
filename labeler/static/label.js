// Review screen for one item. Data comes from window.LABEL (see web.py).
// No framework: state lives in `S`, and render() redraws from it. Mark
// offsets are character offsets into a part's text, measured with a DOM
// Range from the start of the part's body, so the body must contain exactly
// the part's text (point marks are drawn with CSS ::before, which a Range
// doesn't count).
"use strict";
(function () {
  const L = window.LABEL;
  const ITEM = L.item;
  const KINDS = { w: "wrong", g: "good", h: "from_here", m: "missing" };
  const KIND_NAMES = { wrong: "wrong", good: "good", from_here: "went wrong from here",
                       missing: "missing" };

  // Every markable block: the item's parts, its context, and refs' parts.
  const blocks = [];
  function addBlocks(item, group, parts, refTitle) {
    parts.forEach((p, i) => blocks.push({ itemId: item.id, part: p, group, order: i,
                                          refTitle }));
  }
  addBlocks(ITEM, "parts", ITEM.parts);
  addBlocks(ITEM, "context", ITEM.context || []);
  L.refs.forEach(r => { if (r.id) {
    addBlocks(r, "ref", r.parts, r.title);
    addBlocks(r, "ref", r.context || [], r.title);
  } });
  const findBlock = (itemId, partId) =>
    blocks.find(b => b.itemId === itemId && b.part.id === partId);

  const prior = L.label;
  const S = {
    verdict: prior ? prior.verdict : null,
    fixable: prior ? !!prior.fixable : false,
    confidence: prior ? prior.confidence : null,
    note: prior ? prior.note || "" : "",
    marks: prior ? prior.marks.map(m => ({ ...m })) : [],
    sel: null,            // index into S.marks
    cursor: 0,            // index into blocks
    dirty: false,
    help: false,
  };

  // A mark is a reason for the verdict unless the labeler says otherwise.
  function defaultReason(kind, verdict) {
    if (verdict === "bad") return kind !== "good";
    if (verdict === "good") return kind === "good";
    return false;
  }
  const isReason = m => m.reason == null ? defaultReason(m.kind, S.verdict) : m.reason;

  // --- DOM helpers -----------------------------------------------------------
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  function toast(msg) {
    const t = h("div", { class: "toast" }, msg);
    document.body.append(t);
    setTimeout(() => t.remove(), 1800);
  }

  // --- where "went wrong from here" starts ------------------------------------
  function fromHere() {
    const m = S.marks.find(m => m.kind === "from_here" && m.item_id === ITEM.id && m.part);
    if (!m) return null;
    const b = findBlock(m.item_id, m.part);
    if (!b || b.group !== "parts") return null;
    return { order: b.order, part: m.part, offset: m.start == null ? 0 : m.start };
  }

  // --- rendering -------------------------------------------------------------
  function renderBody(blk, after) {
    // after: offset from which the text is dimmed (went wrong from here), or null
    const text = blk.part.text;
    const mine = S.marks.map((m, i) => ({ m, i }))
      .filter(x => x.m.item_id === blk.itemId && x.m.part === blk.part.id &&
                   x.m.start != null);
    const spans = mine.filter(x => x.m.end > x.m.start);
    const points = mine.filter(x => x.m.end === x.m.start);
    const cuts = new Set([0, text.length]);
    spans.forEach(x => { cuts.add(x.m.start); cuts.add(x.m.end); });
    if (after != null) cuts.add(after);
    const bounds = [...cuts].filter(c => c >= 0 && c <= text.length).sort((a, b) => a - b);
    const body = h("div", { class: "body" });
    const pointsAt = pos => points.filter(x => x.m.start === pos).forEach(x =>
      body.append(h("span", { class: `pt k-${x.m.kind}${S.sel === x.i ? " sel" : ""}`,
                              "data-marks": String(x.i), title: KIND_NAMES[x.m.kind] })));
    for (let j = 0; j + 1 < bounds.length; j++) {
      const a = bounds[j], b = bounds[j + 1];
      pointsAt(a);
      const cover = spans.filter(x => x.m.start <= a && x.m.end >= b);
      const seg = text.slice(a, b);
      const dim = after != null && a >= after;
      if (cover.length) {
        const top = cover[cover.length - 1];
        const cls = ["m", `k-${top.m.kind}`];
        if (cover.some(x => x.i === S.sel)) cls.push("sel");
        if (!cover.some(x => isReason(x.m))) cls.push("noreason");
        if (dim) cls.push("after-text");
        body.append(h("span", { class: cls.join(" "),
                                "data-marks": cover.map(x => x.i).join(","),
                                title: cover.map(x => KIND_NAMES[x.m.kind]).join(", ") }, seg));
      } else if (dim) {
        body.append(h("span", { class: "after-text" }, seg));
      } else {
        body.append(seg);
      }
    }
    pointsAt(text.length);
    body._blk = blk;
    return body;
  }

  function renderBlock(blk, idx, fh) {
    const whole = S.marks.map((m, i) => ({ m, i }))
      .filter(x => x.m.item_id === blk.itemId && x.m.part === blk.part.id &&
                   x.m.start == null);
    const cls = ["part", blk.part.kind];
    if (idx === S.cursor) cls.push("cursor");
    whole.forEach(x => cls.push(`whole-${x.m.kind}`));
    let after = null;
    if (fh && blk.group === "parts") {
      if (blk.order > fh.order) cls.push("after");
      else if (blk.part.id === fh.part) after = fh.offset;
    }
    const head = h("div", { class: "head" }, blk.part.label || blk.part.id,
      whole.map(x => h("span", { class: `badge k-${x.m.kind}${S.sel === x.i ? " sel" : ""}`,
                                 "data-marks": String(x.i) },
                       KIND_NAMES[x.m.kind] + (isReason(x.m) ? "" : " (not a reason)"))));
    const el = h("div", { class: cls.join(" "), "data-block": String(idx) }, head,
                 renderBody(blk, after));
    return el;
  }

  function describe(m) {
    if (!m.part) return "the whole item";
    const blk = findBlock(m.item_id, m.part);
    const where = (blk && blk.refTitle ? blk.refTitle + " › " : "") +
                  (blk ? blk.part.label || blk.part.id : m.part);
    if (m.start == null) return `${where} (whole part)`;
    if (m.start === m.end) {
      const t = blk ? blk.part.text : "";
      if (m.start === 0) return `${where}, from its start`;
      return `${where} at “…${t.slice(Math.max(0, m.start - 20), m.start)}⁁”`;
    }
    const t = blk ? blk.part.text.slice(m.start, m.end) : "";
    return `${where}: “${t.length > 60 ? t.slice(0, 57) + "…" : t}”`;
  }

  function seg(options, current, onpick) {
    return h("span", { class: "seg" }, options.map(([value, text, key]) =>
      h("button", { class: `v-${value}${current === value ? " on" : ""}`, type: "button",
                    title: key ? `key: ${key}` : null, onclick: () => onpick(value) }, text)));
  }

  function renderPanel() {
    const v = S.verdict;
    const panel = h("div", { class: "panel" },
      h("div", { class: "row" }, h("span", { class: "lbl" }, "Verdict, as it stands"),
        seg([["good", "👍 good", "1"], ["bad", "👎 bad", "2"], ["unsure", "unsure", "3"]], v,
            x => set({ verdict: x }))),
      v === "bad" && h("div", { class: "row" }, h("label", {},
        h("input", { type: "checkbox", checked: S.fixable,
                     onchange: e => set({ fixable: e.target.checked }) }),
        " good if the marked things were fixed ", h("kbd", {}, "f"))),
      h("div", { class: "row" }, h("span", { class: "lbl" }, "Confidence (optional)"),
        seg([[null, "–"], [1, "low"], [2, "medium"], [3, "high"]], S.confidence,
            x => set({ confidence: x }))),
      h("div", { class: "row" }, h("span", { class: "lbl" }, "Note"),
        h("textarea", { id: "note", rows: 3, placeholder: "anything worth saying (n)",
                        oninput: e => { S.note = e.target.value; S.dirty = true; } },
          S.note)),
      h("div", { class: "row" },
        h("span", { class: "lbl" }, `Marks (${S.marks.length})`),
        S.marks.length ? h("ul", { class: "marks" }, S.marks.map((m, i) =>
          h("li", { class: `k-${m.kind}${S.sel === i ? " sel" : ""}`,
                    onclick: e => { if (e.target.tagName === "LI" ||
                                        e.target.classList.contains("where")) select(i); } },
            h("div", { class: "ctl" },
              h("select", { onchange: e => changeKind(i, e.target.value) },
                Object.entries(KIND_NAMES).map(([k, n]) =>
                  h("option", { value: k, selected: k === m.kind }, n))),
              h("label", { title: "Is this why it's good or bad? (r)" },
                h("input", { type: "checkbox", checked: isReason(m),
                             onchange: e => { m.reason = e.target.checked; S.dirty = true;
                                              render(); } }), " reason"),
              h("button", { class: "del", type: "button", title: "delete (x)",
                            onclick: () => del(i) }, "✕")),
            h("div", { class: "where", title: describe(m) }, describe(m)),
            h("input", { type: "text", class: "mark-note", "data-i": String(i),
                         placeholder: "note", value: m.note || "",
                         oninput: e => { m.note = e.target.value; S.dirty = true; } }))))
          : h("div", { class: "keys" }, "Select text, then ", h("kbd", {}, "w"), " wrong · ",
              h("kbd", {}, "g"), " good · ", h("kbd", {}, "h"), " went wrong here · ",
              h("kbd", {}, "m"), " missing")),
      h("button", { class: "save", type: "button", disabled: !v,
                    onclick: () => save() }, "Save and next ⏎"),
      h("div", { class: "keys", style: "margin-top:8px" },
        h("a", { href: "#", onclick: e => { e.preventDefault(); save("skip"); } }, "skip"),
        " (s) · ", h("a", { href: L.queue }, "queue"), " (q) · ",
        h("a", { href: "#", onclick: e => { e.preventDefault(); toggleHelp(); } }, "keys"),
        " (?)"));
    return panel;
  }

  function renderHelp() {
    const rows = [
      ["select text, then w / g", "mark it wrong / good (no selection: the whole part)"],
      ["click, then h", "it went wrong from here (only one per item)"],
      ["click, then m", "something is missing here"],
      ["M", "something is missing from the whole item"],
      ["j / k", "move between parts (marks without a selection go here)"],
      [", / .", "previous / next mark"],
      ["r", "toggle whether the selected mark is a reason for the verdict"],
      ["x", "delete the selected mark"],
      ["n", "note on the selected mark (or the item)"],
      ["N", "note on the item"],
      ["1 / 2 / 3", "good / bad / unsure"],
      ["f", "bad, but good if the marked things were fixed"],
      ["c", "cycle confidence"],
      ["Enter", "save and go to the next item (Ctrl+Enter in a text box)"],
      ["s", "skip this item"],
      ["u / q", "back / queue"],
      ["Esc", "leave a text box, deselect"],
    ];
    return h("div", { class: "help", onclick: toggleHelp },
      h("h3", {}, "Keys"),
      h("table", {}, rows.map(([k, d]) => h("tr", {}, h("td", {}, h("kbd", {}, k)),
                                            h("td", {}, d)))),
      h("p", { class: "muted" }, "Marks that are reasons for the verdict have a solid " +
        "underline; the rest are dashed. By default, wrong / missing / went-wrong marks are " +
        "reasons for a bad verdict and good marks for a good one."));
  }

  function render() {
    const app = document.getElementById("app");
    const scroll = window.scrollY;
    const fh = fromHere();
    const groups = { parts: [], context: [], ref: {} };
    blocks.forEach((b, i) => {
      const el = renderBlock(b, i, fh);
      if (b.group === "ref") (groups.ref[b.refTitle] ||= []).push(el);
      else groups[b.group].push(el);
    });
    const itemMarks = S.marks.map((m, i) => ({ m, i })).filter(x => !x.m.part);
    const main = h("div", {},
      h("div", { class: "muted" }, h("a", { href: L.queue }, "← queue"), " · ",
        ITEM.source, ITEM.link ? [" · ", h("a", { href: ITEM.link }, "open")] : null),
      h("h2", {}, ITEM.title,
        itemMarks.map(x => [" ", h("span", { class: `badge k-${x.m.kind}${S.sel === x.i ? " sel" : ""}`,
                                             "data-marks": String(x.i) },
                                   KIND_NAMES[x.m.kind])])),
      ITEM.question && h("div", { class: "question" }, ITEM.question),
      !ITEM.current && h("div", { class: "banner" },
        "This is an older version; the item has changed since."),
      prior && h("div", { class: "banner muted" },
        `Editing your label from ${new Date(prior.t * 1000).toLocaleString()}.`),
      L.previous && h("div", { class: "banner muted" },
        `You labeled an older version of this: ${L.previous.verdict}` +
        (L.previous.marks.length ? `, ${L.previous.marks.length} marks` : "") +
        (L.previous.note ? ` (“${L.previous.note}”)` : "") + ". ",
        h("a", { href: `${L.prefix}/i/${L.previous.item_id}` }, "see it")),
      groups.parts,
      groups.context.length ? [h("div", { class: "group-title" }, "Context"),
                               groups.context] : null,
      Object.entries(groups.ref).map(([t, els]) =>
        [h("div", { class: "group-title" }, "Related: " + t), els]));
    app.replaceChildren(h("div", { class: "layout" }, main, renderPanel()),
                        S.help ? renderHelp() : "");
    window.scrollTo(0, scroll);
  }

  // --- actions ---------------------------------------------------------------
  function set(changes) { Object.assign(S, changes, { dirty: true }); render(); }
  function select(i) { S.sel = i; render(); }
  function del(i) {
    S.marks.splice(i, 1);
    S.sel = S.marks.length ? Math.min(i, S.marks.length - 1) : null;
    S.dirty = true;
    render();
  }
  function changeKind(i, kind) {
    const m = S.marks[i];
    if (kind === "from_here") S.marks = S.marks.filter(x => x === m || x.kind !== "from_here");
    m.kind = kind;
    S.sel = S.marks.indexOf(m);
    S.dirty = true;
    render();
  }
  function toggleHelp() { S.help = !S.help; render(); }

  function closestBody(node) {
    while (node && !(node.classList && node.classList.contains("body"))) node = node.parentNode;
    return node;
  }
  function offsetIn(body, node, off) {
    const r = document.createRange();
    r.setStart(body, 0);
    r.setEnd(node, off);
    return r.toString().length;
  }
  function selection() {
    const s = window.getSelection();
    if (!s || !s.rangeCount) return null;
    const r = s.getRangeAt(0);
    const b1 = closestBody(r.startContainer), b2 = closestBody(r.endContainer);
    if (!b1 && !b2) return null;
    if (b1 !== b2) return { cross: true };
    return { blk: b1._blk, start: offsetIn(b1, r.startContainer, r.startOffset),
             end: offsetIn(b1, r.endContainer, r.endOffset) };
  }

  function addMark(kind, wholeItem) {
    let m;
    if (wholeItem) {
      m = { item_id: ITEM.id, part: null, start: null, end: null };
    } else {
      const sel = selection();
      if (sel && sel.cross) return toast("Keep a mark inside one part");
      if (sel && sel.end > sel.start) {
        m = { item_id: sel.blk.itemId, part: sel.blk.part.id, start: sel.start, end: sel.end };
      } else if (sel && (kind === "from_here" || kind === "missing")) {
        m = { item_id: sel.blk.itemId, part: sel.blk.part.id, start: sel.start, end: sel.start };
      } else {
        const b = (sel && sel.blk) || blocks[S.cursor];
        if (!b) return;
        m = { item_id: b.itemId, part: b.part.id, start: null, end: null };
      }
    }
    const same = S.marks.findIndex(x => x.kind === kind && x.item_id === m.item_id &&
      x.part === m.part && x.start === m.start && x.end === m.end);
    if (same >= 0) { window.getSelection().removeAllRanges(); return select(same); }
    if (kind === "from_here") S.marks = S.marks.filter(x => x.kind !== "from_here");
    S.marks.push({ ...m, kind, reason: null, note: "" });
    S.sel = S.marks.length - 1;
    S.dirty = true;
    window.getSelection().removeAllRanges();
    render();
  }

  function moveCursor(d) {
    S.cursor = Math.max(0, Math.min(blocks.length - 1, S.cursor + d));
    render();
    const el = document.querySelector(`[data-block="${S.cursor}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
  }

  async function save(verdict) {
    verdict = verdict || S.verdict;
    if (!verdict) return toast("Pick a verdict first: 1 good, 2 bad, 3 unsure");
    const body = {
      item_id: ITEM.id, verdict, note: S.note,
      fixable: verdict === "bad" ? S.fixable : null,
      confidence: S.confidence,
      marks: S.marks.map(m => ({ item_id: m.item_id, part: m.part, start: m.start,
                                 end: m.end, kind: m.kind,
                                 reason: m.reason == null ? null : m.reason,
                                 note: m.note || "" })),
    };
    const resp = await fetch(L.api, { method: "POST", body: JSON.stringify(body),
                                      headers: { "Content-Type": "application/json" } });
    if (!resp.ok) return toast("Not saved: " + await resp.text());
    S.dirty = false;
    location.href = L.next;
  }

  // --- events ----------------------------------------------------------------
  document.addEventListener("click", e => {
    const hit = e.target.closest("[data-marks]");
    if (hit && window.getSelection().isCollapsed) {
      const ids = hit.dataset.marks.split(",").map(Number);
      const at = ids.indexOf(S.sel);
      S.sel = at >= 0 ? ids[(at + 1) % ids.length] : ids[ids.length - 1];
    }
    const blk = e.target.closest("[data-block]");
    if (blk) S.cursor = Number(blk.dataset.block);
    if (blk && !window.getSelection().isCollapsed) {
      // A text selection is in progress: re-rendering would drop it.
      document.querySelectorAll(".part.cursor").forEach(el => el.classList.remove("cursor"));
      blk.classList.add("cursor");
      return;
    }
    if (hit || blk) {
      // Keep the caret where it was clicked: re-rendering would lose it.
      const s = window.getSelection();
      const keep = s.rangeCount && s.isCollapsed && closestBody(s.anchorNode) ?
        { blk: closestBody(s.anchorNode)._blk, off: offsetIn(closestBody(s.anchorNode),
                                                              s.anchorNode, s.anchorOffset) }
        : null;
      render();
      if (keep) restoreCaret(keep.blk, keep.off);
    }
  });

  function restoreCaret(blk, off) {
    const body = [...document.querySelectorAll(".body")].find(b => b._blk === blk);
    if (!body) return;
    const walk = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    let n, left = off;
    while ((n = walk.nextNode())) {
      if (left <= n.length) {
        const r = document.createRange();
        r.setStart(n, left);
        r.collapse(true);
        const s = window.getSelection();
        s.removeAllRanges();
        s.addRange(r);
        return;
      }
      left -= n.length;
    }
  }

  document.addEventListener("keydown", e => {
    const tag = e.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      if (e.key === "Escape") e.target.blur();
      else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); }
      else if (e.key === "Enter" && e.target.classList.contains("mark-note")) {
        e.preventDefault(); e.target.blur();
      }
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key;
    let handled = true;
    if (KINDS[k]) addMark(KINDS[k]);
    else if (k === "M") addMark("missing", true);
    else if (k === "1") set({ verdict: "good" });
    else if (k === "2") set({ verdict: "bad" });
    else if (k === "3") set({ verdict: "unsure" });
    else if (k === "f") set({ verdict: "bad", fixable: S.verdict === "bad" ? !S.fixable : true });
    else if (k === "c") set({ confidence: S.confidence == null ? 1 :
                                          S.confidence === 3 ? null : S.confidence + 1 });
    else if (k === "j") moveCursor(1);
    else if (k === "k") moveCursor(-1);
    else if (k === "." && S.marks.length) select(S.sel == null ? 0 : (S.sel + 1) % S.marks.length);
    else if (k === "," && S.marks.length) select(S.sel == null ? S.marks.length - 1 :
                                                 (S.sel + S.marks.length - 1) % S.marks.length);
    else if (k === "r" && S.sel != null) {
      const m = S.marks[S.sel]; m.reason = !isReason(m); S.dirty = true; render();
    }
    else if ((k === "x" || k === "Delete" || k === "Backspace") && S.sel != null) del(S.sel);
    else if (k === "n" && S.sel != null)
      document.querySelector(`.mark-note[data-i="${S.sel}"]`).focus();
    else if (k === "n" || k === "N") document.getElementById("note").focus();
    else if (k === "Enter") save();
    else if (k === "s") save("skip");
    else if (k === "u") history.back();
    else if (k === "q") location.href = L.queue;
    else if (k === "?") toggleHelp();
    else if (k === "Escape") {
      if (S.help) S.help = false; else S.sel = null;
      window.getSelection().removeAllRanges();
      render();
    }
    else handled = false;
    if (handled) e.preventDefault();
  });

  window.addEventListener("beforeunload", e => { if (S.dirty) e.preventDefault(); });

  render();
})();
