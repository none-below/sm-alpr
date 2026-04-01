#!/usr/bin/env python3
"""
Generate an interactive Leaflet map of Flock ALPR sharing relationships.

Reads the sharing graph and geocodes agencies to California city coordinates.
Produces a standalone HTML file with:
  - Markers for each agency (sized by camera count)
  - Click an agency to see its sharing web (outbound lines)
  - Color-coded markers (private=red, out-of-state=orange, normal=blue)

Usage:
  uv run python scripts/build_map.py
  uv run python scripts/build_map.py --out outputs/sharing_map.html
"""

import argparse
import json
import sys
from pathlib import Path

DEFAULT_DATA_DIR = Path("assets/transparency.flocksafety.com")
DEFAULT_OUT = Path("docs/sharing_map.html")
REGISTRY_PATH = Path("assets/agency_registry.json")


# All geocoding comes from the agency registry. If an agency has no
# lat/lng in the registry, it will not appear on the map.
# To add coordinates, update assets/agency_registry.json.

def main():
    parser = argparse.ArgumentParser(description="Generate sharing map")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    # Load sharing graph
    graph_path = args.data_dir / ".sharing_graph_full.json"
    if not graph_path.exists():
        print("Run build_sharing_graph.py first.", file=sys.stderr)
        sys.exit(1)

    graph = json.loads(graph_path.read_text())

    # Load agency registry for classification data
    registry_path = Path("assets/agency_registry.json")
    registry_by_slug = {}
    alias_to_primary = {}  # alias_slug -> primary_slug
    if registry_path.exists():
        for e in json.loads(registry_path.read_text()):
            registry_by_slug[e["slug"]] = e
            for aka in e.get("also_known_as", []):
                alias_to_primary[aka] = e["slug"]

    # Validate: warn about graph slugs not in registry
    not_in_registry = []
    for slug in graph["agencies"]:
        if slug not in registry_by_slug and slug not in alias_to_primary:
            not_in_registry.append(slug)
    if not_in_registry:
        print(f"WARNING: {len(not_in_registry)} slugs in sharing graph not in registry:")
        for s in sorted(not_in_registry)[:10]:
            print(f"  {s}")
        if len(not_in_registry) > 10:
            print(f"  ... and {len(not_in_registry) - 10} more")
        print("Run build_agency_registry.py --merge to add them.\n")

    # Build map data — only from registry entries
    markers = []
    geocoded = 0
    ungeocodable = []

    for slug, data in graph["agencies"].items():
        # Skip alias slugs
        if slug in alias_to_primary:
            continue
        # Skip slugs not in registry
        if slug not in registry_by_slug:
            continue
        reg = registry_by_slug[slug]
        if not reg.get("lat") or not reg.get("lng"):
            ungeocodable.append(slug)
            continue
        geocoded += 1

        cameras = data.get("camera_count") or 0
        crawled = data.get("crawled", True)
        markers.append({
            "slug": slug,
            "lat": reg["lat"],
            "lng": reg["lng"],
            "cameras": cameras,
            "crawled": crawled,
            "outbound_count": data["outbound_count"],
            "inbound_count": data["inbound_count"],
            "retention_days": data.get("data_retention_days"),
            "outbound_slugs": data.get("outbound_slugs", []),
            "inbound_slugs": data.get("inbound_slugs", []),
        })

    # Resolve edges with coordinates
    slug_coords = {m["slug"]: (m["lat"], m["lng"]) for m in markers}

    # Build classification lookup for JS
    slug_info = {}
    for slug, reg in registry_by_slug.items():
        slug_info[slug] = {
            "public": reg.get("public"),
            "state": reg.get("state"),
            "name": reg.get("flock_name", slug),
            "role": reg.get("agency_role"),
            "type": reg.get("agency_type"),
            "crawled": reg.get("crawled", False),
            "notes": reg.get("notes"),
        }

    # Add alias entries pointing to primary's info
    for alias, primary in alias_to_primary.items():
        if primary in slug_info and alias not in slug_info:
            slug_info[alias] = slug_info[primary]

    # Build mismatch lookup
    mismatch_map = {}
    for m in graph.get("mismatches", []):
        agency = m.get("agency")
        partner = m.get("claims_shared_by") or m.get("shares_with")
        if agency and partner:
            mismatch_map.setdefault(agency, []).append(partner)
            mismatch_map.setdefault(partner, []).append(agency)

    print(f"Geocoded: {geocoded}/{len(graph['agencies'])}")
    if ungeocodable:
        print(f"Could not geocode: {', '.join(ungeocodable[:10])}")
        if len(ungeocodable) > 10:
            print(f"  ... and {len(ungeocodable) - 10} more")

    # Write data file
    docs_dir = args.out.parent
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "data").mkdir(exist_ok=True)
    (docs_dir / "js").mkdir(exist_ok=True)

    map_data = {
        "markers": markers,
        "coords": slug_coords,
        "agencyInfo": slug_info,
        "mismatches": mismatch_map,
    }
    (docs_dir / "data" / "map_data.json").write_text(json.dumps(map_data) + "\n")
    print(f"Data written to {docs_dir}/data/map_data.json")

    # Write JS — read template, inject cache-bust and marker count
    import time
    js_template = Path(__file__).parent / "map.js"
    js_code = js_template.read_text()
    js_code = js_code.replace("CACHE_BUST", str(int(time.time())))
    js_code = js_code.replace("MARKER_COUNT", str(len(markers)))
    (docs_dir / "js" / "map.js").write_text(js_code)
    print(f"JS written to {docs_dir}/js/map.js")

    # Write HTML shell
    html = _generate_html(len(markers))
    args.out.write_text(html)
    print(f"Map written to {args.out}")




