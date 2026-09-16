"""팩스·문서 자료함 — NAS 폴더 감시 → AI 판독 → '날짜_환자이름_주병명' 파일명 정리 → 인박스 카드.

흐름 (docs/FAX-NAS-PLAN.md):
  복합기 → 수신 담당 PC(Easy Printer Manager, PDF 저장) → NAS 공유폴더(FAX_INBOX_DIR)
  → 이 워커가 새 파일을 감지 → Claude가 환자 이름·주병명·보낸 곳·핵심 요약을 읽음
  → FAX_ARCHIVE_DIR/YYYY-MM-DD_이름_주병명.pdf 로 이동 → patient_documents + communications(팩스) 등록
  → 대시보드 미처리 인바운드 카드 · /documents 자료함에서 원본 보기·요약 확인·환자 연결.

원칙:
- 파일은 삭제하지 않는다. 이동(정리)만 하고, 이동이 안 되면(권한·다른 볼륨) 원래 자리에 둔다.
- AI가 못 읽어도 등록은 된다(status='pending', 파일명은 '날짜_미확인_원본명'). 3회까지 다음 주기에 재시도.
- 환자 연결·상담 등록은 직원 확인 후(AI 결과는 초안).
- DB는 NAS 로컬 볼륨이어야 하지만(WAL), 팩스 PDF는 SMB 공유폴더에서 읽기만 하므로 문제없다.
"""
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import date, datetime
from pathlib import Path

import models

logger = logging.getLogger(__name__)

SUPPORTED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".webp"}
AI_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp"}      # Claude가 직접 읽는 형식(tif 제외)
MIME = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".tif": "image/tiff", ".tiff": "image/tiff", ".gif": "image/gif", ".webp": "image/webp"}
SETTLE_SECONDS = 15          # 마지막 수정 후 이 시간이 지나야 '다 쓰인 파일'로 본다(복사 중 파일 방지)
MAX_AI_ATTEMPTS = 3
SOURCE = "팩스"
CHANNEL = "팩스"
_lock = threading.Lock()


# ───────────────────── 설정 ─────────────────────

def inbox_dir() -> Path | None:
    v = (os.getenv("FAX_INBOX_DIR") or "").strip()
    return Path(v) if v else None


def archive_dir() -> Path | None:
    v = (os.getenv("FAX_ARCHIVE_DIR") or "").strip()
    if v:
        return Path(v)
    base = inbox_dir()
    return (base / "정리") if base else None


def enabled() -> bool:
    return os.getenv("FAX_ENABLED", "1") == "1" and inbox_dir() is not None


def poll_seconds() -> int:
    try:
        return max(15, int(os.getenv("FAX_POLL_SECONDS", "60")))
    except ValueError:
        return 60


def keep_days() -> int:
    """원본 보관 일수. 0이면 자동 삭제 안 함."""
    try:
        return max(0, int(os.getenv("FAX_KEEP_DAYS", "10")))
    except ValueError:
        return 10


def retention_hour() -> int:
    try:
        return min(23, max(0, int(os.getenv("FAX_RETENTION_HOUR", "4"))))   # 백업(03시) 다음
    except ValueError:
        return 4


def status() -> dict:
    """자료함 화면 상단 상태 표시용."""
    import llm
    d = inbox_dir(); a = archive_dir()
    store = models.documents_storage()
    return {
        "enabled": enabled(),
        "inbox_dir": str(d) if d else "",
        "inbox_exists": bool(d and d.is_dir()),
        "archive_dir": str(a) if a else "",
        "ai": llm.fax_ai_enabled(),
        "model": os.getenv("CLAUDE_MODEL_FAX", llm.FAX_MODEL),
        "max_pages": llm.fax_max_pages(),
        "poll_seconds": poll_seconds(),
        "keep_days": keep_days(),
        "files": store["files"],
        "mb": round(store["bytes"] / (1024 * 1024), 1),
    }


# ───────────────────── 파일명 ─────────────────────

_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def clean_part(text: str, limit: int) -> str:
    """파일명 조각 — 금지문자·공백 정리, 길이 제한."""
    t = _BAD.sub(" ", (text or "")).strip()
    t = re.sub(r"\s+", " ", t).strip(" ._")
    return t[:limit].strip(" ._")


def build_filename(doc_date: str, patient_name: str, diagnosis: str, ext: str, fallback: str = "") -> str:
    """'YYYY-MM-DD_이름_주병명.pdf'. 이름·병명을 못 읽으면 '미확인'과 원본명으로 채운다."""
    d = (doc_date or "").strip()[:10]
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        d = date.today().isoformat()
    name = clean_part(patient_name, 20) or "미확인"
    dx = clean_part(diagnosis, 30)
    if not dx:
        dx = clean_part(Path(fallback).stem, 30) if fallback else "미분류"
    return f"{d}_{name}_{dx}{ext.lower()}"


