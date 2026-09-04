// 개인 할 일 — 달력/목록 공용. 추가·수정 폼 패널 + 완료·이월·삭제 (fetch 후 새로고침).
(function () {
    'use strict';

    function post(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(body || {}),
        }).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (d) {
                if (!r.ok) throw new Error(d.error || ('오류 (' + r.status + ')'));
                return d;
            });
        });
    }
    function fail(e) { alert(e.message || '처리 중 오류가 발생했습니다.'); }
    function reload() { location.reload(); }

    var panel = document.getElementById('todo-form-panel');       // 우측 드로어
    var backdrop = document.getElementById('todo-drawer-backdrop');
    var form = document.getElementById('todo-form');
    var formTitle = document.getElementById('todo-form-title');
    var delBtn = document.getElementById('todo-form-del');
    var newBtn = document.getElementById('todo-new-btn');
    var today = newBtn ? newBtn.dataset.today : '';

    function openDrawer() {
        if (!panel) return;
        panel.classList.add('open');
        panel.setAttribute('aria-hidden', 'false');
        if (backdrop) backdrop.hidden = false;
    }
    function closeDrawer() {
        if (!panel) return;
        panel.classList.remove('open');
        panel.setAttribute('aria-hidden', 'true');
        if (backdrop) backdrop.hidden = true;
    }

    function showAdd(dateStr) {
        if (!form) return;
        form.reset();
        form.dataset.id = '';
        form.due_date.value = dateStr || today || '';
        form.end_date.value = '';
        form.start_time.value = '';
        form.end_time.value = '';
        form.progress.value = 0;
        form.dday.checked = false;
        form.querySelectorAll('[name="share_user_ids"]').forEach(function (x) { x.checked = false; });
        if (formTitle) formTitle.textContent = 'ToDo 추가';
        if (delBtn) delBtn.hidden = true;
        openDrawer();
        setTimeout(function () { form.title.focus(); }, 60);
    }

    function showEdit(el) {
        if (!form) return;
        var d = el.dataset;
        if (d.owner === '0') return;
        form.dataset.id = d.id;
        form.title.value = d.title || '';
        form.due_date.value = d.due || '';
        form.end_date.value = d.end || '';
        form.start_time.value = d.stime || '';
        form.end_time.value = d.etime || '';
        form.progress.value = d.progress || 0;
        form.dday.checked = (d.dday === '1');
        form.remind_at.value = (d.remind || '').slice(0, 16);
        form.note.value = d.note || '';
        var shared = (d.shares || '').split(',');
        form.querySelectorAll('[name="share_user_ids"]').forEach(function (x) {
            x.checked = shared.indexOf(x.value) !== -1;
        });
        if (formTitle) formTitle.textContent = 'ToDo 수정';
        if (delBtn) delBtn.hidden = false;
        openDrawer();
    }

    if (newBtn) newBtn.addEventListener('click', function () { showAdd(); });
    if (newBtn && newBtn.dataset.autoOpen === '1') {
        showAdd(newBtn.dataset.selectedDate || today);
    }
    ['todo-form-cancel', 'todo-form-cancel2'].forEach(function (id) {
        var el = document.getElementById(id);
        if (el) el.addEventListener('click', closeDrawer);
    });
    if (backdrop) backdrop.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && panel && panel.classList.contains('open')) closeDrawer();
    });

    if (form) form.addEventListener('submit', function (e) {
        e.preventDefault();
        var title = form.title.value.trim();
        if (!title) return;
        var body = {
            title: title,
            due_date: form.due_date.value || today,
            end_date: form.end_date.value || '',
            start_time: form.start_time.value || '',
            end_time: form.end_time.value || '',
            progress: form.progress.value || 0,
            dday: form.dday.checked ? '1' : '0',
            remind_at: form.remind_at.value || '',
            note: form.note.value.trim(),
            share_user_ids: Array.from(form.querySelectorAll('[name="share_user_ids"]:checked')).map(function (x) { return x.value; }),
        };
        var id = form.dataset.id;
        post(id ? '/api/todos/' + id : '/api/todos', body).then(reload).catch(fail);
    });

    if (delBtn) delBtn.addEventListener('click', function () {
        var id = form.dataset.id;
        if (!id) return;
        if (!confirm('이 할 일을 삭제할까요?')) return;
        post('/api/todos/' + id + '/delete', {}).then(reload).catch(fail);
    });

    // ── 달력: 날짜 칸 클릭=추가, 항목 클릭=수정 ──
    document.querySelectorAll('.todo-cell').forEach(function (cell) {
        cell.addEventListener('click', function (e) {
            var todoEl = e.target.closest('.tc-todo');
            if (todoEl) { showEdit(todoEl); return; }
            showAdd(cell.dataset.date);
        });
    });

    // ── 목록: 완료 토글 / 이월 / 삭제 / 클릭 수정 ──
    document.querySelectorAll('.todo-item').forEach(function (li) {
        var id = li.dataset.id;
        var check = li.querySelector('[data-toggle]');
        if (check) check.addEventListener('click', function (e) {
            e.stopPropagation();
            var makeDone = !li.classList.contains('todo-done');
            post('/api/todos/' + id + '/toggle', { done: makeDone ? '1' : '0' }).then(reload).catch(fail);
        });
        var carry = li.querySelector('[data-carry]');
        if (carry) carry.addEventListener('click', function (e) {
            e.stopPropagation();
            post('/api/todos/' + id + '/carry', {}).then(reload).catch(fail);
        });
        var del = li.querySelector('[data-del]');
        if (del) del.addEventListener('click', function (e) {
            e.stopPropagation();
            if (!confirm('이 할 일을 삭제할까요?')) return;
            post('/api/todos/' + id + '/delete', {}).then(reload).catch(fail);
        });
        var open = li.querySelector('.todo-openedit');
        if (open) open.addEventListener('click', function () { showEdit(li); });
    });
})();
