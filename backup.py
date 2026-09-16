"""자동 백업 — bokju.db를 backups/에 주기적으로 복사.

SQLite 온라인 백업 API(`Connection.backup`)를 쓴다. 파일 복사와 달리 상담사가
저장 중인 순간에도 일관된 스냅샷이 나오므로 앱을 멈출 필요가 없다.

동작:
  · 앱 시작 시 1회 (bokju_startup_* — 기동 직후 상태 보존, 배포 직전으로 되돌릴 때 씀)
  · 이후 매일 BACKUP_HOUR 시각 (bokju_daily_* — 기본 03시)
  · 스냅샷은 gzip으로 압축해 저장한다 (.db.gz — SQLite 파일은 보통 1/4 이하로 준다)
  · 보관 규칙(2026-09-16, 배포 1회당 68MB씩 쌓여 backups/가 1.9GB가 된 뒤 정리):
      daily    BACKUP_KEEP_DAYS 일 보관 (기본 30일) — 며칠 전 상태 복구는 이쪽이 맡는다
      startup  최근 BACKUP_KEEP_STARTUP 개만 (기본 5개) — 재기동마다 생기므로 개수로 자른다
      manual_배포전_*  예전 deploy.sh가 배포 전 cp하던 파일 — startup과 중복이라 이제 만들지 않고,
                       남은 것은 최근 BACKUP_KEEP_MANUAL 개(기본 0 = 전부)까지 지운다
      그 밖의 pre_* 같은 1회성 수동 백업은 건드리지 않는다
  · 복구: gunzip -c backups/bokju_daily_YYYYMMDD_HHMMSS.db.gz > data/bokju.db (앱 중지 상태에서)

백업 파일은 컨테이너 밖 볼륨(BACKUP_DIR)에 쌓이므로 컨테이너를 지워도 남는다.
"""
import gzip
import logging
import os
import shutil
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
KEEP_STARTUP = int(os.getenv("BACKUP_KEEP_STARTUP", "5"))
KEEP_MANUAL = int(os.getenv("BACKUP_KEEP_MANUAL", "0"))
COMPRESS = os.getenv("BACKUP_COMPRESS", "1") not in ("0", "false", "no")


def _backup_files(prefix="bokju_"):
    """prefix로 시작하는 백업(.db / .db.gz) — 최신순."""
    if not BACKUP_DIR.exists():
        return []
    files = [f for f in BACKUP_DIR.iterdir()
             if f.is_file() and f.name.startswith(prefix) and (f.name.endswith(".db") or f.name.endswith(".db.gz"))]
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def _open_snapshot(path: Path, tmp_dir: str) -> Path:
    """압축 백업이면 임시 폴더에 풀어 SQLite로 열 수 있는 경로를 돌려준다."""
    if path.name.endswith(".gz"):
        plain = Path(tmp_dir) / path.name[:-3]
        with gzip.open(path, "rb") as src, open(plain, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return plain
    return path


def verify_database(path) -> dict:
    """백업을 실제 복구 후보처럼 열어 무결성·핵심 테이블 조회를 확인한다."""
    result = {"ok": False, "path": str(path), "checked_at": datetime.now().isoformat(timespec="seconds")}
    try:
        # sqlite3의 with 블록은 트랜잭션만 닫고 연결은 열어 둔다 — 파일을 바로 압축·삭제해야 하므로 명시적으로 닫는다.
        conn = sqlite3.connect(str(path), timeout=models.BUSY_TIMEOUT)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"patients", "consultations", "users", "app_meta"}
            result.update({"integrity": check, "missing_tables": sorted(required - tables),
                           "patients": conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0],
                           "consultations": conn.execute("SELECT COUNT(*) FROM consultations").fetchone()[0]})
            result["ok"] = check == "ok" and not result["missing_tables"]
        finally:
            conn.close()
    except Exception as exc:
        result["error"] = str(exc)
    return result


def latest_status() -> dict:
    files = _backup_files()
    saved = models.get_app_meta("backup_last_status", {}) or {}
    return {**saved, "directory": str(BACKUP_DIR.resolve()), "keep_days": KEEP_DAYS,
            "file_count": len(files), "latest_file": files[0].name if files else None,
            "latest_size_mb": round(files[0].stat().st_size / 1024 / 1024, 2) if files else 0}


def verify_latest_restore() -> dict:
    """운영 DB를 덮지 않고 임시 파일로 복원 사전연습을 수행한다."""
    files = _backup_files()
    if not files:
        return {"ok": False, "error": "검증할 백업 파일이 없습니다."}
    with tempfile.TemporaryDirectory(prefix="bokju_restore_") as tmp:
        restored = Path(tmp) / "restore_test.db"
        src = sqlite3.connect(str(_open_snapshot(files[0], tmp)), timeout=models.BUSY_TIMEOUT)
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
        if COMPRESS:
            # 무결성 확인이 끝난 스냅샷만 압축한다. 압축이 실패하면 .db를 그대로 둔다.
            packed = dst.with_name(dst.name + ".gz")
            with open(dst, "rb") as src_f, gzip.open(packed, "wb", compresslevel=6) as dst_f:
                shutil.copyfileobj(src_f, dst_f)
            dst.unlink()
            dst = packed
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


def _remove(f: Path, why: str):
    try:
        f.unlink()
        logger.info("백업 삭제(%s): %s", why, f.name)
    except OSError:
        logger.exception("백업 삭제 실패: %s", f)


def _prune():
    """보관 규칙 적용 — daily는 기간(KEEP_DAYS), startup·manual_배포전은 개수로 자른다.
    daily는 최소 1개를 항상 남긴다."""
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    daily = _backup_files("bokju_daily_")
    for f in daily[1:]:  # 가장 최근 1개는 보호
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            _remove(f, f"{KEEP_DAYS}일 경과")
    for f in _backup_files("bokju_startup_")[KEEP_STARTUP:]:
        _remove(f, f"기동 백업 {KEEP_STARTUP}개 초과")
    for f in _backup_files("manual_배포전_")[KEEP_MANUAL:]:
        _remove(f, f"배포 전 백업 {KEEP_MANUAL}개 초과")


def prune_now() -> dict:
    """보관 규칙을 즉시 적용하고 남은 파일 수·용량을 돌려준다 (점검 화면·수동 정리용)."""
    _prune()
    files = _backup_files() + _backup_files("manual_배포전_")
    return {"file_count": len(files),
            "total_mb": round(sum(f.stat().st_size for f in files) / 1024 / 1024, 1)}


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
    logger.info("백업 스케줄러 시작 — 매일 %02d시, %s, daily %d일 · startup %d개 · 배포전 %d개 보관, 압축 %s",
                BACKUP_HOUR, BACKUP_DIR, KEEP_DAYS, KEEP_STARTUP, KEEP_MANUAL, "on" if COMPRESS else "off")
