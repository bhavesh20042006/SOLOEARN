from __future__ import annotations

"""
core/db_maintenance.py
=======================
Automated SQLite Database Maintenance, WAL Checkpointing, Integrity Auditing,
and Daily Automated Backup System for SOLOEARN.

Features:
- Online, non-blocking atomic backups via SQLite's native backup API.
- WAL (Write-Ahead-Log) checkpointing (PASSIVE/TRUNCATE) to reclaim disk space.
- Automatic retention policy (keeps N daily backups, prunes old ones).
- Automated integrity checking (PRAGMA integrity_check / quick_check).
- VACUUM optimization for defragmentation and page reclamation.
- Schedule manager for daemon integration and standalone CLI execution.
"""

import logging
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import core.config
from core.config import DATA_DIR, DB_PATH, ensure_directories

logger = logging.getLogger("soloearn.db_maintenance")

BACKUPS_DIR = DATA_DIR / "backups"
DEFAULT_BACKUP_RETENTION_DAYS = 7


@dataclass
class DatabaseStats:
    db_size_bytes: int
    wal_size_bytes: int
    shm_size_bytes: int
    page_count: int
    page_size: int
    freelist_count: int
    total_backups: int
    last_backup_file: str | None
    last_backup_utc: str | None
    integrity_ok: bool
    integrity_messages: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_db_path() -> Path:
    return core.config.DB_PATH


def get_wal_path() -> Path:
    return Path(str(core.config.DB_PATH) + "-wal")


def get_shm_path() -> Path:
    return Path(str(core.config.DB_PATH) + "-shm")