def _unique_path(folder: Path, filename: str) -> Path:
    target = folder / filename
    if not target.exists():
        return target
    stem, ext = os.path.splitext(filename)
    for n in range(2, 100):
        cand = folder / f"{stem} ({n}){ext}"
        if not cand.exists():
            return cand
    return folder / f"{stem} ({int(time.time())}){ext}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ───────────────────── 판독 ─────────────────────

def analyze_file(path: str, hint: str = "") -> dict:
    """AI 판독 — 테스트에서 patch 지점. 반환은 llm.FAX_SCHEMA dict."""
    import llm
    return llm.analyze_document(path, hint=hint)


def _summary_text(ai: dict) -> str:
    """카드·타임라인용 본문(평문). 화면은 ai_json으로 구조화해 그린다."""
    lines = []
    head = " · ".join(x for x in (ai.get("document_type"), ai.get("sender_hospital"),
                                  ai.get("sender_department")) if x)
    if head:
        lines.append(f"[{head}]")
    if ai.get("referral_reason"):
        lines.append(f"의뢰 사유: {ai['referral_reason']}")
    for s in ai.get("summary") or []:
        lines.append(f"• {s}")
    if ai.get("precautions"):
        lines.append("주의: " + ", ".join(ai["precautions"]))
    if ai.get("notes"):
        lines.append(f"※ {ai['notes']}")
    return "\n".join(lines)


def _comm_summary(ai: dict | None, doc: dict) -> str:
    name = (ai or {}).get("patient_name") or doc.get("patient_name_ai") or "미확인"
    dx = (ai or {}).get("main_diagnosis") or doc.get("diagnosis_ai") or ""
    sender = (ai or {}).get("sender_hospital") or doc.get("sender_ai") or ""
    parts = [f"팩스 · {name}"]
    if dx:
        parts.append(dx)
    if sender:
        parts.append(f"({sender})")
    return " ".join(parts)[:200]


def _apply_analysis(doc_id: int, ai: dict, *, rename: bool = True) -> dict:
    """판독 결과를 문서 행에 반영하고 파일명을 정리한다. 반환: 갱신된 doc."""
    doc = models.get_document(doc_id)
    fields = {
        "patient_name_ai": (ai.get("patient_name") or "").strip()[:40] or None,
        "diagnosis_ai": (ai.get("main_diagnosis") or "").strip()[:60] or None,
        "sender_ai": (ai.get("sender_hospital") or "").strip()[:80] or None,
        "ai_json": json.dumps(ai, ensure_ascii=False),
        "ai_summary": _summary_text(ai),
        "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ai_error": None,
        "status": "analyzed",
    }
    if ai.get("_pages_total"):
        fields["pages"] = int(ai["_pages_total"])
    d = (ai.get("doc_date") or "").strip()[:10]
    try:
        datetime.strptime(d, "%Y-%m-%d")
        fields["doc_date"] = d
    except ValueError:
        pass
    models.update_document(doc_id, **fields)
    if rename:
        try:
            move_to_archive(doc_id)
        except Exception:
            logger.exception("팩스 파일 정리(이동) 실패 — 원래 자리에 둡니다 (doc #%s)", doc_id)
    doc = models.get_document(doc_id)
    if doc.get("comm_id"):
        models.update_communication(doc["comm_id"], summary=_comm_summary(ai, doc),
                                    body=fields["ai_summary"] or None,
                                    contact=(ai.get("sender_contact") or ai.get("sender_hospital") or None))
    return doc


def move_to_archive(doc_id: int) -> str | None:
    """현재 판독값(날짜·이름·병명)으로 파일명을 만들어 정리 폴더로 옮긴다. 반환: 새 경로."""
    doc = models.get_document(doc_id)
    src = Path(doc.get("stored_path") or "")
    if doc.get("file_deleted_at") or not src.is_file():
        return None
    folder = archive_dir() or src.parent
    folder.mkdir(parents=True, exist_ok=True)
    fname = build_filename(doc.get("doc_date") or "", doc.get("patient_name_ai") or "",
                           doc.get("diagnosis_ai") or "", src.suffix, fallback=doc.get("original_name") or src.name)
    if src.parent == folder and src.name == fname:
        return str(src)
    target = _unique_path(folder, fname)
    shutil.move(str(src), str(target))
    models.update_document(doc_id, filename=target.name, stored_path=str(target))
    logger.info("팩스 정리: %s → %s", src.name, target.name)
    return str(target)


# ───────────────────── 등록 ─────────────────────

