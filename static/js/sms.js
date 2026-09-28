// 문자 전송 — 환자군 템플릿 선택 + 토큰 치환 + 발송 (5번 요청)
(() => {
    const searchEl = document.getElementById('sms-search');
    if (!searchEl) return;
    const phoneEl = document.getElementById('sms-phone');
    const nameEl = document.getElementById('sms-name');
    const bodyEl = document.getElementById('sms-body');
    const countEl = document.getElementById('sms-count');
    const msgEl = document.querySelector('.sms-msg');
    const listEl = document.getElementById('sms-search-list');
    const pickedEl = document.getElementById('sms-picked');
    let current = window.SMS_PRESELECT || null;   // /api/sms/recipient 모양 — 선택한 수신자

    let rawTemplate = '';  // 마지막 선택한 템플릿 원본(토큰 미치환)

    function fillTokens(text) {
        const c = current;
        const map = {
            '{환자명}': c ? c.patient_name : '',
            '{보호자명}': c ? c.guardian_name : '',
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
        renderPreview();
        return b;
    }
    // ─── 받는 사람 휴대폰 미리보기 ───
    // 서버(sms.send_sms)가 실제로 보내는 모양을 그대로 흉내 낸다:
    //  발송사 연동(live)  → [Web발신] + 본문, 장문이면 제목(DEFAULT_LMS_TITLE)이 위에 굵게
    //  테스트(test)       → 위와 같되 본문 머리에 [테스트 → 원래 번호], 실제로는 테스트 번호로 감
    //  미설정(manual)     → 직원 휴대폰 문자앱으로 나가므로 머리말·제목 없이 본문만
    const device = document.getElementById('sms-device');
    const fmtPhone = v => {
        const d = String(v || '').replace(/\D/g, '');
        if (/^02\d{7,8}$/.test(d)) return d.replace(/^(02)(\d{3,4})(\d{4})$/, '$1-$2-$3');
        if (/^\d{8}$/.test(d)) return d.replace(/^(\d{4})(\d{4})$/, '$1-$2');
        if (/^\d{10,11}$/.test(d)) return d.replace(/^(\d{3})(\d{3,4})(\d{4})$/, '$1-$2-$3');
        return d;
    };
    function renderPreview() {
        if (!device) return;
        const mode = device.dataset.mode;
        let tokens = [];
        try { tokens = JSON.parse(device.dataset.tokens || '[]'); } catch { tokens = []; }
        const bubble = device.querySelector('.sms-device-bubble');
        const note = device.querySelector('.sms-device-note');
        const receiver = phoneEl.value.replace(/\D/g, '');
        let body = bodyEl.value;
        if (mode === 'test' && body && receiver && receiver !== device.dataset.testTo) body = '[테스트 → ' + receiver + ']\n' + body;
        const bytes = bodyBytes(body);
        const isLms = bytes > SMS_MAX;

        device.querySelector('.sms-device-from').textContent =
            mode === 'manual' ? '상담사 휴대폰 번호' : (fmtPhone(device.dataset.sender) || '발신번호');
        const now = new Date(), h = now.getHours();
        device.querySelector('.sms-device-time').textContent =
            '오늘 ' + (h < 12 ? '오전 ' : '오후 ') + ((h % 12) || 12) + ':' + String(now.getMinutes()).padStart(2, '0');

        bubble.replaceChildren();
        bubble.classList.toggle('is-empty', !body.trim());
        if (!body.trim()) {
            bubble.textContent = '입력한 내용이 여기에 받는 사람 화면처럼 보입니다.';
        } else {
            if (mode !== 'manual' && isLms) {
                const t = document.createElement('span'); t.className = 'sms-device-title';
                t.textContent = device.dataset.title || ''; bubble.append(t);
            }
            if (mode !== 'manual') {
                const w = document.createElement('span'); w.className = 'sms-device-web';
                w.textContent = '[Web발신]\n'; bubble.append(w);
            }
            // 치환 안 된 토큰은 글자 그대로 보호자에게 간다 — 노랗게 표시
            const pattern = tokens.length ? new RegExp('(' + tokens.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')') : null;
            (pattern ? body.split(pattern) : [body]).forEach(part => {
                if (!part) return;
                if (pattern && tokens.includes(part)) {
                    const m = document.createElement('mark'); m.className = 'sms-device-token'; m.textContent = part; bubble.append(m);
                } else bubble.append(document.createTextNode(part));
            });
        }
        device.querySelector('.sms-device-kind').textContent =
            !body.trim() ? '' : (bytes > LMS_MAX ? '한도 초과 — 발송 안 됨' : (isLms ? '장문(LMS)' : '단문(SMS)'));

        const left = tokens.filter(t => body.includes(t));
        note.replaceChildren();
        if (left.length) {
            const w = document.createElement('div'); w.className = 'warn';
            w.textContent = '⚠ 채워지지 않은 토큰 ' + left.join(' ') + ' — 이대로 보내면 글자 그대로 갑니다.'; note.append(w);
        }
        const tip = document.createElement('div');
        tip.textContent = mode === 'manual'
            ? '발송사 미설정 — 상담사 휴대폰 문자앱에서 보내므로 [Web발신] 없이 이 내용만 갑니다.'
            : mode === 'test'
                ? '테스트 모드 — 실제로는 ' + fmtPhone(device.dataset.testTo) + '로만 갑니다.'
                : '[Web발신]은 통신사가 붙입니다. 줄바꿈 위치는 휴대폰 기종마다 조금 다를 수 있습니다.';
        note.append(tip);
    }

    function setRecipient(r) {
        current = r;
        phoneEl.value = r ? (r.guardian_phone || '') : '';
        nameEl.value = r ? (r.guardian_name || '') : '';
        pickedEl.textContent = r
            ? `${r.patient_name} · 보호자 ${r.guardian_name || '-'} ${r.guardian_phone || '(번호 없음)'}`
            : '선택한 환자 없음';
        if (rawTemplate) { bodyEl.value = fillTokens(rawTemplate); }
        document.dispatchEvent(new CustomEvent('sms:recipient', {detail: r}));  // 템플릿 질환 필터가 듣는다
        updateCount();
    }
    // 이름 검색 — 상담일지와 같은 환자 자동완성(/api/autocomplete/patient)을 쓴다
    let searchTimer = null;
    searchEl.addEventListener('input', () => {
        clearTimeout(searchTimer);
        const q = searchEl.value.trim();
        if (!q) { listEl.hidden = true; return; }
        searchTimer = setTimeout(async () => {
            let res;
            try { res = await api.get('/api/autocomplete/patient?q=' + encodeURIComponent(q)); }
            catch { return; }
            const items = res.items || [];
            listEl.replaceChildren(...items.map(p => {
                const b = document.createElement('button'); b.type = 'button';
                b.textContent = `${p.name} · ${p.guardian_name || '보호자'} ${p.guardian_phone || '(번호 없음)'}`;
                b.addEventListener('click', async () => {
                    listEl.hidden = true; searchEl.value = p.name;
                    try { setRecipient(await api.get('/api/sms/recipient?pid=' + p.id)); }
                    catch (e) { msgEl.className = 'sms-msg err'; msgEl.textContent = '환자 정보를 불러오지 못했습니다: ' + e.message; }
                });
                return b;
            }));
            listEl.hidden = !items.length;
        }, 200);
    });
    searchEl.addEventListener('keydown', e => { if (e.key === 'Escape') listEl.hidden = true; });
    document.addEventListener('click', e => { if (!e.target.closest('.sms-search-wrap')) listEl.hidden = true; });

    phoneEl.addEventListener('input', renderPreview);  // 테스트 모드 머리말에 수신 번호가 들어간다
    bodyEl.addEventListener('input', () => { rawTemplate = ''; templateId = null; updateCount(); });

    // 템플릿 — 시점 탭 × 대상 질환 필터. 질환군은 수신자 최근 상담의 병명(models.consult_disease_groups).
    let timing = document.querySelector('.sms-tpl-tabs button.on')?.dataset.timing || '';
    let templateId = null;
    const allCb = document.getElementById('sms-tpl-all');
    function filterTemplates() {
        const all = allCb.checked;
        const groups = (current && current.disease_groups) || [];
        let shown = 0;
        document.querySelectorAll('.sms-tpl').forEach(b => {
            const okGroup = all || !groups.length || b.dataset.group === '공통' || groups.includes(b.dataset.group);
            b.hidden = !(b.dataset.timing === timing && okGroup);
            if (!b.hidden) shown++;
        });
        const none = document.querySelector('.sms-tpl-none');
        if (none) none.hidden = shown > 0 || !document.querySelectorAll('.sms-tpl').length;
    }
    document.querySelectorAll('.sms-tpl-tabs button').forEach(tab => tab.addEventListener('click', () => {
        document.querySelectorAll('.sms-tpl-tabs button').forEach(x => {
            x.classList.toggle('on', x === tab); x.setAttribute('aria-selected', String(x === tab));
        });
        timing = tab.dataset.timing; filterTemplates();
    }));
    allCb.addEventListener('change', filterTemplates);
    document.addEventListener('sms:recipient', filterTemplates);
    window.SMS_SELECT_TIMING = t => document.querySelector(`.sms-tpl-tabs button[data-timing="${t}"]`)?.click();

    document.querySelectorAll('.sms-tpl').forEach(btn => {
        btn.addEventListener('click', () => {
            templateId = Number(btn.dataset.tid) || null;
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

    // ─── 확인 창 (2026-09-28) — 문자는 회수가 안 되므로 '전송'이 곧 발송이 되지 않게 한 번 더 본다.
    // 미치환 토큰·번호 형식·길이 문제가 있으면 보내기 버튼이 꺼진다(서버 /api/sms/send도 같은 조건으로 거절).
    const dlg = document.getElementById('sms-confirm');
    const goBtn = document.getElementById('sms-confirm-go');
    const TOKENS = (() => { try { return JSON.parse(device.dataset.tokens || '[]'); } catch { return []; } })();
    function confirmProblems(phone, body) {
        const problems = [];
        const left = TOKENS.filter(t => body.includes(t));
        if (!body) problems.push('문자 내용이 비어 있습니다.');
        if (!/^01[016789]\d{7,8}$/.test(phone.replace(/\D/g, ''))) problems.push('휴대폰 번호 형식이 아닙니다.');
        if (left.length) problems.push('채워지지 않은 토큰 ' + left.join(' ') + ' — 수신자를 고르거나 직접 고쳐 주세요.');
        if (bodyBytes(body) > LMS_MAX) problems.push('장문(LMS) 한도를 넘습니다.');
        return problems;
    }
    function openConfirm() {
        const phone = phoneEl.value.trim(), body = bodyEl.value.trim();
        const problems = confirmProblems(phone, body);
        dlg.querySelector('.sms-confirm-to').textContent = `${nameEl.value.trim() || '받는 분'} · ${phone || '번호 없음'}`;
        dlg.querySelector('.sms-confirm-kind').textContent = countEl.textContent;
        const block = dlg.querySelector('.sms-confirm-block');
        block.hidden = !problems.length; block.textContent = problems.join(' / ');
        goBtn.disabled = problems.length > 0;
        const pv = document.getElementById('sms-confirm-preview');
        const copy = device.cloneNode(true); copy.removeAttribute('id');
        pv.replaceChildren(copy);
        const handoff = dlg.querySelector('.sms-confirm-handoff');
        handoff.hidden = true; handoff.replaceChildren();
        goBtn.textContent = device.dataset.mode === 'manual' ? '휴대폰으로 넘기기' : '보내기';
        dlg.showModal();
    }
    async function doSend(mode) {
        const phone = phoneEl.value.trim();
        const body = bodyEl.value.trim();
        if (confirmProblems(phone, body).length) return;   // 창이 열린 뒤 내용이 바뀌었을 때
        const payload = {
            to_phone: phone, body: body, to_name: nameEl.value.trim(),
            consultation_id: current ? current.consultation_id : null,
            patient_id: current ? current.patient_id : null,
            template_id: templateId,
            mode: mode || null,
        };
        goBtn.disabled = true;
        msgEl.className = 'sms-msg muted'; msgEl.textContent = '전송 처리 중...';
        try {
            const res = await api.post('/api/sms/send', payload);
            dlg.close();
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
            const block = dlg.querySelector('.sms-confirm-block');
            block.hidden = false; block.textContent = '실패: ' + e.message;
        } finally {
            goBtn.disabled = false;
        }
    }
    document.getElementById('sms-send').addEventListener('click', openConfirm);
    document.getElementById('sms-confirm-cancel').addEventListener('click', () => dlg.close());
    goBtn.addEventListener('click', () => doSend());

    filterTemplates();
    if (current) setRecipient(current);
    updateCount();
})();
