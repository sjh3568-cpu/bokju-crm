(() => {
    document.querySelectorAll('select[data-cycle-prefix]').forEach(select => {
        const prefix = select.dataset.cyclePrefix, input = select.form.elements[prefix + '_cycle'];
        const update = () => { input.disabled = select.value !== 'custom'; input.closest('label').hidden = input.disabled; };
        select.addEventListener('change', update); update();
    });
    document.querySelectorAll('[data-activity-preset]').forEach(button => button.addEventListener('click', () => {
        const textarea = button.closest('form')?.elements.content;
        if (!textarea) return;
        textarea.value = textarea.value.trim() ? textarea.value.trim() + '\n' + button.dataset.activityPreset : button.dataset.activityPreset;
        textarea.focus();
    }));
    if (window.parent !== window && document.querySelector('[data-coop-saved]')) window.parent.postMessage({type:'cooperation-saved'}, location.origin);
})();
