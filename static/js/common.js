// 공통 유틸 — fetch 헬퍼, flash 메시지
window.api = {
    async post(url, data) {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data || {}),
        });
        if (!res.ok) {
            let err = `${res.status}`;
            try { err = (await res.json()).error || err; } catch {}
            throw new Error(err);
        }
        return res.json();
    },
    async get(url) {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`${res.status}`);
        return res.json();
    },
};

window.toast = function (msg, kind) {
    const el = document.createElement('div');
    el.className = `flash ${kind || 'info'}`;
    el.textContent = msg;
    el.style.cssText = 'position:fixed;top:20px;right:20px;z-index:9999;min-width:240px;box-shadow:0 4px 12px rgba(0,0,0,0.15)';
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3500);
};
