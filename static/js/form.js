// 상담 등록/수정 폼
(() => {
    const form = document.getElementById('consult-form');
    if (!form) return;

    const isEdit = form.dataset.edit === '1';
    const cid = form.dataset.cid;

    // 콤보박스 — 입력 + 화살표 클릭 시 드롭다운
    form.querySelectorAll('.combobox').forEach((combo) => {
        const input = combo.querySelector('input[type="text"]');
        const arrow = combo.querySelector('.combobox-arrow');
        const list = combo.querySelector('.combobox-list');
        if (!input || !arrow || !list) return;

        function open() { list.hidden = false; }
        function close() { list.hidden = true; }
        function toggle() { list.hidden ? open() : close(); }

        arrow.addEventListener('click', (e) => {
            e.preventDefault();
            toggle();
            if (!list.hidden) input.focus();
        });
        input.addEventListener('focus', open);
        list.querySelectorAll('li').forEach((li) => {
            li.addEventListener('mousedown', (e) => {
                e.preventDefault(); // input blur 방지
                input.value = li.textContent.trim();
                close();
                input.focus();
            });
        });
        document.addEventListener('click', (e) => {
            if (!combo.contains(e.target)) close();
        });
    });

    // 시/군/구 → 시/도 자동 채움
    const sigunguInput = form.querySelector('#patient-sigungu');
    const sidoSelect = form.querySelector('#patient-sido');
    if (sigunguInput && sidoSelect && window.SIGUNGU_INDEX) {
        function autoFillSido() {
            const v = sigunguInput.value.trim();
            if (!v) return;
            const candidates = window.SIGUNGU_INDEX[v];
            if (candidates && candidates.length === 1) {
                sidoSelect.value = candidates[0];
            }
        }
        sigunguInput.addEventListener('change', autoFillSido);
        sigunguInput.addEventListener('blur', autoFillSido);
    }

    // 연락처 — 010- 자동 + 8자리 입력 시 010-XXXX-XXXX 포맷
    const phoneInput = form.querySelector('input[name="patient.guardian_phone"]');
    if (phoneInput) {
        function formatPhone(v) {
            let digits = (v || '').replace(/\D/g, '');
            if (digits.startsWith('010')) digits = digits.slice(3);
            digits = digits.slice(0, 8);
            if (!digits) return '';
            if (digits.length <= 4) return '010-' + digits;
            return '010-' + digits.slice(0, 4) + '-' + digits.slice(4);
        }
        if (phoneInput.value) phoneInput.value = formatPhone(phoneInput.value);
        phoneInput.addEventListener('input', (e) => {
            const cursorAtEnd = e.target.selectionStart === e.target.value.length;
            e.target.value = formatPhone(e.target.value);
            if (cursorAtEnd) {
                e.target.setSelectionRange(e.target.value.length, e.target.value.length);
            }
        });
        phoneInput.addEventListener('focus', (e) => {
            if (!e.target.value) e.target.value = '010-';
        });
        phoneInput.addEventListener('blur', (e) => {
            if (e.target.value === '010-') e.target.value = '';
        });
    }

    // 자동완성: 환자명, 모병원, 질환
    document.querySelectorAll('input[data-ac]').forEach(setupAutocomplete);

    function setupAutocomplete(input) {
        const kind = input.dataset.ac; // patient | hospital | diagnosis
        const list = document.getElementById('ac-' + kind);
        if (!list) return;
        let timer = null;
        let activeIdx = -1;

        input.addEventListener('input', () => {
            clearTimeout(timer);
            const q = input.value.trim();
            if (q.length < 1) { hide(); return; }
            timer = setTimeout(() => fetchAndShow(q), 150);
        });
        input.addEventListener('keydown', (e) => {
            const items = list.querySelectorAll('.ac-item');
            if (!items.length) return;
            if (e.key === 'ArrowDown') { e.preventDefault(); activeIdx = (activeIdx + 1) % items.length; updateActive(items); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); activeIdx = (activeIdx - 1 + items.length) % items.length; updateActive(items); }
            else if (e.key === 'Enter' && activeIdx >= 0) { e.preventDefault(); items[activeIdx].click(); }
            else if (e.key === 'Escape') { hide(); }
        });
        document.addEventListener('click', (e) => {
            if (!input.parentElement.contains(e.target)) hide();
        });

        function hide() { list.hidden = true; activeIdx = -1; }
        function updateActive(items) {
            items.forEach((it, i) => it.classList.toggle('active', i === activeIdx));
        }
        async function fetchAndShow(q) {
            try {
                const res = await api.get(`/api/autocomplete/${kind}?q=${encodeURIComponent(q)}`);
                renderItems(res.items || []);
            } catch (e) { hide(); }
        }
        function renderItems(items) {
            if (!items.length) { hide(); return; }
            list.innerHTML = items.map(formatItem).join('');
            list.hidden = false;
            activeIdx = -1;
            list.querySelectorAll('.ac-item').forEach((el, i) => {
                el.addEventListener('click', () => pickItem(items[i]));
            });
        }
        function formatItem(it) {
            if (kind === 'patient') {
                return `<div class="ac-item" data-id="${it.id}">
                    <strong>${it.name}</strong>
                    <span class="meta">${it.guardian_phone || ''} ${it.guardian_name ? '· ' + it.guardian_name : ''}</span>
                </div>`;
            }
            return `<div class="ac-item">${it.name}</div>`;
        }
        function pickItem(it) {
            input.value = it.name;
            if (kind === 'patient') autoFillPatient(it);
            hide();
        }
    }

    function autoFillPatient(it) {
        // 기존 환자 선택 시 환자 정보 자동 채움 (빈 필드만)
        const map = {
            'patient.gender': it.gender,
            'patient.address_full': it.address_full,
            'patient.family_info': it.family_info,
            'patient.insurance_type': it.insurance_type,
            'patient.guardian_name': it.guardian_name,
            'patient.guardian_relation': it.guardian_relation,
            'patient.guardian_phone': it.guardian_phone,
        };
        Object.entries(map).forEach(([name, val]) => {
            if (val == null || val === '') return;
            const el = form.querySelector(`[name="${name}"]`);
            if (!el) return;
            if (el.type === 'radio') {
                const r = form.querySelector(`[name="${name}"][value="${val}"]`);
                if (r) r.checked = true;
            } else if (!el.value) {
                el.value = val;
            }
        });
        toast('기존 환자 정보를 불러왔습니다.', 'info');
    }

    // 폼 제출 — JSON으로 변환해서 전송
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const btn = document.getElementById('save-btn');
        btn.disabled = true;
        // 상담 결과 — 입원보류/입원취소 사유 필수
        const stChecked = form.querySelector('input[name="consultation.admission_status"]:checked');
        const st = stChecked ? stChecked.value : '';
        if (st === '입원보류') {
            const hr = form.querySelector('input[name="consultation.hold_reason"]');
            if (!hr || !hr.value.trim()) {
                toast('입원보류 사유를 입력하세요.', 'error');
                btn.disabled = false; return;
            }
        } else if (st === '입원취소') {
            const rr = form.querySelector('select[name="consultation.rejection_reason"]');
            const rd = form.querySelector('input[name="consultation.rejection_reason_detail"]');
            if ((!rr || !rr.value.trim()) && (!rd || !rd.value.trim())) {
                toast('입원취소 사유를 선택하거나 입력하세요.', 'error');
                btn.disabled = false; return;
            }
        }
        const payload = collectPayload();
        try {
            const url = isEdit ? `/api/consult/${cid}` : '/api/consult';
            const res = await api.post(url, payload);
            const targetId = res.id || cid;
            location.href = `/consult/${targetId}`;
        } catch (err) {
            toast('저장 실패: ' + err.message, 'error');
            btn.disabled = false;
        }
    });

    function collectPayload() {
        // 'section.key' = 단일값, 'section.key[]' = 다중 체크박스 → 배열
        const out = { patient: {}, consultation: {} };
        for (const el of form.elements) {
            if (!el.name || !el.name.includes('.')) continue;
            const isArray = el.name.endsWith('[]');
            const baseName = isArray ? el.name.slice(0, -2) : el.name;
            const [section, key] = baseName.split('.');
            if (!out[section]) continue;

            if (isArray) {
                if (el.type === 'checkbox' && el.checked) {
                    if (!Array.isArray(out[section][key])) out[section][key] = [];
                    out[section][key].push(el.value);
                }
            } else if (el.type === 'radio') {
                if (el.checked) out[section][key] = el.value;
            } else if (el.type === 'checkbox') {
                out[section][key] = el.checked;
            } else {
                const v = el.value.trim();
                if (v !== '') out[section][key] = v;
            }
        }
        // 다중 체크박스 그룹은 빈 배열도 명시 — 모두 해제했을 때 DB에 반영
        document.querySelectorAll('input[type="checkbox"][name$="[]"]').forEach((el) => {
            const baseName = el.name.slice(0, -2);
            const [section, key] = baseName.split('.');
            if (!Array.isArray(out[section][key])) out[section][key] = [];
        });
        return out;
    }

    // ─── 회복기 자동 판정 (의료법 재활의료기관 본지정 기준) ───
    // 발병일 + 진단군(병명 체크) → 입원(예정)일 또는 상담일과의 차이로 회복기/비회복기 판정
    const RECOVERY_RULES = [
        // [키워드, 인정 기간(일)] — 여러 병명 매칭 시 가장 긴 기간 적용
        [['뇌출혈','뇌경색','뇌손상','척수손상','뇌성마비','마비','편마비','사지마비','중추신경계'], 90],
        [['골유합 지연','골유합지연'], 60],
        [['고관절','대퇴','대퇴부','골반','절단','하지 부위 절단','슬관절','근골격계'], 30],
        [['호흡질환','폐질환','심장질환','신생물','폐렴','폐수종','패혈증','농양','다제내성','CRE','VRE',
          '신부전','동정맥루','복부대동맥류','급성복막염','장폐색',
          '파킨슨(신규)','길랑바레증후군','비사용증후군'], 60],
    ];

    function parseDateStr(s) {
        if (!s) return null;
        const m = String(s).match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
        if (!m) return null;
        const d = new Date(`${m[1]}-${m[2].padStart(2,'0')}-${m[3].padStart(2,'0')}T00:00:00`);
        return isNaN(d) ? null : d;
    }

    function computeRecovery(refDate, onsetDate, diseases) {
        const rd = parseDateStr(refDate);
        const od = parseDateStr(onsetDate);
        if (!rd || !od) return null;
        const days = Math.floor((rd - od) / 86400000);
        if (days < 0) return null;
        let matched = 0;
        for (const d of diseases) {
            if (!d) continue;
            for (const [kws, period] of RECOVERY_RULES) {
                if (kws.some(kw => d.includes(kw))) {
                    if (period > matched) matched = period;
                    break;
                }
            }
        }
        if (matched === 0) return null;
        return { label: days <= matched ? '회복기' : '비회복기', days, period: matched };
    }

    // 하이브리드 발병일: 날짜 선택기(onset-date) ↔ 자유 텍스트(onset-text, "정확한 날짜 모름"),
    // 실제 제출값은 hidden(onset-hidden, name=consultation.disease_onset)에 동기화
    const onsetDateEl = document.getElementById('onset-date');
    const onsetTextEl = document.getElementById('onset-text');
    const onsetUnknownEl = document.getElementById('onset-unknown');
    const onsetEl = document.getElementById('onset-hidden');
    const consultDateEl = form.querySelector('[name="consultation.consult_date"]');
    const plannedEl = form.querySelector('[name="consultation.planned_admission_date"]');
    const purposeEl = document.getElementById('admission-purpose-input');
    const hintEl = document.getElementById('recovery-hint');
    const metaEl = document.getElementById('recovery-meta');
    const onsetHintEl = document.getElementById('onset-recovery');  // 발병일/수술일 옆 회복기 즉시 표시

    const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

    // admission_purpose가 자동 판정값인지 추적 (사용자 수동 입력 보존)
    const AUTO_VALUES = new Set([
        '회복기재활', '비회복기재활', '회복기', '비회복기', '',
        '회복기재활 및 간호간병 통합서비스', '비회복기재활 및 간호간병 통합서비스',
    ]);
    let lastAutoValue = '';

    function applyOnsetMode() {
        if (!onsetDateEl || !onsetTextEl || !onsetUnknownEl) return;
        const unknown = onsetUnknownEl.checked;
        onsetDateEl.style.display = unknown ? 'none' : '';
        onsetTextEl.style.display = unknown ? '' : 'none';
    }
    function syncOnset() {
        if (!onsetEl) return;
        const unknown = onsetUnknownEl && onsetUnknownEl.checked;
        onsetEl.value = unknown
            ? (onsetTextEl ? onsetTextEl.value.trim() : '')
            : (onsetDateEl ? onsetDateEl.value : '');
        recomputeRecovery();
    }
    // 수정 모드: 저장값이 YYYY-MM-DD면 날짜 선택기, 아니면 자유 텍스트 모드로 복원
    if (onsetEl && onsetEl.value.trim()) {
        const saved = onsetEl.value.trim();
        if (ISO_DATE.test(saved)) {
            if (onsetDateEl) onsetDateEl.value = saved;
        } else {
            if (onsetTextEl) onsetTextEl.value = saved;
            if (onsetUnknownEl) onsetUnknownEl.checked = true;
        }
    }
    applyOnsetMode();

    function recomputeRecovery() {
        if (!purposeEl) return;
        const setHint = (txt, cls) => {
            const full = 'recovery-hint' + (cls ? ' ' + cls : '');
            if (hintEl) { hintEl.textContent = txt || ''; hintEl.className = full; }
            if (onsetHintEl) { onsetHintEl.textContent = txt || ''; onsetHintEl.className = full; }
        };
        const setMeta = (txt) => { if (metaEl) metaEl.textContent = txt || ''; };

        const onset = onsetEl ? onsetEl.value.trim() : '';
        const ref = (plannedEl ? plannedEl.value : '') || (consultDateEl ? consultDateEl.value : '');
        const diseases = Array.from(form.querySelectorAll('[name="consultation.diseases[]"]:checked')).map(c => c.value);

        if (!onset) {
            setHint('발병일을 입력하면 회복기 여부가 자동 판정됩니다.', ''); setMeta(''); return;
        }
        if (!ISO_DATE.test(onset)) {
            setHint('※ 발병일이 정확한 날짜가 아니어서 자동 판정 불가 — 입원목적을 직접 선택하세요.', 'rh-warn');
            setMeta(''); return;
        }
        if (!ref) {
            setHint('상담일 또는 입원예정일이 있어야 자동 판정됩니다.', ''); setMeta(''); return;
        }
        if (diseases.length === 0) {
            setHint('병명을 선택하면 회복기 여부가 자동 판정됩니다.', ''); setMeta(''); return;
        }
        const result = computeRecovery(ref, onset, diseases);
        if (!result) {
            setHint('선택한 병명은 회복기 판정 기준이 없습니다 — 입원목적을 직접 선택하세요.', '');
            setMeta(''); return;
        }
        const autoVal = result.label + '재활 및 간호간병 통합서비스';
        // 회복기 → 회복기(S005), 비회복기 → 비회복기(S006)
        const recCode = result.label === '회복기' ? '회복기(S005)' : '비회복기(S006)';
        setHint(`※ 자동 판정: ${recCode}`, result.label === '회복기' ? 'rh-yes' : 'rh-no');
        setMeta(`발병 후 ${result.days}일 / 인정 기간 ${result.period}일 (입원시점 기준)`);
        // 사용자가 별도 메모를 적은 게 아니면 자동 입력값으로 채움
        const cur = purposeEl.value.trim();
        if (cur === '' || cur === lastAutoValue || AUTO_VALUES.has(cur)) {
            purposeEl.value = autoVal;
            lastAutoValue = autoVal;
        }
    }

    // 발병일 모드 전환 + 발병일·상담일·입원예정일·병명 변경 → 재계산
    if (onsetUnknownEl) {
        onsetUnknownEl.addEventListener('change', () => { applyOnsetMode(); syncOnset(); });
    }
    [onsetDateEl, onsetTextEl].forEach(el => {
        if (el) ['change', 'blur', 'input'].forEach(ev => el.addEventListener(ev, syncOnset));
    });
    [consultDateEl, plannedEl].forEach(el => {
        if (el) ['change', 'blur', 'input'].forEach(ev => el.addEventListener(ev, recomputeRecovery));
    });
    form.querySelectorAll('[name="consultation.diseases[]"]').forEach(cb => {
        cb.addEventListener('change', recomputeRecovery);
    });
    // 페이지 로드 시 한 번 (수정 모드에서)
    setTimeout(recomputeRecovery, 100);

    // ─── 상담일자 → 요일 자동 표시 ───
    (function() {
        const dateEl = form.querySelector('[name="consultation.consult_date"]');
        const wdEl = document.getElementById('consult-weekday');
        if (!dateEl || !wdEl) return;
        const WD = ['일', '월', '화', '수', '목', '금', '토'];
        function showWeekday() {
            const m = String(dateEl.value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
            if (!m) { wdEl.textContent = ''; return; }
            const d = new Date(+m[1], +m[2] - 1, +m[3]);
            // 예: 2026.05.11(수)
            wdEl.textContent = isNaN(d.getTime()) ? '' : `${m[1]}.${m[2]}.${m[3]}(${WD[d.getDay()]})`;
        }
        ['change', 'input'].forEach(ev => dateEl.addEventListener(ev, showWeekday));
        showWeekday();
    })();

    // ─── 상담 결과: 입원보류/취소 사유칸·입원완료 입원일칸 토글 ───
    (function() {
        const statusRadios = form.querySelectorAll('input[name="consultation.admission_status"]');
        if (!statusRadios.length) return;
        const holdRow = document.getElementById('status-reason-hold');
        const cancelRow = document.getElementById('status-reason-cancel');
        const completedRow = document.getElementById('status-extra-completed');
        function refresh() {
            const c = form.querySelector('input[name="consultation.admission_status"]:checked');
            const s = c ? c.value : '';
            if (holdRow) holdRow.hidden = (s !== '입원보류');
            if (cancelRow) cancelRow.hidden = (s !== '입원취소');
            if (completedRow) completedRow.hidden = (s !== '입원완료');
        }
        statusRadios.forEach(r => r.addEventListener('change', refresh));
        refresh();
    })();
})();
