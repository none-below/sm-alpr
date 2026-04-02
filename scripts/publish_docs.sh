#!/bin/sh
# Publish outputs to docs/ for GitHub Pages
set -e

echo "Building docs..."

# Rebuild map and scoreboard
uv run python scripts/build_sharing_graph.py
uv run python scripts/build_map.py
uv run python scripts/build_scoreboard.py

# Copy findings
cp outputs/SMPD_ALPR_Findings.pdf docs/SMPD_ALPR_Findings.pdf
cp outputs/SMPD_ALPR_Findings.md docs/SMPD_ALPR_Findings.md

echo "Done. docs/ ready for GitHub Pages."
echo "  docs/index.html"
echo "  docs/sharing_map.html"
echo "  docs/scoreboard.html"
echo "  docs/SMPD_ALPR_Findings.pdf"
echo "  docs/SMPD_ALPR_Findings.md"
