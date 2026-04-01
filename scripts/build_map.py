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
import re
import sys
from pathlib import Path

DEFAULT_DATA_DIR = Path("assets/transparency.flocksafety.com")
DEFAULT_OUT = Path("docs/sharing_map.html")

# ── Geocoding ──
# California city centroids (lat, lng) — covers the agencies in our dataset.
# Sourced from census/geographic data. County seats used for county agencies.

CA_COORDS = {
    "alameda": (37.7652, -122.2416),
    "albany": (37.8869, -122.2978),
    "atherton": (37.4613, -122.1975),
    "auburn": (38.8966, -121.0769),
    "bakersfield": (35.3733, -119.0187),
    "baldwin-park": (34.0854, -117.9609),
    "barstow": (34.8958, -117.0173),
    "beaumont": (33.9295, -116.9770),
    "bell": (33.9775, -118.1870),
    "belmont": (37.5202, -122.2758),
    "benicia": (38.0494, -122.1586),
    "berkeley": (37.8716, -122.2727),
    "beverly-hills": (34.0736, -118.4004),
    "brisbane": (37.6808, -122.3999),
    "burbank": (34.1808, -118.3090),
    "burlingame": (37.5841, -122.3661),
    "calistoga": (38.5788, -122.5797),
    "campbell": (37.2872, -121.9500),
    "capitola": (36.9752, -121.9531),
    "carmel": (36.5554, -121.9233),
    "cathedral-city": (33.7797, -116.4653),
    "central-marin": (37.9735, -122.5311),
    "chula-vista": (32.6401, -117.0842),
    "citrus-heights": (38.7071, -121.2810),
    "clearlake": (38.9582, -122.6264),
    "cloverdale": (38.8054, -123.0171),
    "colma": (37.6769, -122.4597),
    "colton": (34.0739, -117.3137),
    "concord": (37.9780, -122.0311),
    "contra-costa-county": (37.9535, -121.9018),
    "corona": (33.8753, -117.5664),
    "coronado": (32.6859, -117.1831),
    "costa-mesa": (33.6412, -117.9187),
    "cypress": (33.8170, -118.0374),
    "daly-city": (37.6879, -122.4702),
    "danville": (37.8216, -121.9999),
    "delano": (35.7688, -119.2471),
    "dixon": (38.4455, -121.8233),
    "dublin": (37.7022, -121.9358),
    "east-palo-alto": (37.4688, -122.1411),
    "el-cajon": (32.7948, -116.9625),
    "el-cerrito": (37.9161, -122.3122),
    "el-monte": (34.0686, -118.0276),
    "elk-grove": (38.4088, -121.3716),
    "emeryville": (37.8313, -122.2852),
    "escalon": (37.7974, -120.9983),
    "escondido": (33.1192, -117.0864),
    "fairfield": (38.2494, -122.0400),
    "folsom": (38.6780, -121.1761),
    "fontana": (34.0922, -117.4350),
    "fort-bragg": (39.4457, -123.8053),
    "foster-city": (37.5585, -122.2711),
    "fountain-valley": (33.7092, -117.9536),
    "fremont": (37.5485, -121.9886),
    "fresno": (36.7378, -119.7871),
    "fullerton": (33.8704, -117.9242),
    "garden-grove": (33.7743, -117.9379),
    "gilroy": (37.0058, -121.5683),
    "glendale": (34.1425, -118.2551),
    "grass-valley": (39.2190, -121.0611),
    "greenfield": (36.3208, -121.2438),
    "hanford": (36.3274, -119.6457),
    "hayward": (37.6688, -122.0808),
    "hercules": (38.0172, -122.2886),
    "hillsborough": (37.5741, -122.3794),
    "hollister": (36.8525, -121.4016),
    "huntington-beach": (33.6595, -117.9988),
    "imperial-county": (32.8473, -115.5694),
    "indio": (33.7206, -116.2156),
    "irvine": (33.6846, -117.8265),
    "kern-county": (35.3733, -118.9515),
    "kings-county": (36.0988, -119.8159),
    "la-habra": (33.9319, -117.9462),
    "la-mesa": (32.7679, -117.0231),
    "la-verne": (34.1008, -117.7678),
    "laguna-beach": (33.5427, -117.7854),
    "lake-county": (39.0429, -122.7538),
    "lakeport": (39.0432, -122.9158),
    "lancaster": (34.6868, -118.1542),
    "lincoln": (38.8916, -121.2930),
    "livermore": (37.6819, -121.7681),
    "livingston": (37.3866, -120.7233),
    "lodi": (38.1302, -121.2724),
    "lompoc": (34.6392, -120.4579),
    "los-altos": (37.3852, -122.1141),
    "los-angeles": (34.0522, -118.2437),
    "los-angeles-county": (34.0522, -118.2437),
    "madera": (36.9613, -120.0607),
    "manteca": (37.7974, -121.2161),
    "marin-county": (38.0834, -122.7633),
    "marina": (36.6844, -121.8022),
    "menlo-park": (37.4530, -122.1817),
    "merced-county": (37.3022, -120.4830),
    "milpitas": (37.4323, -121.8996),
    "modoc-county": (41.5885, -120.7253),
    "monterey-county": (36.2400, -121.3153),
    "monterey-park": (34.0625, -118.1228),
    "moraga": (37.8349, -122.1297),
    "morgan-hill": (37.1305, -121.6544),
    "murrieta": (33.5539, -117.2139),
    "napa": (38.2975, -122.2869),
    "napa-county": (38.5025, -122.2654),
    "national-city": (32.6781, -117.0992),
    "ncric": (37.7749, -122.4194),  # SF-based
    "nevada-county": (39.2616, -121.0160),
    "newark": (37.5316, -122.0402),
    "newport-beach": (33.6189, -117.9298),
    "novato": (38.1074, -122.5697),
    "oakland": (37.8044, -122.2712),
    "oakley": (37.9974, -121.7125),
    "oceanside": (33.1959, -117.3795),
    "ontario": (34.0633, -117.6509),
    "orange": (33.7878, -117.8531),
    "orange-county": (33.7175, -117.8311),
    "oroville": (39.5138, -121.5564),
    "oxnard": (34.1975, -119.1771),
    "pacifica": (37.6138, -122.4869),
    "palo-alto": (37.4419, -122.1430),
    "pasadena": (34.1478, -118.1445),
    "petaluma": (38.2324, -122.6367),
    "piedmont": (37.8244, -122.2316),
    "placer-county": (38.9666, -121.0958),
    "pleasant-hill": (37.9480, -122.0608),
    "pleasanton": (37.6624, -121.8747),
    "redlands": (34.0556, -117.1825),
    "redondo-beach": (33.8492, -118.3884),
    "redwood-city": (37.4852, -122.2364),
    "reedley": (36.5963, -119.4504),
    "ridgecrest": (35.6225, -117.6709),
    "rio-vista": (38.1748, -121.6925),
    "rocklin": (38.7908, -121.2358),
    "roseville": (38.7521, -121.2880),
    "sacramento": (38.5816, -121.4944),
    "salinas": (36.6777, -121.6555),
    "san-bruno": (37.6305, -122.4111),
    "san-diego": (32.7157, -117.1611),
    "san-fernando": (34.2889, -118.4390),
    "san-francisco": (37.7749, -122.4194),
    "san-gabriel": (34.0961, -118.1058),
    "san-joaquin-county": (37.9577, -121.2908),
    "san-jose": (37.3382, -121.8863),
    "san-leandro": (37.7249, -122.1561),
    "san-luis-obispo": (35.2828, -120.6596),
    "san-mateo": (37.5630, -122.3255),
    "san-mateo-county": (37.4337, -122.4014),
    "san-pablo": (37.9621, -122.3458),
    "san-ramon": (37.7799, -121.9780),
    "santa-barbara-county": (34.4208, -119.6982),
    "santa-clara": (37.3541, -121.9552),
    "santa-monica": (34.0195, -118.4912),
    "santa-rosa": (38.4404, -122.7141),
    "sausalito": (37.8591, -122.4852),
    "seal-beach": (33.7414, -118.1048),
    "seaside": (36.6107, -121.8514),
    "shasta-county": (40.5865, -122.3917),
    "simi-valley": (34.2694, -118.7815),
    "solano-county": (38.2494, -122.0400),
    "soledad": (36.4247, -121.3263),
    "sonoma-county": (38.5110, -122.9888),
    "south-gate": (33.9547, -118.2120),
    "south-pasadena": (34.1161, -118.1503),
    "south-san-francisco": (37.6547, -122.4077),
    "stanford": (37.4275, -122.1697),
    "stockton": (37.9577, -121.2908),
    "suisun-city": (38.2383, -122.0400),
    "tehama-county": (40.0258, -122.1236),
    "tiburon": (37.8735, -122.4567),
    "tracy": (37.7397, -121.4252),
    "tulare": (36.2077, -119.3473),
    "turlock": (37.4947, -120.8466),
    "union-city": (37.5934, -122.0439),
    "university-of-california-berkeley": (37.8716, -122.2727),
    "university-of-san-francisco": (37.7767, -122.4506),
    "university-of-the-pacific": (37.9812, -121.3114),
    "upland": (34.0975, -117.6484),
    "vacaville": (38.3566, -121.9877),
    "vallejo": (38.1041, -122.2566),
    "ventura": (34.2805, -119.2945),
    "ventura-county": (34.3705, -119.1391),
    "vernon": (33.9953, -118.2298),
    "visalia": (36.3302, -119.2921),
    "walnut-creek": (37.9101, -122.0652),
    "watsonville": (36.9102, -121.7569),
    "west-covina": (34.0686, -117.9394),
    "west-sacramento": (38.5805, -121.5302),
    "westminster": (33.7514, -117.9940),
    "whittier": (33.9792, -118.0328),
    "woodland": (38.6785, -121.7733),
    "yolo-county": (38.6785, -121.7733),
    "yuba-city": (39.1404, -121.6169),
    "yuba-county": (39.2627, -121.3502),
    # Additional cities for uncrawled agencies
    "alhambra": (34.0953, -118.1270),
    "alpine-county": (38.5941, -119.8207),
    "amador-county": (38.4468, -120.6543),
    "anaheim": (33.8366, -117.9143),
    "anderson": (40.4485, -122.2977),
    "angels-camp": (38.0685, -120.5396),
    "antioch": (38.0049, -121.8058),
    "arcadia": (34.1397, -118.0353),
    "atwater": (37.3477, -120.6090),
    "azusa": (34.1336, -117.9076),
    "bishop": (37.3636, -118.3951),
    "brawley": (32.9787, -115.5305),
    "brentwood": (37.9319, -121.6958),
    "butte-county": (39.6667, -121.6000),
    "calexico": (32.6790, -115.4989),
    "california-highway-patrol": (38.5816, -121.4944),
    "california-state-parks": (38.5816, -121.4944),
    "cathedral-city": (33.7797, -116.4653),
    "chino": (34.0122, -117.6889),
    "citrus-heights": (38.7071, -121.2810),
    "claremont": (34.0967, -117.7198),
    "clayton": (37.9410, -121.9358),
    "colusa": (39.2141, -122.0097),
    "corcoran": (36.0980, -119.5604),
    "cotati": (38.3277, -122.7069),
    "covina": (34.0900, -117.8903),
    "culver-city": (34.0211, -118.3965),
    "delano": (35.7688, -119.2471),
    "desert-hot-springs": (33.9611, -116.5017),
    "dinuba": (36.5430, -119.3868),
    "downey": (33.9401, -118.1332),
    "dublin": (37.7022, -121.9358),
    "east-bay-parks": (37.8044, -122.2712),
    "el-cajon": (32.7948, -116.9625),
    "el-centro": (32.7920, -115.5631),
    "el-dorado-county": (38.7296, -120.7985),
    "el-monte": (34.0686, -118.0276),
    "el-segundo": (33.9192, -118.4165),
    "elk-grove": (38.4088, -121.3716),
    "escalon": (37.7974, -120.9983),
    "escondido": (33.1192, -117.0864),
    "farmersville": (36.2991, -119.2068),
    "foothill-deanza": (37.3616, -122.1281),
    "fort-bragg": (39.4457, -123.8053),
    "fountain-valley": (33.7092, -117.9536),
    "fowler": (36.6307, -119.6809),
    "galt": (38.2546, -121.3000),
    "glendora": (34.1361, -117.8653),
    "grover-beach": (35.1217, -120.6210),
    "healdsburg": (38.6127, -122.8694),
    "hemet": (33.7475, -116.9719),
    "humboldt-county": (40.7450, -123.8695),
    "irwindale": (34.1070, -117.9351),
    "kerman": (36.7236, -120.0596),
    "kings-county": (36.0988, -119.8159),
    "kingsburg": (36.5136, -119.5537),
    "kensington": (37.9103, -122.2802),
    "lassen-county": (40.6736, -120.7253),
    "lathrop": (37.8227, -121.2766),
    "los-gatos": (37.2358, -121.9624),
    "mcfarland": (35.6783, -119.2293),
    "mendota": (36.7536, -120.3818),
    "menifee": (33.6781, -117.1464),
    "mill-valley": (37.9060, -122.5416),
    "modoc-county": (41.5885, -120.7253),
    "monrovia": (34.1442, -118.0020),
    "montclair": (34.0775, -117.6898),
    "monterey": (36.6002, -121.8947),
    "nevada-city": (39.2616, -121.0160),
    "newport-beach": (33.6189, -117.9298),
    "ontario": (34.0633, -117.6509),
    "orange-cove": (36.6236, -119.3129),
    "pacific-grove": (36.6177, -121.9166),
    "palm-springs": (33.8303, -116.5453),
    "placentia": (33.8722, -117.8703),
    "pomona": (34.0551, -117.7500),
    "port-hueneme": (34.1478, -119.1951),
    "port-of-stockton": (37.9577, -121.2908),
    "porterville": (36.0653, -119.0168),
    "redding": (40.5865, -122.3917),
    "redlands": (34.0556, -117.1825),
    "reedley": (36.5963, -119.4504),
    "rialto": (34.1064, -117.3703),
    "richmond": (37.9358, -122.3477),
    "ridgecrest": (35.6225, -117.6709),
    "rio-vista": (38.1748, -121.6925),
    "riverside-county": (33.9806, -117.3755),
    "san-benito-county": (36.6069, -121.0836),
    "san-bernardino-county": (34.1083, -117.2898),
    "san-diego-county": (32.7157, -117.1611),
    "san-joaquin-county": (37.9577, -121.2908),
    "san-pasqual": (33.0947, -116.9578),
    "san-rafael": (37.9735, -122.5311),
    "santa-ana": (33.7455, -117.8677),
    "santa-barbara": (34.4208, -119.6982),
    "santa-cruz": (36.9741, -122.0308),
    "selma": (36.5708, -119.6121),
    "solano-county": (38.2494, -122.0400),
    "sunnyvale": (37.3688, -122.0363),
    "sutter-county": (39.1596, -121.6947),
    "tehachapi": (35.1322, -118.4490),
    "trinity-county": (40.7390, -122.9423),
    "truckee": (39.3280, -120.1833),
    "tulare-county": (36.2077, -119.3473),
    "tustin": (33.7458, -117.8262),
    "ukiah": (39.1502, -123.2078),
    "union-pacific-railroad": (41.2565, -95.9345),  # Omaha, NE
    "university-of-the-pacific": (37.9812, -121.3114),
    "williams": (39.1541, -122.1497),
    "willits": (39.4096, -123.3556),
    # Additional cities/places
    "amador-county": (38.4468, -120.6543),
    "american-canyon": (38.1749, -122.2608),
    "arvin": (35.2094, -118.8282),
    "avenal": (36.0041, -120.1271),
    "bear-valley-springs": (35.1539, -118.6284),
    "bell-gardens": (33.9653, -118.1514),
    "belvedere": (37.8727, -122.4946),
    "blue-lake-rancheria": (40.8835, -123.9828),
    "blythe": (33.6175, -114.5883),
    "brea": (33.9167, -117.9000),
    "buena-park": (33.8676, -117.9981),
    "burbank-airport": (34.1975, -118.3585),
    "cal-state-fullerton": (33.8829, -117.8853),
    "cal-state-san-bernadino": (34.1817, -117.3232),
    "california-city": (35.1258, -117.9859),
    "california-state-university-long-beach": (33.7830, -118.1129),
    "california-state-university-long-beach-campus-pd": (33.7830, -118.1129),
    "cerritos": (33.8583, -118.0648),
    "chabot-college": (37.6525, -122.0947),
    "chaffey-college": (34.1047, -117.5765),
    "chino": (34.0122, -117.6889),
    "city-of-lemoore": (36.3008, -119.7826),
    "city-of-menifee": (33.6781, -117.1464),
    "city-of-monte-sereno": (37.2363, -121.9928),
    "claremont": (34.0967, -117.7198),
    "corcoran": (36.0980, -119.5604),
    "cornerstone-community-school": (33.8676, -117.2139),
    "cotati": (38.3277, -122.7069),
    "culver-city": (34.0211, -118.3965),
    "del-norte-county": (41.7434, -124.1829),
    "east-bay-parks": (37.8044, -122.2712),
    "el-segundo": (33.9192, -118.4165),
    "el-dorado-county": (38.7296, -120.7985),
    "farmersville": (36.2991, -119.2068),
    "goshen-village": (41.3812, -74.3240),  # NY
    "healdsburg": (38.6127, -122.8694),
    "hermosa-beach": (33.8622, -118.3995),
    "humboldt-county": (40.7450, -123.8695),
    "iipay-santa-ysabel": (33.1100, -116.6700),
    "kingsburg": (36.5136, -119.5537),
    "lasd-san-dimas": (34.1067, -117.8087),
    "lassen-county": (40.6736, -120.7253),
    "los-angeles-port": (33.7361, -118.2639),
    "madera-county": (37.2180, -119.7631),
    "mendocino-county": (39.3076, -123.7995),
    "mill-valley": (37.9060, -122.5416),
    "montclair": (34.0775, -117.6898),
    "monterey": (36.6002, -121.8947),
    "napa-valley-college": (38.2618, -122.2727),
    "nevada-city": (39.2616, -121.0160),
    "orange-coast-college": (33.6715, -117.9129),
    "pacific-grove": (36.6177, -121.9166),
    "palm-springs": (33.8303, -116.5453),
    "port-hueneme": (34.1478, -119.1951),
    "port-of-stockton": (37.9577, -121.2908),
    "porterville": (36.0653, -119.0168),
    "rancho-cordova": (38.5891, -121.3028),
    "redding": (40.5865, -122.3917),
    "rialto": (34.1064, -117.3703),
    "richmond": (37.9358, -122.3477),
    "ridgecrest": (35.6225, -117.6709),
    "rio-hondo-college": (34.0231, -118.0359),
    "riverside-county": (33.9806, -117.3755),
    "rohnert-park": (38.3397, -122.7011),
    "san-benito-county": (36.6069, -121.0836),
    "san-bernardino": (34.1083, -117.2898),
    "san-bernardino-county": (34.1083, -117.2898),
    "san-diego-county": (32.7157, -117.1611),
    "san-joaquin-delta-college": (37.9891, -121.3270),
    "san-jose-evergreen": (37.3130, -121.8081),
    "san-jose-state": (37.3352, -121.8811),
    "san-juan-bautista": (36.8454, -121.5383),
    "san-rafael": (37.9735, -122.5311),
    "santa-ana": (33.7455, -117.8677),
    "santa-barbara": (34.4208, -119.6982),
    "santa-cruz": (36.9741, -122.0308),
    "santa-paula": (34.3542, -119.0590),
    "selma": (36.5708, -119.6121),
    "sequoias-community-college": (36.3302, -119.2921),
    "shasta-arson": (40.5865, -122.3917),
    "solano-county": (38.2494, -122.0400),
    "stanford": (37.4275, -122.1697),
    "sunnyvale": (37.3688, -122.0363),
    "tehama-county": (40.0258, -122.1236),
    "trinity-county": (40.7390, -122.9423),
    "truckee": (39.3280, -120.1833),
    "tulare-county": (36.2077, -119.3473),
    "uc-irvine": (33.6405, -117.8443),
    "uc-santa-barbara": (34.4140, -119.8489),
    "university-of-california-berkeley": (37.8716, -122.2727),
    "university-of-san-francisco": (37.7767, -122.4506),
    "wasco": (35.5944, -119.3406),
    "west-valley-mission-college": (37.2630, -121.9188),
    "western-states-information-network": (38.5816, -121.4944),
    "woodlake": (36.4136, -119.0987),
    # "City of" / "Town of" aliases — same location as their PD
    "city-of-vallejo-ca": (38.1041, -122.2566),
    "city-of-half-moon-bay": (37.4636, -122.4286),
    "city-of-lemoore": (36.3008, -119.7826),
    "city-of-menifee": (33.6781, -117.1464),
    "city-of-monte-sereno": (37.2363, -121.9928),
    "half-moon-bay": (37.4636, -122.4286),
    "newman": (37.3133, -121.0208),
    "lindsay": (36.2030, -119.0882),
    "modesto": (37.6391, -120.9969),
    # Out-of-state (direct slug matches since suffix stripping won't work)
    "18th-judicial-district-das-office-la-pd": (30.4515, -91.1871),  # Baton Rouge, LA
    "goshen-village-ny-pd": (41.3812, -74.3240),  # Goshen, NY
}


