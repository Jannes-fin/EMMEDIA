import os
import re
import json
import time
import random
import logging
import argparse
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from ddgs import DDGS
import anthropic
from apscheduler.schedulers.blocking import BlockingScheduler

from agents.prompts import RESEARCH_AGENT_PROMPT, SUMMARY_AGENT_PROMPT, KEYWORD_DISCOVERY_PROMPT, LEARNING_UPDATE_PROMPT

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(f"logs/{datetime.now().strftime('%Y-%m-%d')}.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── Load keywords ─────────────────────────────────────────────────────────────
def load_keywords():
    with open("keywords.json", "r") as f:
        data = json.load(f)
    return data["keywords"], data.get("exclude", [])

# ── Load existing partners ────────────────────────────────────────────────────
def load_existing_partners():
    path = "existing_partners.json"
    if not os.path.exists(path):
        return [], {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    partners = data.get("partners", [])
    names = {p["name"].lower() for p in partners}
    return partners, names

# ── Load / save learnings ─────────────────────────────────────────────────────
def load_learnings():
    path = "learnings.json"
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_learnings(data):
    with open("learnings.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ── DDG wrapper with retry + backoff ─────────────────────────────────────────
def ddgs_search(query, max_results=8, retries=3):
    for attempt in range(retries):
        try:
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    results.append(r)
            return results
        except Exception as e:
            if attempt < retries - 1:
                wait = 8 * (attempt + 1)
                log.warning(f"DDG rate limit, retrying in {wait}s... ({e})")
                time.sleep(wait)
            else:
                log.warning(f"DDG search failed after {retries} attempts: {e}")
                return []

# ── Headers for scraping ──────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

X_RESERVED_PATHS = {
    "home", "search", "explore", "i", "intent", "share", "notifications",
    "messages", "settings", "login", "logout", "signup", "compose"
}
X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def extract_x_handle(text_or_url):
    """Return the first valid X/Twitter handle found in text or URL."""
    if not text_or_url:
        return ""

    text = str(text_or_url)
    url_matches = re.finditer(
        r"https?://(?:www\.)?(?:x|twitter)\.com/([^/?#\s]+)",
        text,
        flags=re.IGNORECASE
    )
    for match in url_matches:
        handle = match.group(1).strip("@").lower()
        if handle not in X_RESERVED_PATHS and X_HANDLE_RE.fullmatch(handle):
            return handle

    mention_matches = re.finditer(r"(?<![\w.])@([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_-])", text)
    for match in mention_matches:
        handle = match.group(1).lower()
        if handle not in X_RESERVED_PATHS and X_HANDLE_RE.fullmatch(handle):
            return handle

    return ""


def safe_get(url, timeout=10):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.warning(f"Failed to fetch {url}: {e}")
        return ""

# ── Source 1a: Whop — listings with Discord link ──────────────────────────────
def search_whop_discord(keyword):
    """Hunt Whop listings that visibly link a discord.gg — owner reachable via DM in their server."""
    log.info(f"[Whop+Discord] {keyword}")
    results = []
    seen = set()

    queries = [
        f'site:whop.com {keyword} "discord.gg"',
        f'site:whop.com {keyword} discord community',
    ]
    for query in queries:
        for r in ddgs_search(query, max_results=10, retries=2):
            href = r.get("href", "")
            if "whop.com/" not in href or href in seen:
                continue
            seen.add(href)
            body = r.get("body", "")
            invites = re.findall(r'discord\.gg/[A-Za-z0-9]+', body)
            results.append({
                "source": "whop_discord",
                "title": r.get("title", "")[:120],
                "url": href,
                "description": body[:300],
                "discord_invite": f"https://{invites[0]}" if invites else "",
                "has_discord": bool(invites) or "discord" in body.lower(),
                "keyword": keyword
            })
        time.sleep(1)

    # Also try scraping listing pages for discord.gg links
    for item in list(results)[:5]:
        try:
            html = safe_get(item["url"], timeout=8)
            if html:
                found = re.findall(r'discord\.gg/[A-Za-z0-9]+', html)
                junk = {"wumpus", "discord-testers", "discord", "example", "invite"}
                found = [i for i in found if i.split("/")[-1].lower() not in junk]
                if found:
                    item["discord_invite"] = f"https://{found[0]}"
                    item["has_discord"] = True
            time.sleep(0.5)
        except Exception:
            pass

    log.info(f"[Whop+Discord] {len(results)} results for '{keyword}'")
    return results[:8]


# ── Source 1b: Whop — listings with free trial / free plan ───────────────────
def search_whop_free_trial(keyword):
    """Hunt Whop listings that offer a free plan or free trial — easy entry point."""
    log.info(f"[Whop+Free] {keyword}")
    results = []
    seen = set()

    queries = [
        f'site:whop.com {keyword} "free trial"',
        f'site:whop.com {keyword} "free plan" OR "free access" OR "free tier"',
        f'site:whop.com {keyword} free',
    ]
    for query in queries:
        for r in ddgs_search(query, max_results=8, retries=2):
            href = r.get("href", "")
            if "whop.com/" not in href or href in seen:
                continue
            seen.add(href)
            body = r.get("body", "")
            free_signals = ["free trial", "free plan", "free access", "free tier", "try for free", "join free"]
            has_free = any(s in body.lower() for s in free_signals)
            invites = re.findall(r'discord\.gg/[A-Za-z0-9]+', body)
            results.append({
                "source": "whop_free",
                "title": r.get("title", "")[:120],
                "url": href,
                "description": body[:300],
                "has_free_trial": has_free,
                "discord_invite": f"https://{invites[0]}" if invites else "",
                "keyword": keyword
            })
        time.sleep(1)

    log.info(f"[Whop+Free] {len(results)} results for '{keyword}'")
    return results[:8]


# ── Source 1c: Whop — general (fallback, small allocation) ───────────────────
def search_whop(keyword):
    log.info(f"[Whop] {keyword}")
    results = []
    seen = set()
    try:
        queries = [
            f"site:whop.com {keyword}",
            f"site:whop.com {keyword} group OR community OR bot OR proxy",
        ]
        for query in queries:
            for r in ddgs_search(query, max_results=10, retries=2):
                href = r.get("href", "")
                if "whop.com/" not in href or href in seen:
                    continue
                seen.add(href)
                body = r.get("body", "")
                results.append({
                    "source": "whop",
                    "title": r.get("title", "")[:120],
                    "url": href,
                    "description": body[:300],
                    "keyword": keyword
                })
            time.sleep(0.8)
            if len(results) >= 12:
                break
    except Exception as e:
        log.error(f"Whop DDG error: {e}")
    log.info(f"[Whop] {len(results)} results for '{keyword}'")
    return results[:12]

# ── Source 2: Reddit ──────────────────────────────────────────────────────────
def search_reddit(keyword):
    log.info(f"[Reddit] {keyword}")
    results = []
    try:
        query = keyword.replace(" ", "+")
        url = f"https://www.reddit.com/search.json?q={query}&type=sr&limit=10"
        r = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=10)
        data = r.json()
        for item in data.get("data", {}).get("children", []):
            d = item.get("data", {})
            results.append({
                "source": "reddit",
                "title": d.get("display_name_prefixed", ""),
                "url": f"https://reddit.com{d.get('url', '')}",
                "description": d.get("public_description", "")[:200],
                "subscribers": d.get("subscribers", 0),
                "keyword": keyword
            })
        log.info(f"[Reddit] {len(results)} results for '{keyword}'")
    except Exception as e:
        log.error(f"Reddit error: {e}")
    return results[:2]

# ── Source 3: Web via DDG ─────────────────────────────────────────────────────
def search_web(keyword):
    log.info(f"[Web] {keyword}")
    results = []
    skip_domains = ["google.com", "youtube.com", "amazon.com", "ebay.com", "facebook.com"]
    try:
        query = f"{keyword} discord OR forum OR community"
        for r in ddgs_search(query, max_results=10):
            href = r.get("href", "")
            if any(s in href for s in skip_domains):
                continue
            results.append({
                "source": "web",
                "title": r.get("title", "")[:120],
                "url": href,
                "description": r.get("body", "")[:200],
                "keyword": keyword
            })
            if len(results) >= 6:
                break
        log.info(f"[Web] {len(results)} results for '{keyword}'")
    except Exception as e:
        log.error(f"Web error: {e}")
    return results

# ── Source 4: Twitter/X — specifically hunting profiles that link Discord ────
def search_twitter(keyword):
    log.info(f"[Twitter] {keyword}")
    results = []
    seen = set()
    try:
        # Two passes: general community search + targeted discord.gg link hunt
        for query in [
            f"site:x.com {keyword} discord.gg",
            f"site:x.com {keyword} discord OR community OR group",
        ]:
            for r in ddgs_search(query, max_results=8):
                href = r.get("href", "")
                if ("x.com/" not in href and "twitter.com/" not in href) or href in seen:
                    continue
                seen.add(href)
                body = r.get("body", "")
                handle = extract_x_handle(href) or extract_x_handle(body) or extract_x_handle(r.get("title", ""))
                if not handle:
                    continue
                has_discord = "discord.gg" in body or "discord.gg" in href
                results.append({
                    "source": "twitter",
                    "title": r.get("title", "")[:120],
                    "url": href,
                    "description": body[:200],
                    "has_discord_link": has_discord,
                    "x_handle": handle,
                    "profile_url": f"https://x.com/{handle}",
                    "discovered_via": f"twitter_search:{keyword}",
                    "keyword": keyword
                })
                if len(results) >= 8:
                    break
            if len(results) >= 8:
                break
        log.info(f"[Twitter] {len(results)} results for '{keyword}'")
    except Exception as e:
        log.error(f"Twitter error: {e}")
    return results

# ── Source 5: Skool ───────────────────────────────────────────────────────────
def search_skool(keyword):
    log.info(f"[Skool] {keyword}")
    results = []
    try:
        query = f"site:skool.com {keyword}"
        for r in ddgs_search(query, max_results=6):
            href = r.get("href", "")
            if "skool.com/" not in href:
                continue
            results.append({
                "source": "skool",
                "title": r.get("title", "")[:120],
                "url": href,
                "description": r.get("body", "")[:200],
                "keyword": keyword
            })
            if len(results) >= 4:
                break
        log.info(f"[Skool] {len(results)} results for '{keyword}'")
    except Exception as e:
        log.error(f"Skool error: {e}")
    return results

# ── Source 6: Telegram ────────────────────────────────────────────────────────
def search_telegram(keyword):
    log.info(f"[Telegram] {keyword}")
    results = []
    seen = set()
    try:
        for query in [
            f"site:t.me {keyword} resell OR group OR channel",
            f"telegram {keyword} resell group OR channel",
            f"t.me {keyword} bot OR proxy OR cook OR automation",
        ]:
            for r in ddgs_search(query, max_results=15):
                href = r.get("href", "")
                if "t.me/" not in href or href in seen:
                    continue
                seen.add(href)
                results.append({
                    "source": "telegram",
                    "title": r.get("title", "")[:120],
                    "url": href,
                    "description": r.get("body", "")[:200],
                    "keyword": keyword
                })
                if len(results) >= 15:
                    break
            if len(results) >= 15:
                break
        log.info(f"[Telegram] {len(results)} results for '{keyword}'")
    except Exception as e:
        log.error(f"Telegram error: {e}")
    return results

# ── Source 7: Discord community search (direct discord.gg links, no Disboard) ─
def search_discord(keyword):
    log.info(f"[Discord] {keyword}")
    results = []
    seen = set()
    try:
        for query in [
            f'"{keyword}" discord.gg cook OR bot OR proxy OR automation',
            f"{keyword} discord invite cook group OR bot group OR proxy",
        ]:
            for r in ddgs_search(query, max_results=10):
                href = r.get("href", "")
                if "disboard.org" in href or href in seen:
                    continue
                body = r.get("body", "")
                invites = re.findall(r'discord\.gg/[A-Za-z0-9]+', body + " " + href)
                if not invites:
                    continue  # only include if we extracted an actual discord.gg link
                seen.add(href)
                results.append({
                    "source": "discord",
                    "title": r.get("title", "")[:120],
                    "url": href,
                    "description": body[:200],
                    "discord_invite": f"https://{invites[0]}",
                    "keyword": keyword
                })
                if len(results) >= 8:
                    break
            if len(results) >= 8:
                break
        log.info(f"[Discord] {len(results)} results for '{keyword}'")
    except Exception as e:
        log.error(f"Discord search error: {e}")
    return results

# ── Source 8: Niche forums (per-niche internet forum search) ─────────────────
_NICHE_FORUM_SIGNALS = {
    "ticket": ["twickets", "ticombo", "fanswap", "ticketswapping", "lyte", "bot", "automation", "forum", "community"],
    "sneaker": ["niketalk", "sneakernews", "bot", "aio", "proxy", "cook", "forum", "community"],
    "general_resell": ["slickdeals", "brickseek", "bot", "monitor", "restock", "proxy", "forum", "community"],
}
_SKIP_DOMAINS = ["youtube.com", "amazon.com", "ebay.com", "google.com", "wikipedia.org",
                 "reddit.com", "facebook.com", "instagram.com"]

def search_niche_forums(keyword, niche):
    log.info(f"[Forums/{niche}] {keyword}")
    results = []
    signals = _NICHE_FORUM_SIGNALS.get(niche, ["forum", "community"])
    try:
        query = f"{keyword} forum community owner contact"
        for r in ddgs_search(query, max_results=14):
            href = r.get("href", "")
            if any(s in href for s in _SKIP_DOMAINS):
                continue
            if any(s in href or s in r.get("body", "").lower() for s in signals):
                results.append({
                    "source": "forum",
                    "title": r.get("title", "")[:120],
                    "url": href,
                    "description": r.get("body", "")[:200],
                    "keyword": keyword
                })
            if len(results) >= 5:
                break
        log.info(f"[Forums/{niche}] {len(results)} results for '{keyword}'")
    except Exception as e:
        log.error(f"Forum search error: {e}")
    return results

# ── Telegram activity check ───────────────────────────────────────────────────
def check_telegram_active(url):
    """
    Returns (is_active, member_count, days_since_last_post).
    Fetches t.me/s/channel preview page and checks for recent messages.
    Returns (False, 0, 999) if the group looks dead or unreachable.
    """
    try:
        # Normalise to preview URL: t.me/name -> t.me/s/name
        username = url.rstrip("/").split("t.me/")[-1].lstrip("s/").lstrip("+")
        if not username or len(username) < 2:
            return True, 0, 0  # Can't check, assume ok
        preview_url = f"https://t.me/s/{username}"
        html = safe_get(preview_url)
        if not html:
            return True, 0, 0  # Can't reach, don't discard

        soup = BeautifulSoup(html, "lxml")

        # Member count
        members = 0
        extra = soup.find(class_="tgme_page_extra")
        if extra:
            text = extra.get_text()
            nums = re.findall(r'[\d\s,]+', text)
            for n in nums:
                try:
                    val = int(n.replace(",", "").replace(" ", ""))
                    if val > members:
                        members = val
                except ValueError:
                    pass

        # Last post date — look for datetime attrs in message elements
        days_since = 0
        dates = soup.find_all("time")
        if dates:
            last_dt = None
            for t in dates:
                dt_str = t.get("datetime", "")
                if dt_str:
                    try:
                        from datetime import datetime, timezone
                        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                        if last_dt is None or dt > last_dt:
                            last_dt = dt
                    except Exception:
                        pass
            if last_dt:
                now = datetime.now(timezone.utc)
                days_since = (now - last_dt).days

        # Dead if: last post > 60 days ago, or member count < 30 with no recent posts
        if days_since > 60:
            log.info(f"  [DEAD] {url} — last post {days_since}d ago")
            return False, members, days_since
        if members > 0 and members < 30:
            log.info(f"  [DEAD] {url} — only {members} members")
            return False, members, days_since

        return True, members, days_since
    except Exception as e:
        log.warning(f"Telegram check failed for {url}: {e}")
        return True, 0, 0  # On error, keep the lead


# ── Link enrichment: find + verify Discord invites + check Telegram activity ──
def enrich_and_verify_leads(leads):
    if not leads:
        return leads
    log.info(f"Enriching {len(leads)} leads...")

    active_leads = []
    for lead in leads:
        name = lead.get("name", "")
        contact = lead.get("contact", "")

        # Check Telegram groups for activity — drop dead ones immediately
        tg_urls = re.findall(r'https?://t\.me/[A-Za-z0-9_/+]+', (contact or "") + " " + (lead.get("description") or ""))
        if tg_urls or lead.get("type") == "telegram_group":
            url_to_check = tg_urls[0] if tg_urls else contact.split()[0]
            is_active, members, days = check_telegram_active(url_to_check)
            if not is_active:
                log.info(f"  Dropping dead Telegram lead: {name}")
                continue
            if members:
                lead["member_count"] = members
                lead["size_estimate"] = (
                    "large" if members > 8000 else
                    "medium" if members > 1000 else "small"
                )
        active_leads.append(lead)

    leads = active_leads
    log.info(f"  {len(leads)} leads remain after activity check")

    for lead in leads:
        name = lead.get("name", "") or ""
        contact = lead.get("contact", "") or ""
        description = lead.get("description", "") or ""

        # Extract any discord.gg links already present
        all_text = contact + " " + description + " " + (lead.get("discord_invite") or "")
        found_invites = re.findall(r'discord\.gg/[A-Za-z0-9]+', all_text)

        # If none found, search specifically for this group
        if not found_invites and name:
            try:
                query = f'"{name}" discord.gg OR discord invite'
                for r in ddgs_search(query, max_results=4, retries=1):
                    combined = r.get("body", "") + " " + r.get("href", "") + " " + r.get("title", "")
                    found = re.findall(r'discord\.gg/[A-Za-z0-9]+', combined)
                    if found:
                        found_invites = found
                        break
                time.sleep(1)
            except Exception:
                pass

        # Filter out placeholder/example codes that appear on web pages as examples
        _junk = {"wumpus", "discord-testers", "discord", "example", "invite", "join", "verify", "receiptify"}
        found_invites = [i for i in found_invites if i.split("/")[-1].lower() not in _junk]

        # Verify via Discord API
        if found_invites:
            code = found_invites[0].split("/")[-1]
            invite_url = f"https://discord.gg/{code}"
            try:
                resp = requests.get(
                    f"https://discord.com/api/v9/invites/{code}?with_counts=true",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=5
                )
                if resp.status_code == 200:
                    data = resp.json()
                    members = data.get("approximate_member_count", 0)
                    lead["discord_invite"] = invite_url
                    lead["invite_verified"] = True
                    if members:
                        lead["member_count"] = members
                        lead["size_estimate"] = (
                            "large" if members > 10000 else
                            "medium" if members > 1000 else "small"
                        )
                    log.info(f"  [OK] {name}: {invite_url} ({members} members)")
                else:
                    log.info(f"  [EXPIRED/DEAD] {name}: {invite_url} — dropping")
                    lead["_dead"] = True
            except Exception:
                lead["discord_invite"] = invite_url
                lead["invite_verified"] = None
        time.sleep(0.3)

    before = len(leads)
    leads = [l for l in leads if not l.get("_dead")]
    dropped = before - len(leads)
    if dropped:
        log.info(f"  Dropped {dropped} dead/expired Discord server(s)")
    verified = sum(1 for l in leads if l.get("invite_verified") is True)
    log.info(f"Enrichment done: {verified}/{len(leads)} invites verified")
    return leads

# ── Keyword niche inference ───────────────────────────────────────────────────
def infer_niche(keyword):
    kw = keyword.lower()
    if any(t in kw for t in [
        "ticket", "concert", "event", "axs", "dice", "ticketmaster",
        "see tickets", "scalp", "queue bot", "checkout tool"
    ]):
        return "ticket"
    if any(t in kw for t in [
        "sneaker", "nike", "jordan", "yeezy", "adidas", "supreme",
        "snkrs", "footlocker", "aio bot", "aio cook", "sneaker bot",
        "sneaker proxy", "sneaker monitor", "sneaker cook"
    ]):
        return "sneaker"
    return "general_resell"

# ── Weighted keyword sampling (biased toward high-gap niches) ─────────────────
def sample_keywords_weighted(keywords, size=25):
    learnings = load_learnings()
    niche_perf = learnings.get("niche_performance", {})

    # Weight = gap_score (opportunity) + small bonus per lead already found (momentum)
    niche_weights = {}
    for niche, data in niche_perf.items():
        gap = data.get("gap_score", 5)
        leads = data.get("leads_total", 0)
        niche_weights[niche] = max(1, gap + min(leads * 0.3, 5))

    # Assign weight to each keyword based on its niche
    kw_weights = []
    for kw in keywords:
        niche = infer_niche(kw)
        kw_weights.append(niche_weights.get(niche, 5))

    # Weighted sampling without replacement
    pool = list(zip(keywords, kw_weights))
    selected = []
    for _ in range(min(size, len(pool))):
        total = sum(w for _, w in pool)
        if total == 0:
            break
        r = random.uniform(0, total)
        cumulative = 0
        for i, (kw, w) in enumerate(pool):
            cumulative += w
            if cumulative >= r:
                selected.append(kw)
                pool.pop(i)
                break

    # Log niche distribution of selected keywords
    niche_dist = {}
    for kw in selected:
        n = infer_niche(kw)
        niche_dist[n] = niche_dist.get(n, 0) + 1
    log.info(f"Weighted keyword sample — niche distribution: {niche_dist}")
    return selected

# ── Scrape a Whop page for Discord + free trial signals ───────────────────────
def scrape_whop_page(item):
    """Enrich a Whop listing dict in-place with discord_invite + has_free_trial."""
    url = item.get("url", "")
    if not url:
        return
    try:
        html = safe_get(url, timeout=8)
        if not html:
            return
        found = re.findall(r'discord\.gg/[A-Za-z0-9]+', html)
        junk = {"wumpus", "discord-testers", "discord", "example", "invite", "join", "verify"}
        found = [i for i in found if i.split("/")[-1].lower() not in junk]
        if found:
            item["discord_invite"] = f"https://{found[0]}"
            item["has_discord"] = True
        free_signals = ["free trial", "free plan", "free access", "free tier", "try for free", "join free", "free membership"]
        item["has_free_trial"] = any(s in html.lower() for s in free_signals)
    except Exception:
        pass


# ── Run research session ──────────────────────────────────────────────────────
def run_research(keywords, session_size=40):
    all_raw = []
    session_keywords = sample_keywords_weighted(keywords, session_size)
    log.info(f"Session keywords ({len(session_keywords)}): {session_keywords}")

    for kw in session_keywords:
        # BROAD: General Whop search — finds many more listings than discord-specific queries
        general_hits = search_whop(kw)
        # Scrape each page to discover Discord links + free trial flags
        for item in general_hits[:7]:
            scrape_whop_page(item)
            time.sleep(0.5)
        all_raw.extend(general_hits)
        time.sleep(1)

        # TARGETED: Discord-specific Whop search — high-confidence hits with Discord links
        discord_hits = search_whop_discord(kw)
        all_raw.extend(discord_hits)
        time.sleep(1.5)

    # SECONDARY: Telegram — 40% of session keywords
    tg_keywords = random.sample(session_keywords, max(2, len(session_keywords) * 2 // 5))
    for kw in tg_keywords:
        all_raw.extend(search_telegram(kw))
        time.sleep(1.5)

    save_x_candidates(all_raw)

    # Only filter out non-Whop/Telegram URLs — do NOT require Discord or free trial in snippet
    # (those signals are discovered via page scraping, not DDG snippets)
    all_raw = [
        r for r in all_raw
        if "whop.com/" in r.get("url", "")
        or "t.me/" in r.get("url", "")
        or r.get("source") in ("whop", "whop_discord", "whop_free", "telegram")
    ]

    # Deduplicate by URL
    seen_urls = set()
    unique = []
    for item in all_raw:
        url = item.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique.append(item)

    tg_final = sum(1 for r in unique if "t.me" in r.get("url", ""))
    whop_final = sum(1 for r in unique if "whop.com" in r.get("url", ""))
    discord_flagged = sum(1 for r in unique if r.get("has_discord") or r.get("discord_invite"))
    free_flagged = sum(1 for r in unique if r.get("has_free_trial"))
    log.info(f"Unique raw results: {len(unique)} (Whop: {whop_final}, Telegram: {tg_final}, has_discord: {discord_flagged}, has_free: {free_flagged})")
    return unique

# ── Claude: analyze results ───────────────────────────────────────────────────
def analyze_with_claude(raw_results, exclude_keywords):
    log.info("Sending results to Claude for analysis...")
    filtered = [
        r for r in raw_results
        if not any(ex.lower() in r.get("title", "").lower() for ex in exclude_keywords)
    ]

    if not filtered:
        log.info("No results after exclusion filter.")
        return []

    _, existing_names = load_existing_partners()
    learnings = load_learnings()

    context = ""
    if existing_names:
        context += f"\n\nEXISTING PARTNERS — already in Edgar's network, DO NOT suggest these:\n{json.dumps(sorted(existing_names))}"
    if learnings.get("strategic_insights"):
        context += f"\n\nSTRATEGIC CONTEXT (use this to prioritize what you flag as high-value):\n{learnings['strategic_insights']}"

    batch_size = 30
    all_found = []

    for i in range(0, len(filtered), batch_size):
        batch = filtered[i:i + batch_size]
        batch_text = json.dumps(batch, indent=2)
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4000,
                messages=[{
                    "role": "user",
                    "content": f"{RESEARCH_AGENT_PROMPT}{context}\n\nHere are today's raw search results to analyze:\n\n{batch_text}"
                }]
            )
            content = response.content[0].text.strip()
            content = content.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(content)
            if isinstance(parsed, list):
                all_found.extend(parsed)
            time.sleep(3)
        except json.JSONDecodeError as e:
            log.error(f"JSON parse error from Claude: {e}")
        except Exception as e:
            log.error(f"Claude API error: {e}")
            time.sleep(60)

    log.info(f"Claude found {len(all_found)} valid partners this session")
    return all_found

# ── Claude: update learnings ─────────────────────────────────────────────────
def update_learnings(session_results):
    if not session_results:
        return
    try:
        learnings = load_learnings()
        partners, _ = load_existing_partners()

        # Update raw performance counters
        learnings["sessions_analyzed"] = learnings.get("sessions_analyzed", 0) + 1
        learnings["last_updated"] = datetime.now().strftime("%Y-%m-%d")

        niche_hits = {}
        for r in session_results:
            niche = r.get("niche", "general_resell")
            source = r.get("source", "")
            region = r.get("region", "Global")
            is_contactable = bool(r.get("owner_contact") or r.get("invite_verified"))
            niche_hits[niche] = niche_hits.get(niche, 0) + 1

            if niche in learnings.get("niche_performance", {}):
                learnings["niche_performance"][niche]["leads_total"] = \
                    learnings["niche_performance"][niche].get("leads_total", 0) + 1
                if is_contactable:
                    learnings["niche_performance"][niche]["contactable_leads"] = \
                        learnings["niche_performance"][niche].get("contactable_leads", 0) + 1
            if source in learnings.get("source_performance", {}):
                learnings["source_performance"][source]["leads_total"] = \
                    learnings["source_performance"][source].get("leads_total", 0) + 1
                if is_contactable:
                    learnings["source_performance"][source]["contactable_leads"] = \
                        learnings["source_performance"][source].get("contactable_leads", 0) + 1
            if region in learnings.get("region_performance", {}):
                learnings["region_performance"][region]["leads_total"] = \
                    learnings["region_performance"][region].get("leads_total", 0) + 1

        # Adjust gap_score: productive niches trend down (we're covering them),
        # silent niches trend up (still unexplored)
        for niche, data in learnings.get("niche_performance", {}).items():
            hits = niche_hits.get(niche, 0)
            current = data.get("gap_score", 5)
            if hits >= 3:
                data["gap_score"] = max(1, current - 1)   # well-covered this session
            elif hits == 0:
                data["gap_score"] = min(10, current + 0.5) # unexplored, boost slightly
        log.info(f"Gap scores updated: { {n: d['gap_score'] for n, d in learnings['niche_performance'].items()} }")

        # Ask Claude for strategic insight update (cap leads to avoid truncation)
        sample_leads = session_results[:30]
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": (
                    f"{LEARNING_UPDATE_PROMPT}\n\n"
                    f"Session leads found ({len(sample_leads)} of {len(session_results)}):\n{json.dumps(sample_leads, indent=2)}\n\n"
                    f"Current learnings:\n{json.dumps(learnings, indent=2)}\n\n"
                    f"Existing partners count by niche/region:\n"
                    f"EU ticket: 30, EU sneaker: 15, US ticket: 3, US sneaker: 7, "
                    f"ASIA ticket: 8, ASIA sneaker: 12, Pokemon/TCG: 0, General: 12"
                )
            }]
        )
        content = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        update = json.loads(content)

        if "strategic_insights" in update:
            learnings["strategic_insights"] = update["strategic_insights"]
        if "top_keywords" in update:
            learnings["top_keywords"] = update["top_keywords"]

        save_learnings(learnings)
        log.info(f"Learnings updated after session #{learnings['sessions_analyzed']}")
    except Exception as e:
        log.error(f"Learning update error: {e}")

