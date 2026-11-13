"""
Seed Indian D2C & Retail Analytics Discovery Sources
Registers verified local dropzone and public feeds for autonomous opportunity discovery.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import LEADS_DROPZONE_DIR
from core.db import connect, list_discovery_sources, add_discovery_source, utc_now
from core.discovery_engine import DiscoveryEngine

NEW_SOURCES = [
    {
        "name": "Indian D2C Operations & Dropzone Leads",
        "source_type": "local_folder",
        "url_or_path": str(LEADS_DROPZONE_DIR),
        "poll_interval_minutes": 15,
        "is_permitted": True,
        "is_active": True,
    },
]

def seed_sources() -> None:
    print("Checking and seeding Indian D2C discovery sources...")
    existing = {r["url_or_path"]: r for r in list_discovery_sources(active_only=False)}
    added_count = 0
    for src in NEW_SOURCES:
        if src["url_or_path"] not in existing:
            src_id = add_discovery_source(
                name=src["name"],
                source_type=src["source_type"],
                url_or_path=src["url_or_path"],
                poll_interval_minutes=src["poll_interval_minutes"],
                is_permitted=src["is_permitted"],
                is_active=src["is_active"],
            )
            print(f"  + Added source #{src_id}: {src['name']}")
            added_count += 1
        else:
            print(f"  = Already registered: {src['name']}")

    print(f"Discovery sources up-to-date ({added_count} new registered).")
    
    # Run immediate test poll
    print("\nExecuting test discovery poll...")
    active_sources = list_discovery_sources(active_only=True)
    results = [DiscoveryEngine.poll_source(s) for s in active_sources]
    print(f"Polled {len(results)} sources.")
    for res in results:
        print(f"  - Source: {res.source_name} | Found: {res.total_found} | Imported: {res.leads_imported} | Duplicates skipped: {res.duplicates_skipped} | Unsuitable: {res.unsuitable_skipped}")
        for lead in res.leads:
            print(f"    * [{lead.score:.2f} INR/hr] {lead.title} (INR {lead.amount_inr:,.0f}, {lead.estimated_hours}h) - Fit: {lead.is_suitable}")

if __name__ == "__main__":
    seed_sources()
