"""
Product & plan seed definitions.

Single source of truth used by:
  - msuite/install.py (fresh install)
  - msuite/patches/*   (in-place migrations)

Each module exports the full config for one product: features,
plans, and per-plan feature values.
"""
