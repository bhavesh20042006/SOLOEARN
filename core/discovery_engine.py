from __future__ import annotations

import csv
import hashlib
import ipaddress
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.capability_matcher import CapabilityMatcher
from core.config import LEADS_DROPZONE_DIR
from core.db import add_opportunity, connect, log_event, update_discovery_source_polled
from core.risk_analyzer import RiskAnalyzer
from core.scoring import opportunity_score


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


def extract_budget_inr(text: str) -> float:
    """Extracts budget mentions (INR or USD converted) from job text with fallback."""
    # Look for INR symbols or text: ₹ 5,000 or INR 5,000 or Rs. 5000
    inr_match = re.search(r"(?:₹|inr|rs\.?)\s*([0-9]+(?:,[0-9]+)*(?:\.[0-9]+)?)", text, re.IGNORECASE)
    if inr_match:
        try:
            val_str = inr_match.group(1).replace(",", "")
            val = float(val_str)
            if 100 <= val <= 500000:
                return round(val, 2)
        except ValueError:
            pass

    # Look for USD symbols: $ 100 -> convert at ₹85/USD
    usd_match = re.search(r"\$\s*([0-9]+(?:,[0-9]+)*(?:\.[0-9]+)?)", text)
    if usd_match:
        try:
            val_str = usd_match.group(1).replace(",", "")
            val = float(val_str) * 85.0
            if 100 <= val <= 500000:
                return round(val, 2)
        except ValueError:
            pass

    # Default realistic baseline for verified data-analytics gigs
    return 5000.0


def validate_source_url(url: str) -> tuple[bool, str]:
    """
    Validates URL safety and guards against Server-Side Request Forgery (SSRF).
    Only http and https protocols are permitted; local and private network addresses are blocked.
    """
    if not url or not isinstance(url, str):
        return False, "URL is empty or invalid"
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"URL parse failure: {exc}"

    if parsed.scheme.lower() not in ("http", "https"):
        return False, f"Invalid scheme '{parsed.scheme}'. Only http and https are allowed."

    hostname = parsed.hostname
    if not hostname:
        return False, "URL hostname is missing"

    # Reject localhost & internal loopback hosts
    if hostname.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False, f"SSRF Protection: Access to localhost ({hostname}) is forbidden."

    # Check for private IP address ranges
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
            return False, f"SSRF Protection: Private or reserved IP address ({hostname}) is forbidden."
    except ValueError:
        # Hostname is a domain name, not a raw IP address
        pass

    return True, ""


def parse_feed_xml(xml_content: str | bytes, source_name: str = "RSS Feed") -> list[dict[str, Any]]:
    """
    Parses RSS 2.0 or Atom XML feed content safely using standard library ElementTree.
    """
    if isinstance(xml_content, str):
        xml_bytes = xml_content.encode("utf-8")
    else:
        xml_bytes = xml_content

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

    return items


def parse_json_leads(json_content: str | bytes, source_name: str = "JSON API") -> list[dict[str, Any]]:
    """Parses JSON data containing opportunities list or feed."""
    try:
        data = json.loads(json_content)
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

            # Capability Evaluation
            cap_result = CapabilityMatcher.match(title, desc)
            if not cap_result.is_suitable and cap_result.skill_match < 0.25:
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

            # Amount and Hours determination
            amount = float(item.get("amount_inr") or extract_budget_inr(f"{title} {desc}"))
            hours = float(item.get("estimated_hours") or max(2.0, min(10.0, round(len(desc.split()) / 50.0 + 2.0, 1))))

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
            # 1. Local folder dropzone
            if s_type == "local_folder":
                target_path = Path(s_target)
                if not target_path.exists():
                    target_path = LEADS_DROPZONE_DIR
                for fpath in target_path.glob("*"):
                    if fpath.suffix.lower() == ".json":
                        raw_items.extend(parse_json_leads(fpath.read_text(encoding="utf-8"), s_name))
                    elif fpath.suffix.lower() in (".xml", ".rss", ".atom"):
                        raw_items.extend(parse_feed_xml(fpath.read_text(encoding="utf-8"), s_name))

            # 2. Remote RSS / Atom Feed
            elif s_type in ("rss", "atom"):
                # If target is local file for testing
                local_f = Path(s_target)
                if local_f.exists() and local_f.is_file():
                    content = local_f.read_bytes()
                else:
                    valid, err = validate_source_url(s_target)
                    if not valid:
                        raise ValueError(err)
                    req = urllib.request.Request(
                        s_target,
                        headers={"User-Agent": "SOLOEARN-DiscoveryAgent/1.0 (Private Workbench; Local-First)"},
                    )
                    with urllib.request.urlopen(req, timeout=12.0) as resp:
                        content = resp.read()
                raw_items = parse_feed_xml(content, s_name)

            # 3. Remote JSON API
            elif s_type == "json_api":
                local_f = Path(s_target)
                if local_f.exists() and local_f.is_file():
                    content = local_f.read_bytes()
                else:
                    valid, err = validate_source_url(s_target)
                    if not valid:
                        raise ValueError(err)
                    req = urllib.request.Request(
                        s_target,
                        headers={"User-Agent": "SOLOEARN-DiscoveryAgent/1.0 (Private Workbench; Local-First)"},
                    )
                    with urllib.request.urlopen(req, timeout=12.0) as resp:
                        content = resp.read()
                raw_items = parse_json_leads(content, s_name)

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
