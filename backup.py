"""자동 백업 — bokju.db를 backups/에 주기적으로 복사.

SQLite 온라인 백업 API(`Connection.backup`)를 쓴다. 파일 복사와 달리 상담사가
저장 중인 순간에도 일관된 스냅샷이 나오므로 앱을 멈출 필요가 없다.

동작:
  · 앱 시작 시 1회 (기동 직후 상태 보존)
  · 이후 매일 BACKUP_HOUR 시각 (기본 03시)
  · BACKUP_KEEP_DAYS 보다 오래된 파일은 자동 삭제 (기본 30일)

백업 파일은 컨테이너 밖 볼륨(BACKUP_DIR)에 쌓이므로 컨테이너를 지워도 남는다.
"""
import logging
import os
import sqlite3
import threading
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import models

logger = logging.getLogger(__name__)

BACKUP_DIR = Path(os.getenv("BACKUP_DIR") or "./backups")
BACKUP_HOUR = int(os.getenv("BACKUP_HOUR", "3"))
KEEP_DAYS = int(os.getenv("BACKUP_KEEP_DAYS", "30"))


def verify_database(path) -> dict:
    """백업을 실제 복구 후보처럼 열어 무결성·핵심 테이블 조회를 확인한다."""
    result = {"ok": False, "path": str(path), "checked_at": datetime.now().isoformat(timespec="seconds")}
    try:
        with sqlite3.connect(str(path), timeout=models.BUSY_TIMEOUT) as conn:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"patients", "consultations", "users", "app_meta"}
            result.update({"integrity": check, "missing_tables": sorted(required - tables),
                           "patients": conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0],
                           "consultations": conn.execute("SELECT COUNT(*) FROM consultations").fetchone()[0]})
            result["ok"] = check == "ok" and not result["missing_tables"]
    except Exception as exc:
        result["error"] = str(exc)
    return result


def latest_status() -> dict:
    files = sorted(BACKUP_DIR.glob("bokju_*.db"), key=lambda f: f.stat().st_mtime, reverse=True) if BACKUP_DIR.exists() else []
    saved = models.get_app_meta("backup_last_status", {}) or {}
    return {**saved, "directory": str(BACKUP_DIR.resolve()), "keep_days": KEEP_DAYS,
            "file_count": len(files), "latest_file": files[0].name if files else None,
            "latest_size_mb": round(files[0].stat().st_size / 1024 / 1024, 2) if files else 0}


def verify_latest_restore() -> dict:
    """운영 DB를 덮지 않고 임시 파일로 복원 사전연습을 수행한다."""
    files = sorted(BACKUP_DIR.glob("bokju_*.db"), key=lambda f: f.stat().st_mtime, reverse=True) if BACKUP_DIR.exists() else []
    if not files:
        return {"ok": False, "error": "검증할 백업 파일이 없습니다."}
    with tempfile.TemporaryDirectory(prefix="bokju_restore_") as tmp:
        restored = Path(tmp) / "restore_test.db"
        src = sqlite3.connect(str(files[0]), timeout=models.BUSY_TIMEOUT)
        dst = sqlite3.connect(str(restored))
        try:
            src.backup(dst)
        finally:
            dst.close(); src.close()
        result = verify_database(restored)
        result["source"] = files[0].name
        result["mode"] = "임시 복구 검증"
    models.set_app_meta("backup_restore_test", result)
    return result


def run_backup(tag: str = "daily") -> Path | None:
    """DB 스냅샷 1건 생성 후 경로 반환. 실패해도 예외를 밖으로 내보내지 않는다
    (백업 실패가 상담 업무를 막으면 안 된다 — 로그만 남기고 다음 주기에 재시도)."""
    src_path = models.DB_PATH
    if not os.path.exists(src_path):
        logger.warning("백업 건너뜀 — DB 없음: %s", src_path)
        return None
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dst = BACKUP_DIR / f"bokju_{tag}_{datetime.now():%Y%m%d_%H%M%S}.db"
        src = sqlite3.connect(src_path, timeout=models.BUSY_TIMEOUT)
        try:
            dst_conn = sqlite3.connect(dst)
            try:
                src.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src.close()
        verification = verify_database(dst)
        if not verification["ok"]:
            raise RuntimeError(f"백업 무결성 검사 실패: {verification}")
        size_mb = dst.stat().st_size / 1024 / 1024
        logger.info("백업 완료: %s (%.1f MB)", dst, size_mb)
        _prune()
        models.set_app_meta("backup_last_status", {
            "ok": True, "file": dst.name, "size_mb": round(size_mb, 2),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "integrity": verification.get("integrity"),
        })
        return dst
    except Exception as exc:
        logger.exception("백업 실패 — 다음 주기에 재시도")
        try:
            models.set_app_meta("backup_last_status", {
                "ok": False, "error": str(exc),
                "created_at": datetime.now().isoformat(timespec="seconds")})
        except Exception:
            pass
        return None


def _prune():
    """보관 기간이 지난 백업 삭제. 최소 1개는 항상 남긴다."""
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    files = sorted(BACKUP_DIR.glob("bokju_*.db"), key=lambda f: f.stat().st_mtime)
    for f in files[:-1]:  # 가장 최근 1개는 보호
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            try:
                f.unlink()
                logger.info("오래된 백업 삭제: %s", f.name)
            except OSError:
                logger.exception("백업 삭제 실패: %s", f)


def _seconds_until_next_run() -> float:
    now = datetime.now()
    nxt = now.replace(hour=BACKUP_HOUR, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def _loop():
    while True:
        threading.Event().wait(_seconds_until_next_run())
        run_backup("daily")


def start_scheduler():
    """기동 백업 1회 + 매일 BACKUP_HOUR 백업 스레드 시작.
    데몬 스레드라 앱 종료 시 함께 내려간다."""
    run_backup("startup")
    t = threading.Thread(target=_loop, name="backup-scheduler", daemon=True)
    t.start()
    logger.info("백업 스케줄러 시작 — 매일 %02d시, %s, %d일 보관",
                BACKUP_HOUR, BACKUP_DIR, KEEP_DAYS)
