import json
import re
from html import escape


ROOT_RELATIVE_ATTR_RE = re.compile(r'(?P<prefix>\b(?:href|src|action)=["\'])(?P<url>/[^"\']*)')


def inject_status_script(html: str, status_group_slug: str, cache_ttl_seconds: int) -> str:
    script = f"""
<script>
(() => {{
  const statusGroupSlug = {json.dumps(status_group_slug)};
  const refreshMs = {max(1, cache_ttl_seconds) * 1000};
  const consumptionEndpoint = window.location.origin + "/cch/status/weekly-consumption";
  const leaderboardEndpoint = window.location.origin + "/cch/status/weekly-leaderboard";
  const priceCoefficientsEndpoint = window.location.origin + "/cch/status/group-price-coefficients";
  const money = (value) => typeof value === "number"
    ? "$" + value.toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})
    : "—";
  const dateLabel = (value) => {{
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) return "";
    return `${{date.getMonth() + 1}}月${{date.getDate()}}日`;
  }};
  const compactNumber = (value) => typeof value === "number"
    ? value.toLocaleString(undefined, {{ maximumFractionDigits: 0 }})
    : "0";
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({{
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }}[char]));
  const percent = (value) => typeof value === "number" ? value.toFixed(2) + "%" : "—";
  const alpha = (token, pct) => `color-mix(in oklab, var(${{token}}) ${{pct}}%, transparent)`;
  const palette = (value) => {{
    if (typeof value !== "number") {{
      return {{
        border: "hsl(var(--border))",
        background: "hsl(var(--background) / .6)",
        color: "var(--muted-foreground)",
      }};
    }}
    if (value < 70) {{
      return {{
        border: "rgb(16 185 129 / .55)",
        background: "rgb(16 185 129 / .12)",
        color: "rgb(5 150 105)",
      }};
    }}
    if (value < 100) {{
      return {{
        border: "rgb(234 179 8 / .6)",
        background: "rgb(234 179 8 / .14)",
        color: "rgb(161 98 7)",
      }};
    }}
    return {{
      border: "rgb(239 68 68 / .6)",
      background: "rgb(239 68 68 / .14)",
      color: "rgb(220 38 38)",
    }};
  }};
  const pillStyle = (colors) =>
    `border:1px solid ${{colors.border}};border-radius:6px;padding:2px 8px;background:${{colors.background}};color:${{colors.color}};`;
  const findGroupHeader = () => {{
    const link = document.querySelector(`a[href$="/status/${{statusGroupSlug}}"]`);
    if (!link) return null;
    return link.closest("div.flex.items-center.gap-2") || link.parentElement;
  }};
  const findFirstGroupCard = () => {{
    const header = findGroupHeader();
    if (!header) return null;
    return header.closest("section") || header.closest('[class*="rounded"][class*="border"]');
  }};
  const scheduleRenderQuota = (data, attempts = 20) => {{
    if (renderQuota(data) || attempts <= 0) return;
    window.setTimeout(() => scheduleRenderQuota(data, attempts - 1), 250);
  }};
  const scheduleRenderLeaderboard = (data, attempts = 20) => {{
    if (renderLeaderboard(data) || attempts <= 0) return;
    window.setTimeout(() => scheduleRenderLeaderboard(data, attempts - 1), 250);
  }};
  const scheduleRenderPriceCoefficients = (data, attempts = 20) => {{
    if (renderPriceCoefficients(data) || attempts <= 0) return;
    window.setTimeout(() => scheduleRenderPriceCoefficients(data, attempts - 1), 250);
  }};
  const trophyStyle = (rank) => {{
    if (rank === 1) return "filter:none;background:rgb(250 204 21 / .16);border-color:rgb(250 204 21 / .55);";
    if (rank === 2) return "filter:grayscale(1) brightness(1.18);background:rgb(148 163 184 / .16);border-color:rgb(148 163 184 / .55);";
    return "filter:sepia(1) saturate(1.6) hue-rotate(330deg) brightness(.88);background:rgb(180 83 9 / .14);border-color:rgb(180 83 9 / .45);";
  }};
  const renderLeaderboard = (data) => {{
    if (!data.weeklyLeaderboardEnabled) return true;
    const entries = Array.isArray(data.leaderboard) ? data.leaderboard.slice(0, 3) : [];
    const existing = document.getElementById("cch-weekly-leaderboard");
    if (entries.length === 0) {{
      if (existing) existing.remove();
      return true;
    }}

    const firstGroupCard = findFirstGroupCard();
    if (!firstGroupCard || !firstGroupCard.parentElement) return false;

    let board = document.getElementById("cch-weekly-leaderboard");
    if (!board) {{
      board = document.createElement("section");
      board.id = "cch-weekly-leaderboard";
      board.style.cssText = "margin:0 0 16px 0;";
      firstGroupCard.parentElement.insertBefore(board, firstGroupCard);
    }}

    const cards = entries.map((entry) => `
      <article style="min-width:0;border:1px solid ${{alpha("--border", 60)}};border-radius:12px;background:${{alpha("--card", 40)}};padding:16px;display:flex;align-items:center;gap:12px;backdrop-filter:blur(4px);box-shadow:0 1px 2px rgb(0 0 0 / .05);">
        <div style="width:32px;height:32px;flex:0 0 auto;border:1px solid ${{alpha("--border", 60)}};border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:18px;line-height:1;background:${{alpha("--muted", 30)}};${{trophyStyle(entry.rank)}}">🏆</div>
        <div style="min-width:0;flex:1 1 auto;display:flex;align-items:center;justify-content:space-between;gap:10px;">
          <div style="min-width:0;">
            <div style="font-size:10px;text-transform:uppercase;letter-spacing:.025em;color:var(--muted-foreground);">第${{entry.rank}}名</div>
            <div style="margin-top:2px;font-size:14px;font-weight:600;color:var(--foreground);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${{escapeHtml(entry.name || "未知用户")}}</div>
          </div>
          <div style="flex:0 0 auto;text-align:right;">
            <div style="font-size:14px;font-weight:700;color:var(--foreground);">${{money(entry.usedUsd)}}</div>
            <div style="margin-top:2px;font-size:12px;color:var(--muted-foreground);">${{compactNumber(entry.requestCount)}} 次请求</div>
          </div>
        </div>
      </article>
    `).join("");

    const rangeStart = dateLabel(data.weekStart);
    const rangeEndDate = data.weekEnd ? new Date(data.weekEnd) : null;
    if (rangeEndDate && !Number.isNaN(rangeEndDate.getTime())) {{
      rangeEndDate.setDate(rangeEndDate.getDate() - 1);
    }}
    const rangeEnd = dateLabel(rangeEndDate);
    const rangeNote = rangeStart && rangeEnd ? `（${{rangeStart}}-${{rangeEnd}}）` : "";
    board.innerHTML = `
      <div style="margin-bottom:10px;font-size:14px;font-weight:600;color:var(--foreground);">本周消费排行榜${{rangeNote}}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;">${{cards}}</div>
    `;
    return true;
  }};
  const renderQuota = (data) => {{
    const header = findGroupHeader();
    if (!header) return false;
    let badge = document.getElementById("cch-weekly-consumption");
    if (!badge) {{
      badge = document.createElement("div");
      badge.id = "cch-weekly-consumption";
      badge.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-left:8px;font-size:12px;color:var(--muted-foreground);";
      header.appendChild(badge);
    }}
    const colors = palette(data.usagePct);
    const style = pillStyle(colors);
    badge.innerHTML = [
      `<span style="${{style}}">本周消费 ${{money(data.usedUsd)}} / ${{money(data.quotaUsd)}}</span>`,
      `<span style="${{style}}">${{percent(data.usagePct)}}</span>`,
      `<span style="${{style}}">剩余 ${{money(data.remainingUsd)}}</span>`,
    ].join("");
    return true;
  }};
  const groupSlugFromHref = (href) => {{
    const match = String(href || "").match(/\\/status\\/([^/?#]+)(?:[?#].*)?$/);
    return match ? decodeURIComponent(match[1]) : "";
  }};
  const coefficientBadgeStyle = () =>
    "border:1px solid rgb(16 185 129 / .55);border-radius:6px;background:rgb(16 185 129 / .12);padding:2px 8px;font-size:12px;line-height:1.25;color:rgb(5 150 105);";
  const renderPriceCoefficients = (data) => {{
    const groups = data && data.groups && typeof data.groups === "object" ? data.groups : null;
    if (!groups) return true;

    let renderedAny = false;
    const links = Array.from(document.querySelectorAll('a[href*="/status/"]'));
    for (const link of links) {{
      const slug = groupSlugFromHref(link.getAttribute("href"));
      if (!slug || !Array.isArray(groups[slug]) || groups[slug].length === 0) continue;
      if (!String(link.textContent || "").trim()) continue;

      const header = link.closest("div.flex.items-center.gap-2") || link.parentElement;
      if (!header) continue;
      link.style.flex = "0 1 auto";

      let badges = header.querySelector(`span[data-cch-price-coefficients="${{slug}}"]`);
      if (!badges) {{
        badges = document.createElement("span");
        badges.dataset.cchPriceCoefficients = slug;
        badges.style.cssText = "display:inline-flex;flex:0 0 auto;align-items:center;gap:4px;margin-left:6px;";
        link.insertAdjacentElement("afterend", badges);
      }}

      badges.innerHTML = groups[slug].map((value) =>
        `<span style="${{coefficientBadgeStyle()}}">${{escapeHtml(value)}}x</span>`
      ).join("");
      renderedAny = true;
    }}
    return renderedAny || document.querySelector('a[href*="/status/"]') !== null;
  }};
  const loadConsumption = () => {{
    fetch(consumptionEndpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("weekly consumption unavailable")))
      .then((body) => body && body.ok && scheduleRenderQuota(body.data))
      .catch(() => {{}});
  }};
  const loadLeaderboard = () => {{
    fetch(leaderboardEndpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("weekly leaderboard unavailable")))
      .then((body) => body && body.ok && scheduleRenderLeaderboard(body.data))
      .catch(() => {{}});
  }};
  const loadPriceCoefficients = () => {{
    fetch(priceCoefficientsEndpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("group price coefficients unavailable")))
      .then((body) => body && body.ok && scheduleRenderPriceCoefficients(body.data))
      .catch(() => {{}});
  }};
  const startEnhancements = () => {{
    loadConsumption();
    loadLeaderboard();
    loadPriceCoefficients();
    window.setInterval(loadConsumption, refreshMs);
    window.setInterval(loadLeaderboard, refreshMs);
    window.setInterval(loadPriceCoefficients, refreshMs);
  }};
  const waitForHydration = (attempts = 80) => {{
    if (window.next && document.querySelector("next-route-announcer")) {{
      startEnhancements();
      return;
    }}
    if (attempts <= 0) {{
      startEnhancements();
      return;
    }}
    window.setTimeout(() => waitForHydration(attempts - 1), 100);
  }};
  waitForHydration();
}})();
</script>
"""
    if "</body>" in html:
        return html.replace("</body>", script + "</body>", 1)
    return html + script


def rewrite_root_relative_urls(html: str, *, base_url: str, locale: str) -> str:
    def replace(match: re.Match[str]) -> str:
        url = match.group("url")
        if (
            url.startswith("//")
            or url.startswith("/_next/")
            or url.startswith(f"/{locale}/status")
        ):
            return match.group(0)
        return match.group("prefix") + escape(base_url + url, quote=True)

    return ROOT_RELATIVE_ATTR_RE.sub(replace, html)
