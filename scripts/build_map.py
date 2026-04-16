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

sys.path.insert(0, str(Path(__file__).parent))
from lib import agency_coords, agency_display_name, agency_active_slug, agency_state, crawl_status, has_tag, load_registry, registry_by_id

DEFAULT_DATA_DIR = Path("assets/transparency.flocksafety.com")
DEFAULT_OUT = Path("docs/sharing_map.html")



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

    # Load agency registry indexed by agency_id
    reg_by_id = registry_by_id()

    # Also build slug-based lookups for JS output
    registry_by_slug = {}
    id_to_slug = {}  # agency_id -> slug (for translating graph edges to JS)
    for e in load_registry():
        slug = e["slug"]
        registry_by_slug[slug] = e
        id_to_slug[e["agency_id"]] = slug

    def _ids_to_slugs(ids):
        """Translate a list of agency_ids to slugs for JS output."""
        return [id_to_slug[i] for i in ids if i in id_to_slug]

    # Validate: warn about graph agency_ids not in registry
    not_in_registry = [aid for aid in graph["agencies"] if aid not in reg_by_id]
    if not_in_registry:
        print(f"WARNING: {len(not_in_registry)} agency_ids in sharing graph not in registry")
        print("Run build_agency_registry.py --merge to add them.\n")

    # Build map data.
    #
    # Visibility rules (applied by JS via the "visible" flag):
    #   1. All crawled CA agencies — always shown
    #   2. Agencies with any sharing relationship with a CA agency (either
    #      direction) — shown regardless of crawl/state
    #   3. Non-public CA agencies that only send (HOAs, businesses with no
    #      CA-agency connection) — included in data but hidden by default;
    #      JS reveals them when a connected agency is clicked

    markers = []
    geocoded = 0
    ungeocodable = []
    seen_ids = set()

    # Pre-compute which agency_ids receive data from a CA agency
    ca_outbound_targets = set()
    for aid, data in graph["agencies"].items():
        reg = reg_by_id.get(aid, {})
        if agency_state(reg) == "CA":
            for target_id in data.get("sharing_outbound_ids", []):
                ca_outbound_targets.add(target_id)

    def should_show(aid, reg, data):
        """Determine default visibility for a marker."""
        crawled = data.get("crawled", False)
        if agency_state(reg) == "CA" and crawled:
            return True
        if aid in ca_outbound_targets:
            return True
        return False

    for aid, data in graph["agencies"].items():
        if aid not in reg_by_id:
            continue
        reg = reg_by_id[aid]
        slug = id_to_slug.get(aid)
        if not slug:
            continue
        lat, lng = agency_coords(reg)
        if lat is None or lng is None:
            ungeocodable.append(slug)
            continue
        geocoded += 1
        seen_ids.add(aid)

        cameras = data.get("camera_count") or 0
        crawled = data.get("crawled", True)
        markers.append({
            "slug": slug,
            "lat": lat,
            "lng": lng,
            "cameras": cameras,
            "crawled": crawled,
            "visible": should_show(aid, reg, data),
            "outbound_count": data.get("sharing_outbound_count", 0),
            "inbound_count": data.get("sharing_inbound_count", 0),
            "retention_days": data.get("data_retention_days"),
            "outbound_slugs": _ids_to_slugs(data.get("sharing_outbound_ids", [])),
            "inbound_slugs": _ids_to_slugs(data.get("sharing_inbound_ids", [])),
        })

    # Add registry entries not yet in the sharing graph
    for e in load_registry():
        aid = e["agency_id"]
        if aid in seen_ids:
            continue
        slug = e["slug"]
        lat, lng = agency_coords(e)
        if lat is None or lng is None:
            continue
        geocoded += 1
        seen_ids.add(aid)
        empty_data = {"crawled": False}
        markers.append({
            "slug": slug,
            "lat": lat,
            "lng": lng,
            "cameras": 0,
            "crawled": False,
            "visible": should_show(aid, e, empty_data),
            "outbound_count": 0,
            "inbound_count": 0,
            "retention_days": None,
            "outbound_slugs": [],
            "inbound_slugs": [],
        })

    # Add Flock Safety vendor as an implicit outbound target for San Mateo PD.
    # We've verified SMPD's Flock MSA contains §5.3 disclosure authority.
    # Other agencies likely have similar contracts but we haven't confirmed yet.
    FLOCK_53_AGENCIES = ["san-mateo-ca-pd"]
    flock_reg = registry_by_slug.get("flock-safety-vendor")
    flock_lat, flock_lng = agency_coords(flock_reg or {})
    if flock_reg and flock_lat and flock_lng:
        existing_slugs = {m["slug"] for m in markers}
        flock_inbound = [s for s in FLOCK_53_AGENCIES if s in existing_slugs]

        if flock_inbound:
            # Update existing marker or create new one
            flock_existing = next((m for m in markers if m["slug"] == "flock-safety-vendor"), None)
            if flock_existing:
                flock_existing["inbound_slugs"] = flock_inbound
                flock_existing["inbound_count"] = len(flock_inbound)
                flock_existing["visible"] = True
            else:
                markers.append({
                    "slug": "flock-safety-vendor",
                    "lat": flock_lat,
                    "lng": flock_lng,
                    "cameras": 0,
                    "crawled": False,
                    "visible": True,
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

    # Build classification lookup for JS — only agencies in graph or with markers
    graph_slugs = {id_to_slug.get(aid) for aid in graph["agencies"]} - {None}
    marker_slugs = {m["slug"] for m in markers}
    relevant_slugs = graph_slugs | marker_slugs
    slug_info = {}
    for slug in relevant_slugs:
        reg = registry_by_slug.get(slug, {})
        if not reg:
            continue
        is_crawled, crawled_date = crawl_status(reg, args.data_dir)
        slug_info[slug] = {
            "public": True if has_tag(reg, "public") else (False if has_tag(reg, "private") else None),
            "state": agency_state(reg),
            "name": agency_display_name(reg, slug),
            "role": reg.get("agency_role"),
            "type": reg.get("agency_type"),
            "crawled": is_crawled,
            "crawled_date": crawled_date,
            "notes": reg.get("notes"),
            "ag_lawsuit": has_tag(reg, "ag-lawsuit"),
        }

    # slug_info covers all slugs via the registry; resolve_agency()
    # handles alias lookups for any secondary flock_slugs

    # Build mismatch lookup (translate agency_ids to slugs for JS)
    mismatch_map = {}
    for m in graph.get("mismatches", []):
        agency_slug = id_to_slug.get(m.get("agency"))
        partner_id = m.get("claims_shared_by") or m.get("shares_with")
        partner_slug = id_to_slug.get(partner_id)
        if agency_slug and partner_slug:
            mismatch_map.setdefault(agency_slug, []).append(partner_slug)
            mismatch_map.setdefault(partner_slug, []).append(agency_slug)

    # Compute indirect flags: if A shares with B, and B shares with
    # a flagged entity V, then A has an indirect flag "V via B"
    def is_flagged_entity(aid):
        r = reg_by_id.get(aid, {})
        if has_tag(r, "private"):
            return True
        r_state = agency_state(r)
        if r_state and r_state != "CA":
            return True
        if r.get("agency_type") in ("federal", "fusion_center", "decommissioned", "test"):
            return True
        return False

    # Build outbound lookup from graph (agency_id -> [target_ids])
    outbound_by_id = {}
    for aid, data in graph["agencies"].items():
        outbound_by_id[aid] = data.get("sharing_outbound_ids", [])

    # For each agency, find indirect flags (depth 1: via intermediaries)
    # Output uses slugs for JS consumption
    indirect_flags = {}  # slug -> [{"flagged": slug, "via": slug}]
    for aid, data in graph["agencies"].items():
        slug = id_to_slug.get(aid)
        if not slug:
            continue
        indirects = []
        direct_flags = set()
        for target_id in data.get("sharing_outbound_ids", []):
            if is_flagged_entity(target_id):
                direct_flags.add(target_id)
        for target_id in data.get("sharing_outbound_ids", []):
            if target_id in outbound_by_id:
                for second_hop_id in outbound_by_id[target_id]:
                    if is_flagged_entity(second_hop_id) and second_hop_id not in direct_flags:
                        indirects.append({
                            "flagged": id_to_slug.get(second_hop_id, second_hop_id),
                            "via": id_to_slug.get(target_id, target_id),
                            "via_name": agency_display_name(reg_by_id.get(target_id, {})),
                            "flagged_name": agency_display_name(reg_by_id.get(second_hop_id, {})),
                        })
        if indirects:
            seen = set()
            deduped = []
            for iv in indirects:
                if iv["flagged"] not in seen:
                    seen.add(iv["flagged"])
                    deduped.append(iv)
            indirect_flags[slug] = deduped

    indirect_count = sum(len(v) for v in indirect_flags.values())
    print(f"Indirect flags: {indirect_count} across {len(indirect_flags)} agencies")

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
        "indirectFlags": indirect_flags,
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
    #flag-banner {{
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
    .back-link {{
      top: 52px; left: 10px;
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
  .edge-indicator.has-flag {{ background: #dc2626; }}
  #flag-banner {{
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
<div id="flag-banner"></div>
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
  <div class="legend-item"><div class="legend-dot" style="background:#f97316"></div> Shares with flagged entity</div>
  <div class="legend-item"><div class="legend-dot" style="background:#dc2626"></div> Flagged entity (private/out-of-state/federal)</div>
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
