# Convenience targets for the SMPD ALPR project.
#
#   make help            Show this list.
#
# Testing:
#   make test            Build site/data artifacts if missing, then run pytest.
#                        Bare `pytest` fails in a fresh worktree because the
#                        site/data artifacts it reads are gitignored; this
#                        builds them first.
#   make build           Force a full rebuild of all site/data artifacts and
#                        the findings PDF (run after editing a generator).
#
# Daily PRA workflow:
#   make pra-update      Pull new portal activity, OCR sidecars, seed stubs,
#                        and rebuild the registry. The one-shot you usually want.
#   make pra-scrape      Run pra_download.py against every known PRA folder.
#   make pra-scrape-one ID=W012XXX-MMDDYY
#                        Scrape a single PRA.
#   make pra-ocr         Generate OCR sidecars for any new PDFs.
#   make pra-init        Create stub metadata.json files for new W-folders.
#   make pra-build       Regenerate docs/data/pra_registry.json.
#
# Browsing:
#   make serve           Serve docs/ locally on http://127.0.0.1:8765
#   make serve-public    Serve docs/ on all interfaces (LAN-visible)
#
# Hygiene:
#   make clean           Remove generated site/data artifacts (forces the next
#                        `make build` / `make test` to rebuild from scratch).

.PHONY: help build test pra-update pra-scrape pra-scrape-one pra-ocr pra-init \
        pra-build serve serve-public clean

# Gitignored artifacts produced by `make build` and read by the test suite.
# `make build` regenerates exactly these; `make clean` removes exactly these
# (plus the stamp), so `make clean test` is a guaranteed-fresh run.
BUILD_FILES := \
	docs/sharing_map.html \
	docs/js/map.js \
	docs/data/map_data.json \
	docs/data/scoreboard_data.json \
	docs/data/report_data.json \
	docs/data/justifications.json \
	docs/data/agency_changelog.json \
	docs/data/dashboard.json \
	assets/transparency.flocksafety.com/.sharing_graph_full.json
BUILD_DIRS := docs/data/audit docs/data/history
# Touched on a successful `make build`. `make test` depends on it, so the
# build runs only when the stamp is absent (fresh worktree or after clean) —
# not on every test run. Edit a generator? Run `make build` to force a rebuild.
BUILD_STAMP := .make/build.stamp

help:
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} \
	      /^[a-zA-Z_-]+:.*?##/ {printf "  %-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST) \
	      2>/dev/null || sed -n '2,/^$$/p' Makefile | sed 's/^# \{0,1\}//'

build: ## Force-rebuild all site/data artifacts + findings PDF
	uv run python scripts/build_sharing_graph.py
	uv run python scripts/build_history.py
	uv run python scripts/build_audit_log.py
	uv run python scripts/build_map.py
	uv run python scripts/build_scoreboard.py
	uv run python scripts/build_report_data.py
	uv run python scripts/build_justifications.py
	uv run python scripts/build_dashboard.py
	uv run python scripts/md_to_pdf.py
	@mkdir -p $(dir $(BUILD_STAMP))
	@touch $(BUILD_STAMP)

# Real-file target with no prerequisites: Make runs its recipe only when the
# stamp is missing. That recipe rebuilds everything (and re-touches the stamp).
$(BUILD_STAMP):
	@$(MAKE) build

test: $(BUILD_STAMP) ## Build artifacts if missing, then run the full test suite
	uv run pytest

pra-update: pra-scrape pra-ocr pra-init pra-build ## Full local PRA refresh
	@echo ""
	@echo "Done. Review new/modified files with: git status -s assets/san-mateo-public-records/"
	@echo "Then edit any TODO metadata.json files and commit."

pra-scrape: ## Auto-login, then scrape all SMPD portal PRAs
	uv run python scripts/pra_download.py --auto-login
	uv run python scripts/pra_download.py --all

pra-scrape-one: ## Scrape one PRA: make pra-scrape-one ID=W012XXX-MMDDYY
	@test -n "$(ID)" || (echo "usage: make pra-scrape-one ID=W012XXX-MMDDYY" && exit 1)
	uv run python scripts/pra_download.py $(ID)

pra-ocr: ## Generate OCR sidecars for new PRA PDFs
	uv run python scripts/ocr_sidecar.py --dir assets/san-mateo-public-records

pra-init: ## Seed stub metadata.json for new W-folders (no overwrite)
	uv run python scripts/build_pra_registry.py --init

pra-build: ## Regenerate docs/data/pra_registry.json, pra_productivity.json, pra_ledger.json
	uv run python scripts/build_pra_registry.py
	uv run python scripts/build_pra_productivity.py
	uv run python scripts/build_pra_ledger.py

serve: ## Serve docs/ locally on http://127.0.0.1:8765
	cd docs && python3 -m http.server 8765 --bind 127.0.0.1

serve-public: ## Serve docs/ on all interfaces (LAN-visible)
	cd docs && python3 -m http.server 8765

clean: ## Remove generated site/data + PRA artifacts
	rm -f $(BUILD_FILES) $(BUILD_STAMP) \
	      docs/data/pra_registry.json docs/data/pra_productivity.json \
	      docs/data/pra_ledger.json
	rm -rf $(BUILD_DIRS)
	@echo "Removed generated artifacts. 'make build' rebuilds the site/data;"
	@echo "'make pra-build' rebuilds the PRA registry."
