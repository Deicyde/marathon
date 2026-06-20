/*
 * katex-fallback.js — graceful LaTeX rendering for the deck.
 *
 * local-first contract: the informal rendering contains LaTeX math. When the
 * KaTeX CDN loaded (window.katex present), we typeset inline ($...$) and
 * display ($$...$$) spans. When it did NOT load (offline, blocked CDN, SRI
 * mismatch), we MUST still show the math — never a blank — by surfacing the
 * raw LaTeX in a styled <code> span. This file exposes a single entry point,
 * window.MarathonLatex.render(container), that app.js calls after it injects
 * the informal text. It mutates nothing on the ledger and makes no network
 * calls of its own.
 */
(function () {
  "use strict";

  function katexAvailable() {
    // KaTeX is "available" only if the script actually loaded AND its onerror
    // flag was never tripped. window.katex is the real signal; the *_FAILED
    // flags are belt-and-suspenders for slow/SRI-rejected loads.
    return (
      typeof window.katex !== "undefined" &&
      window.katex &&
      typeof window.katex.render === "function" &&
      !window.__KATEX_JS_FAILED
    );
  }

  // Split a string into alternating [text, math, text, math, ...] segments by
  // $$...$$ (display) and $...$ (inline). Returns a list of {kind, value}
  // where kind is "text" | "inline" | "display". A trailing unmatched $ is
  // treated as literal text (we never drop characters).
  function tokenize(src) {
    var out = [];
    var i = 0;
    var n = src.length;
    var buf = "";
    function flushText() {
      if (buf.length) {
        out.push({ kind: "text", value: buf });
        buf = "";
      }
    }
    while (i < n) {
      if (src[i] === "$" && src[i + 1] === "$") {
        var end = src.indexOf("$$", i + 2);
        if (end === -1) {
          buf += src.slice(i);
          break;
        }
        flushText();
        out.push({ kind: "display", value: src.slice(i + 2, end) });
        i = end + 2;
      } else if (src[i] === "$") {
        var endI = src.indexOf("$", i + 1);
        if (endI === -1) {
          buf += src.slice(i);
          break;
        }
        flushText();
        out.push({ kind: "inline", value: src.slice(i + 1, endI) });
        i = endI + 1;
      } else {
        buf += src[i];
        i += 1;
      }
    }
    flushText();
    return out;
  }

  // Render one math segment into `parent`. Uses KaTeX when available; on ANY
  // failure (unavailable, or a KaTeX parse error) falls back to a styled raw
  // code span carrying the original LaTeX. Never throws to the caller.
  function renderMath(parent, latex, display) {
    if (katexAvailable()) {
      try {
        var span = document.createElement("span");
        span.className = display ? "katex-display-wrap" : "katex-inline-wrap";
        window.katex.render(latex, span, {
          displayMode: !!display,
          throwOnError: false,
          errorColor: "#b54",
        });
        parent.appendChild(span);
        return;
      } catch (e) {
        // fall through to raw fallback
      }
    }
    var code = document.createElement("code");
    code.className = display
      ? "latex-fallback latex-fallback-display"
      : "latex-fallback latex-fallback-inline";
    code.textContent = (display ? "$$" : "$") + latex + (display ? "$$" : "$");
    code.title = "raw LaTeX (math renderer unavailable — offline-safe fallback)";
    parent.appendChild(code);
  }

  // UNIFIED inline renderer: math ($$…$$, $…$) AND markdown (**bold**, `code`,
  // *italic*) in ONE left-to-right pass. A markdown span MAY contain math — the
  // #98 case `**multilinear over $C^\infty(M)$**` renders as a bold node whose
  // content is rendered RECURSIVELY (so the inner $…$ is typeset). Inside $…$,
  // text is opaque LaTeX (no markdown). Italic needs non-space just inside the
  // *…* so a plain "a * b" isn't mistaken for emphasis. DOM nodes only
  // (createElement + textContent, never innerHTML) so the LLM-rendered text
  // can't inject markup. An unclosed/unmatched delimiter is emitted verbatim.
  var _RICH_RE =
    /\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$|\*\*([\s\S]+?)\*\*|`([^`]+?)`|\*(\S(?:[^*\n]*?\S)?)\*/;
  function renderRich(parent, text) {
    var rest = text == null ? "" : String(text);
    while (rest) {
      var m = _RICH_RE.exec(rest);
      if (!m) {
        parent.appendChild(document.createTextNode(rest));
        return;
      }
      if (m.index > 0) {
        parent.appendChild(document.createTextNode(rest.slice(0, m.index)));
      }
      if (m[1] != null) {
        renderMath(parent, m[1], true);            // $$display$$
      } else if (m[2] != null) {
        renderMath(parent, m[2], false);           // $inline$
      } else if (m[3] != null) {
        var b = document.createElement("strong");  // **bold** (may hold math)
        renderRich(b, m[3]);
        parent.appendChild(b);
      } else if (m[4] != null) {
        var c = document.createElement("code");    // `code` (opaque)
        c.textContent = m[4];
        parent.appendChild(c);
      } else {
        var e = document.createElement("em");       // *italic* (may hold math)
        renderRich(e, m[5]);
        parent.appendChild(e);
      }
      rest = rest.slice(m.index + m[0].length);
    }
  }

  // Public: render the LaTeX+markdown text of `container.dataset.latexSource`
  // (or the container's textContent if no dataset) into rich nodes in place.
  function render(container) {
    if (!container) return;
    var src =
      container.dataset && typeof container.dataset.latexSource === "string"
        ? container.dataset.latexSource
        : container.textContent || "";
    container.textContent = "";
    var fellBack = !katexAvailable();
    renderRich(container, src);
    if (fellBack) {
      container.classList.add("latex-degraded");
      container.setAttribute(
        "data-latex-degraded",
        "math renderer offline — showing raw LaTeX"
      );
    }
  }

  // Block-level MARKDOWN renderer for the refine-reviewer prompts (and any rich
  // markdown text): ATX headers (#…), fenced ``` code blocks (verbatim), bullet
  // (-, *) and ordered (1.) lists, and paragraphs. Inline content runs through
  // renderRich, so **bold** / `code` / *italic* / $math$ all work inside. Built
  // as DOM nodes (no innerHTML). `container` is cleared and filled in place.
  var _FENCE_RE = /^\s*```/;
  var _ATX_RE = /^(#{1,6})\s+(.*)$/;
  var _ULI_RE = /^\s*[-*]\s+(.*)$/;
  var _OLI_RE = /^\s*\d+\.\s+(.*)$/;
  function renderMarkdown(container, src) {
    if (!container) return;
    container.textContent = "";
    var lines = String(src == null ? "" : src).split("\n");
    var i = 0;
    function appendList(items, ordered) {
      var list = document.createElement(ordered ? "ol" : "ul");
      list.className = "md-list";
      items.forEach(function (it) {
        var li = document.createElement("li");
        renderRich(li, it);
        list.appendChild(li);
      });
      container.appendChild(list);
    }
    while (i < lines.length) {
      var line = lines[i];
      if (_FENCE_RE.test(line)) {                       // ``` fenced code block
        var code = [];
        i++;
        while (i < lines.length && !_FENCE_RE.test(lines[i])) { code.push(lines[i]); i++; }
        i++;                                            // consume closing fence
        var pre = document.createElement("pre");
        pre.className = "md-code";
        var c = document.createElement("code");
        c.textContent = code.join("\n");
        pre.appendChild(c);
        container.appendChild(pre);
        continue;
      }
      var h = _ATX_RE.exec(line);
      if (h) {                                          // # ATX header
        var lvl = h[1].length;
        var hd = document.createElement("h" + Math.min(lvl + 2, 6));
        hd.className = "md-h md-h" + lvl;
        renderRich(hd, h[2]);
        container.appendChild(hd);
        i++;
        continue;
      }
      if (_ULI_RE.test(line)) {                          // - / * bullet list
        var u = [];
        while (i < lines.length && _ULI_RE.test(lines[i])) { u.push(_ULI_RE.exec(lines[i])[1]); i++; }
        appendList(u, false);
        continue;
      }
      if (_OLI_RE.test(line)) {                          // 1. ordered list
        var o = [];
        while (i < lines.length && _OLI_RE.test(lines[i])) { o.push(_OLI_RE.exec(lines[i])[1]); i++; }
        appendList(o, true);
        continue;
      }
      if (line.trim() === "") { i++; continue; }         // blank line
      var para = [];                                     // paragraph
      while (i < lines.length && lines[i].trim() !== "" &&
             !_FENCE_RE.test(lines[i]) && !_ATX_RE.test(lines[i]) &&
             !_ULI_RE.test(lines[i]) && !_OLI_RE.test(lines[i])) {
        para.push(lines[i]); i++;
      }
      var p = document.createElement("p");
      p.className = "md-p";
      renderRich(p, para.join(" "));
      container.appendChild(p);
    }
  }

  window.MarathonLatex = {
    render: render, katexAvailable: katexAvailable, tokenize: tokenize,
    renderMarkdown: renderMarkdown,
  };
})();