def slug_to_location(slug):
    """Try to geocode a slug to (lat, lng) using the lookup table."""
    # Direct match
    if slug in CA_COORDS:
        return CA_COORDS[slug]

    # Strip common suffixes progressively
    candidates = set()
    stripped = re.sub(r"-(ca|county)?-(pd|so|sd|da|police|sheriff|sheriffs-office|das-office)$", "", slug)
    stripped = re.sub(r"-ca$", "", stripped)
    candidates.add(stripped)
    candidates.add(re.sub(r"-county", "", stripped))
    # "city-of-X" -> "X"
    candidates.add(re.sub(r"^city-of-", "", stripped))
    # "X-pd-ca" -> "X"
    candidates.add(re.sub(r"-pd-ca$", "", slug))
    candidates.add(re.sub(r"-pd$", "", stripped))
    # "ca-X" -> "X" (e.g. ca-wasco-pd)
    candidates.add(re.sub(r"^ca-", "", stripped))
    # University patterns
    candidates.add(re.sub(r"-(campus|college|university).*$", "", stripped))

    for c in candidates:
        if c and c in CA_COORDS:
            return CA_COORDS[c]

    return None


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

    # Build map data — skip aliases, merge their data into primary
    markers = []
    geocoded = 0
    ungeocodable = []

    for slug, data in graph["agencies"].items():
        # Skip alias slugs — their data is on the primary
        if slug in alias_to_primary:
            continue
        loc = slug_to_location(slug)
        if not loc:
            ungeocodable.append(slug)
            continue
        geocoded += 1

        cameras = data.get("camera_count") or 0
        crawled = data.get("crawled", True)
        markers.append({
            "slug": slug,
            "lat": loc[0],
            "lng": loc[1],
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

    # Write JS
    js_code = _generate_js(len(markers))
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
<div class="offmap-panel" id="offmap"></div>
<script src="data/map_data.json" type="application/json" id="mapData"></script>
<script src="js/map.js"></script>
</body>
</html>"""


def _generate_js(marker_count):
    # Plain JS — no f-string escaping needed
    return r"""
// Load data
fetch('data/map_data.json').then(r => r.json()).then(data => {
  const markers = data.markers;
  const coords = data.coords;
  const agencyInfo = data.agencyInfo;
  const mismatches = data.mismatches;

  const map = L.map('map').setView([37.5, -121.5], 7);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png', {
    attribution: '&copy; OpenStreetMap, &copy; CARTO',
    maxZoom: 18,
  }).addTo(map);

  const markerLayer = L.markerClusterGroup({
    maxClusterRadius: 20,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    zoomToBoundsOnClick: true,
    iconCreateFunction: function(cluster) {
      const children = cluster.getAllChildMarkers();
      const size = Math.min(44, 22 + children.length * 2);
      const r = size / 2;

      // Count categories
      let red = 0, orange = 0, blue = 0, gray = 0;
      children.forEach(cm => {
        const slug = cm.options.slug;
        if (!slug) { gray++; return; }
        const info = agencyInfo[slug] || {};
        if (isViolation(slug)) red++;
        else if (hasOutboundViolation(cm._markerData || {})) orange++;
        else if (info.crawled || cm.options.fillColor === '#2563eb') blue++;
        else gray++;
      });

      // Build SVG pie chart
      const total = children.length;
      const segments = [];
      let angle = 0;
      [[red, '#dc2626'], [orange, '#f97316'], [blue, '#2563eb'], [gray, '#8b5cf6']].forEach(([count, color]) => {
        if (count === 0) return;
        const sweep = (count / total) * 360;
        if (count === total) {
          segments.push('<circle cx="' + r + '" cy="' + r + '" r="' + (r-1) + '" fill="' + color + '"/>');
        } else {
          const startRad = angle * Math.PI / 180;
          const endRad = (angle + sweep) * Math.PI / 180;
          const x1 = r + (r-1) * Math.sin(startRad);
          const y1 = r - (r-1) * Math.cos(startRad);
          const x2 = r + (r-1) * Math.sin(endRad);
          const y2 = r - (r-1) * Math.cos(endRad);
          const large = sweep > 180 ? 1 : 0;
          segments.push('<path d="M' + r + ',' + r + ' L' + x1 + ',' + y1 + ' A' + (r-1) + ',' + (r-1) + ' 0 ' + large + ',1 ' + x2 + ',' + y2 + ' Z" fill="' + color + '"/>');
        }
        angle += sweep;
      });

      const svg = '<svg width="' + size + '" height="' + size + '" xmlns="http://www.w3.org/2000/svg">' +
        segments.join('') +
        '<circle cx="' + r + '" cy="' + r + '" r="' + (r * 0.55) + '" fill="white"/>' +
        '<text x="' + r + '" y="' + (r + 4) + '" text-anchor="middle" font-size="11" font-weight="bold" fill="#374151">' + total + '</text>' +
        '</svg>';

      return L.divIcon({
        html: svg,
        className: '',
        iconSize: [size, size],
      });
    },
  });

  // Show member names on cluster hover
  markerLayer.on('clustermouseover', function(e) {
    const children = e.layer.getAllChildMarkers();
    if (children.length > 15) {
      e.layer.bindTooltip(children.length + ' agencies').openTooltip();
      return;
    }
    const names = children.map(cm => {
      const slug = cm.options.slug;
      const info = agencyInfo[slug] || {};
      let name = info.name || slug;
      if (isViolation(slug)) name = '\u26a0 ' + name;
      return name;
    }).sort();
    e.layer.bindTooltip(names.join('<br>'), { direction: 'top' }).openTooltip();
  });
  markerLayer.on('clustermouseout', function(e) {
    e.layer.unbindTooltip();
  });

  markerLayer.addTo(map);
  const lineLayer = L.layerGroup().addTo(map);
  const markersBySlug = {};

  function defaultRadius(m) {
    if (m.crawled) return Math.max(4, Math.min(10, Math.sqrt(m.cameras || 1) * 2));
    return 4;  // uncrawled: same base size as small cities
  }

  function isViolation(slug) {
    const info = agencyInfo[slug] || {};
    if (info.public === false && info.type !== 'test') return true;  // private entity
    if (info.state && info.state !== 'CA') return true;               // out-of-state
    if (info.type === 'federal') return true;                         // federal — not "agency of the state" per §1798.90.5(f)
    if (info.type === 'decommissioned') return true;
    if (info.type === 'test') return true;
    return false;
  }

  // Does this agency share with any violation entities?
  function hasOutboundViolation(m) {
    return (m.outbound_slugs || []).some(s => isViolation(s));
  }

  function defaultColor(m) {
    if (isViolation(m.slug)) return { fill: '#dc2626', border: '#991b1b', opacity: 0.8 };
    if (hasOutboundViolation(m)) return { fill: '#f97316', border: '#c2410c', opacity: 0.7 };
    if (m.crawled) return { fill: '#2563eb', border: '#1e40af', opacity: 0.6 };
    return { fill: '#8b5cf6', border: '#6d28d9', opacity: 0.5 };
  }

  function distKm(lat1, lng1, lat2, lng2) {
    const R = 6371;
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLng = (lng2 - lng1) * Math.PI / 180;
    const a = Math.sin(dLat/2)**2 + Math.cos(lat1*Math.PI/180) * Math.cos(lat2*Math.PI/180) * Math.sin(dLng/2)**2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
  }

  function sortPriority(info) {
    if (info.state && info.state !== 'CA') return 0;           // out-of-state
    if (info.public === false) return 1;                        // private
    if (info.type === 'federal') return 2;                      // federal — not agency of the state
    if (info.type === 'decommissioned') return 3;               // decommissioned/DNU
    if (info.type === 'test') return 4;                         // test/demo
    if (info.notes && info.notes.indexOf('re-sharing') >= 0) return 5;  // re-sharing risk
    return 10;                                                  // normal
  }

  function sortOutbound(slugs, fromLat, fromLng) {
    return [...slugs].sort((a, b) => {
      const ai = agencyInfo[a] || {};
      const bi = agencyInfo[b] || {};
      const aPri = sortPriority(ai);
      const bPri = sortPriority(bi);
      if (aPri !== bPri) return aPri - bPri;
      const aCoord = coords[a];
      const bCoord = coords[b];
      const aDist = aCoord ? distKm(fromLat, fromLng, aCoord[0], aCoord[1]) : 0;
      const bDist = bCoord ? distKm(fromLat, fromLng, bCoord[0], bCoord[1]) : 0;
      return bDist - aDist;
    });
  }

  function slugLabel(s) {
    const info = agencyInfo[s] || {};
    let label = info.name || s;
    let tag = '';
    if (info.state && info.state !== 'CA')
      tag += ' <span style="color:#dc2626;font-weight:bold" title="Out-of-state sharing may violate CA Civil Code \u00a71798.90.55(b)">[' + info.state + ' \u2014 out of state]</span>';
    if (info.public === false && info.type !== 'decommissioned' && info.type !== 'test')
      tag += ' <span style="color:#dc2626;font-weight:bold" title="CA Civil Code \u00a71798.90.55(b) restricts ALPR sharing to public agencies">[PRIVATE \u2014 likely violates SB 34]</span>';
    if (info.type === 'federal')
      tag += ' <span style="color:#dc2626;font-weight:bold" title="Federal entity \u2014 not an agency of the state per CA Civil Code \u00a71798.90.5(f). AG Bulletin 2023-DLE-06 prohibits sharing with federal agencies.">[FEDERAL]</span>';
    if (info.type === 'decommissioned')
      tag += ' <span style="color:#f97316;font-weight:bold" title="Marked Do Not Use by Flock but still appears in sharing lists">[DECOMMISSIONED]</span>';
    if (info.type === 'test')
      tag += ' <span style="color:#f97316;font-weight:bold" title="Test/demo entry still in sharing list">[TEST]</span>';
    if (info.notes && info.notes.indexOf('re-sharing') >= 0)
      tag += ' <span style="color:#d97706;font-weight:bold" title="' + info.notes.replace(/"/g, '&quot;').replace(/<[^>]*>/g, '') + '">[RE-SHARES TO VIOLATIONS]</span>';
    const loc = coords[s];
    if (!loc) tag += ' <span style="color:#9ca3af">(not mapped)</span>';
    if (info.crawled) {
      tag += ' <a href="https://transparency.flocksafety.com/' + s + '" target="_blank" style="color:#6b7280;text-decoration:none" title="View transparency portal">\u2197</a>';
    }
    return label + tag;
  }

  // Place markers
  markers.forEach(m => {
    const col = defaultColor(m);
    const radius = isViolation(m.slug) ? Math.max(6, defaultRadius(m)) : defaultRadius(m);
    const circle = L.circleMarker([m.lat, m.lng], {
      radius: radius,
      fillColor: col.fill,
      color: col.border,
      weight: isViolation(m.slug) ? 2 : 1,
      fillOpacity: col.opacity,
      slug: m.slug,
    }).addTo(markerLayer);
    const info = agencyInfo[m.slug] || {};
    circle._markerData = m;
    const tipName = (info.name || m.slug) + (isViolation(m.slug) ? ' \u26a0' : '');
    circle.bindTooltip(tipName, { direction: 'top', offset: [0, -8], sticky: true });
    circle.on('click', (e) => { L.DomEvent.stopPropagation(e); showAgency(m); });
    markersBySlug[m.slug] = circle;
  });

  // After spiderfy, re-bind tooltips on the spidered markers
  markerLayer.on('spiderfied', function(e) {
    e.markers.forEach(cm => {
      const slug = cm.options.slug;
      const info = agencyInfo[slug] || {};
      const tipName = (info.name || slug) + (isViolation(slug) ? ' \u26a0' : '');
      cm.bindTooltip(tipName, { direction: 'top', offset: [0, -8], sticky: true });
    });
  });

  function showAgency(m) {
    lineLayer.clearLayers();

    let outConnected = 0;
    (m.outbound_slugs || []).forEach(target => {
      if (coords[target]) {
        L.polyline([[m.lat, m.lng], coords[target]], { color: '#2563eb', weight: 1.5, opacity: 0.3 }).addTo(lineLayer);
        outConnected++;
      }
    });

    let inConnected = 0;
    (m.inbound_slugs || []).forEach(source => {
      if (coords[source]) {
        L.polyline([coords[source], [m.lat, m.lng]], { color: '#16a34a', weight: 1.5, opacity: 0.3, dashArray: '4 4' }).addTo(lineLayer);
        inConnected++;
      }
    });

    const info = document.getElementById('info');
    const status = m.crawled ? 'Crawled' : 'No transparency page found (inferred from other portals)';
    const statusColor = m.crawled ? '#16a34a' : '#f97316';
    let html = '<h3>' + m.slug + '</h3>';
    if (m.crawled) {
      html += '<p class="stat"><a href="https://transparency.flocksafety.com/' + m.slug + '" target="_blank" style="color:#2563eb">View transparency portal \u2197</a></p>';
    }
    html += '<p class="stat" style="color:' + statusColor + '">' + status + '</p>';
    if (m.cameras) html += '<p class="stat">Cameras: ' + m.cameras + '</p>';
    if (m.retention_days) html += '<p class="stat">Retention: ' + m.retention_days + ' days</p>';
    html += '<p class="stat">Shares with: ' + m.outbound_count + ' agencies (' + outConnected + ' mapped)</p>';
    html += '<p class="stat">Receives from: ' + (m.inbound_count || (m.inbound_slugs ? m.inbound_slugs.length : 0)) + ' agencies (' + inConnected + ' mapped)</p>';
    const mInfo = agencyInfo[m.slug] || {};
    if (mInfo.notes) html += '<p class="stat" style="background:#fef3c7;padding:6px 8px;border-radius:4px;color:#92400e;margin-top:6px">' + mInfo.notes + '</p>';

    if (m.outbound_slugs && m.outbound_slugs.length) {
      html += '<div class="sharing-list"><strong>Shares with (outbound):</strong>';
      sortOutbound(m.outbound_slugs, m.lat, m.lng).slice(0, 50).forEach(function(s) {
        html += '<div style="cursor:pointer" onclick="clickSlug(\'' + s + '\')">' + slugLabel(s) + '</div>';
      });
      if (m.outbound_slugs.length > 50) html += '<div>... and ' + (m.outbound_slugs.length - 50) + ' more</div>';
      html += '</div>';
    }

    if (m.inbound_slugs && m.inbound_slugs.length) {
      html += '<div class="sharing-list"><strong>Receives from (inbound):</strong>';
      sortOutbound(m.inbound_slugs, m.lat, m.lng).slice(0, 50).forEach(function(s) {
        html += '<div style="cursor:pointer" onclick="clickSlug(\'' + s + '\')">' + slugLabel(s) + '</div>';
      });
      if (m.inbound_slugs.length > 50) html += '<div>... and ' + (m.inbound_slugs.length - 50) + ' more</div>';
      html += '</div>';
    }

    info.innerHTML = html;

    const connected = new Set();
    connected.add(m.slug);
    (m.outbound_slugs || []).forEach(s => connected.add(s));
    (m.inbound_slugs || []).forEach(s => connected.add(s));
    const myMismatches = new Set(mismatches[m.slug] || []);

    markers.forEach(mm => {
      const c = markersBySlug[mm.slug];
      if (!c) return;
      if (mm.slug === m.slug) {
        c.setRadius(14);
        c.setStyle({ fillColor: '#06b6d4', fillOpacity: 1, weight: 3, color: '#0e7490' });
        c.bringToFront();
      } else if (myMismatches.has(mm.slug)) {
        c.setRadius(Math.max(6, defaultRadius(mm)));
        c.setStyle({ fillColor: '#f97316', fillOpacity: 0.9, weight: 2, color: '#c2410c' });
      } else if (connected.has(mm.slug)) {
        const col = defaultColor(mm);
        c.setRadius(defaultRadius(mm));
        c.setStyle({ fillColor: col.fill, fillOpacity: 0.8, weight: 1, color: col.border });
      } else if (isViolation(mm.slug)) {
        c.setRadius(3);
        c.setStyle({ fillColor: '#dc2626', fillOpacity: 0.3, weight: 1, color: '#991b1b' });
      } else {
        c.setRadius(2);
        c.setStyle({ fillColor: '#d1d5db', fillOpacity: 0.2, weight: 0.5, color: '#e5e7eb' });
      }
    });
  }

  function resetMarkers() {
    markers.forEach(mm => {
      const c = markersBySlug[mm.slug];
      if (!c) return;
      const col = defaultColor(mm);
      const radius = isViolation(mm.slug) ? Math.max(5, defaultRadius(mm)) : defaultRadius(mm);
      c.setRadius(radius);
      c.setStyle({ fillColor: col.fill, fillOpacity: col.opacity, weight: isViolation(mm.slug) ? 2 : 1, color: col.border });
    });
  }

  // Click map background to reset
  map.on('click', () => {
    lineLayer.clearLayers();
    resetMarkers();
    document.getElementById('info').innerHTML =
      '<h3>Flock ALPR Sharing Map</h3>' +
      '<p class="stat">Click an agency to see its sharing web.</p>' +
      '<p class="stat">""" + str(marker_count) + r""" agencies mapped.</p>';
  });

  // Navigate to slug from info panel
  const markerDataBySlug = {};
  markers.forEach(m => { markerDataBySlug[m.slug] = m; });

  window.clickSlug = function(slug) {
    const m = markerDataBySlug[slug];
    if (m) {
      map.setView([m.lat, m.lng], 10);
      showAgency(m);
    } else {
      const info = agencyInfo[slug] || {};
      const panel = document.getElementById('info');
      let html = '<h3>' + (info.name || slug) + '</h3>';
      html += '<p class="stat" style="color:#f97316">No map location</p>';
      if (info.crawled) {
        html += '<p class="stat"><a href="https://transparency.flocksafety.com/' + slug + '" target="_blank" style="color:#2563eb">View transparency portal \u2197</a></p>';
      }
      if (info.state) html += '<p class="stat">State: ' + info.state + '</p>';
      if (info.role) html += '<p class="stat">Role: ' + info.role + '</p>';
      if (info.type) html += '<p class="stat">Type: ' + info.type + '</p>';
      if (info.type === 'federal') html += '<p class="stat" style="color:#dc2626">Federal entity \u2014 not an \u201cagency of the state\u201d per \u00a71798.90.5(f). AG Bulletin prohibits sharing with federal agencies.</p>';
      else if (info.public === true) html += '<p class="stat" style="color:#16a34a">Public agency</p>';
      if (info.public === false) html += '<p class="stat" style="color:#dc2626">Not a public agency \u2014 sharing likely violates SB 34</p>';
      if (info.notes) html += '<p class="stat" style="background:#fef3c7;padding:6px 8px;border-radius:4px;color:#92400e;margin-top:6px">' + info.notes + '</p>';

      const sharedBy = markers.filter(mm => (mm.outbound_slugs || []).includes(slug));
      if (sharedBy.length) {
        html += '<div class="sharing-list"><strong>Receives data from (' + sharedBy.length + '):</strong>';
        sharedBy.forEach(function(mm) {
          html += '<div style="cursor:pointer" onclick="clickSlug(\'' + mm.slug + '\')">' + slugLabel(mm.slug) + '</div>';
        });
        html += '</div>';
      }
      panel.innerHTML = html;
    }
  };

  // Populate off-map violations panel
  const offmapPanel = document.getElementById('offmap');
  const offmapEntities = Object.entries(agencyInfo).filter(([slug, info]) => {
    return isViolation(slug) && !coords[slug];
  }).sort((a, b) => sortPriority(a[1]) - sortPriority(b[1]));

  if (offmapEntities.length) {
    let html = '<h4>\u26a0 Off-map violations (' + offmapEntities.length + ')</h4>';
    offmapEntities.slice(0, 30).forEach(([slug, info]) => {
      html += '<div onclick="clickSlug(\'' + slug + '\')">' + slugLabel(slug) + '</div>';
    });
    if (offmapEntities.length > 30) html += '<div>... and ' + (offmapEntities.length - 30) + ' more</div>';
    offmapPanel.innerHTML = html;
  } else {
    offmapPanel.style.display = 'none';
  }
});
"""


if __name__ == "__main__":
    main()
