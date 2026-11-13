from __future__ import annotations

"""
SOLOEARN System Health & Diagnostics Engine
Performs multi-point self-tests across Database, LLM, File System, Storage, and Security Configuration.
"""

import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import DATA_DIR, WORKSPACES_DIR, DB_PATH
from core.db import connect
from core.llm import OllamaClient
from core.secrets_manager import SecretsManager


@dataclass
class ComponentHealth:
    component: str
    status: str  # "ok", "degraded", "down"
    message: str
    latency_ms: float
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SystemHealthReport:
    status: str  # "healthy", "degraded", "critical"
    timestamp: str
    checks: list[ComponentHealth]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "timestamp": self.timestamp,
            "checks": [c.to_dict() for c in self.checks],
            "summary": self.summary,
        }


def check_database() -> ComponentHealth:
    start = time.perf_counter()
    details: dict[str, Any] = {}
    try:
        with connect() as conn:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            opp_count = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            invoice_count = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]

            details = {
                "journal_mode": journal_mode,
                "foreign_keys_enabled": bool(foreign_keys),
                "opportunities": opp_count,
                "tasks": task_count,
                "invoices": invoice_count,
                "db_path": str(DB_PATH),
            }
            latency = round((time.perf_counter() - start) * 1000, 2)
            if str(journal_mode).lower() != "wal":
                return ComponentHealth(
                    component="database",
                    status="degraded",
                    message="Database accessible but not in WAL mode",
                    latency_ms=latency,
                    details=details,
                )
            return ComponentHealth(
                component="database",
                status="ok",
                message="SQLite WAL database fully operational",
                latency_ms=latency,
                details=details,
            )
    except Exception as exc:
        latency = round((time.perf_counter() - start) * 1000, 2)
        return ComponentHealth(
            component="database",
            status="down",
            message=f"Database connection error: {exc}",
            latency_ms=latency,
            details={"error": str(exc)},
        )


def check_storage() -> ComponentHealth:
    start = time.perf_counter()
    try:
        total, used, free = shutil.disk_usage(DATA_DIR)
        free_mb = round(free / (1024 * 1024), 2)
        total_mb = round(total / (1024 * 1024), 2)
        used_pct = round((used / total) * 100, 1)

        test_file = WORKSPACES_DIR / ".health_probe"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()

        latency = round((time.perf_counter() - start) * 1000, 2)
        details = {
            "free_space_mb": free_mb,
            "total_space_mb": total_mb,
            "used_pct": used_pct,
            "workspaces_writable": True,
        }

        if free_mb < 500:
            return ComponentHealth(
                component="storage",
                status="degraded",
                message=f"Low disk space: {free_mb} MB available",
                latency_ms=latency,
                details=details,
            )

        return ComponentHealth(
            component="storage",
            status="ok",
            message="Storage and isolated workspaces directory healthy",
            latency_ms=latency,
            details=details,
        )
    except Exception as exc:
        latency = round((time.perf_counter() - start) * 1000, 2)
        return ComponentHealth(
            component="storage",
            status="down",
            message=f"Storage probe failed: {exc}",
            latency_ms=latency,
            details={"error": str(exc)},
        )


def check_llm() -> ComponentHealth:
    start = time.perf_counter()
    client = OllamaClient()
    ok, models, message = client.status()
    latency = round((time.perf_counter() - start) * 1000, 2)
    details = {
        "reachable": ok,
        "models_installed": models,
        "target_model": client.model,
    }
    if ok:
        model_present = client.model in models
        if model_present:
            return ComponentHealth(
                component="local_llm",
                status="ok",
                message=f"Ollama running with target model '{client.model}' loaded",
                latency_ms=latency,
                details=details,
            )
        return ComponentHealth(
            component="local_llm",
            status="degraded",
            message=f"Ollama running but model '{client.model}' not yet pulled (deterministic fallback active)",
            latency_ms=latency,
            details=details,
        )
    return ComponentHealth(
        component="local_llm",
        status="degraded",
        message=f"{message} (deterministic ReAct fallback active)",
        latency_ms=latency,
        details=details,
    )


def check_secrets_and_config() -> ComponentHealth:
    start = time.perf_counter()
    mgr = SecretsManager()
    val = mgr.validate_secrets()
    latency = round((time.perf_counter() - start) * 1000, 2)

    details = {
        "valid": val["valid"],
        "missing_required": val["missing_required"],
        "missing_optional": val["missing_optional"],
    }
    if not val["valid"]:
        return ComponentHealth(
            component="secrets_config",
            status="down",
            message=f"Missing required configuration: {val['missing_required']}",
            latency_ms=latency,
            details=details,
        )
    return ComponentHealth(
        component="secrets_config",
        status="ok",
        message="All required configuration parameters verified",
        latency_ms=latency,
        details=details,
    )


def run_system_health_audit() -> SystemHealthReport:
    """Runs full system self-check and returns an actionable diagnostic report."""
    checks = [
        check_database(),
        check_storage(),
        check_secrets_and_config(),
        check_llm(),
    ]

    statuses = [c.status for c in checks]
    if "down" in statuses:
        overall = "critical"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "healthy"

    ok_count = sum(1 for s in statuses if s == "ok")
    summary = f"{ok_count}/{len(checks)} components healthy. Overall state: {overall.upper()}."

    return SystemHealthReport(
        status=overall,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        checks=checks,
        summary=summary,
    )
