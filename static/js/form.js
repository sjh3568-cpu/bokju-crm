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
})();