# ── Claude: discover new keywords ────────────────────────────────────────────
def discover_and_save_keywords(results):
    if not results:
        return
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"{KEYWORD_DISCOVERY_PROMPT}\n\nResults found today:\n{json.dumps(results[:15], indent=2)}"
            }]
        )
        content = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        new_kws = json.loads(content)
        if not isinstance(new_kws, list):
            return

        with open("keywords.json", "r") as f:
            data = json.load(f)

        existing = set(data["keywords"])
        added = [kw for kw in new_kws if isinstance(kw, str) and kw not in existing]
        if added:
            data["keywords"].extend(added)
            with open("keywords.json", "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            log.info(f"Auto-added {len(added)} new keywords: {added}")
    except Exception as e:
        log.error(f"Keyword discovery error: {e}")

# ── Save results (append mode) ────────────────────────────────────────────────
def load_sent_leads():
    path = "logs/sent_leads.json"
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def mark_leads_sent(leads):
    path = "logs/sent_leads.json"
    existing = load_sent_leads()
    for lead in leads:
        key = lead.get("name", "").strip().lower()
        if key:
            existing.add(key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(existing), f, indent=2, ensure_ascii=False)


def build_x_candidate(item, date_str=None):
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    text = " ".join(str(item.get(k, "")) for k in ("url", "contact", "description", "title"))
    handle = item.get("x_handle") or extract_x_handle(text)
    if not handle:
        return None

    keyword = item.get("keyword", "")
    discovered_via = item.get("discovered_via") or item.get("source") or "raw_result"
    if keyword and ":" not in discovered_via:
        discovered_via = f"{discovered_via}:{keyword}"

    return {
        "x_handle": handle,
        "profile_url": item.get("profile_url") or f"https://x.com/{handle}",
        "discovered_via": discovered_via,
        "source": item.get("source", ""),
        "source_url": item.get("url", ""),
        "source_title": item.get("title", ""),
        "keyword": keyword,
        "status": "candidate",
        "first_seen": date_str,
        "last_seen": date_str
    }


def save_x_candidates(raw_results, date_str=None):
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    path = f"logs/x_candidates_{date_str}.json"

    existing = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    by_handle = {
        c.get("x_handle", "").lower(): c
        for c in existing
        if c.get("x_handle")
    }

    added = 0
    for item in raw_results:
        candidate = build_x_candidate(item, date_str=date_str)
        if not candidate:
            continue
        key = candidate["x_handle"].lower()
        if key in by_handle:
            by_handle[key]["last_seen"] = date_str
            continue
        by_handle[key] = candidate
        added += 1

    if added:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(by_handle.values(), key=lambda c: c["x_handle"]), f, indent=2, ensure_ascii=False)
        log.info(f"Saved {added} new X candidates (total today: {len(by_handle)}) -> {path}")
    return list(by_handle.values())