def _generate_html(marker_count):
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Flock ALPR Sharing Map — California</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<style>
  body {{ margin: 0; font-family: -apple-system, sans-serif; }}
  #map {{ height: 100vh; width: 100%; }}
  .info-panel {{
    position: absolute; top: 10px; right: 10px; z-index: 1000;
    background: white; padding: 12px 16px; border-radius: 8px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2); max-width: 350px;
    max-height: 80vh; overflow-y: auto; font-size: 13px;
  }}
  .info-panel h3 {{ margin: 0 0 8px 0; }}
  .info-panel .stat {{ color: #666; margin: 2px 0; }}
  .info-panel .sharing-list {{ margin-top: 8px; }}
  .info-panel .sharing-list div {{ padding: 1px 0; }}
  .legend {{
    position: absolute; bottom: 20px; left: 10px; z-index: 1000;
    background: white; padding: 10px 14px; border-radius: 8px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2); font-size: 12px;
  }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; margin: 3px 0; }}
  .legend-dot {{ width: 12px; height: 12px; border-radius: 50%; }}
  .offmap-panel {{
    position: absolute; bottom: 20px; right: 10px; z-index: 1000;
    background: white; padding: 10px 14px; border-radius: 8px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2); font-size: 11px;
    max-height: 200px; overflow-y: auto; max-width: 280px;
  }}
  .offmap-panel h4 {{ margin: 0 0 6px 0; color: #dc2626; font-size: 12px; }}
  .offmap-panel div {{ padding: 1px 0; cursor: pointer; }}
  .edge-indicator {{
    position: absolute; z-index: 1000;
    background: #2563eb; color: white;
    border-radius: 16px; padding: 4px 10px;
    font-size: 12px; font-weight: bold;
    box-shadow: 0 2px 6px rgba(0,0,0,0.3);
    cursor: pointer; white-space: nowrap;
    display: none;
  }}
  .edge-indicator.has-violation {{ background: #dc2626; }}
  #edge-left {{ left: 10px; top: 50%; transform: translateY(-50%); }}
  #edge-right {{ right: 370px; top: 50%; transform: translateY(-50%); }}
  #edge-top {{ top: 10px; left: 50%; transform: translateX(-50%); }}
  #edge-bottom {{ bottom: 30px; left: 50%; transform: translateX(-50%); }}
</style>
</head>
<body>
<div id="map"></div>
<div class="info-panel" id="info">
  <h3>Flock ALPR Sharing Map</h3>
  <p class="stat">Click an agency to see its sharing web.</p>
  <p class="stat">{marker_count} agencies mapped.</p>
</div>
<div class="legend">
  <div class="legend-item"><div class="legend-dot" style="background:#2563eb"></div> Public agency</div>
  <div class="legend-item"><div class="legend-dot" style="background:#f97316"></div> Shares with violation entity</div>
  <div class="legend-item"><div class="legend-dot" style="background:#dc2626"></div> Violation entity (private/out-of-state/decommissioned)</div>
  <div class="legend-item"><div class="legend-dot" style="background:#06b6d4"></div> Selected</div>
  <div class="legend-item"><div class="legend-dot" style="background:#8b5cf6"></div> No transparency page found</div>
  <div class="legend-item"><div style="width:20px;height:2px;background:#2563eb"></div> Shares with (outbound)</div>
  <div class="legend-item"><div style="width:20px;height:2px;background:#16a34a;border-top:2px dashed #16a34a"></div> Receives from (inbound)</div>
</div>
<div id="edge-left" class="edge-indicator"></div>
<div id="edge-right" class="edge-indicator"></div>
<div id="edge-top" class="edge-indicator"></div>
<div id="edge-bottom" class="edge-indicator"></div>
<div class="offmap-panel" id="offmap"></div>
<script src="data/map_data.json" type="application/json" id="mapData"></script>
<script src="js/map.js"></script>
</body>
</html>"""


if __name__ == "__main__":
    main()
