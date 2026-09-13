/**
 * 복주 운행 공유 시트 ↔ CRM 연동 (Apps Script 웹앱)
 *
 * 설치: docs/TRANSPORT-SHEET.md 참고.
 *  - SHEET_ID: 시트 주소 https://docs.google.com/spreadsheets/d/<이 부분>/edit
 *  - TOKEN   : CRM .env 의 TRANSPORT_SHEET_TOKEN 과 같은 값 (아무 긴 문자열)
 *
 * 규칙
 *  - 탭은 절대 만들지 않는다. 날짜 탭이 없으면 NO_TAB 을 돌려주고 CRM이 기다린다.
 *  - 탭 이름 "2026.9.14" / "2026.09.14" 둘 다 같은 날짜로 본다.
 *  - 행은 데이터 마지막 줄 아래에만 추가한다. B~I 열만 쓰고 J~M(운행팀 칸)은 손대지 않는다.
 */
var SHEET_ID = '여기에_시트_ID';
var TOKEN = '여기에_긴_비밀문자열';

var HEADER_ROWS = 4;          // 1~4행이 제목/머리글, 5행부터 데이터
var COL = { NO: 1, DEPT: 2, REQUESTER: 3, NAME: 4, MOBILITY: 5, REASON: 6, PLACE: 7, ARRIVE: 8, CONTACT: 9, DRIVER: 10, VEHICLE: 11 };

function doPost(e) {
  var out;
  try {
    var body = JSON.parse(e.postData.contents || '{}');
    if (body.token !== TOKEN) throw new Error('BAD_TOKEN');
    var ss = SpreadsheetApp.openById(SHEET_ID);
    if (body.action === 'ping') out = ping(ss, body.date);
    else if (body.action === 'upsert') out = upsert(ss, body);
    else if (body.action === 'read') out = readRows(ss, body.date);
    else if (body.action === 'dump') out = dump(ss, body.date);
    else throw new Error('UNKNOWN_ACTION');
  } catch (err) {
    out = { ok: false, error: String(err.message || err) };
  }
  return ContentService.createTextOutput(JSON.stringify(out)).setMimeType(ContentService.MimeType.JSON);
}

function doGet() {
  return ContentService.createTextOutput(JSON.stringify({ ok: true, hint: 'POST only' })).setMimeType(ContentService.MimeType.JSON);
}

/** '2026-09-14' → 탭 '2026.9.14' 또는 '2026.09.14' */
function findTab(ss, isoDate) {
  var p = String(isoDate).split('-').map(Number);          // [2026, 9, 14]
  var sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    var m = sheets[i].getName().trim().match(/^(\d{4})[.\-\/](\d{1,2})[.\-\/](\d{1,2})$/);
    if (m && Number(m[1]) === p[0] && Number(m[2]) === p[1] && Number(m[3]) === p[2]) return sheets[i];
  }
  return null;
}

function ping(ss, isoDate) {
  var tab = isoDate ? findTab(ss, isoDate) : null;
  var out = { ok: true, sheet: ss.getName(), tabs: ss.getSheets().length, tab_for_date: tab ? tab.getName() : null };
  try { out.running_as = Session.getEffectiveUser().getEmail(); } catch (e) { out.running_as = '?'; }
  try { out.editors = ss.getEditors().map(function (u) { return u.getEmail(); }); } catch (e) { out.editors = 'getEditors 실패: ' + e.message; }
  try { out.owner = ss.getOwner() ? ss.getOwner().getEmail() : null; } catch (e) { out.owner = '?'; }
  if (tab) {
    out.tab_last_row = tab.getLastRow(); out.tab_max_rows = tab.getMaxRows(); out.tab_last_data_row = lastDataRow(tab);
    try { out.tab_protections = tab.getProtections(SpreadsheetApp.ProtectionType.SHEET).length + tab.getProtections(SpreadsheetApp.ProtectionType.RANGE).length; } catch (e) { out.tab_protections = '?'; }
  }
  return out;
}

/** 진단 — 그 날짜 탭의 데이터 끝부분(A~M) 원본 값·병합·확인규칙을 그대로 돌려준다 */
function dump(ss, isoDate) {
  var tab = findTab(ss, isoDate);
  if (!tab) return { ok: false, error: 'NO_TAB' };
  var lr = lastDataRow(tab), from = Math.max(HEADER_ROWS + 1, lr - 3), to = Math.min(tab.getMaxRows(), lr + 3);
  var rng = tab.getRange(from, 1, to - from + 1, 13);
  var merged = rng.getMergedRanges().map(function (r) { return r.getA1Notation(); });
  var dv = [];
  ['B', 'E', 'F'].forEach(function (col) {
    var rule = tab.getRange(col + (lr + 1)).getDataValidation();
    dv.push({ cell: col + (lr + 1), rule: rule ? rule.getCriteriaType() + ' allowInvalid=' + rule.getAllowInvalid() + ' ' + JSON.stringify(rule.getCriteriaValues()).slice(0, 200) : null });
  });
  return { ok: true, tab: tab.getName(), from_row: from, values: rng.getValues(), merged: merged, validation: dv,
           last_row: tab.getLastRow(), last_data_row: lr, max_rows: tab.getMaxRows() };
}

