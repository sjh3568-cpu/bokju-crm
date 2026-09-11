// 문자 전송 — 환자군 템플릿 선택 + 토큰 치환 + 발송 (5번 요청)
(() => {
    const consultSel = document.getElementById('sms-consult');
    if (!consultSel) return;
    const phoneEl = document.getElementById('sms-phone');
    const nameEl = document.getElementById('sms-name');
    const bodyEl = document.getElementById('sms-body');
    const countEl = document.getElementById('sms-count');
    const msgEl = document.querySelector('.sms-msg');
    const CONSULTS = window.SMS_CONSULTS || {};

    let rawTemplate = '';  // 마지막 선택한 템플릿 원본(토큰 미치환)

    function currentConsult() {
        return CONSULTS[consultSel.value] || null;
    }
    function fillTokens(text) {
        const c = currentConsult();
        const map = {
            '{환자명}': c ? c.name : '',
            '{보호자명}': c ? c.guardian : '',
            '{병원명}': '복주회복병원',
            '{입원예정일}': c ? c.planned : '',
            '{주치의}': c ? c.doctor : '',
        };
        let out = text;
        for (const [k, v] of Object.entries(map)) {
            out = out.split(k).join(v || k);  // 값 없으면 토큰 그대로 둠
        }
        return out;
    }
    // 발송사 기준(EUC-KR) 바이트 근사 — 한글·기호 2, 영숫자 1. 정확한 판정은 서버가 한다.
    const SMS_MAX = Number(countEl.dataset.smsMax) || 90;
    const LMS_MAX = Number(countEl.dataset.lmsMax) || 2000;
    function bodyBytes(text) {
        let n = 0;
        for (const ch of text) n += ch.charCodeAt(0) > 127 ? 2 : 1;
        return n;
    }
    function updateCount() {
        const text = bodyEl.value;
        const b = bodyBytes(text);
        let kind, cls = '';
        if (b <= SMS_MAX) { kind = '단문(SMS)'; }
        else if (b <= LMS_MAX) { kind = '장문(LMS)'; cls = 'is-lms'; }
        else { kind = '한도 초과 — ' + LMS_MAX + '바이트까지'; cls = 'is-over'; }
        countEl.textContent = text.length + '자 · ' + b + '/' + (b <= SMS_MAX ? SMS_MAX : LMS_MAX) + '바이트 · ' + kind;
        countEl.className = 'muted ' + cls;
        return b;
    }
    function applyConsult() {
        const c = currentConsult();
        if (c) {
            phoneEl.value = c.phone || '';
            nameEl.value = c.guardian || '';
        }
        if (rawTemplate) { bodyEl.value = fillTokens(rawTemplate); }
        updateCount();
    }

    consultSel.addEventListener('change', applyConsult);
    bodyEl.addEventListener('input', () => { rawTemplate = ''; updateCount(); });

    document.querySelectorAll('.sms-tpl').forEach(btn => {
        btn.addEventListener('click', () => {
            rawTemplate = btn.dataset.body || '';
            bodyEl.value = fillTokens(rawTemplate);
            updateCount();
        });
    });

    const copyBtn = document.getElementById('sms-copy');
    if (copyBtn) {
        copyBtn.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(bodyEl.value);
                toast('문자 내용을 복사했습니다.', 'info');
            } catch {
                toast('복사 실패 — 본문을 직접 선택해 복사하세요.', 'error');
            }
        });
    }

    document.getElementById('sms-send').addEventListener('click', async () => {
        const phone = phoneEl.value.trim();
        const body = bodyEl.value.trim();
        if (!phone) { msgEl.className = 'sms-msg err'; msgEl.textContent = '수신 번호를 입력하세요.'; return; }
        if (!body) { msgEl.className = 'sms-msg err'; msgEl.textContent = '문자 내용을 입력하세요.'; return; }
        if (bodyBytes(body) > LMS_MAX) { msgEl.className = 'sms-msg err'; msgEl.textContent = '본문이 장문(LMS) 한도를 넘습니다. 내용을 줄이세요.'; return; }
        const c = currentConsult();
        const payload = {
            to_phone: phone, body: body, to_name: nameEl.value.trim(),
            consultation_id: consultSel.value ? Number(consultSel.value) : null,
            patient_id: c ? c.pid : null,
        };
        msgEl.className = 'sms-msg muted'; msgEl.textContent = '전송 처리 중...';
        try {
            const res = await api.post('/api/sms/send', payload);
            if (res.status === 'sent') {
                msgEl.className = 'sms-msg ok'; msgEl.textContent = '문자를 발송했습니다. (' + (res.msg_type || '') + ')';
                setTimeout(() => location.reload(), 900);
            } else if (res.status === 'test') {
                msgEl.className = 'sms-msg ok';
                msgEl.textContent = '테스트 발송 — ' + (res.sent_to || '테스트 번호') + '로 보냈습니다. 보호자에게는 가지 않았습니다.';
                setTimeout(() => location.reload(), 1500);
            } else if (res.status === 'failed') {
                msgEl.className = 'sms-msg err';
                msgEl.textContent = '발송 실패: ' + (res.error || '') + ' (이력은 기록됨)';
            } else {
                // manual — 발송사 미설정. 휴대폰 문자앱을 내용 채운 채로 연다.
                msgEl.className = 'sms-msg ok';
                msgEl.textContent = '발송 이력 기록됨 — 휴대폰 문자앱을 엽니다.';
                const digits = phone.replace(/[^0-9]/g, '');
                window.location.href = 'sms:' + digits + '?body=' + encodeURIComponent(body);
                setTimeout(() => location.reload(), 2500);
            }
        } catch (e) {
            msgEl.className = 'sms-msg err'; msgEl.textContent = '실패: ' + e.message;
        }
    });

    if (consultSel.value) applyConsult();
    updateCount();
})();
