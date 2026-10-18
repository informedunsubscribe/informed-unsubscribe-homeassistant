"""Find a sender's website, crawl it for email addresses and rank them for an opt-out request."""

import http.client
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.request
from typing import NamedTuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from . import db

MAX_PAGES = 10
MAX_BYTES = 2_000_000
TIMEOUT = 15
# Sites checked per sender, and the pause between searches so the search engine isn't hammered.
MAX_CANDIDATES = 4
SEARCH_PAUSE = 5
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)
# Some sites turn away anything claiming to be a browser that does not behave like one, but
# let a plainly named program through.
PLAIN_USER_AGENT = "InformedUnsubscribe (Home Assistant app)"

# Search results that are about a company rather than the company's own site.
SKIP_DOMAINS = {
    "duckduckgo.com", "wikipedia.org", "facebook.com", "linkedin.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "yelp.com", "bbb.org", "amazon.com", "yellowpages.com",
    "mapquest.com", "bloomberg.com", "crunchbase.com", "glassdoor.com", "indeed.com",
    "reddit.com", "trustpilot.com", "tiktok.com", "pinterest.com", "google.com", "usps.com",
    # business directories, which list everyone and so "match" any sender
    "angi.com", "homeadvisor.com", "nextdoor.com", "thumbtack.com", "houzz.com", "manta.com",
    "porch.com", "buildzoom.com", "birdeye.com", "superpages.com", "whitepages.com",
    "chamberofcommerce.com", "opencorporates.com", "dnb.com", "zoominfo.com", "zillow.com",
    "realtor.com", "redfin.com", "tripadvisor.com", "foursquare.com", "bizapedia.com",
}
# Words too common in business names to tell one sender from another.
GENERIC_WORDS = {
    "and", "of", "for", "plumbing", "service", "services", "home", "homes", "group", "bank",
    "insurance", "realty", "real", "estate", "dental", "auto", "store", "shop", "county",
    "city", "national", "american", "america", "first", "united", "general", "center",
    "care", "health", "financial", "credit", "union", "mortgage", "roofing", "heating",
    "cooling", "electric", "landscaping", "cleaning", "construction", "solutions",
    "associates", "partners", "club", "foundation", "society", "association", "department",
}
PHONE_RE = re.compile(r"(?<!\d)\(?([2-9]\d{2})\)?[\s.-]{0,2}(\d{3})[\s.-](\d{4})(?!\d)")
_TEL_LINK = re.compile(r"tel:\+?1?[\s.-]?(\d{10})\b")

# Most relevant first; pages are fetched in this order until MAX_PAGES.
LINK_HINTS = (
    "opt-out", "optout", "opt out", "unsubscribe", "do-not-mail", "do not mail", "mailing",
    "privacy", "preferences", "contact", "customer", "support", "help", "legal", "about",
)
COMMON_PATHS = ("privacy", "privacy-policy", "contact", "contact-us")

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}")
_ASSET_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico", "woff", "woff2"}
_JUNK_DOMAINS = {"example.com", "domain.com", "email.com", "sentry.io", "wixpress.com"}

_SCORES = (
    (100, ("optout", "opt-out", "opt_out", "unsubscribe", "donotmail", "remove", "mailinglist",
           "maillist", "mailpreference")),
    (80, ("privacy", "dpo", "dataprotection", "datarequest", "ccpa", "compliance")),
    (60, ("customerservice", "customercare", "customersupport", "service", "support", "care",
          "help", "members", "donor")),
    (40, ("contact", "info", "hello", "feedback", "questions", "inquir", "enquir")),
)
_NEVER = ("noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "mailer-daemon",
          "postmaster", "abuse")
_UNLIKELY = ("career", "jobs", "recruit", "press", "media", "investor", "sales", "partner",
             "vendor", "talent", "advertis", "webmaster", "security")


def registered_domain(host: str) -> str:
    return ".".join(host.lower().rstrip(".").split(".")[-2:])


class LookupFailed(Exception):
    """The web search for a sender's site could not be completed."""


_NETWORK_ERRORS = (OSError, http.client.HTTPException, ValueError)


def _require_public_host(url: str) -> None:
    # Sites are chosen from search results, so never let one point us at the home network.
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"not a web address: {url}")
    for info in socket.getaddrinfo(parsed.hostname, None):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError(f"refusing non-public address for {parsed.hostname}")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_public_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects)


def _open(url: str, data: bytes | None = None, headers: dict | None = None):
    _require_public_host(url)
    request = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9", **(headers or {})},
    )
    return _opener.open(request, timeout=TIMEOUT)


