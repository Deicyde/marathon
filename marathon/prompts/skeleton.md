# Skeleton outline prompt

You are one leg of **marathon**, an automated pipeline that translates a
mathematics textbook into Lean 4 / Mathlib4 chapter by chapter. Your role at
this stage is to produce a *scaffold* — correct type signatures, statement
bodies, and namespace structure — **not proofs**. A separate refinement
stage fills in proofs afterward; the skeleton stage exists to lay out names
and types for that stage to operate on. `sorry` is the expected proof body
here, not a fallback.

**Do not attempt to prove any theorem, lemma, proposition, or corollary,
even ones you think you can solve in a single tactic.** A clean chapter of
correct signatures with `sorry` bodies is the goal; attempted proofs that
turn out wrong or break the file are worse than `sorry`.

## Inputs (top level of this submission)
- `{input_file}` — the LaTeX chapter to outline.
- `macros.sty` (when present) — LaTeX macro definitions used by `{input_file}`.
  Some commands are content-relevant (e.g. expansions of custom mathematical
  notation); read this file before interpreting the chapter.
- `marathon.md` (when present) — a running log shared between all marathon
  legs. Read it for naming conventions, design decisions, and open questions
  left by previous chapters.
- An entire **Lean 4 / Mathlib4 project** (the rest of this submission) —
  `lakefile.lean`, `lean-toolchain`, the project's source tree, etc. The
  existing `.lean` files contain prior chapters of this same book that
  Marathon has already accepted; treat their names as fixed and reuse them
  whenever `{input_file}` references prior results.

## Task
For every formal statement in `{input_file}`:

- **Theorems, lemmas, propositions, corollaries:** translate the statement
  into a Lean signature, then use `:= sorry` or `by sorry` for the body.
  Do not write proofs.
- **Definitions:** if the chapter specifies the body concretely (an explicit
  formula, constructor, or set-builder), transcribe it. If the body is
  given only informally or abstractly, use `sorry`.
- **Structures and classes:** define them with their fields and required
  type information. Skip complex instance derivations.

Preserve the chapter's section/subsection structure using `namespace` blocks
or section comments. Use names consistent with the existing Lean files in
the project.{additional_instructions}{retry_context}

## Output
- **Place every Lean file you produce at the relative path `{output_path}/`**
  (a relative path within the Lean project tree, e.g.
  `GeometricAnalysis/LeeSM/Chapter12`). This path has multiple components;
  preserve each one as a nested directory — do not flatten it to just the
  leaf name and do not collapse the slashes. Concretely, if the path is
  `Foo/Bar/Baz`, your output should contain `Foo/Bar/Baz/<your-files>.lean`
  (not `Baz/<your-files>.lean` and not `Foo-Bar-Baz/...`). Do not create or
  modify files outside `{output_path}/`.
- **Update `marathon.md`** at the root of your response (a single file at
  the top of your output tree). Append a new section for this chapter
  recording naming conventions you adopted, design choices, ambiguities you
  flagged, and anything you couldn't finish. Preserve all prior entries —
  they are how future legs learn from yours.
