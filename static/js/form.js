// 상담 등록/수정 폼
(() => {
    const form = document.getElementById('consult-form');
    if (!form) return;

    const isEdit = form.dataset.edit === '1';
    const cid = form.dataset.cid;

    // 새 상담 임시저장 — 상담사 계정별 서버 보관. 여러 환자 초안을 독립적으로 유지한다.
    const legacyDraftKey = 'bokju_consult_draft_v1';
    const draftKey = isEdit ? null : legacyDraftKey + '_' + (form.dataset.userId || 'user');
    let activeDraftId = null;
    let draftTimer;
    const draftState = document.createElement('div');
    draftState.className = 'form-draft-state';
    draftState.setAttribute('aria-live', 'polite');
    if (!isEdit) {
        draftState.textContent = '임시저장 준비';
        form.prepend(draftState);
        const listBtn = document.getElementById('draft-list-btn');
        const saveBtns = ['draft-save-btn', 'draft-save-btn-top'].map(id => document.getElementById(id)).filter(Boolean);   // 하단 + 상단 우측
        const dialog = document.getElementById('draft-dialog');
        const list = document.getElementById('draft-list');
        const count = document.getElementById('draft-count');
        const fieldsNow = () => Array.from(form.elements).filter(el => el.name).map(el => ({
            name:el.name, type:el.type, value:el.value, checked:el.checked
        }));
        const fieldValue = name => form.elements.namedItem(name)?.value?.trim() || '';
        const saveDraft = async (announce=false) => {
            const patientName = fieldValue('patient.name');
            const guardianPhone = fieldValue('patient.guardian_phone');
            const result = await api.post('/api/consult-drafts', {
                id:activeDraftId, fields:fieldsNow(), patient_name:patientName,
                guardian_phone:guardianPhone,
                title:patientName ? `${patientName} 환자 상담` : '이름 미입력 상담'
            });
            activeDraftId = result.id;
            draftState.textContent = '서버 임시저장 완료 · ' + new Date().toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit'});
            if (announce) toast('현재 상담을 임시저장했습니다.', 'success');
            refreshDrafts();
        };
        const restoreDraft = draft => {
            (draft.payload || []).forEach(item => {
                const candidates = Array.from(form.elements).filter(el => el.name === item.name);
                const el = (item.type === 'radio' || item.type === 'checkbox')
                    ? candidates.find(x => x.value === item.value) : candidates[0];
                if (!el) return;
                if (item.type === 'radio' || item.type === 'checkbox') el.checked = !!item.checked;
                else el.value = item.value || '';
                el.dispatchEvent(new Event('change', {bubbles:true}));
            });
            activeDraftId = draft.id;
            draftState.textContent = '임시저장본 불러옴 · ' + (draft.patient_name || '이름 미입력');
            dialog.close(); window.scrollTo({top:0, behavior:'smooth'});
        };
        const esc = value => String(value || '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
        async function refreshDrafts() {
            try {
                const data = await api.get('/api/consult-drafts');
                const drafts = data.drafts || [];
                count.textContent = drafts.length; count.hidden = !drafts.length;
                list.innerHTML = drafts.length ? drafts.map(d => `<div class="draft-item" data-id="${d.id}"><div><strong>${esc(d.patient_name || '이름 미입력 상담')}</strong><small>${esc(d.guardian_phone || '연락처 미입력')} · ${esc((d.updated_at || '').replace('T',' ').slice(0,16))}</small></div><div class="draft-item-actions"><button type="button" class="btn btn-primary btn-xs draft-load">불러오기</button><button type="button" class="btn btn-secondary btn-xs draft-delete">삭제</button></div></div>`).join('') : '<div class="empty">보관 중인 임시저장 상담이 없습니다.</div>';
                list.querySelectorAll('.draft-item').forEach((row, index) => {
                    const draft = drafts[index];
                    row.querySelector('.draft-load').onclick = () => restoreDraft(draft);
                    row.querySelector('.draft-delete').onclick = async () => {
                        if (!confirm(`${draft.patient_name || '이름 미입력 상담'} 임시저장본을 삭제할까요?`)) return;
                        const res = await fetch(`/api/consult-drafts/${draft.id}`, {method:'DELETE'});
                        if (!res.ok) return toast('임시저장본을 삭제하지 못했습니다.', 'error');
                        if (activeDraftId === draft.id) activeDraftId = null;
                        refreshDrafts();
                    };
                });
            } catch (_) { list.innerHTML = '<div class="empty">임시저장 목록을 불러오지 못했습니다.</div>'; }
        }
        form.addEventListener('input', () => {
            draftState.textContent = '작성 중…'; clearTimeout(draftTimer);
            draftTimer = setTimeout(() => saveDraft(false).catch(() => { draftState.textContent = '임시저장 실패 · 다시 시도해 주세요'; }), 1500);
        });
        saveBtns.forEach(b => b.addEventListener('click', () => saveDraft(true).catch(err => toast('임시저장 실패: ' + err.message, 'error'))));
        listBtn?.addEventListener('click', () => { refreshDrafts(); dialog.showModal(); });
        document.getElementById('draft-dialog-close')?.addEventListener('click', () => dialog.close());
        dialog?.addEventListener('click', e => { if (e.target === dialog) dialog.close(); });
        refreshDrafts();
    }

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
                // 목록에서 골라도 input 이벤트를 내보내야 ② 입원 진행의 미러 칸(data-mirror)이 따라온다
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
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

    // 나이 보조 입력 — 보호자가 "44년생", "만 79세"처럼 말해도 저장용 나이로 환산
    const ageInput = form.querySelector('#patient-age');
    const ageMode = form.querySelector('#age-input-mode');
    const ageRawInput = form.querySelector('#age-raw-input');
    const ageHint = form.querySelector('#age-hint');
    const consultDateInput = form.querySelector('#consult-date-real');
    if (ageInput && ageMode && ageRawInput && ageHint) {
        const currentYear = () => {
            const raw = consultDateInput && consultDateInput.value ? consultDateInput.value.slice(0, 4) : '';
            const y = Number(raw);
            return y >= 1900 ? y : new Date().getFullYear();
        };
        const setAgeHint = (text, tone = '') => {
            ageHint.textContent = text || '';
            ageHint.className = 'age-hint' + (tone ? ' ' + tone : '');
        };
        const readAgeNumber = (value) => {
            const match = String(value || '').match(/\d{1,4}/);
            return match ? Number(match[0]) : null;
        };
        const normalizeBirthYear = (value, year) => {
            const raw = String(value || '').trim();
            const match = raw.match(/\d{1,4}/);
            if (!match) return null;
            const digits = match[0];
            let birthYear = Number(digits);
            if (digits.length <= 2) {
                birthYear = 1900 + birthYear;
            }
            if (birthYear > year) return null;
            return birthYear;
        };
        const applyAgeHelper = () => {
            const mode = ageMode.value;
            const raw = ageRawInput.value.trim();
            if (!raw) {
                setAgeHint('');
                return;
            }
            const year = currentYear();
            if (mode === 'birth_year') {
                const birthYear = normalizeBirthYear(raw, year);
                if (!birthYear) {
                    setAgeHint('출생년도를 확인하세요. 예: 1944 또는 44', 'warn');
                    return;
                }
                const age = year - birthYear;
                if (age < 0 || age > 120) {
                    setAgeHint('계산된 나이가 범위를 벗어났습니다.', 'warn');
                    return;
                }
                ageInput.value = age;
                const fullAgeMin = Math.max(age - 1, 0);
                const yyHint = raw.match(/^\D*\d{1,2}\D*$/)
                    ? ' 두 자리 연도는 1900년대로 계산합니다.'
                    : '';
                setAgeHint(`${birthYear}년생 → 저장 나이 ${age}세 (만 ${fullAgeMin}~${age}세 추정).${yyHint}`);
                return;
            }
            const value = readAgeNumber(raw);
            if (value === null || value < 0 || value > 120) {
                setAgeHint('나이를 0~120 사이로 입력하세요.', 'warn');
                return;
            }
            if (mode === 'full_age') {
                const displayAge = value + 1;
                if (displayAge > 120) {
                    setAgeHint('만 나이를 상담용 나이로 환산하면 120세를 초과합니다.', 'warn');
                    return;
                }
                ageInput.value = displayAge;
                setAgeHint(`만 ${value}세로 들음 → 상담용 나이 ${displayAge}세로 저장`);
            } else {
                ageInput.value = value;
                setAgeHint(`${value}세로 저장`);
            }
        };
        ageMode.addEventListener('change', applyAgeHelper);
        ageRawInput.addEventListener('input', applyAgeHelper);
        if (consultDateInput) consultDateInput.addEventListener('change', applyAgeHelper);
    }

    // 자동완성: 환자명, 모병원, 질환
    document.querySelectorAll('input[data-ac]').forEach(setupAutocomplete);

    // 병원·요양원 정식명 강제 — 자동완성 마스터 매칭. 자동완성 클릭으로 인한 blur는
    // pickItem이 먼저 dataset.hospVerified를 세팅하므로 setTimeout으로 우선순위 보장.
    // 요양원 마스터가 비어 있으면 enforce는 자유 입력 허용으로 통과.
    const HOSPITAL_NAME_INPUTS = [
        'consultation.current_location_name',
        'consultation.current_nursing_name',
        'consultation.referrer_institution',
    ];
    HOSPITAL_NAME_INPUTS.forEach(nm => {
        const inp = form.querySelector(`[name="${nm}"]`);
        if (!inp) return;
        inp.addEventListener('blur', () => {
            setTimeout(() => enforceHospitalOfficial(inp), 150);
        });
    });

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
        // input의 부모 안에 .ac-list가 있으면 그걸 우선 사용 (한 페이지에 같은 kind가
        // 여러 칸인 경우 — 모병원·추천기관 둘 다 hospital — dropdown 충돌 방지).
        // 없으면 글로벌 #ac-{kind} fallback.
        const list = input.parentElement.querySelector(':scope > .ac-list')
                  || document.getElementById('ac-' + kind);
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
            if (kind === 'hospital' || kind === 'nursing') {
                // 같은 이름·약칭 후보 식별을 위해 region·kind를 함께 표시
                const meta = [it.region, it.kind].filter(Boolean).join(' · ');
                // 통칭(법인·재단 접두어 뗀 이름)을 굵게, 공식명은 작게 — 상담사가 아는 이름으로 고르게
                const short = it.short_name && it.short_name !== it.name;
                return `<div class="ac-item">
                    <strong>${escHpw(short ? it.short_name : it.name)}</strong>
                    ${short ? `<span class="meta">${escHpw(it.name)}</span>` : ''}
                    ${meta ? `<span class="meta">${escHpw(meta)}</span>` : ''}
                </div>`;
            }
            return `<div class="ac-item">${it.name}</div>`;
        }
        function pickItem(it) {
            input.value = it.name;
            if (kind === 'patient') autoFillPatient(it);
            else if (kind === 'hospital' || kind === 'nursing') markHospitalOfficial(input, it.name);
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
        hospHelpHide(input);
    }

    // ── 마스터에 없는 병원 — 입력 칸 바로 아래에서 해결 (2026-09-15) ──
    // 기관협력 화면까지 가지 않고 [심평원에서 찾기]로 후보를 골라 등록하거나, 심평원에도 없으면 [이 이름으로 등록].
    function hospHelpBox(input) {
        if (input._hospHelp) return input._hospHelp;
        const box = document.createElement('div');
        box.className = 'hosp-help';
        box.hidden = true;
        // 자동완성 목록(.ac-list)이 input 바로 뒤에 absolute로 붙어 있으면 그 뒤에 둔다
        const anchor = (input.nextElementSibling && input.nextElementSibling.classList.contains('ac-list')) ? input.nextElementSibling : input;
        anchor.insertAdjacentElement('afterend', box);
        input._hospHelp = box;
        return box;
    }
    function hospHelpHide(input) { if (input._hospHelp) { input._hospHelp.hidden = true; input._hospHelp.innerHTML = ''; } }
    function hospAccept(input, name) {
        input.value = name;
        markHospitalOfficial(input, name);
        hospHelpHide(input);
    }
    function hospCandidateRow(input, it, onPick) {
        const row = document.createElement('div');
        row.className = 'hosp-help-row';
        const meta = [it.kind, it.region, it.address].filter(Boolean).join(' · ');
        row.innerHTML = `<span><b></b><small></small></span>`;
        row.querySelector('b').textContent = it.name;
        row.querySelector('small').textContent = meta;
        const btn = document.createElement('button');
        btn.type = 'button'; btn.className = 'btn btn-secondary btn-sm'; btn.textContent = '선택';
        btn.addEventListener('click', () => onPick(it, btn));
        row.appendChild(btn);
        return row;
    }
    async function hospRegister(input, entry, btn) {
        if (btn) btn.disabled = true;
        try {
            const res = await api.post('/api/hospital/register', entry);
            hospAccept(input, res.name);
            toast(entry.official_code ? `심평원 명부에서 등록했습니다: ${res.name}` : `입력한 이름으로 등록했습니다: ${res.name}`, 'success');
        } catch (e) {
            toast(`등록 실패: ${e.message}`, 'error');
            if (btn) btn.disabled = false;
        }
    }
    function hospHelpShow(input, raw, candidates, reason) {
        const box = hospHelpBox(input);
        box.innerHTML = '';
        box.hidden = false;
        const head = document.createElement('div');
        head.className = 'hosp-help-head';
        head.textContent = reason;
        box.appendChild(head);
        // 마스터 부분일치 후보가 있으면 그 자리에서 고른다(자동완성 목록으로 돌아갈 필요 없음)
        (candidates || []).slice(0, 5).forEach(it => box.appendChild(hospCandidateRow(input, it, () => hospAccept(input, it.name))));
        const actions = document.createElement('div');
        actions.className = 'hosp-help-actions';
        const lookupBtn = document.createElement('button');
        lookupBtn.type = 'button'; lookupBtn.className = 'btn btn-primary btn-sm'; lookupBtn.textContent = '심평원에서 찾기';
        lookupBtn.addEventListener('click', () => hospLookup(input, raw, lookupBtn));
        const manualBtn = document.createElement('button');
        manualBtn.type = 'button'; manualBtn.className = 'btn btn-secondary btn-sm'; manualBtn.textContent = `"${raw}" 이름 그대로 등록`;
        manualBtn.addEventListener('click', () => {
            if (!confirm(`"${raw}"을(를) 병원 마스터에 그대로 등록할까요?\n심평원 정식명과 다르면 통계에서 따로 집계될 수 있습니다. 먼저 [심평원에서 찾기]를 권합니다.`)) return;
            hospRegister(input, { name: raw }, manualBtn);
        });
        actions.appendChild(lookupBtn); actions.appendChild(manualBtn);
        box.appendChild(actions);
    }
    async function hospLookup(input, raw, btn) {
        const box = hospHelpBox(input);
        btn.disabled = true; btn.textContent = '심평원 조회 중…';
        let res;
        try {
            const r = await fetch(`/api/hospital/lookup?q=${encodeURIComponent(raw)}`);
            res = await r.json();
        } catch (e) { res = { items: [], error: '조회 실패' }; }
        btn.disabled = false; btn.textContent = '심평원에서 찾기';
        let list = box.querySelector('.hosp-help-lookup');
        if (!list) { list = document.createElement('div'); list.className = 'hosp-help-lookup'; box.insertBefore(list, box.querySelector('.hosp-help-actions')); }
        list.innerHTML = '';
        if (res.configured === false) {
            list.textContent = '심평원 API 키가 설정되지 않아 조회할 수 없습니다. 관리자에게 알리거나 이름 그대로 등록하세요.';
            return;
        }
        if (res.error) { list.textContent = res.error; return; }
        if (!res.items || !res.items.length) {
            list.textContent = `심평원 명부에 "${raw}"이(가) 없습니다. 검색어를 줄여 다시 찾거나(예: 대표 두세 글자), 이름 그대로 등록하세요.`;
            return;
        }
        const title = document.createElement('div'); title.className = 'hosp-help-head'; title.textContent = `심평원 명부 ${res.items.length}곳 — 맞는 곳을 선택하면 바로 등록됩니다`;
        list.appendChild(title);
        res.items.forEach(it => list.appendChild(hospCandidateRow(input, it, (item, b) => hospRegister(input, item, b))));
    }
    async function enforceHospitalOfficial(input) {
        const raw = (input.value || '').trim();
        if (!raw) { clearHospitalMark(input); return; }
        if (input.dataset.hospVerified === raw) return;
        // 어떤 마스터를 쓸지 input의 data-ac에 따라 결정. nursing 마스터가 비어 있으면
        // 자유 입력 허용(아직 사용자가 요양원 마스터 데이터를 import하지 않은 단계).
        const acKind = input.dataset.ac === 'nursing' ? 'nursing' : 'hospital';
        let res = {};
        try {
            res = await api.get(`/api/autocomplete/${acKind}?q=${encodeURIComponent(raw)}`);
        } catch (e) { return; /* 네트워크 오류 시 차단 안 함 */ }
        if ((res.master_size || 0) === 0) {
            // 마스터 비어 있음 — 사용자가 데이터를 적재하기 전엔 자유 입력 허용
            clearHospitalMark(input);
            return;
        }
        const items = res.items || [];
        // 공식명 정확일치가 최우선, 없으면 법인·재단 접두어를 뗀 통칭 정확일치(exact 플래그) — '서울아산병원'→'재단법인아산사회복지재단 서울아산병원'
        const exact = items.find(it => it.name === raw) || items.find(it => it.exact);
        if (exact) {
            if (exact.name !== raw) { input.value = exact.name; toast(`정식 명칭으로 자동 변환: ${raw} → ${exact.name}`, 'info'); }
            markHospitalOfficial(input, exact.name); return;
        }
        if (items.length === 1) {
            const official = items[0].name;
            input.value = official;
            markHospitalOfficial(input, official);
            toast(`정식 명칭으로 자동 변환: ${raw} → ${official}`, 'info');
            return;
        }
        if (items.length > 1) {
            markHospitalInvalid(input, '정식 명칭을 아래에서 선택하세요.');
            if (acKind === 'hospital') hospHelpShow(input, raw, items, `"${raw}"와 비슷한 병원이 여러 곳입니다 — 맞는 곳을 선택하세요. 없으면 심평원에서 찾거나 그대로 등록할 수 있습니다.`);
            return;
        }
        const facLabel = acKind === 'nursing' ? '요양원' : '병원';
        if (acKind === 'hospital') {
            // 전국 명부(심평원 4만여 곳)가 아직 안 들어온 상태면 "없는 병원"이 아니라 "명부 미적재"가 원인이다(2026-09-15 서울아산병원 보고).
            const sparse = (res.master_size || 0) < 1000;
            markHospitalInvalid(input, '마스터에 없는 병원입니다. 아래 버튼으로 바로 등록하세요.');
            hospHelpShow(input, raw, [], sparse
                ? `전국 병원 명부가 아직 적재되지 않아 등록된 ${res.master_size}곳에서만 찾습니다. [심평원에서 찾기]로 바로 등록할 수 있습니다.`
                : `"${raw}"은(는) 병원 마스터에 없습니다. 심평원에서 찾아 정식명으로 등록하거나, 이름 그대로 등록하세요.`);
            return;
        }
        markHospitalInvalid(input, `마스터에 없는 ${facLabel}입니다. 정확한 이름을 입력하거나 관리자에게 등록을 요청하세요.`);
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
            // 보험유형은 복수 체크박스(name[]) — ', '로 이어진 저장값을 각각 체크
            const boxes = form.querySelectorAll(`[name="${name}[]"]`);
            if (boxes.length) {
                const vals = String(val).split(', ');
                boxes.forEach(b => { if (vals.includes(b.value)) b.checked = true; });
                return;
            }
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
        clearTimeout(draftTimer);
        // 하단 저장 버튼 + 상단 우측 저장 버튼을 함께 잠금/해제
        const saveBtns = ['save-btn', 'save-btn-top'].map(id => document.getElementById(id)).filter(Boolean);
        const btn = { set disabled(v) { saveBtns.forEach(b => { b.disabled = v; }); } };
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
        // 상담 결과 ② 입원예정 — 예정일·주치의·병실 필수 (2026-09-16 규칙: 재원 데이터 정확성). 서버도 같은 규칙으로 막는다.
        if (st === '입원예정') {
            const need = [['consultation.planned_admission_date', '입원예정일'],
                          ['consultation.attending_doctor', '주치의'],
                          ['consultation.room_number', '병실']];
            for (const [nm, label] of need) {
                const inp = form.querySelector(`[name="${nm}"]`);
                if (!inp || !inp.value.trim()) {
                    toast(`입원예정에는 ${label}이(가) 필요합니다. 상단 헤더 칸을 채워주세요.`, 'error');
                    if (inp) inp.focus();
                    btn.disabled = false; return;
                }
            }
        }
        // 병원·요양원 정식명 최종 강제 — 세 칸 모두 검증.
        // 자유 입력 후 blur 없이 바로 submit한 케이스 대응. 요양원은 마스터 비어 있으면 통과.
        const HOSP_FIELD_LABELS = {
            'consultation.current_location_name': '모병원',
            'consultation.current_nursing_name': '요양원',
            'consultation.referrer_institution': '추천 기관',
        };
        for (const [nm, label] of Object.entries(HOSP_FIELD_LABELS)) {
            const inp = form.querySelector(`[name="${nm}"]`);
            if (!inp || !inp.value.trim()) continue;
            await enforceHospitalOfficial(inp);
            if (inp.classList.contains('hosp-invalid')) {
                toast(`${label} 칸이 마스터에 없습니다. 칸 아래에서 선택하거나 [심평원에서 찾기]로 등록하세요.`, 'error');
                inp.focus();
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
            if (activeDraftId) {
                await fetch(`/api/consult-drafts/${activeDraftId}`, {method:'DELETE'}).catch(() => {});
            }
            if (draftKey) { sessionStorage.removeItem(draftKey); sessionStorage.removeItem(legacyDraftKey); }
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
        return {
            label: days <= matched ? '회복기' : '비회복기',
            days,
            period: matched,
            daysLeft: matched - days,
            dueDate: formatDate(addDays(od, matched)),
        };
    }

    function addDays(date, days) {
        const d = new Date(date.getTime());
        d.setDate(d.getDate() + days);
        return d;
    }

    function formatDate(date) {
        const y = date.getFullYear();
        const m = String(date.getMonth() + 1).padStart(2, '0');
        const d = String(date.getDate()).padStart(2, '0');
        return `${y}-${m}-${d}`;
    }

    function formatRecoveryDday(result) {
        if (!result) return '';
        if (result.daysLeft >= 0) return `D-${result.daysLeft} 남음`;
        return `D+${Math.abs(result.daysLeft)} 초과`;
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
        const dday = formatRecoveryDday(result);
        setHint(`※ 자동 판정: ${recCode} · ${dday} · 기준일 ${result.dueDate}까지`,
            result.label === '회복기' ? 'rh-yes' : 'rh-no');
        setMeta(`입원시기 기준일 ${result.dueDate}까지 · ${dday} / 발병·수술 후 ${result.days}일 경과 / 인정 기간 ${result.period}일`);
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

    // ─── 입원예정일·시간 미러 칸(② 입원 진행 안) ↔ 헤더 칸 양방향 동기화 ───
    // 입원예정을 고른 자리에서 바로 날짜·시간을 적게 한다. 저장은 헤더 칸(name 있음) 하나로만 나간다.
    form.querySelectorAll('[data-mirror]').forEach(mirror => {
        const src = form.querySelector(`[name="${mirror.dataset.mirror}"]`);
        if (!src) return;
        mirror.value = src.value;
        mirror.addEventListener('input', () => { src.value = mirror.value; src.dispatchEvent(new Event('input', { bubbles: true })); });
        mirror.addEventListener('change', () => { src.value = mirror.value; src.dispatchEvent(new Event('change', { bubbles: true })); });
        src.addEventListener('input', () => { if (mirror.value !== src.value) mirror.value = src.value; });
        src.addEventListener('change', () => { if (mirror.value !== src.value) mirror.value = src.value; });
    });

    // ─── 병실 배정 충돌 확인 — 재원 현황(명부)·다른 입원예정과 맞춰 보고 경고 ───
    (function() {
        const roomEl = form.querySelector('input[name="consultation.room_number"]');
        if (!roomEl) return;
        // 경고 상자는 헤더 호실 칸 아래와 ② 입원 진행의 병실 미러 칸 아래 양쪽에 같은 내용으로
        const boxes = [];
        [roomEl, form.querySelector('[data-mirror="consultation.room_number"]')].filter(Boolean).forEach(el => {
            const b = document.createElement('div');
            b.className = 'room-conflict'; b.hidden = true;
            el.insertAdjacentElement('afterend', b);
            boxes.push(b);
        });
        const box = {
            set hidden(v) { boxes.forEach(b => { b.hidden = v; }); },
            set innerHTML(h) { boxes.forEach(b => { b.innerHTML = h; }); },
            classList: {
                toggle(c, on) { boxes.forEach(b => b.classList.toggle(c, on)); },
                remove(c) { boxes.forEach(b => b.classList.remove(c)); },
            },
            addEventListener(ev, fn) { boxes.forEach(b => b.addEventListener(ev, fn)); },
        };
        const cid = (location.pathname.match(/^\/consult\/(\d+)\/edit/) || [])[1];
        let seq = 0;
        const mirrorRoom = form.querySelector('[data-mirror="consultation.room_number"]');
        function setInvalid(on) { [roomEl, mirrorRoom].filter(Boolean).forEach(el => el.classList.toggle('room-invalid', on)); }
        async function check() {
            const room = roomEl.value.trim();
            const my = ++seq;
            if (!room) { box.hidden = true; setInvalid(false); return; }
            const g = form.querySelector('input[name="patient.gender"]:checked');
            let res;
            try {
                res = await api.get(`/api/room-status?room=${encodeURIComponent(room)}&gender=${g ? g.value : ''}${cid ? '&exclude=' + cid : ''}`);
            } catch (e) { return; }
            if (my !== seq) return;
            if (!res.known) { box.hidden = true; setInvalid(false); return; }
            const who = res.residents.map(r => `${r.name}${r.gender === 'M' ? '(남)' : r.gender === 'F' ? '(여)' : ''}`).join(', ');
            const conflict = res.conflicts.length > 0;
            const reasons = res.conflicts.map(c => ({
                full: `만실 — ${res.used}/${res.capacity}명`,
                gender: `성별 다름 — 지금 ${res.genders.map(x => x === 'M' ? '남' : '여').join('·')} 환자 방`,
                planned: `다른 입원예정 ${res.planned.length}명과 겹쳐 정원 초과 예상`,
            })[c]);
            box.innerHTML = conflict
                ? `<b>⚠ ${res.room} 배정 충돌</b> ${reasons.join(' · ')}<br>` +
                  `<small>재원 ${res.used}/${res.capacity}${who ? ' · ' + who : ''}${res.planned.length ? ' · 입원예정 ' + res.planned.map(p => `${p.name}${p.date ? '(' + p.date.slice(5) + ')' : ''}`).join(', ') : ''}</small>` +
                  `<span class="room-conflict-actions"><button type="button" data-act="retry">병실 다시 정하기</button><button type="button" data-act="keep">조율 완료, 그대로 둠</button></span>`
                : `<small>✓ ${res.room} 재원 ${res.used}/${res.capacity} · 빈 병상 ${res.free}${who ? ' · ' + who : ''}</small>`;
            box.classList.toggle('is-conflict', conflict);
            setInvalid(conflict);
            box.hidden = false;
        }
        box.addEventListener('click', e => {
            const b = e.target.closest('[data-act]'); if (!b) return;
            if (b.dataset.act === 'retry') {
                roomEl.value = ''; roomEl.dispatchEvent(new Event('input', { bubbles: true }));   // 미러 칸도 비움
                box.hidden = true; setInvalid(false);
                const near = e.currentTarget.previousElementSibling;   // 누른 경고 바로 위의 칸에 포커스
                (near && near.tagName === 'INPUT' ? near : roomEl).focus();
            }
            else { setInvalid(false); box.classList.remove('is-conflict'); box.innerHTML = `<small>조율 완료 — ${roomEl.value.trim()} 그대로 둡니다.</small>`; }
        });
        roomEl.addEventListener('change', check);
        form.querySelectorAll('input[name="patient.gender"]').forEach(r => r.addEventListener('change', () => { if (roomEl.value.trim()) check(); }));
        if (roomEl.value.trim()) check();
    })();

    // ─── 상담 결과 ② 입원 진행: 입원예정/보류/취소/완료에 따른 부가칸 토글 ───
    (function() {
        const statusRadios = form.querySelectorAll('input[name="consultation.admission_status"]');
        if (!statusRadios.length) return;
        const holdRow = document.getElementById('status-reason-hold');
        const cancelRow = document.getElementById('status-reason-cancel');
        const completedRow = document.getElementById('status-extra-completed');
        const plannedRow = document.getElementById('status-extra-planned');
        const waitingRow = document.getElementById('status-extra-waiting');
        const plannedDateEl = form.querySelector('input[name="consultation.planned_admission_date"]');
        function refresh(triggered) {
            const c = form.querySelector('input[name="consultation.admission_status"]:checked');
            const s = c ? c.value : '';
            if (holdRow) holdRow.hidden = (s !== '입원보류');
            if (cancelRow) cancelRow.hidden = (s !== '입원취소');
            if (completedRow) completedRow.hidden = (s !== '입원완료');
            if (plannedRow) plannedRow.hidden = (s !== '입원예정');
            if (waitingRow) waitingRow.hidden = (s !== '입원대기');
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