def register_file(path: Path, *, created_by: str = "팩스 자동", analyze: bool = True) -> int | None:
    """새 파일 1개 → 문서 행 + 인박스 카드(+AI 판독). 이미 등록된 파일(해시 동일)이면 None."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXT or not path.is_file():
        return None
    sha = _sha256(path)
    if models.document_by_sha(sha):
        logger.info("팩스 중복 건너뜀: %s", path.name)
        return None
    st = path.stat()
    doc_date = datetime.fromtimestamp(st.st_mtime).date().isoformat()
    doc_id = models.create_document(
        filename=path.name, stored_path=str(path), mime=MIME.get(ext), source=SOURCE,
        status="pending", created_by=created_by)
    models.update_document(doc_id, sha256=sha, original_name=path.name, doc_date=doc_date,
                           size_bytes=st.st_size)
    comm_id = models.create_communication(
        channel=CHANNEL, direction="in", summary=f"팩스 · {path.name}",
        body="판독 대기 중 — 자료함에서 원본을 확인하세요.",
        occurred_at=datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        created_by=created_by)
    models.update_document(doc_id, comm_id=comm_id)
    logger.info("팩스 등록 #%s: %s", doc_id, path.name)
    if analyze:
        analyze_document(doc_id)
    else:
        # AI 없이도 파일명은 날짜_미확인_원본명으로 정리해 둔다
        try:
            move_to_archive(doc_id)
        except Exception:
            logger.exception("팩스 파일 정리 실패 (doc #%s)", doc_id)
    return doc_id


def analyze_document(doc_id: int, *, force: bool = False) -> bool:
    """문서 1개 AI 판독. 성공 True. 실패는 ai_error에 남기고 False(재시도는 워커가)."""
    import llm
    doc = models.get_document(doc_id)
    if not doc:
        return False
    path = Path(doc.get("stored_path") or "")
    if not llm.fax_ai_enabled():
        if not doc.get("analyzed_at"):
            models.update_document(doc_id, ai_error="AI 판독 비활성(ANTHROPIC_API_KEY 또는 FAX_AI_ENABLED)")
            _ensure_archived(doc_id)
        return False
    if path.suffix.lower() not in AI_EXT:
        models.update_document(doc_id, ai_error=f"{path.suffix} 형식은 자동 판독 불가 — PDF로 저장되게 설정하세요",
                               ai_attempts=MAX_AI_ATTEMPTS)
        _ensure_archived(doc_id)
        return False
    if not force and (doc.get("ai_attempts") or 0) >= MAX_AI_ATTEMPTS:
        return False
    models.update_document(doc_id, ai_attempts=(doc.get("ai_attempts") or 0) + 1)
    try:
        ai = analyze_file(str(path), hint=doc.get("original_name") or "")
    except Exception as e:
        logger.warning("팩스 AI 판독 실패 (doc #%s, %s): %s", doc_id, path.name, e)
        models.update_document(doc_id, ai_error=str(e)[:300])
        if (doc.get("ai_attempts") or 0) + 1 >= MAX_AI_ATTEMPTS:
            _ensure_archived(doc_id)
        return False
    _apply_analysis(doc_id, ai)
    return True


def _ensure_archived(doc_id: int):
    """AI 없이 끝난 문서도 정리 폴더로 옮겨 수신 폴더가 비도록 한다."""
    try:
        move_to_archive(doc_id)
    except Exception:
        logger.exception("팩스 파일 정리 실패 (doc #%s)", doc_id)


def save_upload(filename: str, data: bytes, *, created_by: str) -> int | None:
    """화면에서 직접 올린 파일 — 수신 폴더(없으면 아카이브 폴더)에 저장 후 같은 흐름으로 등록."""
    folder = inbox_dir() or archive_dir()
    if folder is None:
        raise RuntimeError("FAX_INBOX_DIR이 설정되지 않아 저장할 곳이 없습니다.")
    folder.mkdir(parents=True, exist_ok=True)
    safe = clean_part(Path(filename).stem, 60) or "upload"
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise RuntimeError("PDF·JPG·PNG·TIF 파일만 올릴 수 있습니다.")
    target = _unique_path(folder, f"{safe}{ext}")
    target.write_bytes(data)
    return register_file(target, created_by=created_by)


# ───────────────────── 보관기간 — 원본 자동 삭제 ─────────────────────
# 팩스는 모병원 의무기록 사본(의뢰서·판독지·검사지·투약기록)이라 장수가 많고 NAS를 빠르게 채운다.
# 상담 참고용이므로 FAX_KEEP_DAYS(사용자 결정 2026-09-17: 10일)가 지나면 원본 파일만 지우고,
# 판독값·AI 요약·연결(환자·상담)은 DB에 남긴다. 보통 10~20장, 많으면 책 두께로도 온다.
# 정식 보존은 원본을 보낸 모병원과 우리 병원 EMR/의무기록실 몫이다 — 자료함이 유일한 사본이 되면 안 된다.

def _managed(path: Path) -> bool:
    """수신·정리 폴더 안의 파일만 지운다 — 다른 곳을 가리키는 경로는 절대 건드리지 않는다."""
    for base in (inbox_dir(), archive_dir()):
        if base and (base == path.parent or base in path.parents):
            return True
    return False


def purge_expired(today: date | None = None) -> int:
    """보관기간이 지난 원본 삭제. 삭제 건수 반환. FAX_KEEP_DAYS=0이면 아무것도 안 한다."""
    days = keep_days()
    if not days:
        return 0
    from datetime import timedelta
    cutoff = ((today or date.today()) - timedelta(days=days)).isoformat()
    count = 0
    for doc in models.documents_expired(cutoff):
        path = Path(doc.get("stored_path") or "")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if path.is_file():
            if not _managed(path):
                logger.warning("보관기간 경과 문서 #%s 경로가 팩스 폴더 밖이라 건너뜀: %s", doc["id"], path)
                continue
            try:
                path.unlink()
            except OSError as e:
                logger.warning("팩스 원본 삭제 실패 (doc #%s): %s", doc["id"], e)
                continue
        models.update_document(doc["id"], file_deleted_at=now)
        count += 1
    if count:
        logger.info("팩스 원본 %d건 삭제 — 보관 %d일 경과(%s 이전). 요약·판독값은 유지", count, days, cutoff)
    return count


_last_purge_date: str | None = None


def _maybe_purge():
    """하루 한 번, FAX_RETENTION_HOUR 이후 첫 폴링에서 실행."""
    global _last_purge_date
    now = datetime.now()
    if now.hour < retention_hour() or _last_purge_date == now.date().isoformat():
        return
    _last_purge_date = now.date().isoformat()
    try:
        purge_expired(now.date())
    except Exception:
        logger.exception("팩스 보관기간 정리 실패")


# ───────────────────── 폴더 감시 ─────────────────────

def _candidates(folder: Path):
    """수신 폴더의 새 파일 후보 — 정리 폴더·숨김·쓰는 중인 파일 제외."""
    arch = archive_dir()
    now = time.time()
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.name.startswith((".", "~$")):
            continue
        if p.suffix.lower() not in SUPPORTED_EXT:
            continue
        if arch and (arch == p.parent or arch in p.parents):
            continue
        try:
            if now - p.stat().st_mtime < SETTLE_SECONDS:
                continue                      # 아직 복사 중일 수 있음
        except OSError:
            continue
        yield p


def scan_once() -> int:
    """수신 폴더 1회 확인 + 판독 실패분 재시도. 등록 건수 반환."""
    if not _lock.acquire(blocking=False):
        return 0                              # 이전 스캔이 아직 진행 중
    try:
        folder = inbox_dir()
        if not folder:
            return 0
        if not folder.is_dir():
            logger.warning("팩스 수신 폴더가 없습니다: %s", folder)
            return 0
        count = 0
        fresh: set[int] = set()                   # 이번 주기에 막 등록한 건 — 같은 주기에 재시도하지 않는다
        for p in list(_candidates(folder)):
            try:
                did = register_file(p)
                if did is not None:
                    count += 1
                    fresh.add(did)
            except Exception:
                logger.exception("팩스 파일 등록 실패: %s", p)
        for doc in models.documents_retry_candidates(MAX_AI_ATTEMPTS):
            if doc["id"] in fresh:
                continue
            try:
                analyze_document(doc["id"])
            except Exception:
                logger.exception("팩스 재판독 실패 (doc #%s)", doc["id"])
        if count:
            logger.info("팩스 새 파일 %d건 등록", count)
        return count
    finally:
        _lock.release()


def _loop():
    while True:
        time.sleep(poll_seconds())
        try:
            scan_once()
        except Exception:
            logger.exception("팩스 폴더 감시 예외")
        _maybe_purge()


def start_worker():
    if not enabled():
        logger.info("팩스 자료함 비활성 — FAX_INBOX_DIR 미설정")
        return
    logger.info("팩스 자료함 시작 — %s 를 %d초마다 확인, 원본 보관 %d일(0=무제한), AI 판독 앞 %d쪽",
                inbox_dir(), poll_seconds(), keep_days(), __import__("llm").fax_max_pages())
    threading.Thread(target=_bootstrap, name="fax-inbox", daemon=True).start()


def _bootstrap():
    try:
        scan_once()
    except Exception:
        logger.exception("팩스 최초 스캔 실패")
    _loop()