def site_roots(urls, limit: int = 3) -> list[str]:
    """The first few result URLs that could be a sender's own site, as site roots."""
    sites: list[str] = []
    for target in urls:
        url = urlparse(target)
        if url.scheme not in ("http", "https") or not url.hostname:
            continue
        if registered_domain(url.hostname) in SKIP_DOMAINS:
            continue
        site = f"{url.scheme}://{url.hostname}/"
        if site not in sites:
            sites.append(site)
        if len(sites) == limit:
            break
    return sites


def pick_results(html: str, limit: int = 3) -> list[str]:
    """Result sites from DuckDuckGo's plain HTML results page."""
    links = (a.get("href", "") for a in BeautifulSoup(html, "html.parser").select("a.result__a"))
    return site_roots((parse_qs(urlparse(h).query).get("uddg", [h])[0] for h in links), limit)


def _search_brave(query: str, api_key: str) -> list[str]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urlencode({"q": query, "count": 10})
    try:
        with _open(url, headers={"X-Subscription-Token": api_key, "Accept": "application/json"}) as response:
            results = json.loads(response.read(MAX_BYTES)).get("web", {}).get("results", [])
    except urllib.error.HTTPError as exc:
        raise LookupFailed(f"the search API refused the request (HTTP {exc.code})") from exc
    except _NETWORK_ERRORS as exc:
        raise LookupFailed(f"web search failed: {exc}") from exc
    return site_roots(str(result.get("url", "")) for result in results if isinstance(result, dict))


def _search_duckduckgo(query: str) -> list[str]:
    try:
        with _open("https://html.duckduckgo.com/html/", urlencode({"q": query}).encode()) as response:
            status = response.status
            html = response.read(MAX_BYTES).decode("utf-8", "replace")
    except _NETWORK_ERRORS as exc:
        raise LookupFailed(f"web search failed: {exc}") from exc
    sites = pick_results(html)
    # After a few searches in quick succession the engine answers with a challenge page.
    if not sites and (status != 200 or "anomaly" in html.lower()):
        raise LookupFailed("the search engine is limiting requests")
    return sites


def search(query: str, api_key: str = "") -> list[str]:
    """Search with the Brave Search API when a key is configured, else scrape DuckDuckGo."""
    return _search_brave(query, api_key) if api_key else _search_duckduckgo(query)


def phones_in(text: str) -> list[str]:
    """Phone numbers in a text as ten digits, in order of appearance."""
    found = ["".join(m.groups()) for m in PHONE_RE.finditer(text)] + _TEL_LINK.findall(text)
    return list(dict.fromkeys(found))


def queries(name: str, phones=(), location: str = "", home: str = "") -> list[str]:
    """Searches to run, most specific first."""
    out = []
    if phones:
        phone = phones[0]
        out.append(f'"{phone[:3]}-{phone[3:6]}-{phone[6:]}" {name}')
    out.append(f"{name} {location}" if location else f"{name} official website")
    if not location and home:
        out.append(f"{name} {home}")  # a sender with no return address is often a local business
    return out


def _decode_cfemail(encoded: str) -> str:
    try:
        data = bytes.fromhex(encoded)
    except ValueError:
        return ""
    return bytes(b ^ data[0] for b in data[1:]).decode("utf-8", "ignore") if data else ""


def extract_emails(html: str) -> set[str]:
    candidates = set(EMAIL_RE.findall(html.replace("%40", "@")))
    for tag in BeautifulSoup(html, "html.parser").select("[data-cfemail]"):
        candidates.update(EMAIL_RE.findall(_decode_cfemail(tag["data-cfemail"])))
    emails = set()
    for candidate in candidates:
        address = candidate.lower().strip(".")
        domain = address.split("@")[1]
        if domain.rsplit(".", 1)[-1] in _ASSET_TLDS or registered_domain(domain) in _JUNK_DOMAINS:
            continue
        emails.add(address)
    return emails


def score(address: str, site_domain: str) -> int:
    local, domain = address.split("@")
    if any(word in local for word in _NEVER):
        return -1
    points = 20
    # Checked first so "careers@" isn't read as customer "care".
    if any(word in local for word in _UNLIKELY):
        points = 5
    else:
        for value, words in _SCORES:
            if any(word in local for word in words):
                points = value
                break
    if registered_domain(domain) != site_domain:
        points -= 30
    return points


def rank(emails: set[str], site_domain: str) -> list[str]:
    scored = [(score(e, site_domain), e) for e in emails]
    return [e for s, e in sorted(scored, key=lambda pair: (-pair[0], pair[1])) if s >= 0]


