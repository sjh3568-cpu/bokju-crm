/**
 * 복주 상담내역 시트("상담내역 종합") → CRM 자동 반영 (Apps Script 웹앱)
 *
 * 설치: docs/CONSULT-SHEET.md 참고. 운행 시트 연동(transport_sheet.gs)과 같은 방식이다.
 *  - SHEET_ID: 시트 주소 https://docs.google.com/spreadsheets/d/<이 부분>/edit
 *  - TOKEN   : CRM .env 의 CONSULT_SHEET_TOKEN 과 같은 값 (아무 긴 문자열)
 *
 * 규칙
 *  - 읽기만 한다. 시트에 아무것도 쓰지 않는다.
 *  - 모든 탭을 그대로 돌려준다(어느 탭이 상담 내역인지는 CRM이 머리글을 보고 가린다).
 *  - 날짜 칸은 시트의 시간대 기준 'yyyy-MM-dd HH:mm:ss' 문자열로, 그 밖의 칸은 화면에 보이는 값(문자열)으로 보낸다.
 *    (숫자를 그대로 보내면 '010…' 연락처의 앞 0이 사라지고, 날짜를 그대로 보내면 UTC로 바뀌어 하루가 밀린다.)
 */
var SHEET_ID = '여기에_시트_ID';
var TOKEN = '여기에_긴_비밀문자열';

function doPost(e) {
  var out;
  try {
    var body = JSON.parse(e.postData.contents || '{}');
    if (body.token !== TOKEN) throw new Error('BAD_TOKEN');
    var ss = SpreadsheetApp.openById(SHEET_ID);
    if (body.action === 'ping') out = ping(ss);
    else if (body.action === 'dump') out = dump(ss);
    else throw new Error('UNKNOWN_ACTION');
  } catch (err) {
    out = { ok: false, error: String(err.message || err) };
  }
  return ContentService.createTextOutput(JSON.stringify(out)).setMimeType(ContentService.MimeType.JSON);
}

/** 설치 점검 — 편집기에서 이 함수를 골라 ▶실행하면 시트 접근 권한 승인 창이 뜬다(한 번만).
 *  승인 뒤 실행 로그에 시트 이름·탭 수가 찍히면 CRM 쪽 미리보기가 된다. */
function checkAccess() {
  var ss = SpreadsheetApp.openById(SHEET_ID);
  Logger.log(JSON.stringify(ping(ss)));
}

function doGet() {
  return ContentService.createTextOutput(JSON.stringify({ ok: true, hint: 'POST only' })).setMimeType(ContentService.MimeType.JSON);
}

function ping(ss) {
  var out = { ok: true, sheet: ss.getName(), tabs: ss.getSheets().map(function (s) { return s.getName(); }), url: ss.getUrl() };
  try { out.running_as = Session.getEffectiveUser().getEmail(); } catch (e) { out.running_as = '?'; }
  return out;
}

/** 모든 탭의 값 — [{name, rows:[[셀,...],...]}, ...]. 빈 탭은 rows가 []. */
function dump(ss) {
  var tz = ss.getSpreadsheetTimeZone();
  var sheets = ss.getSheets().map(function (sh) {
    var range = sh.getDataRange();
    var values = range.getValues();
    var shown = range.getDisplayValues();
    var rows = [];
    for (var r = 0; r < values.length; r++) {
      var row = [];
      for (var c = 0; c < values[r].length; c++) {
        var v = values[r][c];
        if (v instanceof Date) row.push(Utilities.formatDate(v, tz, 'yyyy-MM-dd HH:mm:ss'));
        else row.push(shown[r][c]);
      }
      rows.push(row);
    }
    return { name: sh.getName(), rows: rows };
  });
  return { ok: true, url: ss.getUrl(), sheets: sheets, at: Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd HH:mm:ss') };
}
