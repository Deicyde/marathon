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

  // Render the inline MARKDOWN of a (non-math) text segment into `parent`:
  // **bold** -> <strong>, `code` -> <code>, *italic* -> <em>. Built as DOM
  // nodes with textContent (never innerHTML), so the LLM-rendered informal text
  // can't inject markup. Math has already been tokenized out, so a `$` won't
  // appear here. Unmatched `*`/`` ` `` are emitted verbatim.
  // Italic requires non-space just inside the *…* (standard markdown), so a
  // plain "a * b" is NOT mistaken for emphasis. Bold + `code` are unambiguous.
  var _MD_INLINE_RE = /\*\*([^*]+?)\*\*|`([^`]+?)`|\*(\S(?:[^*\n]*?\S)?)\*/g;
  function appendInlineMarkdown(parent, text) {
    var last = 0, m;
    _MD_INLINE_RE.lastIndex = 0;
    while ((m = _MD_INLINE_RE.exec(text)) !== null) {
      if (m.index > last) {
        parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      }
      var tag = m[1] != null ? "strong" : (m[2] != null ? "code" : "em");
      var inner = m[1] != null ? m[1] : (m[2] != null ? m[2] : m[3]);
      var node = document.createElement(tag);
      node.textContent = inner;
      parent.appendChild(node);
      last = _MD_INLINE_RE.lastIndex;
    }
    if (last < text.length) {
      parent.appendChild(document.createTextNode(text.slice(last)));
    }
  }

  // Public: render the LaTeX-bearing text of `container.dataset.latexSource`
  // (or the container's textContent if no dataset) into rich nodes in place.
  function render(container) {
    if (!container) return;
    var src =
      container.dataset && typeof container.dataset.latexSource === "string"
        ? container.dataset.latexSource
        : container.textContent || "";
    container.textContent = "";
    var fellBack = !katexAvailable();
    var segments = tokenize(src);
    segments.forEach(function (seg) {
      if (seg.kind === "text") {
        appendInlineMarkdown(container, seg.value);
      } else {
        renderMath(container, seg.value, seg.kind === "display");
      }
    });
    if (fellBack) {
      container.classList.add("latex-degraded");
      container.setAttribute(
        "data-latex-degraded",
        "math renderer offline — showing raw LaTeX"
      );
    }
  }

  window.MarathonLatex = { render: render, katexAvailable: katexAvailable, tokenize: tokenize };
})();
