"""Refresh the cost model and web prices.

    uv run python costs/refresh.py [--reason "why"] [--jobs-dir PATH] [--no-worktrees]

Reads costs/cost_model.xlsx (never changes Inputs or the Prices override column), re-ingests test
outputs, checks the Python numbers against the workbook formulas (LibreOffice headless), and
writes costs/pricing.json. See costs/README.md.
"""

import sys

from p2p_costs.refresh import main

if __name__ == "__main__":
    sys.exit(main())