def save_results(new_results, date_str=None):
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    path = f"logs/results_{date_str}.json"

    existing = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    existing_keys = {r.get("name", r.get("url", "")).lower() for r in existing}
    added = [r for r in new_results if r.get("name", r.get("url", "")).lower() not in existing_keys]
    combined = existing + added

    with open(path, "w", encoding="utf-8") as f:
        json.dump(combined, f, indent=2, ensure_ascii=False)
    log.info(f"Saved {len(added)} new leads (total today: {len(combined)}) -> {path}")
    return combined

# ── Discord embed ─────────────────────────────────────────────────────────────
def send_discord_embed(results, date_label=None):
    if date_label is None:
        date_label = datetime.now().strftime("%d.%m.%Y")

    if not results:
        payload = {
            "embeds": [{
                "title": "📋 EM Media — Daily Research Recap",
                "description": "No new partners found after filtering.\nAll results were either fully paid or had no free entry point.",
                "color": 0x808080,
                "footer": {"text": f"EM Media Research Agent • {date_label}"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=payload)
        return

    summary_text = ""
    try:
        summary_resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": f"{SUMMARY_AGENT_PROMPT}\n\nResults:\n{json.dumps(results, indent=2)}"
            }]
        )
        summary_text = summary_resp.content[0].text.strip()
    except Exception as e:
        log.error(f"Summary error: {e}")
        summary_text = f"Found {len(results)} potential partners."

    niche_colors = {
        "ticket": 0x5865F2,
        "sneaker": 0xFF6B35,
        "pokemon_tcg": 0xFFCC00,
        "general_resell": 0x57F287
    }

    embeds = [{
        "title": "📋 EM Media — Daily Research Recap",
        "description": summary_text,
        "color": 0x5865F2,
        "footer": {"text": f"EM Media Research Agent • {date_label}"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }]

    niche_icons = {
        "ticket": "🎟️",
        "sneaker": "👟",
        "pokemon_tcg": "🃏",
        "general_resell": "📦"
    }

    # Send summary embed first
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": embeds})
        if resp.status_code not in [200, 204]:
            log.error(f"Discord summary error: {resp.status_code} — {resp.text}")
    except Exception as e:
        log.error(f"Discord send failed: {e}")
        return

    # Send leads in batches of 8 (stays well within Discord's 6000 char limit)
    batch_size = 8
    total_batches = (len(results[:40]) + batch_size - 1) // batch_size
    for batch_num, batch_start in enumerate(range(0, len(results[:40]), batch_size)):
        batch = results[batch_start:batch_start + batch_size]
        batch_embed = {
            "title": f"🎯 Partner Leads — {date_label} ({batch_start + 1}–{batch_start + len(batch)})",
            "color": 0x57F287,
            "fields": []
        }
        for r in batch:
            name = r.get("name", "Unknown")
            entry = r.get("free_entry", "N/A")
            contact = r.get("contact", "N/A")
            desc = r.get("description", "")
            region = r.get("region", "?")
            size = r.get("size_estimate", "?")
            rtype = r.get("type", "?")
            niche = r.get("niche", "general_resell")
            icon = niche_icons.get(niche, "📦")
            invite = r.get("discord_invite", "")
            verified = r.get("invite_verified")
            members = r.get("member_count", 0)
            size_str = f"{size} ({members:,} members)" if members else size
            invite_str = ""
            if invite:
                status = "✅" if verified is True else ("❌" if verified is False else "❓")
                invite_str = f"\n**Discord:** {status} {invite}"
            whop_url = r.get("whop_url", r.get("url", ""))
            free_trial = "✅ Free trial/plan available" if r.get("free_trial") else ""
            free_trial_str = f"\n**Free Trial:** {free_trial}" if free_trial else ""
            whop_str = f"\n**Whop:** {whop_url}" if whop_url and "whop.com" in whop_url else ""
            field_value = (
                f"**Type:** {rtype} | **Region:** {region} | **Size:** {size_str}\n"
                f"**Free Entry:** {entry}{invite_str}{free_trial_str}{whop_str}\n"
                f"**Contact:** {contact}\n"
                f"_{desc}_"
            )
            batch_embed["fields"].append({
                "name": f"{icon} {name}",
                "value": field_value[:1024],
                "inline": False
            })
        try:
            time.sleep(0.8)  # Discord rate limit
            resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [batch_embed]})
            if resp.status_code in [200, 204]:
                log.info(f"Discord batch {batch_num + 1}/{total_batches} sent.")
            else:
                log.error(f"Discord batch error: {resp.status_code} — {resp.text}")
        except Exception as e:
            log.error(f"Discord batch send failed: {e}")

