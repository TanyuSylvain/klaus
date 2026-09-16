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
  const groupUsageEndpoint = window.location.origin + "/cch/status/group-usage";
  const providerUsageEndpoint = window.location.origin + "/cch/status/provider-usage";
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
  const tokensM = (value) => {{
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    const millions = value / 1e6;
    const digits = millions >= 100 ? 0 : 1;
    return millions.toLocaleString(undefined, {{
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }}) + " M";
  }};
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
    if (value < 90) {{
      return {{
        border: "rgb(249 115 22 / .6)",
        background: "rgb(249 115 22 / .14)",
        color: "rgb(194 65 12)",
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
  const countdownStr = (resetsAt) => {{
    if (!resetsAt) return null;
    const diffMs = new Date(resetsAt).getTime() - Date.now();
    if (!Number.isFinite(diffMs) || diffMs <= 0) return null;
    const hours = Math.floor(diffMs / (1000 * 60 * 60));
    const minutes = Math.floor((diffMs % (1000 * 60 * 60)) / (1000 * 60));
    if (hours >= 24) {{
      const days = Math.floor(hours / 24);
      return `${{days}}d${{hours % 24}}h`;
    }}
    if (hours > 0) return `${{hours}}h${{minutes}}m`;
    return `${{minutes}}m`;
  }};
  const tierLabel = (name) => {{
    if (name === "five_hour") return "5h";
    if (name === "weekly_limit" || name === "seven_day") return "周";
    return String(name || "");
  }};
  const usageTierPills = (tiers) => (Array.isArray(tiers) ? tiers : []).map((tier) => {{
    const util = typeof tier.utilization === "number" ? tier.utilization : null;
    const style = pillStyle(palette(util));
    const label = tierLabel(tier.name);
    const utilText = typeof util === "number" ? Math.round(util) + "%" : "—";
    const countdown = countdownStr(tier.resetsAt);
    const countdownText = countdown ? ` · ${{countdown}}` : "";
    return `<span style="${{style}}">${{escapeHtml(label)}} ${{utilText}}${{countdownText}}</span>`;
  }}).join("");
  let usageTooltipSlug = null;
  const ensureUsageTooltip = () => {{
    let tip = document.getElementById("cch-provider-usage-tooltip");
    if (tip) return tip;
    tip = document.createElement("div");
    tip.id = "cch-provider-usage-tooltip";
    tip.style.cssText = `position:fixed;z-index:2147483647;display:none;pointer-events:none;min-width:140px;max-width:min(360px,calc(100vw - 16px));padding:10px;border:1px solid ${{alpha("--border", 60)}};border-radius:10px;background:hsl(var(--card) / .96);color:var(--foreground);box-shadow:0 8px 24px rgb(0 0 0 / .18);backdrop-filter:blur(8px);font-size:12px;line-height:1.25;`;
    document.body.appendChild(tip);
    return tip;
  }};
  const hideUsageTooltip = () => {{
    usageTooltipSlug = null;
    const tip = document.getElementById("cch-provider-usage-tooltip");
    if (tip) tip.style.display = "none";
  }};
  const positionUsageTooltip = (anchor, tip) => {{
    const rect = anchor.getBoundingClientRect();
    const gap = 8;
    const tipRect = tip.getBoundingClientRect();
    let left = rect.left;
    let top = rect.bottom + gap;
    if (left + tipRect.width > window.innerWidth - 8) {{
      left = window.innerWidth - tipRect.width - 8;
    }}
    if (left < 8) left = 8;
    if (top + tipRect.height > window.innerHeight - 8) {{
      top = rect.top - tipRect.height - gap;
    }}
    if (top < 8) top = 8;
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }};
  const showUsageTooltip = (anchor, slug, group) => {{
    const keyUsages = Array.isArray(group && group.keyUsages) ? group.keyUsages : [];
    if (keyUsages.length === 0) {{
      hideUsageTooltip();
      return;
    }}
    const tip = ensureUsageTooltip();
    usageTooltipSlug = slug;
    tip.innerHTML = keyUsages.map((entry) => {{
      if (!entry || !entry.success) {{
        const style = pillStyle(palette(null));
        return `<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;"><span style="${{style}}">用量查询失败</span></div>`;
      }}
      const opacity = entry.stale ? "0.55" : "1";
      return `<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;opacity:${{opacity}};">${{usageTierPills(entry.tiers)}}</div>`;
    }}).join(`<div style="height:8px;"></div>`);
    tip.style.display = "block";
    positionUsageTooltip(anchor, tip);
  }};
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
  const scheduleRenderProviderUsage = (data, attempts = 20) => {{
    if (renderProviderUsage(data) || attempts <= 0) return;
    window.setTimeout(() => scheduleRenderProviderUsage(data, attempts - 1), 250);
  }};
  const scheduleRenderGroupUsage = (data, attempts = 20) => {{
    if (renderGroupUsage(data) || attempts <= 0) return;
    window.setTimeout(() => scheduleRenderGroupUsage(data, attempts - 1), 250);
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
    const totalCostNote = typeof data.weeklyTotalUsedUsd === "number"
      ? `, 总消费：${{money(data.weeklyTotalUsedUsd)}}`
      : "";
    const totalRequestsNote = typeof data.weeklyTotalRequests === "number"
      ? `, 总请求：${{compactNumber(data.weeklyTotalRequests)}}`
      : "";
    const rangeNote = rangeStart && rangeEnd ? `（${{rangeStart}}-${{rangeEnd}}${{totalCostNote}}${{totalRequestsNote}}）` : "";
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
  const usageBadgeColors = {{
    border: "rgb(59 130 246 / .55)",
    background: "rgb(59 130 246 / .12)",
    color: "rgb(37 99 235)",
  }};
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
  let lastProviderUsageData = null;
  // Same mount strategy as weekly consumption (renderQuota): find the group
  // header row via the title link, then appendChild a badge strip as the last
  // flex child. Do not nest into span.ml-auto or walk Collapsible siblings.
  const findHeaderForSlug = (slug) => {{
    const links = Array.from(document.querySelectorAll('a[href*="/status/"]'));
    for (const link of links) {{
      if (groupSlugFromHref(link.getAttribute("href")) !== slug) continue;
      // Prefer the titled group link (skip icon-only external-link anchors).
      if (!String(link.textContent || "").trim()) continue;
      return link.closest("div.flex.items-center.gap-2") || link.parentElement;
    }}
    return null;
  }};
  const renderProviderUsage = (data) => {{
    const groups = Array.isArray(data && data.groups) ? data.groups : [];
    if (groups.length === 0) {{
      // Drop the cached payload too, otherwise the countdown refresh below
      // re-adds badges the server has since stopped reporting.
      lastProviderUsageData = null;
      hideUsageTooltip();
      document.querySelectorAll("[data-cch-provider-usage]").forEach((el) => el.remove());
      return true;
    }}
    lastProviderUsageData = data;

    const bySlug = new Map();
    for (const group of groups) {{
      const slugs = Array.isArray(group.statusGroupSlugs) ? group.statusGroupSlugs : [];
      for (const slug of slugs) {{
        if (!slug) continue;
        if (!bySlug.has(slug)) bySlug.set(slug, group);
      }}
    }}

    let renderedAny = false;
    for (const [slug, group] of bySlug.entries()) {{
      const header = findHeaderForSlug(slug);
      if (!header) continue;

      // Drop any stale node left in the wrong parent (e.g. model grid).
      document.querySelectorAll(`span[data-cch-provider-usage="${{slug}}"]`).forEach((el) => {{
        if (el.parentElement !== header) el.remove();
      }});

      let badges = header.querySelector(`:scope > span[data-cch-provider-usage="${{slug}}"]`);
      if (!badges) {{
        badges = document.createElement("span");
        badges.dataset.cchProviderUsage = slug;
        // Match weekly consumption strip: flex row of pills after header controls.
        badges.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-left:8px;font-size:12px;color:var(--muted-foreground);";
        header.appendChild(badges);
      }}

      // The server keeps serving the last good reading through a transient
      // query failure; dim it so a frozen number is not mistaken for a live one.
      badges.style.opacity = group.stale ? "0.55" : "1";
      if (group.stale) {{
        badges.title = "上次成功查询的数据，最新一次查询失败";
      }} else {{
        badges.removeAttribute("title");
      }}

      if (!group.success) {{
        const style = pillStyle(palette(null));
        badges.innerHTML = `<span style="${{style}}">用量查询失败</span>`;
      }} else {{
        const tiers = Array.isArray(group.tiers) ? group.tiers : [];
        badges.innerHTML = usageTierPills(tiers);
      }}
      if (Array.isArray(group.keyUsages) && group.keyUsages.length > 0 && badges.innerHTML) {{
        badges.style.cursor = "help";
        badges.onmouseenter = () => showUsageTooltip(badges, slug, group);
        badges.onmouseleave = hideUsageTooltip;
        if (usageTooltipSlug === slug) showUsageTooltip(badges, slug, group);
      }} else {{
        badges.style.cursor = "";
        badges.onmouseenter = null;
        badges.onmouseleave = null;
        if (usageTooltipSlug === slug) hideUsageTooltip();
      }}
      renderedAny = true;
    }}
    return renderedAny || bySlug.size === 0;
  }};
  const renderGroupUsage = (data) => {{
    const groups = data && data.groups && typeof data.groups === "object" ? data.groups : null;
    if (!groups) {{
      document.querySelectorAll("[data-cch-group-usage]").forEach((el) => el.remove());
      return true;
    }}

    const renderedSlugs = new Set();
    let renderedAny = false;
    for (const [slug, entry] of Object.entries(groups)) {{
      if (!slug || !entry || typeof entry !== "object") continue;
      const header = findHeaderForSlug(slug);
      if (!header) continue;

      document.querySelectorAll(`span[data-cch-group-usage="${{slug}}"]`).forEach((el) => {{
        if (el.parentElement !== header) el.remove();
      }});

      let badges = header.querySelector(`:scope > span[data-cch-group-usage="${{slug}}"]`);
      if (!badges) {{
        badges = document.createElement("span");
        badges.dataset.cchGroupUsage = slug;
        badges.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-left:8px;font-size:12px;color:var(--muted-foreground);";
        header.appendChild(badges);
      }}

      const style = pillStyle(usageBadgeColors);
      const pills = [
        `<span style="${{style}}">${{compactNumber(entry.requestCount)}} 次</span>`,
        `<span style="${{style}}">${{tokensM(entry.totalTokens)}}</span>`,
      ];
      // The configured status group (opus) already shows weekly cost/quota
      // pills; omit the compact cost tag there.
      if (slug !== statusGroupSlug) {{
        pills.push(`<span style="${{style}}">${{money(entry.costUsd)}}</span>`);
      }}
      badges.innerHTML = pills.join("");
      renderedSlugs.add(slug);
      renderedAny = true;
    }}

    document.querySelectorAll("[data-cch-group-usage]").forEach((el) => {{
      if (!renderedSlugs.has(el.getAttribute("data-cch-group-usage"))) el.remove();
    }});
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
  const loadProviderUsage = () => {{
    fetch(providerUsageEndpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("provider usage unavailable")))
      .then((body) => body && body.ok && scheduleRenderProviderUsage(body.data))
      .catch(() => {{}});
  }};
  const loadGroupUsage = () => {{
    fetch(groupUsageEndpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("group usage unavailable")))
      .then((body) => body && body.ok && scheduleRenderGroupUsage(body.data))
      .catch(() => {{}});
  }};
  const startEnhancements = () => {{
    loadConsumption();
    loadLeaderboard();
    loadPriceCoefficients();
    loadProviderUsage();
    loadGroupUsage();
    window.setInterval(loadConsumption, refreshMs);
    window.setInterval(loadLeaderboard, refreshMs);
    window.setInterval(loadPriceCoefficients, refreshMs);
    window.setInterval(loadProviderUsage, Math.min(refreshMs, 30000));
    window.setInterval(loadGroupUsage, refreshMs);
    window.setInterval(() => {{
      if (lastProviderUsageData) renderProviderUsage(lastProviderUsageData);
    }}, 30000);
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
