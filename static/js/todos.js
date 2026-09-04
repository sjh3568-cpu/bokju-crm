// 상담사 개인 할 일 — 추가·완료·이월·삭제·수정 (fetch 기반, 처리 후 새로고침).
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

    // ── 추가 ──
    var addForm = document.getElementById('todo-add');
    if (addForm) {
        addForm.addEventListener('submit', function (e) {
            e.preventDefault();
            var title = addForm.title.value.trim();
            if (!title) return;
            var noteEl = document.getElementById('todo-add-note');
            post('/api/todos', {
                title: title,
                due_date: addForm.dataset.date,
                remind_at: addForm.remind_at.value || '',
                note: noteEl ? noteEl.value.trim() : '',
            }).then(function () { location.reload(); }).catch(fail);
        });
    }

    // ── 항목별 동작 (완료·이월·삭제·수정) ──
    document.querySelectorAll('.todo-item').forEach(function (li) {
        var id = li.dataset.id;

        var check = li.querySelector('[data-toggle]');
        if (check) check.addEventListener('click', function () {
            var makeDone = !li.classList.contains('todo-done');
            post('/api/todos/' + id + '/toggle', { done: makeDone ? '1' : '0' })
                .then(function () { location.reload(); }).catch(fail);
        });

        var carry = li.querySelector('[data-carry]');
        if (carry) carry.addEventListener('click', function () {
            post('/api/todos/' + id + '/carry', {})
                .then(function () { location.reload(); }).catch(fail);
        });

        var del = li.querySelector('[data-del]');
        if (del) del.addEventListener('click', function () {
            if (!confirm('이 할 일을 삭제할까요?')) return;
            post('/api/todos/' + id + '/delete', {})
                .then(function () { location.reload(); }).catch(fail);
        });

        // 인라인 수정
        var editBtn = li.querySelector('[data-edit]');
        var form = li.querySelector('.todo-editform');
        var meta = li.querySelector('.todo-meta');
        var titleView = li.querySelector('[data-edit-title]');
        if (editBtn && form) {
            editBtn.addEventListener('click', function () {
                form.hidden = false;
                if (titleView) titleView.hidden = true;
                if (meta) meta.hidden = true;
                form.title.focus();
            });
            var cancel = form.querySelector('[data-edit-cancel]');
            if (cancel) cancel.addEventListener('click', function () {
                form.hidden = true;
                if (titleView) titleView.hidden = false;
                if (meta) meta.hidden = false;
            });
            form.addEventListener('submit', function (e) {
                e.preventDefault();
                var t = form.title.value.trim();
                if (!t) return;
                post('/api/todos/' + id, {
                    title: t,
                    remind_at: form.remind_at.value || '',
                    note: form.note.value.trim(),
                }).then(function () { location.reload(); }).catch(fail);
            });
        }
    });
})();
