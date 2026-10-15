"""Cost model and price refresh for custom multicolor prints.

costs/cost_model.xlsx is the model (live formulas, Jackson edits Inputs, Market and the Prices
override column). costs/refresh.py re-ingests test outputs, rebuilds the formula sheets, checks
them against the same numbers computed here in Python, and writes costs/pricing.json.
"""

from .defaults import CHANNELS, INPUTS, PRODUCTS

__all__ = ["CHANNELS", "INPUTS", "PRODUCTS"]
