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

  // Keep generated/formalization prose from handing an unbounded expression
  // to KaTeX. KaTeX renders synchronously, so both one expression and the sum
  // of all expressions on a Verso mount need independent limits.
  var MAX_LATEX_SOURCE = 4096;
  var MAX_VERSO_MATH_NODES = 300;
  var MAX_VERSO_PROSE_CHARS = 250000;
  var MAX_VERSO_TEX_CHARS = 24000;
  var VERSO_MATH_RENDERED_ATTR = "data-marathon-verso-math-rendered";

  function katexAvailable() {
    // KaTeX is "available" only if the script actually loaded AND its onerror
    // flag was never tripped. window.katex is the real signal; the *_FAILED
    // flags are belt-and-suspenders for slow/SRI-rejected loads.
    return (
      typeof window.katex !== "undefined" &&
      window.katex &&
      typeof window.katex.render === "function" &&
      !window.__KATEX_JS_FAILED &&
      !window.__KATEX_CSS_FAILED
    );
  }

  function dollarIsEscaped(src, index) {
    var slashes = 0;
    for (var i = index - 1; i >= 0 && src[i] === "\\"; i--) slashes++;
    return slashes % 2 === 1;
  }

  function nextUnescapedDollar(src, start) {
    for (var i = start; i < src.length; i++) {
      if (src[i] === "$" && !dollarIsEscaped(src, i)) return i;
    }
    return -1;
  }

  function nextDisplayClose(src, start) {
    var i = nextUnescapedDollar(src, start);
    while (i !== -1) {
      if (src[i + 1] === "$" && !dollarIsEscaped(src, i + 1)) return i;
      i = nextUnescapedDollar(src, i + 1);
    }
    return -1;
  }

  // Split a string into alternating [text, math, text, math, ...] segments by
  // $$...$$ (display) and $...$ (inline). Returns a list of {kind, value}
  // where kind is "text" | "inline" | "display". Escaped dollars and
  // malformed/unmatched delimiters remain literal text (we never drop
  // characters). Requiring non-whitespace immediately inside inline delimiters
  // also prevents ordinary prose such as "cost $5 and tax $10" from being
  // paired across its two currency markers.
  function tokenize(src) {
    src = String(src == null ? "" : src);
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
      if (src[i] === "$" && !dollarIsEscaped(src, i) && src[i + 1] === "$") {
        var end = nextDisplayClose(src, i + 2);
        if (end !== -1 && src.slice(i + 2, end).trim()) {
          flushText();
          out.push({ kind: "display", value: src.slice(i + 2, end) });
          i = end + 2;
        } else {
          buf += "$$";
          i += 2;
        }
      } else if (src[i] === "$" && !dollarIsEscaped(src, i)) {
        var first = src[i + 1] || "";
        var endI = nextUnescapedDollar(src, i + 1);
        var inlineSource = endI === -1 ? "" : src.slice(i + 1, endI);
        var validEnd = endI !== -1 && src[endI + 1] !== "$" &&
          inlineSource.indexOf("\n") === -1 && inlineSource.indexOf("\r") === -1 &&
          !/\s/.test(first) && !/\s/.test(src[endI - 1] || "");
        if (validEnd) {
          flushText();
          out.push({ kind: "inline", value: inlineSource });
          i = endI + 1;
        } else {
          // Consume only this marker. A later marker may still begin a valid
          // expression (for example, "cost $5; variable $x$").
          buf += "$";
          i += 1;
        }
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
  function reserveTypesetBudget(budget, chars) {
    if (!budget) return true;
    var used = Number(budget.texChars) || 0;
    if (chars > MAX_VERSO_TEX_CHARS - used) return false;
    budget.texChars = used + chars;
    return true;
  }

  function renderMath(parent, latex, display, typesetBudget) {
    latex = String(latex == null ? "" : latex);
    var withinLimits = latex.length <= MAX_LATEX_SOURCE &&
      reserveTypesetBudget(typesetBudget, latex.length);
    if (withinLimits && katexAvailable()) {
      try {
        var span = document.createElement("span");
        span.className = display ? "katex-display-wrap" : "katex-inline-wrap";
        window.katex.render(latex, span, {
          displayMode: !!display,
          throwOnError: false,
          trust: false,
          maxExpand: 1000,
          maxSize: 20,
          errorColor: "#b54",
        });
        parent.appendChild(span);
        return span;
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
    return code;
  }

  function markVersoMath(node, display) {
    if (!node || !node.classList) return;
    node.classList.add("verso-math");
    node.classList.add(display ? "verso-math-display" : "verso-math-inline");
    node.setAttribute(VERSO_MATH_RENDERED_ATTR, "1");
  }

  function generatedVersoMathElement(node) {
    if (!node || node.nodeType !== 1 || !node.classList) return false;
    return node.getAttribute(VERSO_MATH_RENDERED_ATTR) === "1" ||
      node.classList.contains("katex") ||
      node.classList.contains("verso-math") ||
      node.classList.contains("latex-fallback");
  }

  function insideGeneratedVersoMath(node, container) {
    for (var current = node; current && current !== container; current = current.parentElement) {
      if (generatedVersoMathElement(current)) return true;
    }
    return false;
  }

  function replaceVersoMathCode(node, typesetBudget) {
    if (!node || !node.parentNode) return false;
    var latex = node.textContent || "";
    var display = node.classList.contains("display");
    var fragment = document.createDocumentFragment();
    var rendered = renderMath(fragment, latex, display, typesetBudget);
    markVersoMath(rendered, display);
    node.parentNode.replaceChild(fragment, node);
    return true;
  }

  // Collect text nodes without crossing elements whose contents must remain
  // literal or whose click target should not be rewritten. This preserves the
  // Markdown DOM emitted by Verso instead of flattening a whole prose block.
  function collectVersoMathText(node, out, budget) {
    if (!node || out.length >= MAX_VERSO_MATH_NODES || budget.chars >= MAX_VERSO_PROSE_CHARS) {
      return;
    }
    if (node.nodeType === 3) {
      var value = node.nodeValue || "";
      if (value.length > MAX_VERSO_PROSE_CHARS - budget.chars) {
        budget.chars = MAX_VERSO_PROSE_CHARS;
        return;
      }
      budget.chars += value.length;
      if (value.indexOf("$") >= 0) out.push(node);
      return;
    }
    if (node.nodeType !== 1) return;
    if (generatedVersoMathElement(node)) return;
    var tag = String(node.tagName || "").toUpperCase();
    if (tag === "CODE" || tag === "PRE" || tag === "A" || tag === "KBD") return;
    for (var i = 0; i < node.childNodes.length; i++) {
      collectVersoMathText(node.childNodes[i], out, budget);
      if (out.length >= MAX_VERSO_MATH_NODES || budget.chars >= MAX_VERSO_PROSE_CHARS) break;
    }
  }

  function replaceVersoDelimitedMath(node, remaining, typesetBudget) {
    if (!node || !node.parentNode || remaining <= 0) return 0;
    var parts = tokenize(node.nodeValue || "");
    var mathCount = 0;
    for (var i = 0; i < parts.length; i++) {
      if (parts[i].kind !== "text") mathCount++;
    }
    if (!mathCount) return 0;

    var fragment = document.createDocumentFragment();
    var renderedCount = 0;
    for (var j = 0; j < parts.length; j++) {
      var part = parts[j];
      if (part.kind === "text") {
        fragment.appendChild(document.createTextNode(part.value));
      } else if (renderedCount < remaining) {
        var display = part.kind === "display";
        var rendered = renderMath(fragment, part.value, display, typesetBudget);
        markVersoMath(rendered, display);
        renderedCount++;
      } else {
        // Preserve delimiters verbatim once the per-mount budget is reached.
        var delimiter = part.kind === "display" ? "$$" : "$";
        fragment.appendChild(document.createTextNode(delimiter + part.value + delimiter));
      }
    }
    node.parentNode.replaceChild(fragment, node);
    return renderedCount;
  }

  // Render both Verso's explicit math nodes and ordinary $...$/$$...$$ text
  // found in prose. Explicit nodes arrive as inert <code class="math ...">
  // from the sanitizer. Raw delimiters are handled text-node by text-node and
  // never inside CODE/PRE/A/KBD, so existing markup and source are untouched.
  function renderVersoMath(container) {
    if (!container || !container.querySelectorAll || !container.getAttribute ||
        !container.setAttribute) return 0;
    // A repeat call must not walk KaTeX's generated MathML/HTML or spend the
    // limits a second time. Callers that replace the mount's contents clear
    // this marker before installing the new sanitized fragment.
    if (container.getAttribute(VERSO_MATH_RENDERED_ATTR) === "1") return 0;
    container.setAttribute(VERSO_MATH_RENDERED_ATTR, "1");
    var renderedCount = 0;
    var typesetBudget = { texChars: 0 };
    var explicit = container.querySelectorAll("code.math.inline, code.math.display");
    for (var i = 0; i < explicit.length && renderedCount < MAX_VERSO_MATH_NODES; i++) {
      if (insideGeneratedVersoMath(explicit[i], container)) continue;
      if (replaceVersoMathCode(explicit[i], typesetBudget)) renderedCount++;
    }

    var prose = container.querySelectorAll(".md-text, .verso-text");
    var candidates = [];
    var budget = { chars: 0 };
    for (var j = 0; j < prose.length && candidates.length < MAX_VERSO_MATH_NODES; j++) {
      collectVersoMathText(prose[j], candidates, budget);
      if (budget.chars >= MAX_VERSO_PROSE_CHARS) break;
    }
    for (var k = 0; k < candidates.length && renderedCount < MAX_VERSO_MATH_NODES; k++) {
      renderedCount += replaceVersoDelimitedMath(
        candidates[k], MAX_VERSO_MATH_NODES - renderedCount, typesetBudget
      );
    }
    return renderedCount;
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
    renderMarkdown: renderMarkdown, renderVersoMath: renderVersoMath,
  };
})();
