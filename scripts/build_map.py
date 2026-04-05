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

    # Add Flock Safety vendor as an implicit outbound target for San Mateo PD.
    # We've verified SMPD's Flock MSA contains §5.3 disclosure authority.
    # Other agencies likely have similar contracts but we haven't confirmed yet.
    FLOCK_53_AGENCIES = ["san-mateo-ca-pd"]
    flock_reg = registry_by_slug.get("flock-safety-vendor")
    if flock_reg and flock_reg.get("lat") and flock_reg.get("lng"):
        existing_slugs = {m["slug"] for m in markers}
        flock_inbound = [s for s in FLOCK_53_AGENCIES if s in existing_slugs]

        if flock_inbound:
            markers.append({
                "slug": "flock-safety-vendor",
                "lat": flock_reg["lat"],
                "lng": flock_reg["lng"],
                "cameras": 0,
                "crawled": False,
                "outbound_count": 0,
                "inbound_count": len(flock_inbound),
                "retention_days": None,
                "outbound_slugs": [],
                "inbound_slugs": flock_inbound,
            })
            geocoded += 1
            # Add Flock to each confirmed agency's outbound
            for m in markers:
                if m["slug"] in FLOCK_53_AGENCIES:
                    m["outbound_slugs"].append("flock-safety-vendor")
                    m["outbound_count"] += 1

    # Compute inbound for all markers from outbound data.
    # This fills in inbound even for agencies whose portal doesn't have
    # the "Organizations sharing their data with" section.
    marker_by_slug = {m["slug"]: m for m in markers}
    computed_inbound = {m["slug"]: set() for m in markers}
    for m in markers:
        for target in m.get("outbound_slugs", []):
            if target in computed_inbound:
                computed_inbound[target].add(m["slug"])
    for m in markers:
        existing = set(m.get("inbound_slugs", []))
        inferred_in = computed_inbound.get(m["slug"], set()) - existing
        merged = sorted(existing | inferred_in)
        m["inbound_slugs"] = merged
        m["inbound_count"] = len(merged)
        m["inferred_inbound"] = sorted(inferred_in)

    # Compute inferred outbound from other agencies' inbound claims.
    # If Fort Bragg's portal says "Alameda shares with me," Alameda
    # should show Fort Bragg as an outbound target (inferred).
    computed_outbound = {m["slug"]: set() for m in markers}
    for m in markers:
        for source in m.get("inbound_slugs", []):
            if source in computed_outbound:
                computed_outbound[source].add(m["slug"])
    for m in markers:
        existing = set(m.get("outbound_slugs", []))
        inferred_out = computed_outbound.get(m["slug"], set()) - existing
        merged = sorted(existing | inferred_out)
        m["outbound_slugs"] = merged
        m["outbound_count"] = len(merged)
        m["inferred_outbound"] = sorted(inferred_out)

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
            "crawled_date": reg.get("crawled_date"),
            "notes": reg.get("notes"),
            "ag_lawsuit": reg.get("ag_lawsuit", False),
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

    # Compute indirect violations: if A shares with B, and B shares with
    # a violation entity V, then A has an indirect violation "V via B"
    def is_violation_entity(slug):
        r = registry_by_slug.get(slug, {})
        if r.get("public") is False:
            return True
        if r.get("state") and r["state"] != "CA":
            return True
        if r.get("agency_type") in ("federal", "fusion_center", "decommissioned", "test"):
            return True
        return False

    # Build outbound lookup from graph
    outbound_by_slug = {}
    for slug, data in graph["agencies"].items():
        outbound_by_slug[slug] = data.get("outbound_slugs", [])

    # For each agency, find indirect violations (depth 1: via intermediaries)
    indirect_violations = {}  # slug -> [{"violation": v, "via": intermediary}]
    for slug, data in graph["agencies"].items():
        indirects = []
        direct_violations = set()
        for target in data.get("outbound_slugs", []):
            if is_violation_entity(target):
                direct_violations.add(target)
        # Check intermediaries
        for target in data.get("outbound_slugs", []):
            if target in outbound_by_slug:
                for second_hop in outbound_by_slug[target]:
                    if is_violation_entity(second_hop) and second_hop not in direct_violations:
                        indirects.append({
                            "violation": second_hop,
                            "via": target,
                            "via_name": registry_by_slug.get(target, {}).get("flock_name", target),
                            "violation_name": registry_by_slug.get(second_hop, {}).get("flock_name", second_hop),
                        })
        if indirects:
            # Deduplicate by violation slug
            seen = set()
            deduped = []
            for iv in indirects:
                if iv["violation"] not in seen:
                    seen.add(iv["violation"])
                    deduped.append(iv)
            indirect_violations[slug] = deduped

    indirect_count = sum(len(v) for v in indirect_violations.values())
    print(f"Indirect violations: {indirect_count} across {len(indirect_violations)} agencies")

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
        "indirectViolations": indirect_violations,
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

    # Write HTML shell with cache-bust on JS reference
    html = _generate_html(len(markers)).replace("JSCACHEBUST", str(int(time.time())))
    args.out.write_text(html)
    print(f"Map written to {args.out}")




