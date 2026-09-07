(() => {
    const dialog = document.getElementById('partner-modal'), frame = document.getElementById('partner-modal-frame');
    let dirty = false;
    document.addEventListener('click', event => {
        const link = event.target.closest('a[data-partner-modal]');
        if (!link || event.ctrlKey || event.metaKey || event.shiftKey) return;
        event.preventDefault();
        const url = new URL(link.href); url.searchParams.set('embedded', '1');
        frame.src = url.href; dirty = false; dialog.showModal();
    });
    document.getElementById('partner-modal-close')?.addEventListener('click', () => dialog.close());
    dialog?.addEventListener('close', () => { frame.src = 'about:blank'; if (dirty) location.reload(); });
    window.addEventListener('message', event => {
        if (event.origin !== location.origin || event.source !== frame.contentWindow) return;
        if (event.data?.type === 'cooperation-saved') dirty = true;
    });
    const form = document.getElementById('institution-search');
    if (!form) return;
    const results = document.getElementById('institution-results'), status = document.getElementById('institution-search-status');
    const more = document.getElementById('institution-more'), add = document.getElementById('institution-add-form');
    let controller, offset = 0, timer;
    async function search(append = false) {
        clearTimeout(timer); controller?.abort(); controller = new AbortController();
        if (!append) { results.replaceChildren(); offset = 0; add.hidden = true; }
        more.hidden = true;
        if (!form.elements.q.value.trim()) { status.textContent = '기관명 또는 지역을 입력해주세요.'; return; }
        status.textContent = '검색 중…';
        const args = new URLSearchParams(new FormData(form)); args.set('offset', offset);
        try {
            const response = await fetch('/partners/search?' + args, {signal: controller.signal});
            if (!response.ok || !response.headers.get('Content-Type')?.includes('application/json')) throw new Error();
            const data = await response.json();
            data.items.forEach(item => {
                const row = document.createElement('div'); row.className = 'coop-search-result';
                const info = document.createElement('div'), title = document.createElement('strong'), meta = document.createElement('p');
                title.textContent = item.name; meta.textContent = [item.kind, item.region, item.address].filter(Boolean).join(' · '); info.append(title, meta); row.append(info);
                if (item.partner_id) {
                    const link = document.createElement('a'); link.href = '/partners/' + item.partner_id; link.textContent = '등록됨 · 상세'; link.dataset.partnerModal = ''; row.append(link);
                } else {
                    const select = document.createElement('button'); select.type = 'button'; select.className = 'btn btn-secondary'; select.textContent = '선택';
                    select.addEventListener('click', () => {
                        add.elements.hospital.value = item.name; add.elements.hospital_id.value = item.id;
                        document.getElementById('institution-selected').textContent = [item.name, item.address].filter(Boolean).join(' · ');
                        add.hidden = false; add.scrollIntoView({behavior:'smooth', block:'nearest'});
                    }); row.append(select);
                }
                results.append(row);
            });
            offset += data.items.length; more.hidden = !data.more;
            status.textContent = offset ? `${offset}개 결과 표시${data.more ? ' · 더 보기로 계속 검색' : ''}` : '일치하는 기관이 없습니다. 지역·종별 조건을 줄이거나 병원명의 일부로 검색해보세요.';
        } catch (error) { if (error.name !== 'AbortError') status.textContent = '검색하지 못했습니다. 로그인 상태를 확인하고 다시 시도해주세요.'; }
    }
    form.addEventListener('submit', event => { event.preventDefault(); search(); });
    form.elements.q.addEventListener('input', () => { clearTimeout(timer); controller?.abort(); add.hidden = true; timer = setTimeout(() => search(), 300); });
    form.querySelectorAll('select').forEach(select => select.addEventListener('change', () => search()));
    more.addEventListener('click', () => search(true));
    document.getElementById('institution-cancel').addEventListener('click', () => { add.hidden = true; });
})();
