// Time-limited per-agency meeting banners — shared across pages.
//
// Pages that want banner support:
//   1. Add <script src="js/meeting_banners.js"></script> to the page head/body.
//   2. Call window.renderMeetingBannerHtml([agency_id, slug, name]) and
//      insert the returned HTML wherever the banner should appear.
//
// To add/remove a banner, edit the MEETING_BANNERS list below.
// `match` accepts any mix of identifiers (slug, agency_id, display name);
// agencies without a Flock portal still work via uuid or name.
// `expires` is the first YYYY-MM-DD on which the banner hides — typically
// the day after the meeting. Let entries self-expire, then remove.

(function() {
  "use strict";

  const MEETING_BANNERS = [
    {
      match: ["sunnyvale-ca-pd", "7ef62d29-d50a-5f12-85fe-6061de259c8d", "Sunnyvale CA PD"],
      meeting: "Sunnyvale City Council",
      when: "Tue, Apr 21 2026",
      expires: "2026-04-22",
      links: [
        { label: "Council agenda", url: "https://sunnyvaleca.legistar.com/MeetingDetail.aspx?ID=1351612&GUID=8D95399B-F698-47D0-A65E-09D6CBC92106" },
        { label: "Flock agenda item", url: "https://sunnyvaleca.legistar.com/LegislationDetail.aspx?ID=7986124&GUID=6726AE42-4486-4796-B885-A7155BDDE804" },
        { label: "Flyer: No Flock in Sunnyvale", url: "https://tinyurl.com/no-flock-in-sunnyvale" },
      ],
    },
    {
      match: ["alameda-county-ca-so", "9c1c5b61-7dec-5fce-a5ec-a3be9ed47c86", "Alameda County CA SO"],
      meeting: "Alameda County Board of Supervisors",
      when: "Tue, Apr 21 2026",
      expires: "2026-04-22",
      links: [
        { label: "Board agenda (PDF)", url: "https://alamedacountyca.gov/board/bos_calendar/documents/GranicusAgenda_04_21_26.pdf" },
        { label: "Organizer post", url: "https://www.instagram.com/p/DXP4JSfktwl/" },
      ],
    },
    {
      match: ["el-cerrito-ca-pd", "cb959829-2ddf-5780-841e-4d78cf1df75e", "El Cerrito CA PD"],
      meeting: "El Cerrito City Council",
      when: "Tue, Apr 21 2026",
      expires: "2026-04-22",
      links: [
        { label: "Council agenda", url: "https://www.elcerrito.gov/Calendar.aspx?EID=11171" },
        { label: "Flyer", url: "https://drive.proton.me/urls/RMFT9WY8N4#2EloSEBOnNtg" },
      ],
    },
    {
      match: ["east-palo-alto-ca-pd", "b0bd9add-ff5d-5af4-a468-0f03eb94214e", "East Palo Alto CA PD"],
      meeting: "East Palo Alto City Council",
      when: "Tue, Apr 21 2026",
      expires: "2026-04-22",
      links: [
        { label: "Council agendas", url: "https://www.cityofepa.org/citycouncil/page/agenda-and-minutes" },
        { label: "Organizer doc", url: "https://docs.google.com/document/d/1l1sNmOQKw2kBzTw-Ww2nrugmMceaL9t7ZzJzI0A0ZRs/edit" },
      ],
    },
  ];

  const STYLES = `
    .meeting-banner {
      background: #fffbeb;
      border: 1px solid #f59e0b;
      border-left: 5px solid #f59e0b;
      border-radius: 6px;
      padding: 10px 14px;
      margin: 0 0 14px 0;
    }
    .meeting-banner-title {
      font-weight: bold;
      font-size: 10.5px;
      color: #92400e;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .meeting-banner-when {
      font-size: 13px;
      color: #78350f;
      font-weight: 600;
      margin: 3px 0 8px 0;
    }
    .meeting-banner-links {
      display: flex;
      flex-wrap: wrap;
      gap: 4px 16px;
    }
    .meeting-banner-links a {
      font-size: 12.5px;
      color: #b45309;
      font-weight: 600;
      text-decoration: none;
    }
    .meeting-banner-links a:hover { text-decoration: underline; }
  `;

  function injectStyles() {
    if (document.getElementById("meeting-banner-style")) return;
    const s = document.createElement("style");
    s.id = "meeting-banner-style";
    s.textContent = STYLES;
    document.head.appendChild(s);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectStyles);
  } else {
    injectStyles();
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  window.activeMeetingBanner = function(identifiers) {
    const today = new Date().toISOString().slice(0, 10);
    const idSet = new Set((identifiers || []).filter(Boolean));
    for (const b of MEETING_BANNERS) {
      if (today >= b.expires) continue;
      if (b.match.some(m => idSet.has(m))) return b;
    }
    return null;
  };

  window.renderMeetingBannerHtml = function(identifiers) {
    const b = window.activeMeetingBanner(identifiers);
    if (!b) return "";
    let html = '<div class="meeting-banner" role="note">';
    html += '<div class="meeting-banner-title">Upcoming public meeting</div>';
    html += '<div class="meeting-banner-when">' + esc(b.meeting) + " \u00b7 " + esc(b.when) + "</div>";
    html += '<div class="meeting-banner-links">';
    b.links.forEach(function(l) {
      html += '<a href="' + esc(l.url) + '" target="_blank" rel="noopener noreferrer">' + esc(l.label) + " \u2192</a>";
    });
    html += "</div></div>";
    return html;
  };
})();