def candidate_links(html: str, base: str) -> list[str]:
    site = registered_domain(urlparse(base).hostname or "")
    ranked = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urlparse(urljoin(base, a["href"]))
        if url.scheme not in ("http", "https") or registered_domain(url.hostname or "") != site:
            continue
        haystack = f"{url.path} {a.get_text(' ')}".lower()
        for position, hint in enumerate(LINK_HINTS):
            if hint in haystack:
                ranked.append((position, url._replace(fragment="").geturl()))
                break
    links = [link for _, link in sorted(ranked)]
    links += [urljoin(base, path) for path in COMMON_PATHS]
    return list(dict.fromkeys(links))


def _fetch(url: str) -> tuple[str, str]:
    """Return (final_url, html), or ("", "") for anything that isn't a readable HTML page."""
    for headers in (None, {"User-Agent": PLAIN_USER_AGENT}):
        try:
            with _open(url, headers=headers) as response:
                if "html" not in response.headers.get("Content-Type", ""):
                    return "", ""
                charset = response.headers.get_content_charset() or "utf-8"
                return response.geturl(), response.read(MAX_BYTES).decode(charset, "replace")
        except urllib.error.HTTPError as error:
            if error.code != 403:
                return "", ""
        except (*_NETWORK_ERRORS, LookupError):
            return "", ""
    return "", ""


class Site(NamedTuple):
    url: str  # where the home page ended up after redirects
    title: str
    emails: list[str]  # ranked, best first
    phones: set[str]


class Result(NamedTuple):
    website: str
    emails: list[str]
    verified: bool  # False means the site is a guess that could not be tied to the sender
    reason: str = ""
    candidates: tuple[str, ...] = ()


def crawl(website: str) -> Site | None:
    final_url, html = _fetch(website)
    if not html:
        return None
    title = BeautifulSoup(html, "html.parser").title
    emails, phones = extract_emails(html), set(phones_in(html))
    for link in candidate_links(html, final_url)[: MAX_PAGES - 1]:
        page = _fetch(link)[1]
        emails |= extract_emails(page)
        phones |= set(phones_in(page))
    site_domain = registered_domain(urlparse(final_url).hostname or "")
    return Site(final_url, title.get_text(" ", strip=True) if title else "",
                rank(emails, site_domain), phones)


def phone_match(phones, site: Site) -> str:
    for phone in phones:
        if phone in site.phones:
            return f"its phone number {phone[:3]}-{phone[3:6]}-{phone[6:]} is on the mailpiece"
    return ""


def name_match(name: str, site: Site) -> str:
    label = re.sub(r"[^a-z0-9]", "", registered_domain(urlparse(site.url).hostname or "").split(".")[0])
    words = db.normalize(name).split()
    about = set(db.normalize(site.title).split())
    distinctive = [w for w in words if len(w) >= 4 and w not in GENERIC_WORDS]
    if distinctive and all(w in about or w in label for w in distinctive):
        return "its name matches the sender"
    compact = "".join(words)
    if len(compact) >= 5 and compact in label:
        return "its web address matches the sender"
    return ""


def confirm(name: str, phones, site: Site) -> str:
    """Why this site is believed to be the sender's, or "" if nothing ties them together."""
    return phone_match(phones, site) or name_match(name, site)


def research(
    name: str, website: str = "", phones=(), location: str = "", home: str = "", api_key: str = ""
) -> Result:
    """Find the sender's website and the email addresses on it.

    A `website` passed in (printed on the piece, or typed by the user) is taken as correct.
    Otherwise searches run one at a time, most specific first, and stop at the first result
    that can be tied to the sender: a matching phone number beats a matching name.
    """
    if website:
        if "://" not in website:
            website = f"https://{website}"
        site = crawl(website)
        return Result(website, site.emails if site else [], True)

    candidates: list[str] = []
    first = failure = None
    for number, query in enumerate(queries(name, phones, location, home)):
        if number:
            time.sleep(SEARCH_PAUSE)
        try:
            found = [url for url in search(query, api_key) if url not in candidates]
        except LookupFailed as exc:
            failure = exc
            break  # blocked or offline: more searches now would only make it worse
        named = None
        for site_url in found:
            candidates.append(site_url)
            if len(candidates) > MAX_CANDIDATES:
                break
            site = crawl(site_url)
            if site is None:
                continue
            first = first or site
            reason = phone_match(phones, site)
            if reason:
                return Result(site_url, site.emails, True, reason, tuple(candidates))
            if named is None and name_match(name, site):
                named = (site_url, site)
        if named:
            return Result(named[0], named[1].emails, True, name_match(name, named[1]), tuple(candidates))
    if not candidates:
        if failure:
            raise failure
        return Result("", [], False)
    return Result(candidates[0], first.emails if first else [], False, "", tuple(candidates))
