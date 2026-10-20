// Right-click menus. Pages mark an element with data-ctx="<type>" and register a
// handler for that type; the handler gets the element and returns
//   { title: 'text', items: [ { label, action, disabled, danger, confirm } ] }
// or null (the browser's own menu then shows). Tables are rebuilt with innerHTML
// on timers, so one listener on the document finds the row, not one per row.
// Safe items first; items with danger:true are drawn last, in red. An item with
// confirm:'text' asks before it runs.
// The same file is in confetti-butler and confetti-traffic (and topo has its own inline copy).
(function () {
    var handlers = {};
    var menu = null;
    var toastBox = null;
    var toastTimer = null;

    function closeMenu() {
        if (menu) { menu.style.display = 'none'; }
    }

    function toast(text) {
        if (!toastBox) {
            toastBox = document.createElement('div');
            toastBox.className = 'ctx-toast';
            toastBox.setAttribute('role', 'status');
            document.body.appendChild(toastBox);
        }
        toastBox.textContent = text;
        toastBox.style.display = 'block';
        clearTimeout(toastTimer);
        toastTimer = setTimeout(function () { toastBox.style.display = 'none'; }, 4500);
    }

    // navigator.clipboard only exists on https and localhost; the VM is plain http, so fall back to a hidden textarea.
    function copy(text) {
        text = text === null || text === undefined ? '' : String(text);
        var done = function () { toast('Copied: ' + (text.length > 60 ? text.slice(0, 57) + '...' : text)); };
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).then(done, function () { toast('Copy failed'); });
            return;
        }
        var box = document.createElement('textarea');
        box.value = text;
        box.style.position = 'fixed';
        box.style.opacity = '0';
        document.body.appendChild(box);
        box.select();
        try { document.execCommand('copy'); done(); } catch (e) { toast('Copy failed'); }
        document.body.removeChild(box);
    }

    function addItem(item) {
        var b = document.createElement('button');
        b.type = 'button';
        b.setAttribute('role', 'menuitem');
        b.textContent = item.label;
        b.disabled = !!item.disabled;
        if (item.danger) { b.className = 'danger'; }
        b.onclick = function () {
            closeMenu();
            if (item.confirm && !window.confirm(item.confirm)) { return; }
            item.action();
        };
        menu.appendChild(b);
    }

    function open(x, y, spec) {
        if (!menu) {
            menu = document.createElement('div');
            menu.className = 'ctx-menu';
            menu.setAttribute('role', 'menu');
            document.body.appendChild(menu);
        }
        menu.textContent = '';
        var title = document.createElement('div');
        title.className = 'ctx-title';
        title.textContent = spec.title || '';
        menu.appendChild(title);
        var items = spec.items || [];
        items.filter(function (i) { return !i.danger; }).forEach(addItem);
        var danger = items.filter(function (i) { return i.danger; });
        if (danger.length) {
            var rule = document.createElement('hr');
            menu.appendChild(rule);
            danger.forEach(addItem);
        }
        menu.style.display = 'block';
        // Keep the menu inside the window.
        menu.style.left = Math.max(0, Math.min(x, window.innerWidth - menu.offsetWidth - 4)) + 'px';
        menu.style.top = Math.max(0, Math.min(y, window.innerHeight - menu.offsetHeight - 4)) + 'px';
        var first = menu.querySelector('button:not(:disabled)');
        if (first) { first.focus(); }
    }

    document.addEventListener('contextmenu', function (ev) {
        var t = ev.target;
        if (!t.closest) { return; }
        if (t.closest('.ctx-menu')) { ev.preventDefault(); return; }
        // Text fields, the editor and a text selection keep the browser's own menu.
        if (t.closest('input, textarea, select, .CodeMirror, [contenteditable="true"]')) { return; }
        if (String(window.getSelection()).length) { return; }
        var el = t.closest('[data-ctx]');
        var fn = el && handlers[el.getAttribute('data-ctx')];
        if (!fn) { closeMenu(); return; }
        var spec = fn(el);
        if (!spec || !spec.items || !spec.items.length) { closeMenu(); return; }
        ev.preventDefault();
        open(ev.clientX, ev.clientY, spec);
    });
    document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') { closeMenu(); } });
    document.addEventListener('click', function (ev) { if (!ev.target.closest('.ctx-menu')) { closeMenu(); } });
    window.addEventListener('scroll', closeMenu, true);
    window.addEventListener('resize', closeMenu);

    window.ctxMenu = {
        register: function (type, fn) { handlers[type] = fn; },
        copy: copy,
        toast: toast,
        // For data-* attributes: esc() leaves quotes alone, so a log line could close the attribute.
        attr: function (s) {
            return String(s === null || s === undefined ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        },
    };
})();
