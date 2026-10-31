# Salinas PD — Flock shared-networks export (October 22, 2026)

`Salinas_PD_SharedNetworks_2026_October_22.csv` is a Flock "Shared Networks"
export from the Salinas Police Department's Flock account, produced under the
California Public Records Act. It is committed byte-for-byte as received.

## Provenance

| | |
| --- | --- |
| Producing agency | Salinas Police Department |
| Export date | 2026-10-22 (from the filename) |
| Received | 2026-10-30, as a forwarded copy of the production |
| Request | [26-1065](https://city-of-salinas-ca.nextrequest.com/requests/26-1065) on the City of Salinas NextRequest portal |
| sha256 | `4a49bdb8fa5208cee72a9561591da965571aed78a05245b406d6668e98e58899` |
| Size | 26,979 bytes, 344 data rows, UTF-8, LF line endings, no trailing newline |

As of 2026-10-30 the request can't be viewed on the portal without a login,
and it lists no public documents. So this copy couldn't be checked against a
city-posted one.

## Format

One row per partner organization, three columns:

| Column | Meaning |
| --- | --- |
| `Organization Name` | A partner organization: a Flock account with its own users |
| `Networks Shared With Me` | That organization's camera networks that Salinas PD can search |
| `Networks I'm Sharing` | Which of Salinas PD's own networks that organization can search |

A Flock organization can own more than one network (a named group of
cameras), and sharing is set per network, so a single row can list several.
Flock's public transparency portals list sharing partners at the organization
level only; they don't show which networks are shared.

As produced, the 344 organizations break down as:

| Direction | Organizations |
| --- | --- |
| Both ways | 230 |
| Share into Salinas only | 12 |
| Receive from Salinas only | 102 |

Salinas PD's own networks are `Salinas CA PD` (shared with 332
organizations), `Salinas CA PD (Condor)` (8) and `Salinas CA PD Raven` (6).

## Parsing notes

- When a cell holds more than one network, the names are joined with commas
  inside one quoted field. Some network names contain a comma themselves
  (e.g. `University of California, Berkeley`), so splitting on commas
  slightly over-counts networks.
- Some names carry leading or trailing spaces as produced; some network names
  repeat within a row.
- Rows are in the order produced.

Comparable exports from Los Altos PD:
`assets/los-altos-pras/json/pra-25-312/SharedNetworks.csv` (same three
columns) and `assets/los-altos-pras/json/pra-26-366/SharedNetworks_2026_July_6.csv`
(no `Networks I'm Sharing` column).