def checkpoint_wal(mode: str = "TRUNCATE") -> dict[str, Any]:
    """
    Executes PRAGMA wal_checkpoint(mode) on the database.
    Supported modes: PASSIVE, FULL, RESTART, TRUNCATE.
    TRUNCATE flushes WAL pages to the database file and shrinks WAL size to zero.
    """
    valid_modes = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
    norm_mode = mode.upper().strip()
    if norm_mode not in valid_modes:
        raise ValueError(f"Invalid WAL checkpoint mode: {mode}. Must be one of {valid_modes}")

    ensure_directories()
    conn = sqlite3.connect(get_db_path(), timeout=60.0)
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        res = conn.execute(f"PRAGMA wal_checkpoint({norm_mode})").fetchone()
        busy = int(res[0]) if res else 0
        log_pages = int(res[1]) if res else 0
        checkpointed = int(res[2]) if res else 0

        # If TRUNCATE was blocked by an active reader, perform non-blocking PASSIVE flush
        if busy != 0 and norm_mode in ("TRUNCATE", "RESTART"):
            logger.info(f"WAL Checkpoint {norm_mode} encountered active reader (busy=1). Executing PASSIVE checkpoint fallback...")
            res_passive = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            if res_passive:
                busy = int(res_passive[0])
                log_pages = int(res_passive[1])
                checkpointed = int(res_passive[2])

        wal_size = get_wal_path().stat().st_size if get_wal_path().exists() else 0
        logger.info(
            f"WAL Checkpoint ({norm_mode}) completed: busy={busy}, log_pages={log_pages}, "
            f"checkpointed={checkpointed}, wal_size={wal_size:,} bytes"
        )
        return {
            "mode": norm_mode,
            "busy": busy,
            "log_pages": log_pages,
            "checkpointed_pages": checkpointed,
            "wal_size_bytes": wal_size,
            "success": busy == 0,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    except Exception as exc:
        logger.error(f"WAL Checkpoint ({norm_mode}) failed: {exc}", exc_info=True)
        return {
            "mode": norm_mode,
            "error": str(exc),
            "success": False,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    finally:
        conn.close()


def check_database_integrity(quick: bool = False) -> tuple[bool, list[str]]:
    """
    Executes PRAGMA integrity_check or PRAGMA quick_check.
    Returns (is_ok, messages).
    """
    ensure_directories()
    pragma = "quick_check" if quick else "integrity_check"
    conn = sqlite3.connect(get_db_path(), timeout=30.0)
    try:
        rows = conn.execute(f"PRAGMA {pragma}").fetchall()
        msgs = [str(r[0]) for r in rows]
        is_ok = len(msgs) == 1 and msgs[0].lower() == "ok"
        return is_ok, msgs
    except Exception as exc:
        logger.error(f"Database integrity check failed: {exc}", exc_info=True)
        return False, [f"Integrity check exception: {exc}"]
    finally:
        conn.close()


def backup_database(
    backup_dir: Path | None = None,
    keep_count: int = DEFAULT_BACKUP_RETENTION_DAYS,
    verify_after: bool = True,
) -> Path:
    """
    Creates an online, non-blocking atomic backup using SQLite's native backup API.
    Safe to run while live transactions are in progress.
    Validates backup integrity and automatically rotates out old backup files.
    """
    ensure_directories()
    dest_dir = backup_dir or BACKUPS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_filename = f"soloearn_backup_{timestamp_str}.sqlite3"
    dest_path = dest_dir / backup_filename
    temp_dest = dest_dir / f"{backup_filename}.tmp"

    src_conn = sqlite3.connect(get_db_path(), timeout=60.0)
    src_conn.execute("PRAGMA busy_timeout = 60000")
    try:
        # First ensure WAL pages are flushed so backup is up-to-date
        try:
            src_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

        dst_conn = sqlite3.connect(temp_dest, timeout=60.0)
        dst_conn.execute("PRAGMA busy_timeout = 60000")
        try:
            # Perform non-blocking atomic backup using SQLite's backup API
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    # Verify integrity of the newly created backup file
    if verify_after:
        vconn = sqlite3.connect(temp_dest, timeout=15.0)
        try:
            v_rows = vconn.execute("PRAGMA quick_check").fetchall()
            v_msgs = [str(r[0]) for r in v_rows]
            if not (len(v_msgs) == 1 and v_msgs[0].lower() == "ok"):
                temp_dest.unlink(missing_ok=True)
                raise RuntimeError(f"Backup verification failed: {v_msgs}")
        finally:
            vconn.close()

    # Atomically move temp file to final destination
    if dest_path.exists():
        dest_path.unlink()
    temp_dest.replace(dest_path)

    backup_size = dest_path.stat().st_size
    logger.info(f"Database backup created successfully: {dest_path.name} ({backup_size:,} bytes)")

    # Prune old timestamped backups keeping keep_count
    prune_old_backups(dest_dir, keep_count)

    return dest_path


def prune_old_backups(dest_dir: Path, keep_count: int = DEFAULT_BACKUP_RETENTION_DAYS) -> list[str]:
    """Removes older backup files matching soloearn_backup_*.sqlite3, keeping the newest keep_count."""
    removed = []
    backups = sorted(
        dest_dir.glob("soloearn_backup_*.sqlite3"),
        key=lambda p: p.stat().st_mtime,
    )
    if len(backups) > keep_count:
        excess = backups[:-keep_count]
        for p in excess:
            try:
                p.unlink()
                removed.append(p.name)
                logger.info(f"Pruned old database backup: {p.name}")
            except OSError as ex:
                logger.warning(f"Could not prune backup {p.name}: {ex}")
    return removed


def vacuum_database(max_retries: int = 3, retry_delay: float = 1.0) -> bool:
    """
    Executes VACUUM to defragment B-trees and reclaim freed disk pages.
    VACUUM must run outside of any active transaction.
    Retries gracefully if database is temporarily locked by active readers.
    """
    ensure_directories()
    for attempt in range(1, max_retries + 1):
        conn = sqlite3.connect(get_db_path(), timeout=60.0)
        try:
            conn.execute("PRAGMA busy_timeout = 60000")
            conn.isolation_level = None  # Autocommit mode required for VACUUM
            conn.execute("VACUUM")
            logger.info("Database VACUUM completed successfully.")
            return True
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                if attempt < max_retries:
                    logger.info(f"Database locked during VACUUM (attempt {attempt}/{max_retries}); retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    continue
            logger.warning(f"Database VACUUM deferred: {exc}")
            return False
        except Exception as exc:
            logger.error(f"Database VACUUM failed: {exc}", exc_info=True)
            return False
        finally:
            conn.close()
    return False


def get_database_stats() -> DatabaseStats:
    """Collects comprehensive database size, page, and backup statistics."""
    ensure_directories()
    db_p = get_db_path()
    wal_p = get_wal_path()
    shm_p = get_shm_path()

    db_size = db_p.stat().st_size if db_p.exists() else 0
    wal_size = wal_p.stat().st_size if wal_p.exists() else 0
    shm_size = shm_p.stat().st_size if shm_p.exists() else 0

    page_count = 0
    page_size = 4096
    freelist_count = 0

    if db_p.exists():
        conn = sqlite3.connect(db_p, timeout=15.0)
        try:
            r = conn.execute("PRAGMA page_count").fetchone()
            if r:
                page_count = int(r[0])
            r = conn.execute("PRAGMA page_size").fetchone()
            if r:
                page_size = int(r[0])
            r = conn.execute("PRAGMA freelist_count").fetchone()
            if r:
                freelist_count = int(r[0])
        except Exception:
            pass
        finally:
            conn.close()

    # Backup statistics
    backups = sorted(BACKUPS_DIR.glob("soloearn_backup_*.sqlite3"), key=lambda p: p.stat().st_mtime)
    last_file = backups[-1].name if backups else None
    last_utc = (
        datetime.fromtimestamp(backups[-1].stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        if backups
        else None
    )

    integrity_ok, integrity_msgs = check_database_integrity(quick=True)

    return DatabaseStats(
        db_size_bytes=db_size,
        wal_size_bytes=wal_size,
        shm_size_bytes=shm_size,
        page_count=page_count,
        page_size=page_size,
        freelist_count=freelist_count,
        total_backups=len(backups),
        last_backup_file=last_file,
        last_backup_utc=last_utc,
        integrity_ok=integrity_ok,
        integrity_messages=integrity_msgs,
    )


class DatabaseMaintenanceManager:
    """
    Automated Database Maintenance Scheduler.
    Executes periodic WAL checkpoints, daily backups, and weekly vacuuming.
    Tracks execution times in memory to avoid redundant operations.
    """

    def __init__(
        self,
        checkpoint_interval_seconds: int = 3600,       # Every 1 hour
        backup_interval_seconds: int = 86400,          # Daily (24 hours)
        vacuum_interval_seconds: int = 7 * 86400,      # Weekly (7 days)
        wal_size_threshold_bytes: int = 10 * 1024 * 1024,  # Checkpoint if WAL > 10MB
        keep_backups: int = DEFAULT_BACKUP_RETENTION_DAYS,
    ):
        self.checkpoint_interval = checkpoint_interval_seconds
        self.backup_interval = backup_interval_seconds
        self.vacuum_interval = vacuum_interval_seconds
        self.wal_size_threshold = wal_size_threshold_bytes
        self.keep_backups = keep_backups

        self.last_checkpoint_time: float = 0.0
        self.last_backup_time: float = 0.0
        self.last_vacuum_time: float = 0.0
        self.total_checkpoints: int = 0
        self.total_backups: int = 0
        self.total_vacuums: int = 0

        # Discover last backup timestamp from disk if any exists
        existing_backups = sorted(BACKUPS_DIR.glob("soloearn_backup_*.sqlite3"), key=lambda p: p.stat().st_mtime)
        if existing_backups:
            self.last_backup_time = existing_backups[-1].stat().st_mtime

    def is_checkpoint_due(self) -> bool:
        now = time.time()
        if (now - self.last_checkpoint_time) >= self.checkpoint_interval:
            return True
        wal_p = get_wal_path()
        if wal_p.exists() and wal_p.stat().st_size >= self.wal_size_threshold:
            return True
        return False

    def is_backup_due(self) -> bool:
        now = time.time()
        return (now - self.last_backup_time) >= self.backup_interval

    def is_vacuum_due(self) -> bool:
        now = time.time()
        return (now - self.last_vacuum_time) >= self.vacuum_interval

    def get_summary(self) -> dict[str, Any]:
        return {
            "checkpoint_interval_seconds": self.checkpoint_interval,
            "backup_interval_seconds": self.backup_interval,
            "vacuum_interval_seconds": self.vacuum_interval,
            "last_checkpoint_utc": datetime.fromtimestamp(self.last_checkpoint_time, tz=timezone.utc).isoformat(timespec="seconds") if self.last_checkpoint_time else None,
            "last_backup_utc": datetime.fromtimestamp(self.last_backup_time, tz=timezone.utc).isoformat(timespec="seconds") if self.last_backup_time else None,
            "last_vacuum_utc": datetime.fromtimestamp(self.last_vacuum_time, tz=timezone.utc).isoformat(timespec="seconds") if self.last_vacuum_time else None,
            "total_checkpoints": self.total_checkpoints,
            "total_backups": self.total_backups,
            "total_vacuums": self.total_vacuums,
        }

    def run_maintenance_if_due(self, force: bool = False) -> dict[str, Any]:
        """
        Executes maintenance tasks that are due, or forces all if force=True.
        Returns a dictionary summary of actions executed.
        """
        actions: dict[str, Any] = {}
        now = time.time()

        # 1. WAL Checkpoint
        if force or self.is_checkpoint_due():
            logger.info("Executing scheduled SQLite WAL checkpoint...")
            cp_res = checkpoint_wal("TRUNCATE")
            actions["checkpoint"] = cp_res
            self.last_checkpoint_time = now
            self.total_checkpoints += 1

        # 2. Automated Daily Backup
        if force or self.is_backup_due():
            logger.info("Executing scheduled automated daily SQLite backup...")
            try:
                b_path = backup_database(keep_count=self.keep_backups, verify_after=True)
                actions["backup"] = {
                    "path": str(b_path),
                    "filename": b_path.name,
                    "size_bytes": b_path.stat().st_size,
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "status": "success",
                }
                self.last_backup_time = now
                self.total_backups += 1
            except Exception as exc:
                logger.error(f"Scheduled backup failed: {exc}", exc_info=True)
                actions["backup"] = {"status": "error", "message": str(exc)}

        # 3. VACUUM
        if force or self.is_vacuum_due():
            logger.info("Executing scheduled SQLite database VACUUM...")
            v_ok = vacuum_database()
            actions["vacuum"] = {
                "status": "success" if v_ok else "error",
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if v_ok:
                self.last_vacuum_time = now
                self.total_vacuums += 1

        # Add current DB stats and maintenance summary
        actions["stats"] = get_database_stats().to_dict()
        actions["summary"] = self.get_summary()
        return actions
