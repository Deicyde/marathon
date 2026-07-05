"""The prover subsystem — leaves-first, dependency-ordered ``sorry``-filling.

The prover proves the FROZEN statements a human has verified, bottom-up: it
orders the pending work by a dependency DAG so foundational API lemmas (the
leaves) are proved before the theorems that rest on them (up to the root —
eventually Stokes). Aristotle writes the proofs; the harness only sequences the
work, checks integrity, and steers.

See :mod:`marathon.prover.dag` for the DAG construction and ordering.
"""
