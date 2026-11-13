from __future__ import annotations

import csv
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from core.capability_matcher import CapabilityMatcher
from core.config import LEADS_DROPZONE_DIR, PROJECT_ROOT
from core.db import add_opportunity, connect, log_event, update_discovery_source_polled
from core.risk_analyzer import RiskAnalyzer
from core.scoring import opportunity_score

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_FEED_ITEMS = 1000
MAX_REDIRECTS = 5
HTTP_TIMEOUT_SECONDS = 12.0


@dataclass
class DiscoveredLead:
    title: str
    source: str
    url: str
    description: str
    amount_inr: float
    estimated_hours: float
    skill_match: float
    payment_confidence: float
    risk_level: float
    source_permitted: bool
    score: float
    content_hash: str
    is_duplicate: bool = False
    is_suitable: bool = True
    rejection_reason: str = ""
    profitability_passed: bool = True
    hourly_yield_inr: float = 0.0
    contribution_margin_pct: float = 0.0
    company_name: str = ""
    industry: str = ""
    tech_stack: list[str] = field(default_factory=list)
    pain_points: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiscoveryBatchResult:
    source_id: int | None
    source_name: str
    source_type: str
    total_found: int
    leads_imported: int
    duplicates_skipped: int
    unsuitable_skipped: int
    leads: list[DiscoveredLead] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def normalize_text(text: str) -> str:
    """Normalizes whitespace and casing for reliable fingerprinting."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def compute_content_hash(title: str, description: str) -> str:
    """Computes a deterministic SHA-256 fingerprint for opportunity deduplication."""
    norm = f"{normalize_text(title)}|{normalize_text(description)}"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


TRACKING_QUERY_PARAMS: set[str] = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "fbclid", "gclid", "msclkid", "trk", "igshid", "mc_eid"
}


def normalize_url(raw_url: str) -> str:
    """
    Canonicalizes an HTTP(S) URL for robust deduplication across syndication feeds & dropzones.
    Removes tracking parameters, normalizes lowercase scheme & host, removes www and default ports,
    and strips trailing slashes.
    """
    if not raw_url or not isinstance(raw_url, str):
        return ""
    try:
        parts = urlsplit(raw_url.strip())
        if not parts.scheme or not parts.netloc:
            return raw_url.strip()
        scheme = parts.scheme.lower()
        netloc = parts.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if (scheme == "http" and netloc.endswith(":80")) or (scheme == "https" and netloc.endswith(":443")):
            netloc = netloc.rsplit(":", 1)[0]
        path = parts.path.rstrip("/")
        if not path:
            path = "/"
        
        from urllib.parse import parse_qsl, urlencode, urlunsplit
        clean_query = ""
        if parts.query:
            query_tuples = parse_qsl(parts.query, keep_blank_values=True)
            filtered = [(k, v) for k, v in query_tuples if k.lower() not in TRACKING_QUERY_PARAMS]
            if filtered:
                clean_query = urlencode(sorted(filtered))
        return urlunsplit((scheme, netloc, path, clean_query, ""))
    except Exception:
        return raw_url.strip()


MINIMUM_ACCEPTABLE_CONTRIBUTION_MARGIN = 50.0  # 50% statutory threshold
MINIMUM_HOURLY_YIELD_INR = 400.0  # ₹400 / hr net post-tax/post-cost threshold
MINIMUM_VIABLE_PROJECT_PRICE_INR = 1500.0  # Minimum standard engagement
MAX_ACCEPTABLE_RISK_LEVEL = 5.0

EXPLOITATIVE_COMPENSATION_PATTERNS = [
    r"\b(unpaid|unpaid test|free test|free work|unpaid trial)\b",
    r"\b(exposure|equity only|commission only|profit share only|rev share only)\b",
    r"\b(pay for test|test task unpaid)\b",
]


def screen_lead_profitability(
    amount_inr: float,
    estimated_hours: float,
    risk_level: float,
    payment_confidence: float,
    is_suspicious: bool,
    risk_flags: list[str],
    overall_fit_score: float,
    description: str = "",
) -> tuple[bool, float, float, str]:
    """
    Evaluates unit economics, risk parameters, and profitability margins
    before an opportunity or lead is qualified for outreach or execution.
    Returns: (is_profitable, hourly_yield_inr, contribution_margin_pct, rejection_reason)
    """
    text = description.lower()

    # 1. Check for exploitative or unpaid work patterns
    for pat in EXPLOITATIVE_COMPENSATION_PATTERNS:
        if re.search(pat, text):
            return False, 0.0, 0.0, "Exploitative or unpaid compensation pattern detected"

    # 2. Risk check: Filter out high-risk or suspicious leads immediately
    if risk_level > MAX_ACCEPTABLE_RISK_LEVEL or is_suspicious:
        flags_str = "; ".join(risk_flags[:2]) if risk_flags else "Risk score above limit"
        return False, 0.0, 0.0, f"Excessive risk level ({risk_level}/10) or fraud indicators: {flags_str}"

    hrs = max(0.5, float(estimated_hours))

    # 3. If budget is specified (> 0)
    if amount_inr > 0:
        if amount_inr < MINIMUM_VIABLE_PROJECT_PRICE_INR:
            rate = round(amount_inr / hrs, 2)
            if rate < MINIMUM_HOURLY_YIELD_INR:
                return (
                    False,
                    rate,
                    round((amount_inr - 150) / amount_inr * 100, 1) if amount_inr > 150 else 0.0,
                    f"Sub-threshold engagement budget (₹{amount_inr:,.0f} < ₹{MINIMUM_VIABLE_PROJECT_PRICE_INR:,.0f}) and low hourly rate (₹{rate:,.0f}/hr)",
                )

        platform_fee = round(amount_inr * 0.05, 2)
        direct_cost = min(round(amount_inr * 0.05, 2), 250.0)
        net_pretax = round(amount_inr - platform_fee - direct_cost, 2)
        margin_pct = round((net_pretax / amount_inr) * 100.0, 1) if amount_inr > 0 else 0.0

        # Estimated post-tax under Indian Income-tax Act (presumptive 44ADA / normal slab reserve)
        est_tax = round(net_pretax * 0.15, 2)
        net_profit = round(net_pretax - est_tax, 2)
        yield_hr = round(net_profit / hrs, 2)

        if margin_pct < MINIMUM_ACCEPTABLE_CONTRIBUTION_MARGIN:
            return (
                False,
                yield_hr,
                margin_pct,
                f"Contribution margin ({margin_pct}%) falls below {MINIMUM_ACCEPTABLE_CONTRIBUTION_MARGIN}% profitability threshold",
            )

        if yield_hr < MINIMUM_HOURLY_YIELD_INR:
            return (
                False,
                yield_hr,
                margin_pct,
                f"Net owner hourly yield (₹{yield_hr:,.0f}/hr) falls below ₹{MINIMUM_HOURLY_YIELD_INR:,.0f}/hr threshold",
            )

        return True, yield_hr, margin_pct, ""

    # 4. If budget is unspecified (0.0):
    # Inbound discovery opportunities with unknown budget are allowed only if overall fit is sufficient and risk is low.
    if overall_fit_score < 0.40:
        return False, 0.0, 0.0, "Unspecified budget with low capability fit score (< 0.40)"

    return True, 0.0, 0.0, ""


def extract_budget_inr(text: str) -> float:
    """Extract an explicitly stated INR budget. Unknown budgets stay zero (unknown)."""
    inr_match = re.search(r"(?:₹|inr|rs\.?)\s*([0-9]+(?:,[0-9]+)*(?:\.[0-9]+)?)", text or "", re.IGNORECASE)
    if not inr_match:
        return 0.0
    try:
        value = float(inr_match.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0
    if not (100 <= value <= 500000):
        return 0.0
    return round(value, 2)


def _is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return bool(ip.is_global and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified))


def validate_source_url(
    url: str,
    *,
    resolve_dns: bool = False,
    resolver=None,
) -> tuple[bool, str]:
    """Validate an HTTP(S) URL. When fetching, resolve and reject *all* non-public DNS answers."""
    if (not url or not isinstance(url, str) or len(url) > 2048 or url != url.strip()
            or "\\" in url or any(ord(ch) < 32 for ch in url)):
        return False, "URL is empty, too long, malformed, or contains unsafe characters"
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except Exception as exc:
        return False, f"URL parse failure: {exc}"

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return False, f"Invalid scheme '{parsed.scheme}'. Only http and https are allowed."
    if parsed.username is not None or parsed.password is not None:
        return False, "URLs containing embedded credentials are forbidden."

    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        return False, "URL hostname is missing"
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        return False, f"SSRF Protection: localhost or internal hostname ({hostname}) is forbidden."
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not _is_public_ip(hostname):
        return False, f"SSRF Protection: localhost/private or reserved IP address ({hostname}) is forbidden."

    expected_port = 80 if scheme == "http" else 443
    if port not in (None, expected_port):
        return False, "Only standard HTTP (80) and HTTPS (443) ports are permitted."

    is_ip = literal_ip is not None

    if resolve_dns and not is_ip:
        dns_resolver = resolver or socket.getaddrinfo
        try:
            answers = dns_resolver(hostname, port or expected_port, type=socket.SOCK_STREAM)
        except Exception as exc:
            return False, f"DNS resolution failed for source host: {exc}"
        addresses = []
        for answer in answers:
            try:
                addr = answer[4][0]
            except (IndexError, TypeError):
                continue
            addresses.append(addr)
        if not addresses:
            return False, "DNS resolution returned no usable addresses."
        blocked = [addr for addr in addresses if not _is_public_ip(addr)]
        if blocked:
            return False, f"SSRF Protection: hostname resolves to a non-public address ({blocked[0]})."

    return True, ""


def safe_dropzone_path(target: str | Path | None, *, expect_directory: bool = False) -> Path:
    """Resolve local feeds inside the approved dropzone or two fixed bundled legacy fixtures."""
    LEADS_DROPZONE_DIR.mkdir(parents=True, exist_ok=True)
    if LEADS_DROPZONE_DIR.is_symlink():
        raise ValueError("Lead dropzone root cannot be a symlink")
    root = LEADS_DROPZONE_DIR.resolve()
    raw = Path(target) if target else root
    if not raw.is_absolute():
        raw = root / raw
    raw = Path(os.path.abspath(str(raw)))

    # Backward compatibility for existing v0.4 installations whose SQLite source
    # records point at the bundled sample_feeds paths. Only these two filenames
    # are allowed from that legacy directory; arbitrary files remain blocked.
    legacy_root = PROJECT_ROOT / "data" / "sample_feeds"
    legacy_allowed_names = {"sample_rss.xml", "sample_leads.json"}
    is_legacy_fixture = False
    try:
        legacy_relative = raw.relative_to(legacy_root)
        is_legacy_fixture = (not expect_directory and len(legacy_relative.parts) == 1
                             and legacy_relative.name in legacy_allowed_names)
    except ValueError:
        pass

    if is_legacy_fixture:
        allowed_root = legacy_root
    else:
        allowed_root = root

    try:
        relative = raw.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError("Local discovery paths must stay inside data/leads_dropzone (or use a bundled legacy sample fixture)") from exc

    if allowed_root.is_symlink():
        raise ValueError("Local discovery root cannot be a symlink")
    cursor = allowed_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Symlinked lead dropzone paths are not permitted")

    try:
        resolved = raw.resolve(strict=True)
        resolved.relative_to(allowed_root.resolve())
    except (OSError, ValueError) as exc:
        raise ValueError("Local discovery path is missing or escapes the approved dropzone") from exc
    if is_legacy_fixture and resolved.name not in legacy_allowed_names:
        raise ValueError("Only the bundled legacy sample fixtures are permitted")
    if expect_directory and not resolved.is_dir():
        raise ValueError("Configured local discovery path must be a directory")
    if not expect_directory and not resolved.is_file():
        raise ValueError("Configured local discovery path must be a regular file")
    return resolved

def fetch_public_feed(url: str, *, max_bytes: int = MAX_FEED_BYTES, timeout: float = HTTP_TIMEOUT_SECONDS,
                      resolver=None, socket_factory=None) -> bytes:
    """Fetch a public HTTP(S) feed with DNS pinning, redirect revalidation, and a strict byte cap."""
    dns_resolver = resolver or socket.getaddrinfo
    connect_socket = socket_factory or socket.create_connection
    current = url
    visited: set[str] = set()
    tls_context = ssl.create_default_context()

    for redirect_count in range(MAX_REDIRECTS + 1):
        if current in visited:
            raise ValueError("Redirect loop detected while fetching source")
        visited.add(current)
        valid, error = validate_source_url(current)
        if not valid:
            raise ValueError(error)
        parsed = urlsplit(current)
        host = parsed.hostname or ""
        scheme = parsed.scheme.lower()
        port = parsed.port or (443 if scheme == "https" else 80)
        try:
            raw_answers = dns_resolver(host, port, type=socket.SOCK_STREAM)
        except Exception as exc:
            raise ValueError(f"DNS resolution failed for source host: {exc}") from exc

        ips: list[str] = []
        for answer in raw_answers:
            try:
                addr = answer[4][0]
            except (IndexError, TypeError):
                continue
            if not _is_public_ip(addr):
                raise ValueError(f"SSRF Protection: hostname resolves to a non-public address ({addr}).")
            if addr not in ips:
                ips.append(addr)
        if not ips:
            try:
                ipaddress.ip_address(host)
                if _is_public_ip(host):
                    ips = [host]
            except ValueError:
                pass
        if not ips:
            raise ValueError("Source hostname did not resolve to a public IP address")

        request_path = parsed.path or "/"
        if parsed.query:
            request_path += "?" + parsed.query
        host_header = f"[{host}]" if ":" in host and not host.startswith("[") else host
        if parsed.port:
            host_header = f"{host_header}:{parsed.port}"
        headers = {
            "Host": host_header,
            "User-Agent": "SOLOEARN-DiscoveryAgent/1.0 (Private Workbench; Local-First)",
            "Accept": "application/rss+xml, application/atom+xml, application/json, application/xml, text/xml, */*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }

        last_error = None
        response = None
        conn = None
        for ip in ips:
            try:
                sock = connect_socket((ip, port), timeout=timeout)
                sock.settimeout(timeout)
                if scheme == "https":
                    sock = tls_context.wrap_socket(sock, server_hostname=host)
                conn = http.client.HTTPConnection(host, port, timeout=timeout)
                conn.sock = sock  # connect to the validated IP; preserve Host and TLS SNI as the domain
                conn.putrequest("GET", request_path, skip_host=True, skip_accept_encoding=True)
                for key, value in headers.items():
                    conn.putheader(key, value)
                conn.endheaders()
                response = conn.getresponse()
                break
            except Exception as exc:
                last_error = exc
                if conn is not None:
                    conn.close()
                conn = None
                response = None
        if response is None:
            raise ValueError(f"Could not connect to public feed host: {last_error}")

        try:
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Redirect response did not include a Location header")
                if redirect_count >= MAX_REDIRECTS:
                    raise ValueError("Too many redirects while fetching source")
                next_url = urljoin(current, location)
                valid, error = validate_source_url(next_url)
                if not valid:
                    raise ValueError(f"Redirect blocked: {error}")
                current = next_url
                continue
            if response.status < 200 or response.status >= 300:
                raise ValueError(f"Feed server returned HTTP {response.status}")
            content_length = response.getheader("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError(f"Feed exceeds maximum allowed size ({max_bytes} bytes)")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError(f"Feed exceeds maximum allowed size ({max_bytes} bytes)")
            return body
        finally:
            if conn is not None:
                conn.close()

    raise ValueError("Unable to fetch source after redirect processing")

def parse_feed_xml(xml_content: str | bytes, source_name: str = "RSS Feed") -> list[dict[str, Any]]:
    """
    Parses RSS 2.0 or Atom XML feed content safely using standard library ElementTree.
    """
    if isinstance(xml_content, str):
        xml_bytes = xml_content.encode("utf-8")
    else:
        xml_bytes = xml_content

    if len(xml_bytes) > MAX_FEED_BYTES:
        raise ValueError(f"XML feed exceeds maximum allowed size ({MAX_FEED_BYTES} bytes)")
    upper_xml = xml_bytes.upper()
    if b"<!DOCTYPE" in upper_xml or b"<!ENTITY" in upper_xml:
        raise ValueError("XML DTD and entity declarations are not permitted")
    try:
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        raise ValueError(f"Invalid or malformed XML feed: {exc}") from exc

    items: list[dict[str, Any]] = []

    # Case 1: RSS 2.0 (<rss><channel><item>...)
    channel = root.find("channel")
    if channel is not None or root.tag.lower() == "channel":
        parent = channel if channel is not None else root
        for item in parent.findall("item"):
            title = (item.findtext("title") or "").strip()
            desc = (item.findtext("description") or item.findtext("summary") or "").strip()
            link = (item.findtext("link") or "").strip()
            if title:
                items.append({
                    "title": title,
                    "description": desc,
                    "url": link,
                    "source": source_name,
                })

    # Case 2: Atom (<feed xmlns="http://www.w3.org/2005/Atom"><entry>...)
    elif "feed" in root.tag.lower() or root.tag.endswith("feed"):
        # Atom entries might use XML namespace
        for elem in root:
            if elem.tag.endswith("entry"):
                title = ""
                desc = ""
                link = ""
                for child in elem:
                    tag = child.tag.split("}")[-1].lower() if "}" in child.tag else child.tag.lower()
                    if tag == "title":
                        title = (child.text or "").strip()
                    elif tag in ("summary", "content"):
                        desc = (child.text or desc).strip()
                    elif tag == "link":
                        link = child.attrib.get("href", (child.text or "")).strip()

                if title:
                    items.append({
                        "title": title,
                        "description": desc,
                        "url": link,
                        "source": source_name,
                    })

    if len(items) > MAX_FEED_ITEMS:
        raise ValueError(f"XML feed exceeds maximum item count ({MAX_FEED_ITEMS})")
    return items


def parse_json_leads(json_content: str | bytes, source_name: str = "JSON API") -> list[dict[str, Any]]:
    """Parses JSON data containing opportunities list or feed with input limits."""
    raw_bytes = json_content.encode("utf-8") if isinstance(json_content, str) else json_content
    if len(raw_bytes) > MAX_FEED_BYTES:
        raise ValueError(f"JSON feed exceeds maximum allowed size ({MAX_FEED_BYTES} bytes)")
    try:
        data = json.loads(raw_bytes)
    except Exception as exc:
        raise ValueError(f"Invalid JSON lead payload: {exc}") from exc

    raw_list: list[Any] = []
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        for key in ("items", "leads", "jobs", "opportunities", "data", "results"):
            if key in data and isinstance(data[key], list):
                raw_list = data[key]
                break
        if not raw_list and "title" in data:
            raw_list = [data]

    if len(raw_list) > MAX_FEED_ITEMS:
        raise ValueError(f"JSON feed exceeds maximum item count ({MAX_FEED_ITEMS})")
    items: list[dict[str, Any]] = []
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or entry.get("name") or "").strip()
        desc = str(entry.get("description") or entry.get("details") or entry.get("scope") or "").strip()
        url = str(entry.get("url") or entry.get("link") or "").strip()
        amount = entry.get("amount_inr") or entry.get("budget") or entry.get("amount")
        hours = entry.get("estimated_hours") or entry.get("hours")

        if title:
            item = {
                "title": title,
                "description": desc,
                "url": url,
                "source": entry.get("source") or source_name,
            }
            if amount is not None:
                item["amount_inr"] = amount
            if hours is not None:
                item["estimated_hours"] = hours
            items.append(item)

    return items


class DiscoveryEngine:
    """
    Milestone 4 Autonomous Opportunity Discovery Engine.
    Discovers, validates, evaluates, and deduplicates leads from authorized RSS/Atom feeds,
    JSON APIs, and local dropzones.
    """

    @classmethod
    def check_database_duplicate(cls, url: str, content_hash: str, title: str) -> bool:
        """Checks if lead already exists in SQLite opportunities table."""
        with connect() as conn:
            # Match 1: URL
            if url:
                row = conn.execute("SELECT id FROM opportunities WHERE url=? AND url!=''", (url,)).fetchone()
                if row:
                    return True

            # Match 2: Content Hash
            if content_hash:
                row = conn.execute("SELECT id FROM opportunities WHERE content_hash=?", (content_hash,)).fetchone()
                if row:
                    return True

            # Match 3: Title exact match
            norm_title = normalize_text(title)
            rows = conn.execute("SELECT title FROM opportunities ORDER BY id DESC LIMIT 200").fetchall()
            for r in rows:
                if normalize_text(r["title"]) == norm_title:
                    return True

        return False

    @classmethod
    def process_raw_leads(
        cls,
        raw_items: list[dict[str, Any]],
        source_name: str,
        source_permitted: bool = True,
        source_id: int | None = None,
        auto_commit: bool = True,
    ) -> DiscoveryBatchResult:
        """
        Processes parsed lead dictionaries through Capability Matching, Risk Analysis,
        Deduplication, and Scoring. Optionally commits novel opportunities to the database.
        """
        batch = DiscoveryBatchResult(
            source_id=source_id,
            source_name=source_name,
            source_type="batch",
            total_found=len(raw_items),
            leads_imported=0,
            duplicates_skipped=0,
            unsuitable_skipped=0,
        )

        seen_in_batch: set[str] = set()

        for item in raw_items:
            title = str(item.get("title", "")).strip()
            desc = str(item.get("description", "")).strip()
            url = str(item.get("url", "")).strip()
            source = str(item.get("source") or source_name).strip()

            if not title:
                batch.unsuitable_skipped += 1
                continue
            if url:
                url_ok, url_error = validate_source_url(url)
                if not url_ok:
                    batch.unsuitable_skipped += 1
                    batch.leads.append(DiscoveredLead(
                        title=title, source=source, url="", description=desc,
                        amount_inr=0.0, estimated_hours=1.0, skill_match=0.0,
                        payment_confidence=0.0, risk_level=10.0,
                        source_permitted=source_permitted, score=0.0,
                        content_hash=compute_content_hash(title, desc),
                        is_suitable=False, rejection_reason=f"Unsafe opportunity link: {url_error}",
                    ))
                    continue

            c_hash = compute_content_hash(title, desc)

            # Deduplication
            is_dup = (c_hash in seen_in_batch) or cls.check_database_duplicate(url, c_hash, title)
            seen_in_batch.add(c_hash)

            if is_dup:
                batch.duplicates_skipped += 1
                lead = DiscoveredLead(
                    title=title,
                    source=source,
                    url=url,
                    description=desc,
                    amount_inr=0.0,
                    estimated_hours=1.0,
                    skill_match=0.0,
                    payment_confidence=0.0,
                    risk_level=5.0,
                    source_permitted=source_permitted,
                    score=0.0,
                    content_hash=c_hash,
                    is_duplicate=True,
                    rejection_reason="Duplicate opportunity (already present in database or batch)",
                )
                batch.leads.append(lead)
                continue

            # Capability Evaluation: any unsupported requirement makes this lead review-only/rejected.
            cap_result = CapabilityMatcher.match(title, desc)
            if not cap_result.is_suitable:
                batch.unsuitable_skipped += 1
                lead = DiscoveredLead(
                    title=title,
                    source=source,
                    url=url,
                    description=desc,
                    amount_inr=0.0,
                    estimated_hours=1.0,
                    skill_match=cap_result.skill_match,
                    payment_confidence=0.0,
                    risk_level=5.0,
                    source_permitted=source_permitted,
                    score=0.0,
                    content_hash=c_hash,
                    is_suitable=False,
                    rejection_reason=cap_result.explanation,
                )
                batch.leads.append(lead)
                continue

            # Unknown budgets remain unknown (₹0 sentinel); never invent a ₹5,000 budget.
            raw_amount = item.get("amount_inr")
            try:
                amount = float(raw_amount) if raw_amount not in (None, "") else extract_budget_inr(f"{title} {desc}")
            except (TypeError, ValueError):
                amount = 0.0
            if not math.isfinite(amount) or amount < 0:
                amount = 0.0
            try:
                hours = float(item.get("estimated_hours") or max(2.0, min(10.0, round(len(desc.split()) / 50.0 + 2.0, 1))))
            except (TypeError, ValueError):
                hours = max(2.0, min(10.0, round(len(desc.split()) / 50.0 + 2.0, 1)))
            if not math.isfinite(hours) or hours <= 0:
                hours = max(2.0, min(10.0, round(len(desc.split()) / 50.0 + 2.0, 1)))

            # Risk & Payment Security Analysis
            risk_result = RiskAnalyzer.analyze(
                title=title,
                description=desc,
                amount_inr=amount,
                estimated_hours=hours,
                url=url,
                source_permitted=source_permitted,
            )

            # Expected Value Scoring
            score = opportunity_score(
                amount_inr=amount,
                estimated_hours=hours,
                skill_match=cap_result.skill_match,
                payment_confidence=risk_result.payment_confidence,
                risk_level=risk_result.risk_level,
                source_permitted=source_permitted,
            )

            lead = DiscoveredLead(
                title=title,
                source=source,
                url=url,
                description=desc,
                amount_inr=amount,
                estimated_hours=hours,
                skill_match=cap_result.skill_match,
                payment_confidence=risk_result.payment_confidence,
                risk_level=risk_result.risk_level,
                source_permitted=source_permitted,
                score=score,
                content_hash=c_hash,
                is_suitable=True,
            )
            batch.leads.append(lead)

            if auto_commit:
                opp_id = add_opportunity(
                    {
                        "title": title,
                        "source": source,
                        "url": url,
                        "description": desc,
                        "amount_inr": amount,
                        "estimated_hours": hours,
                        "skill_match": cap_result.skill_match,
                        "payment_confidence": risk_result.payment_confidence,
                        "risk_level": risk_result.risk_level,
                        "source_permitted": source_permitted,
                        "content_hash": c_hash,
                    },
                    score=score,
                )
                batch.leads_imported += 1

        if source_id is not None:
            update_discovery_source_polled(source_id)

        log_event(
            "discovery_batch_completed",
            f"source={source_name}; total={batch.total_found}; imported={batch.leads_imported}; "
            f"duplicates={batch.duplicates_skipped}; unsuitable={batch.unsuitable_skipped}",
        )
        return batch

    @classmethod
    def poll_source(cls, source_dict: dict[str, Any], auto_commit: bool = True) -> DiscoveryBatchResult:
        """Polls an individual registered discovery source (RSS, Atom, JSON API, or local folder)."""
        s_id = source_dict["id"]
        s_name = source_dict["name"]
        s_type = source_dict["source_type"]
        s_target = source_dict["url_or_path"]
        s_permitted = bool(source_dict.get("is_permitted", 1))

        if not s_permitted:
            return DiscoveryBatchResult(
                source_id=s_id,
                source_name=s_name,
                source_type=s_type,
                total_found=0,
                leads_imported=0,
                duplicates_skipped=0,
                unsuitable_skipped=0,
                errors=["Source is not marked as permitted."],
            )

        raw_items: list[dict[str, Any]] = []

        try:
            # Local files and folders are confined to the explicitly approved dropzone.
            if s_type == "local_folder":
                target_path = safe_dropzone_path(s_target or LEADS_DROPZONE_DIR, expect_directory=True)
                for fpath in target_path.iterdir():
                    if fpath.is_symlink() or not fpath.is_file():
                        continue
                    if fpath.suffix.lower() not in (".json", ".xml", ".rss", ".atom"):
                        continue
                    content = fpath.read_bytes()
                    if len(content) > MAX_FEED_BYTES:
                        continue
                    if fpath.suffix.lower() == ".json":
                        raw_items.extend(parse_json_leads(content, s_name))
                    else:
                        raw_items.extend(parse_feed_xml(content, s_name))
                    if len(raw_items) > MAX_FEED_ITEMS:
                        raise ValueError(f"Local dropzone exceeds maximum item count ({MAX_FEED_ITEMS})")

            elif s_type in ("rss", "atom", "json_api"):
                # A local file is permitted only from data/leads_dropzone; all other targets must be public URLs.
                local_candidate = Path(s_target)
                if local_candidate.exists():
                    local_f = safe_dropzone_path(local_candidate, expect_directory=False)
                    content = local_f.read_bytes()
                    if len(content) > MAX_FEED_BYTES:
                        raise ValueError(f"Feed exceeds maximum allowed size ({MAX_FEED_BYTES} bytes)")
                else:
                    content = fetch_public_feed(s_target)
                if s_type == "json_api":
                    raw_items = parse_json_leads(content, s_name)
                else:
                    raw_items = parse_feed_xml(content, s_name)

            else:
                raise ValueError(f"Unsupported discovery source type: {s_type}")

        except Exception as exc:
            return DiscoveryBatchResult(
                source_id=s_id,
                source_name=s_name,
                source_type=s_type,
                total_found=0,
                leads_imported=0,
                duplicates_skipped=0,
                unsuitable_skipped=0,
                errors=[f"Polling error for source '{s_name}': {exc}"],
            )

        res = cls.process_raw_leads(
            raw_items=raw_items,
            source_name=s_name,
            source_permitted=s_permitted,
            source_id=s_id,
            auto_commit=auto_commit,
        )
        res.source_type = s_type
        return res