# ── Research session (runs 4x/day, no embed) ──────────────────────────────────
def run_research_session():
    log.info("=" * 60)
    log.info(f"Research session starting — {datetime.now().strftime('%H:%M CET')}")
    log.info("=" * 60)

    keywords, exclude = load_keywords()
    raw = run_research(keywords, session_size=40)
    results = analyze_with_claude(raw, exclude)

    if results:
        results = enrich_and_verify_leads(results)
        save_results(results)
        discover_and_save_keywords(results)
        update_learnings(results)

    log.info("Research session complete.")

# ── Morning run: research first, then send combined embed (yesterday 21:00 + today 09:00) ──
def run_morning_session():
    log.info("=" * 60)
    log.info("Morning session — running research, then sending combined recap")
    log.info("=" * 60)

    # Research first so 09:00 results are included in the embed
    run_research_session()

    # Combine yesterday's results (includes 21:00 session) + today's fresh 09:00 results
    today_str = datetime.now().strftime("%Y-%m-%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    already_sent = load_sent_leads()
    combined = []
    seen = set()
    for path in [f"logs/results_{yesterday_str}.json", f"logs/results_{today_str}.json"]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for lead in json.load(f):
                        key = lead.get("name", "").strip().lower()
                        if key and key not in seen and key not in already_sent:
                            seen.add(key)
                            combined.append(lead)
            except Exception as e:
                log.error(f"Failed to load {path}: {e}")
    log.info(f"Combined pool after sent-filter: {len(combined)} leads ({len(already_sent)} already sent previously)")

    if combined:
        # Priority: Whop+Discord first, then Whop+FreeTrial, then Telegram, cap at 15
        whop_discord_leads = [r for r in combined if r.get("type") in ("whop_discord",) or (r.get("discord_invite") and "whop" in r.get("contact", "").lower())]
        whop_free_leads = [r for r in combined if r.get("type") == "whop_free_trial" or r.get("free_trial") is True]
        telegram_leads = [r for r in combined if r.get("type") == "telegram_group"][:3]
        other_leads = [r for r in combined if r not in whop_discord_leads and r not in whop_free_leads and r not in telegram_leads]
        balanced = whop_discord_leads + whop_free_leads + telegram_leads + other_leads
        # Deduplicate (discord/tg/whop may already overlap with combined)
        seen_bal = set()
        final = []
        for r in balanced:
            k = r.get("name", "").lower()
            if k and k not in seen_bal:
                seen_bal.add(k)
                final.append(r)
        final = final[:15]
        log.info(f"Sending morning embed: {len(final)} leads (WhopDiscord:{len(whop_discord_leads)} FreeTrial:{len(whop_free_leads)} TG:{len(telegram_leads)})")
        send_discord_embed(final, date_label=datetime.now().strftime("%d.%m.%Y"))
        mark_leads_sent(final)
    else:
        log.info("No results to send in morning embed.")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", action="store_true", help="Run morning session immediately (send embed + research)")
    parser.add_argument("--research-only", action="store_true", help="Run research session only, no embed")
    parser.add_argument("--test", action="store_true", help="Run research session then immediately send embed with today's results")
    args = parser.parse_args()

    if args.now:
        log.info("Manual run triggered.")
        run_morning_session()
    elif args.research_only:
        log.info("Research-only session triggered.")
        run_research_session()
    elif args.test:
        log.info("Test run: research + immediate embed.")
        run_research_session()
        date_str = datetime.now().strftime("%Y-%m-%d")
        path = f"logs/results_{date_str}.json"
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                results = json.load(f)
            log.info(f"Sending test embed with {len(results)} leads.")
            send_discord_embed(results)
        else:
            log.info("No results to send.")
    else:
        log.info("Scheduler starting — sessions at 09:00, 13:00, 17:00, 21:00 CET")
        scheduler = BlockingScheduler(timezone="Europe/Berlin")
        # 09:00 CET: send yesterday's embed + run morning research
        scheduler.add_job(run_morning_session, "cron", hour=9, minute=0)
        # 13:00, 17:00, 21:00 CET: research only, results accumulate
        scheduler.add_job(run_research_session, "cron", hour=13, minute=0)
        scheduler.add_job(run_research_session, "cron", hour=17, minute=0)
        scheduler.add_job(run_research_session, "cron", hour=21, minute=0)
        scheduler.start()
