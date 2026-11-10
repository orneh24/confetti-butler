// Header shortcuts to the other Confetti projects (confetti-topo, confetti-butler, confetti-traffic).
//
// Each app keeps the URLs of the OTHER projects on its Settings page; GET /api/settings/links answers
// {"butler": "...", "traffic": "..."} (only the other two, "" when not set). This file puts one small button per
// URL that is set into <span id="confetti-links"> in the header's right-hand .status block. It opens in a new tab.
// The URL is only a link target in the browser: the server never fetches it. It is checked again here (http or
// https only) and set as href, never written as HTML. If the fetch fails there are simply no buttons.
// The SAME file is used by confetti-topo, confetti-butler and confetti-traffic: keep the three identical.
var ConfettiLinks = (function () {
    var NAMES = [['traffic', 'Traffic'], ['butler', 'Butler'], ['topo', 'Topo']];

    // Colours come from the theme variables; the layouts that restyle every button get their own look.
    var CSS =
        '#confetti-links { display: inline-flex; gap: 4px; align-items: center; margin-right: 6px; }' +
        '#confetti-links:empty { display: none; }' +
        '.link-out { display: inline-block; font-size: 11px; line-height: 1.5; padding: 0 7px; white-space: nowrap;' +
        '  border: 1px solid var(--border); border-radius: 3px; background: transparent; color: var(--text-dim);' +
        '  text-decoration: none; text-transform: none; letter-spacing: 0; font-weight: 400; }' +
        '.link-out:hover { color: var(--text); border-color: var(--text-dim); text-decoration: none; }' +
        ':root[data-layout="modern"] .link-out { border-radius: 999px; padding: 1px 10px; }' +
        ':root[data-layout="retro95"] .link-out { background: #c0c0c0; color: #000; border: 2px solid; border-radius: 0;' +
        '  border-color: #fff #404040 #404040 #fff; text-shadow: none; }' +
        ':root[data-layout="msdos"] .link-out { background: var(--dos-bar); color: var(--dos-ink); border: 0;' +
        '  border-radius: 0; text-shadow: none; }' +
        ':root[data-layout="msdos"] .link-out:hover { background: var(--dos-ink); color: var(--dos-sel-fg); }';

    function box() { return document.getElementById('confetti-links'); }

    function show(links) {
        var el = box();
        if (!el) { return; }
        el.textContent = '';
        NAMES.forEach(function (n) {
            var url = links && typeof links[n[0]] === 'string' ? links[n[0]] : '';
            if (!/^https?:\/\/\S+$/i.test(url)) { return; }
            var a = document.createElement('a');
            a.className = 'link-out';
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            a.href = url;
            a.textContent = n[1] + ' ↗';
            a.title = 'Open Confetti ' + n[1] + ' in a new tab: ' + url;
            el.appendChild(a);
        });
    }

    // Ask the server again (the Settings page calls this after a save).
    function refresh() {
        fetch('/api/settings/links').then(function (r) { return r.ok ? r.json() : {}; })
            .then(show).catch(function () { /* no buttons */ });
    }

    function init() {
        if (!box()) { return; }
        var style = document.createElement('style');
        style.textContent = CSS;
        document.head.appendChild(style);
        refresh();
    }

    init();
    return { refresh: refresh };
})();
