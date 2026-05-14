# Convenience targets for the SMPD ALPR project.
#
#   make help            Show this list.
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
#   make clean           Remove generated docs/data artifacts.

.PHONY: help pra-update pra-scrape pra-scrape-one pra-ocr pra-init pra-build \
        serve serve-public clean

help:
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} \
	      /^[a-zA-Z_-]+:.*?##/ {printf "  %-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST) \
	      2>/dev/null || sed -n '2,/^$$/p' Makefile | sed 's/^# \{0,1\}//'

pra-update: pra-scrape pra-ocr pra-init pra-build ## Full local PRA refresh
	@echo ""
	@echo "Done. Review new/modified files with: git status -s assets/san-mateo-public-records/"
	@echo "Then edit any TODO metadata.json files and commit."

pra-scrape: ## Scrape all SMPD portal PRAs
	uv run python scripts/pra_download.py --all

pra-scrape-one: ## Scrape one PRA: make pra-scrape-one ID=W012XXX-MMDDYY
	@test -n "$(ID)" || (echo "usage: make pra-scrape-one ID=W012XXX-MMDDYY" && exit 1)
	uv run python scripts/pra_download.py $(ID)

pra-ocr: ## Generate OCR sidecars for new PRA PDFs
	uv run python scripts/ocr_sidecar.py --dir assets/san-mateo-public-records

pra-init: ## Seed stub metadata.json for new W-folders (no overwrite)
	uv run python scripts/build_pra_registry.py --init

pra-build: ## Regenerate docs/data/pra_registry.json
	uv run python scripts/build_pra_registry.py

serve: ## Serve docs/ locally on http://127.0.0.1:8765
	cd docs && python3 -m http.server 8765 --bind 127.0.0.1

serve-public: ## Serve docs/ on all interfaces (LAN-visible)
	cd docs && python3 -m http.server 8765

clean: ## Remove generated docs/data artifacts
	rm -f docs/data/pra_registry.json
	@echo "Removed docs/data/pra_registry.json. Run 'make pra-build' to regenerate."
