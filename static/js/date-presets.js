// 기간 범위 입력이 있는 화면에 공통 빠른 조회 선택기를 붙인다.
(() => {
    const pairs = [['from', 'to'], ['admission_from', 'admission_to'], ['discharge_from', 'discharge_to']];
    const pad = n => String(n).padStart(2, '0');
    const iso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const day = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
    const today = new Date(); today.setHours(12, 0, 0, 0);
    const range = (start, end) => [iso(start), iso(end)];
    const monthRange = (year, month) => range(new Date(year, month, 1, 12), new Date(year, month + 1, 0, 12));
    const year = today.getFullYear(), month = today.getMonth();
    const monday = day(today, -((today.getDay() + 6) % 7));
    const lastMonday = day(monday, -7);
    const quarter = Math.floor(month / 3);
    const sections = [
        { title: '자주 사용', items: [
            ['오늘', () => range(today, today)], ['전일', () => range(day(today, -1), day(today, -1))],
            ['이번 주', () => range(monday, today)], ['지난주', () => range(lastMonday, day(lastMonday, 6))],
            ['이번 달', () => monthRange(year, month)], ['지난달', () => monthRange(year, month - 1)],
            ['올해', () => range(new Date(year, 0, 1, 12), new Date(year, 11, 31, 12))],
            ['전년도', () => range(new Date(year - 1, 0, 1, 12), new Date(year - 1, 11, 31, 12))],
        ]},
        { title: '분기·반기', items: [
            ['이번 분기', () => range(new Date(year, quarter * 3, 1, 12), new Date(year, quarter * 3 + 3, 0, 12))],
            ...[0, 1, 2, 3].map(q => [`${q + 1}분기`, () => range(new Date(year, q * 3, 1, 12), new Date(year, q * 3 + 3, 0, 12))]),
            ['상반기', () => range(new Date(year, 0, 1, 12), new Date(year, 5, 30, 12))],
            ['하반기', () => range(new Date(year, 6, 1, 12), new Date(year, 11, 31, 12))],
        ]},
        { title: `${year}년 월별`, items: Array.from({length: 12}, (_, i) => [`${i + 1}월`, () => monthRange(year, i)]) },
    ];
    function closeAll(except) {
        document.querySelectorAll('.date-preset-panel').forEach(p => { if (p !== except) p.hidden = true; });
        document.querySelectorAll('.date-preset-btn').forEach(b => { const p = b.parentElement?.querySelector('.date-preset-panel'); if (p !== except) b.setAttribute('aria-expanded', 'false'); });
    }
    function mount(form, start, end, index) {
        if (form.dataset.datePresets === 'off') return;
        const anchor = document.createElement('span'); anchor.className = 'date-preset-anchor';
        const button = document.createElement('button'); button.type = 'button'; button.className = 'date-preset-btn';
        button.innerHTML = '<span>▣</span> 빠른 기간 <i>▾</i>'; button.setAttribute('aria-expanded', 'false');
        const panel = document.createElement('div'); panel.className = 'date-preset-panel'; panel.id = `date-preset-${index}`; panel.hidden = true;
        sections.forEach(section => {
            const block = document.createElement('section'), heading = document.createElement('b'), choices = document.createElement('div');
            heading.textContent = section.title; choices.className = 'date-preset-choices'; block.append(heading, choices);
            section.items.forEach(([label, getRange]) => {
                const choice = document.createElement('button'); choice.type = 'button'; choice.textContent = label;
                choice.addEventListener('click', () => {
                    [start.value, end.value] = getRange();
                    [start, end].forEach(el => { el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); });
                    panel.hidden = true; button.setAttribute('aria-expanded', 'false');
                });
                choices.appendChild(choice);
            }); panel.appendChild(block);
        });
        anchor.append(button, panel);
        const shared = end.closest('.date-range,.wd-filter-range');
        if (shared && shared.contains(start)) shared.appendChild(anchor);
        else { const label = end.closest('label'); if (label) label.insertAdjacentElement('afterend', anchor); else end.insertAdjacentElement('afterend', anchor); }
        button.addEventListener('click', e => { e.stopPropagation(); const opening = panel.hidden; closeAll(opening ? panel : null); panel.hidden = !opening; button.setAttribute('aria-expanded', String(opening)); });
        panel.addEventListener('click', e => e.stopPropagation());
    }
    let index = 0;
    document.querySelectorAll('form').forEach(form => pairs.forEach(([a, b]) => {
        const start = form.querySelector(`input[type="date"][name="${a}"]`), end = form.querySelector(`input[type="date"][name="${b}"]`);
        if (start && end) mount(form, start, end, ++index);
    }));
    document.addEventListener('click', () => closeAll());
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAll(); });
})();