def _generate_html(marker_count):
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Flock ALPR Sharing Map — California</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com https://gc.zgo.at; style-src 'self' 'unsafe-inline' https://unpkg.com; img-src 'self' https://*.basemaps.cartocdn.com https://*.tile.openstreetmap.org data:; connect-src 'self' https://*.basemaps.cartocdn.com https://*.tile.openstreetmap.org https://unpkg.com https://nominatim.openstreetmap.org https://gc.zgo.at https://none-below.goatcounter.com;">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H" crossorigin="anonymous" />
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" integrity="sha384-pmjIAcz2bAn0xukfxADbZIb3t8oRT9Sv0rvO+BR5Csr6Dhqq+nZs59P0pPKQJkEV" crossorigin="anonymous" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH" crossorigin="anonymous"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js" integrity="sha384-eXVCORTRlv4FUUgS/xmOyr66XBVraen8ATNLMESp92FKXLAMiKkerixTiBvXriZr" crossorigin="anonymous"></script>
<style>
  body {{ margin: 0; font-family: -apple-system, sans-serif; }}
  #map {{ height: 100vh; width: 100%; }}
  .back-link {{
    position: absolute; top: 10px; left: 60px; z-index: 1001;
    background: white; padding: 6px 12px; border-radius: 6px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.2); font-size: 13px;
    color: #2563eb; text-decoration: none; font-weight: 500;
  }}
  .back-link:hover {{ background: #eff6ff; }}
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
  #search-box {{
    position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
    z-index: 1001; display: flex; gap: 0;
  }}
  #search-input {{
    width: 260px; padding: 8px 12px; border: 2px solid #2563eb;
    border-radius: 8px 0 0 8px; font-size: 14px; outline: none;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15);
  }}
  #search-input::placeholder {{ color: #9ca3af; }}
  #search-btn {{
    padding: 8px 12px; background: #2563eb; color: white; border: 2px solid #2563eb;
    border-left: none; border-radius: 0 8px 8px 0; font-size: 14px; cursor: pointer;
  }}
  #search-results {{
    position: absolute; top: 42px; left: 50%; transform: translateX(-50%);
    z-index: 1002; background: white; border-radius: 8px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.2); max-height: 260px;
    overflow-y: auto; width: 310px; display: none; font-size: 13px;
  }}
  #search-results div {{
    padding: 8px 12px; cursor: pointer; border-bottom: 1px solid #f3f4f6;
  }}
  #search-results div:hover {{ background: #eff6ff; }}
  #search-results div:last-child {{ border-bottom: none; }}
  #search-results .sr-tag {{ color: #6b7280; font-size: 11px; }}
  .info-toggle {{
    display: none; position: absolute; top: 10px; right: 10px; z-index: 1001;
    background: #2563eb; color: white; border: none; border-radius: 8px;
    padding: 8px 12px; font-size: 13px; font-weight: bold; cursor: pointer;
    box-shadow: 0 2px 6px rgba(0,0,0,0.3);
  }}
  .legend {{
    position: absolute; bottom: 20px; left: 10px; z-index: 1000;
    background: white; padding: 10px 14px; border-radius: 8px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2); font-size: 12px;
  }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; margin: 3px 0; }}
  .legend-dot {{ width: 12px; height: 12px; border-radius: 50%; }}
  @media (max-width: 768px) {{
    .info-panel {{
      top: auto; bottom: 0; left: 0; right: 0;
      max-width: 100%; max-height: 40vh;
      border-radius: 12px 12px 0 0;
      font-size: 12px;
    }}
    .info-panel.collapsed {{
      max-height: 0; padding: 0; overflow: hidden;
    }}
    .info-toggle {{
      display: block;
    }}
    .legend {{
      display: none;
    }}
    #violation-banner {{
      font-size: 11px !important;
      padding: 6px 10px !important;
      left: 10px !important;
      right: 10px !important;
      transform: none !important;
      white-space: normal !important;
    }}
    #search-box {{
      left: 10px; right: 10px; transform: none;
    }}
    #search-input {{ width: 100%; box-sizing: border-box; font-size: 13px; }}
    #search-results {{ left: 10px; right: 10px; transform: none; width: auto; }}
  }}
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
  #violation-banner {{
    position: absolute; top: 58px; left: 50%; transform: translateX(-60%);
    z-index: 1000; background: #dc2626; color: white;
    padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: bold;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3); display: none; white-space: nowrap;
  }}
  #edge-left {{ left: 10px; top: 50%; transform: translateY(-50%); }}
  #edge-right {{ right: 370px; top: 50%; transform: translateY(-50%); }}
  #edge-top {{ top: 10px; left: 50%; transform: translateX(-50%); }}
  #edge-bottom {{ bottom: 30px; left: 50%; transform: translateX(-50%); }}
</style>
</head>
<body>
<div id="map"></div>
<a class="back-link" href="index.html">&larr; Back to investigation</a>
<div id="search-box">
  <input type="text" id="search-input" placeholder="Search city, agency, or zip code" autocomplete="off">
  <button id="search-btn">\U0001f50d</button>
</div>
<div id="search-results"></div>
<div id="violation-banner"></div>
<button class="info-toggle" id="infoToggle" onclick="
  var p = document.getElementById('info');
  p.classList.toggle('collapsed');
  this.textContent = p.classList.contains('collapsed') ? '\u25b2 Show details' : '\u25bc Hide details';
">\u25bc Hide details</button>
<div class="info-panel" id="info">
  <h3>Flock ALPR Sharing Map</h3>
  <p class="stat">Click an agency to see its sharing web.</p>
  <p class="stat">{marker_count} agencies mapped.</p>
</div>
<div class="legend">
  <div class="legend-item"><div class="legend-dot" style="background:#2563eb"></div> Public agency</div>
  <div class="legend-item"><div class="legend-dot" style="background:#f97316"></div> Shares with non-conforming entity</div>
  <div class="legend-item"><div class="legend-dot" style="background:#dc2626"></div> Non-conforming entity (private/out-of-state/fusion center)</div>
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
<script src="js/map.js?v=JSCACHEBUST"></script>
<script data-goatcounter="https://none-below.goatcounter.com/count"
        async src="//gc.zgo.at/count.js"></script>
</body>
</html>"""


if __name__ == "__main__":
    main()
