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

    // 모병원 정식명 강제 — blur 시 마스터 매칭. 자동완성 클릭으로 인한 blur는
    // pickItem이 먼저 dataset.hospVerified를 세팅하므로 setTimeout으로 우선순위 보장.
    const hospInputForEnforce = form.querySelector('[name="consultation.current_location_name"]');
    if (hospInputForEnforce) {
        hospInputForEnforce.addEventListener('blur', () => {
            setTimeout(() => enforceHospitalOfficial(hospInputForEnforce), 150);
        });
    }

    // 신규 상담 — 환자명 blur 시 동명이인 사전 경고 (수정 모드 / 기존 환자 선택 후엔 스킵)
    if (!isEdit) setupHomonymPreWarning();

    function setupHomonymPreWarning() {
        const nameInput = form.querySelector('input[name="patient.name"]');
        if (!nameInput) return;
        // patient.id가 이미 세팅된 경우(인박스/prefill/자동완성 선택) — 첫 진입은 스킵.
        // 사용자가 이름을 바꾸면 그때 다시 체크.
        const initialName = (nameInput.value || '').trim();
        let lastCheckedName = initialName;
        // 세션 동안 "신규로 진행" 결정한 이름은 다시 안 띄움
        const dismissed = new Set();

        async function check() {
            const name = (nameInput.value || '').trim();
            if (!name) return;
            if (name === lastCheckedName) return;  // 동일 이름 재체크 방지
            lastCheckedName = name;
            if (dismissed.has(name)) return;
            // 보호자 전화가 이미 채워져 있으면 — 자동완성 선택 직후거나 사용자가 환자 정보를
            // 이미 알고 있는 경우로 간주, 모달 스킵. 진짜 신규 환자 등록 시작 시점엔 빈값.
            const phEl = form.querySelector('input[name="patient.guardian_phone"]');
            const phVal = phEl ? phEl.value.trim() : '';
            if (phVal && phVal !== '010-') return;
            try {
                const r = await fetch(`/api/patients/by-name?name=${encodeURIComponent(name)}`);
                if (!r.ok) return;
                const j = await r.json();
                const items = j.items || [];
                if (!items.length) return;
                showHomonymModal(name, items, () => dismissed.add(name));
            } catch (e) { /* 조회 실패 시 등록을 막지 않음 */ }
        }
        nameInput.addEventListener('blur', () => {
            // 자동완성 클릭으로 blur 발생할 수 있음 — 짧은 지연 후 자동완성 리스트가 닫힌 뒤 체크
            setTimeout(check, 100);
        });
    }

    function showHomonymModal(name, items, onDismiss) {
        let modal = document.getElementById('homonym-prewarn-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'homonym-prewarn-modal';
            modal.className = 'hpw-modal';
            modal.innerHTML = `
                <div class="hpw-backdrop"></div>
                <div class="hpw-content">
                    <h3 class="hpw-title"></h3>
                    <p class="hpw-help">동일인이면 [이 환자입니다]를 눌러 정보를 불러오고, 다른 환자라면 [신규 환자] 버튼을 누르세요.</p>
                    <div class="hpw-body"></div>
                    <div class="hpw-actions">
                        <button type="button" class="btn btn-primary btn-sm" id="hpw-new">＋ 신규 환자입니다 — 계속 진행</button>
                    </div>
                </div>`;
            document.body.appendChild(modal);
            modal.querySelector('.hpw-backdrop').addEventListener('click', () => closeHomonymModal());
        }
        modal.querySelector('.hpw-title').textContent =
            `⚠ 같은 이름 환자 ${items.length}명이 이미 등록되어 있습니다 — "${name}"`;
        const body = modal.querySelector('.hpw-body');
        body.innerHTML = `
            <table class="hpw-tbl">
                <thead>
                    <tr>
                        <th>보호자 전화</th>
                        <th>보호자</th>
                        <th>거주지</th>
                        <th>보험</th>
                        <th>상담</th>
                        <th>최근 상담일</th>
                        <th></th>
                    </tr>
                </thead>
                <tbody>
                    ${items.map(p => {
                        const last = p.last || {};
                        const sido = (p.residence_sido || '').replace(/특별시|광역시|특별자치도|특별자치시/g, '');
                        const res = (sido + ' ' + (p.residence_sigungu || '')).trim();
                        return `<tr class="${p.blacklist ? 'hpw-bl-row' : ''}" data-pid="${p.id}">
                            <td class="${p.guardian_phone ? 'hpw-phone' : 'hpw-empty'}">${p.guardian_phone ? escHpw(p.guardian_phone) : '—'}</td>
                            <td>${escHpw(p.guardian_name || '')}${p.guardian_relation ? ' (' + escHpw(p.guardian_relation) + ')' : ''}</td>
                            <td>${escHpw(res) || '<span class="hpw-empty">—</span>'}</td>
                            <td>${escHpw(p.insurance_type || '') || '<span class="hpw-empty">—</span>'}</td>
                            <td><strong>${p.consultation_count}</strong>회</td>
                            <td>${last.consult_date ? escHpw(last.consult_date) : '<span class="hpw-empty">—</span>'}${p.blacklist ? ' <span class="hpw-bl-badge">⚠블랙</span>' : ''}</td>
                            <td><button type="button" class="btn btn-secondary btn-sm hpw-pick" data-pid="${p.id}">이 환자입니다 →</button></td>
                        </tr>`;
                    }).join('')}
                </tbody>
            </table>`;
        modal.classList.add('show');
        document.body.style.overflow = 'hidden';
        body.querySelectorAll('.hpw-pick').forEach(btn => {
            btn.addEventListener('click', () => {
                const pid = parseInt(btn.dataset.pid);
                const it = items.find(p => p.id === pid);
                if (it) {
                    // 환자 정보 prefill (기존 autoFillPatient 사용)
                    autoFillPatient(it);
                    toast('기존 환자 정보를 불러왔습니다.' + (it.blacklist ? ' ⚠ 블랙리스트 환자입니다.' : ''),
                          it.blacklist ? 'error' : 'info');
                }
                closeHomonymModal();
            });
        });
        modal.querySelector('#hpw-new').onclick = () => {
            onDismiss();
            closeHomonymModal();
            toast('신규 환자로 등록을 계속합니다.', 'info');
        };
    }
    function closeHomonymModal() {
        const modal = document.getElementById('homonym-prewarn-modal');
        if (modal) modal.classList.remove('show');
        document.body.style.overflow = '';
    }
    function escHpw(s) {
        if (s === null || s === undefined) return '';
        return String(s).replace(/[&<>"']/g, ch => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[ch]));
    }
    document.addEventListener('keydown', e => {
        if (e.key !== 'Escape') return;
        const m = document.getElementById('homonym-prewarn-modal');
        if (m && m.classList.contains('show')) closeHomonymModal();
    });

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
            if (kind === 'hospital') {
                // 같은 이름·약칭 후보 식별을 위해 region·kind를 함께 표시
                const meta = [it.region, it.kind].filter(Boolean).join(' · ');
                return `<div class="ac-item">
                    <strong>${escHpw(it.name)}</strong>
                    ${meta ? `<span class="meta">${escHpw(meta)}</span>` : ''}
                </div>`;
            }
            return `<div class="ac-item">${it.name}</div>`;
        }
        function pickItem(it) {
            input.value = it.name;
            if (kind === 'patient') autoFillPatient(it);
            else if (kind === 'hospital') markHospitalOfficial(input, it.name);
            hide();
        }
    }

    // 모병원 정식명 강제 — 자동완성에서 선택했거나, 마스터에 정확히 있는 이름만 허용.
    // 자유 입력(타이핑) 후 blur·submit 시점에 마스터 검증, 매칭 없거나 모호하면 차단.
    function markHospitalOfficial(input, name) {
        input.classList.remove('hosp-invalid');
        input.title = '';
        input.dataset.hospVerified = name;
    }
    function markHospitalInvalid(input, msg) {
        input.classList.add('hosp-invalid');
        input.title = msg;
        delete input.dataset.hospVerified;
    }
    function clearHospitalMark(input) {
        input.classList.remove('hosp-invalid');
        input.title = '';
        delete input.dataset.hospVerified;
    }
    async function enforceHospitalOfficial(input) {
        const raw = (input.value || '').trim();
        if (!raw) { clearHospitalMark(input); return; }
        if (input.dataset.hospVerified === raw) return;
        let items = [];
        try {
            const res = await api.get(`/api/autocomplete/hospital?q=${encodeURIComponent(raw)}`);
            items = res.items || [];
        } catch (e) { return; /* 네트워크 오류 시 차단 안 함 */ }
        const exact = items.find(it => it.name === raw);
        if (exact) { markHospitalOfficial(input, exact.name); return; }
        if (items.length === 1) {
            const official = items[0].name;
            input.value = official;
            markHospitalOfficial(input, official);
            toast(`정식 명칭으로 자동 변환: ${raw} → ${official}`, 'info');
            return;
        }
        if (items.length > 1) {
            markHospitalInvalid(input, '정식 명칭을 자동완성에서 선택하세요.');
            return;
        }
        markHospitalInvalid(input, '마스터에 없는 병원입니다. 정확한 이름을 입력하거나 관리자에게 등록을 요청하세요.');
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
        // 블랙리스트 상태 반영 (4번 요청)
        const blChk = document.getElementById('blacklist-check');
        const blReason = document.getElementById('blacklist-reason');
        if (blChk) {
            blChk.checked = !!it.blacklist;
            if (blReason) {
                blReason.hidden = !it.blacklist;
                if (it.blacklist_reason) blReason.value = it.blacklist_reason;
            }
        }
        toast('기존 환자 정보를 불러왔습니다.' + (it.blacklist ? ' ⚠ 블랙리스트 환자입니다.' : ''),
              it.blacklist ? 'error' : 'info');
    }

    // 폼 제출 — JSON으로 변환해서 전송
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const btn = document.getElementById('save-btn');
        btn.disabled = true;
        // 상담 결과 ① 상담 진행 — 재입원/요청/보류/취소 사유 필수
        const crChecked = form.querySelector('input[name="consultation.consult_result"]:checked');
        const cr = crChecked ? crChecked.value : '';
        if (['재입원 상담', '상담요청', '상담보류', '상담취소'].includes(cr)) {
            const crr = form.querySelector('input[name="consultation.consult_result_reason"]');
            if (!crr || !crr.value.trim()) {
                toast('상담 결과 사유를 입력하세요.', 'error');
                btn.disabled = false; return;
            }
        }
        // 블랙리스트 — 체크 시 사유 필수
        const blChk = document.getElementById('blacklist-check');
        if (blChk && blChk.checked) {
            const blr = document.getElementById('blacklist-reason');
            if (!blr || !blr.value.trim()) {
                toast('블랙리스트 지정 사유를 입력하세요.', 'error');
                btn.disabled = false; return;
            }
        }
        // 신규 상담 — 블랙리스트 환자 등록 경고 (제안 2, 임상 안전)
        if (!isEdit) {
            const nmEl = form.querySelector('[name="patient.name"]');
            const phEl = form.querySelector('[name="patient.guardian_phone"]');
            try {
                const qs = new URLSearchParams({
                    name: nmEl ? nmEl.value.trim() : '',
                    phone: phEl ? phEl.value.trim() : '',
                });
                const chk = await api.get('/api/patient/blacklist-check?' + qs.toString());
                if (chk.blacklisted &&
                    !confirm('⚠ 블랙리스트로 지정된 환자입니다.\n사유: '
                             + (chk.reason || '(미기재)')
                             + '\n\n그래도 상담을 등록할까요?')) {
                    btn.disabled = false; return;
                }
            } catch (e) { /* 조회 실패 시 등록을 막지 않음 */ }
        }
        // 상담 결과 ② 입원 진행 — 입원보류/입원취소 사유 필수
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
        // 모병원 정식명 최종 강제 — 자유 입력 후 blur 없이 바로 submit한 케이스 대응
        const hospInputFinal = form.querySelector('[name="consultation.current_location_name"]');
        if (hospInputFinal && hospInputFinal.value.trim()) {
            await enforceHospitalOfficial(hospInputFinal);
            if (hospInputFinal.classList.contains('hosp-invalid')) {
                toast('모병원 칸이 마스터에 없습니다. 정식 명칭을 자동완성에서 선택하세요.', 'error');
                hospInputFinal.focus();
                btn.disabled = false; return;
            }
        }
        const payload = collectPayload();
        try {
            let url = isEdit ? `/api/consult/${cid}` : '/api/consult';
            // 인박스에서 진입한 신규 상담 — comm_id 전달 → 등록 후 인바운드 자동 처리완료
            if (!isEdit) {
                const commId = form.dataset.commId || '';
                if (commId) url += `?comm_id=${encodeURIComponent(commId)}`;
            }
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
                out[section][key] = v;
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

    // 발병일 자유텍스트에 정확한 날짜(26.4.15·2026.4.15·26-4-15 등)를 입력하면
    // ISO(YYYY-MM-DD)로 자동 변환하고 날짜 선택기 모드로 되돌린다.
    function normalizeDateText(s) {
        s = String(s || '').trim();
        const m = s.match(/^(\d{2,4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?\.?$/);
        if (!m) return null;
        let y = m[1];
        if (y.length === 2) {
            y = '20' + y;
            if (+y > new Date().getFullYear()) y = '19' + m[1];
        }
        const yy = +y, mm = +m[2], dd = +m[3];
        if (mm < 1 || mm > 12 || dd < 1 || dd > 31) return null;
        const dt = new Date(yy, mm - 1, dd);
        if (dt.getFullYear() !== yy || dt.getMonth() !== mm - 1 || dt.getDate() !== dd) return null;
        return `${yy}-${String(mm).padStart(2, '0')}-${String(dd).padStart(2, '0')}`;
    }
    if (onsetTextEl) {
        onsetTextEl.addEventListener('blur', () => {
            const iso = normalizeDateText(onsetTextEl.value);
            if (!iso) return;
            if (onsetDateEl) onsetDateEl.value = iso;
            if (onsetUnknownEl) onsetUnknownEl.checked = false;
            onsetTextEl.value = '';
            applyOnsetMode();
            syncOnset();
        });
    }
    [consultDateEl, plannedEl].forEach(el => {
        if (el) ['change', 'blur', 'input'].forEach(ev => el.addEventListener(ev, recomputeRecovery));
    });
    // 병명 그룹 자동 추론 배지 — 체크된 병명의 소속 그룹(fieldset legend)을 배지로 표시
    const dxBadgesEl = document.getElementById('dx-group-badges');
    function updateDiseaseGroupBadges() {
        if (!dxBadgesEl) return;
        const groups = [];
        form.querySelectorAll('[name="consultation.diseases[]"]:checked').forEach(cb => {
            const fs = cb.closest('fieldset');
            const lg = fs && fs.querySelector('legend');
            const g = lg ? lg.textContent.trim() : '';
            if (g && groups.indexOf(g) === -1) groups.push(g);
        });
        dxBadgesEl.innerHTML = groups.map(g =>
            '<span class="dx-grp-badge">' + g + '</span>').join('');
    }
    form.querySelectorAll('[name="consultation.diseases[]"]').forEach(cb => {
        cb.addEventListener('change', () => {
            recomputeRecovery();
            updateDiseaseGroupBadges();
        });
    });
    // 페이지 로드 시 한 번 (수정 모드에서)
    setTimeout(recomputeRecovery, 100);
    updateDiseaseGroupBadges();

    // ─── 모든 날짜 입력칸 → 요일 자동 표시 (예: 2026-05-22(금)) ───
    (function() {
        const WD = ['일', '월', '화', '수', '목', '금', '토'];
        form.querySelectorAll('input[type="date"]').forEach(dateEl => {
            const tag = document.createElement('span');
            tag.className = 'weekday-tag';
            // .field 직속 입력칸은 라벨 끝에, 그 외(인라인)는 입력칸 바로 뒤에 표시
            const field = dateEl.parentElement;
            const label = field && field.classList.contains('field')
                ? field.querySelector(':scope > label') : null;
            if (label) label.appendChild(tag);
            else dateEl.insertAdjacentElement('afterend', tag);
            function show() {
                const m = String(dateEl.value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
                if (!m) { tag.textContent = ''; return; }
                const d = new Date(+m[1], +m[2] - 1, +m[3]);
                tag.textContent = isNaN(d.getTime())
                    ? '' : `${m[1]}-${m[2]}-${m[3]}(${WD[d.getDay()]})`;
            }
            ['change', 'input'].forEach(ev => dateEl.addEventListener(ev, show));
            show();
        });
    })();

    // ─── 상담 결과 ② 입원 진행: 입원예정/보류/취소/완료에 따른 부가칸 토글 ───
    (function() {
        const statusRadios = form.querySelectorAll('input[name="consultation.admission_status"]');
        if (!statusRadios.length) return;
        const holdRow = document.getElementById('status-reason-hold');
        const cancelRow = document.getElementById('status-reason-cancel');
        const completedRow = document.getElementById('status-extra-completed');
        const plannedRow = document.getElementById('status-extra-planned');
        const plannedDateEl = form.querySelector('input[name="consultation.planned_admission_date"]');
        function refresh(triggered) {
            const c = form.querySelector('input[name="consultation.admission_status"]:checked');
            const s = c ? c.value : '';
            if (holdRow) holdRow.hidden = (s !== '입원보류');
            if (cancelRow) cancelRow.hidden = (s !== '입원취소');
            if (completedRow) completedRow.hidden = (s !== '입원완료');
            if (plannedRow) plannedRow.hidden = (s !== '입원예정');
            // 입원예정 선택 + 날짜 비어있음 → 헤더 입원예정일 칸 시각 강조
            if (plannedDateEl) {
                const needFlash = (s === '입원예정' && !plannedDateEl.value);
                plannedDateEl.classList.toggle('field-need-attention', needFlash);
                if (needFlash && triggered) {
                    plannedDateEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
            }
        }
        statusRadios.forEach(r => r.addEventListener('change', () => refresh(true)));
        if (plannedDateEl) plannedDateEl.addEventListener('input', () => refresh(false));
        refresh(false);
    })();

    // ─── 상담 결과 ① 상담 진행: 사유칸 토글 + 라벨 변경 (7번 요청) ───
    (function() {
        const crRadios = form.querySelectorAll('input[name="consultation.consult_result"]');
        if (!crRadios.length) return;
        const reasonRow = document.getElementById('consult-result-reason-row');
        const reasonLabel = document.getElementById('consult-result-reason-label');
        const reasonInput = form.querySelector('input[name="consultation.consult_result_reason"]');
        const LABELS = {
            '재입원 상담': '재입원 사유 / 이전 입원 정보',
            '상담요청': '재연락 시기',
            '상담보류': '보류 사유',
            '상담취소': '취소 사유',
        };
        function refresh() {
            const c = form.querySelector('input[name="consultation.consult_result"]:checked');
            const v = c ? c.value : '';
            const need = !!LABELS[v];
            if (reasonRow) reasonRow.hidden = !need;
            if (need && reasonLabel) {
                reasonLabel.innerHTML = LABELS[v] + ' <span class="req">*</span>';
            }
            if (need && reasonInput) reasonInput.placeholder = LABELS[v] + ' (필수)';
        }
        crRadios.forEach(r => r.addEventListener('change', refresh));
        refresh();
    })();

    // ─── 블랙리스트: 체크 시 사유칸 표시 (4번 요청) ───
    (function() {
        const blChk = document.getElementById('blacklist-check');
        const blReason = document.getElementById('blacklist-reason');
        if (!blChk || !blReason) return;
        blChk.addEventListener('change', () => { blReason.hidden = !blChk.checked; });
    })();

    // ─── 모병원 빠른 선택 — Top 5 버튼 클릭 시 병원칸 채움 ───
    (function() {
        const hospInput = form.querySelector('[name="consultation.current_location_name"]');
        if (!hospInput) return;
        form.querySelectorAll('.hosp-quick-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                hospInput.value = btn.dataset.hosp || '';
                hospInput.focus();
            });
        });
    })();
})();