/** 데이터가 있는 마지막 행 (이름 D열 기준) */
function lastDataRow(sh) {
  var last = sh.getLastRow();
  if (last <= HEADER_ROWS) return HEADER_ROWS;
  var names = sh.getRange(HEADER_ROWS + 1, COL.NAME, last - HEADER_ROWS, 1).getValues();
  for (var i = names.length - 1; i >= 0; i--) {
    if (String(names[i][0]).trim() !== '') return HEADER_ROWS + 1 + i;
  }
  return HEADER_ROWS;
}

/**
 * body.row = [요청부서, 요청자, 이름, 이동수단, 요청이유, 요청 장소, 도착시간, 연락처]  (B~I)
 * body.match_row 가 있고 그 행의 이름이 같으면 그 행을 덮어쓴다(수정). 아니면 새 행 추가.
 */
function upsert(ss, body) {
  var tab = findTab(ss, body.date);
  if (!tab) return { ok: false, error: 'NO_TAB' };
  var row = body.row;
  var target = null;
  if (body.match_row) {
    var nm = String(tab.getRange(body.match_row, COL.NAME).getValue()).trim();
    if (nm && nm === String(row[2]).trim()) target = body.match_row;
  }
  if (!target) {
    // 같은 이름의 진료협력 행이 이미 있으면 그 행을 쓴다 (중복 방지)
    var lr = lastDataRow(tab);
    if (lr > HEADER_ROWS) {
      var vals = tab.getRange(HEADER_ROWS + 1, COL.DEPT, lr - HEADER_ROWS, 3).getValues(); // B,C,D
      for (var i = 0; i < vals.length; i++) {
        if (String(vals[i][0]).trim() === String(row[0]).trim() && String(vals[i][2]).trim() === String(row[2]).trim()) {
          target = HEADER_ROWS + 1 + i; break;
        }
      }
    }
  }
  var inserted = false;
  if (!target) {
    var lr2 = lastDataRow(tab);
    target = lr2 + 1;
    // 바로 아래 줄이 병합 셀(메모 블록 등)에 걸려 있으면 값이 조용히 사라진다 → 한 줄 끼워 넣는다
    if (target > tab.getMaxRows() || tab.getRange(target, 1, 1, COL.CONTACT).getMergedRanges().length > 0) {
      tab.insertRowsAfter(lr2, 1); inserted = true;
    }
    var prevNo = target > HEADER_ROWS + 1 ? Number(tab.getRange(target - 1, COL.NO).getValue()) : 0;
    tab.getRange(target, COL.NO).setValue(isNaN(prevNo) ? '' : prevNo + 1);
  }
  tab.getRange(target, COL.DEPT, 1, row.length).setValues([row]);
  SpreadsheetApp.flush();
  // 실제로 남았는지 확인 — 편집 권한이 없거나 셀이 잠겨 있으면 여기서 걸린다
  var check = String(tab.getRange(target, COL.NAME).getValue()).trim();
  if (check !== String(row[2]).trim()) {
    return { ok: false, error: 'WRITE_NOT_PERSISTED', row: target, got: check,
             hint: '값이 저장되지 않음 — 실행 계정의 편집 권한 또는 시트 보호를 확인' };
  }
  return { ok: true, tab: tab.getName(), row: target, inserted: inserted };
}

/** 그 날짜 탭의 진료협력 행 — 배정자·차량을 CRM이 읽어간다 */
function readRows(ss, isoDate) {
  var tab = findTab(ss, isoDate);
  if (!tab) return { ok: false, error: 'NO_TAB' };
  var lr = lastDataRow(tab);
  var rows = [];
  if (lr > HEADER_ROWS) {
    var vals = tab.getRange(HEADER_ROWS + 1, COL.DEPT, lr - HEADER_ROWS, COL.VEHICLE - COL.DEPT + 1).getValues();
    for (var i = 0; i < vals.length; i++) {
      var v = vals[i];
      if (String(v[0]).trim() !== '진료협력') continue;
      rows.push({ row: HEADER_ROWS + 1 + i, name: String(v[COL.NAME - COL.DEPT]).trim(),
                  driver: String(v[COL.DRIVER - COL.DEPT]).trim(), vehicle: String(v[COL.VEHICLE - COL.DEPT]).trim() });
    }
  }
  return { ok: true, tab: tab.getName(), rows: rows };
}
