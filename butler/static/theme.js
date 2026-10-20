// Colour theme picker, shared by every page. The palettes live in theme.css
// as :root[data-theme="NAME"]; dark is the default and has no attribute.
// Loaded synchronously in <head> so a saved choice is applied before first
// paint (no dark flash). Pages that draw with JS-held colours
// listen for the 'themechange' event and redraw; a layout change fires
// it too, since Retro 95 and the CRT layouts swap the whole palette.
var THEMES = [
    ['dark', 'Dark'],
    ['light', 'Light'],
    ['monokai', 'Monokai'],
    ['contrast', 'High Contrast'],
    ['terminal', 'Terminal green'],
    ['confetti-night', 'Confetti Night'],
    ['neon', 'Neon Streamers'],
];

// "Shuffle" is a mode, not a palette: it shows a random palette from THEMES
// and moves to a different one every 5-10 minutes. The saved choice is
// 'shuffle'; the palette on screen and when it next changes live under
// confetti-butler-theme-shuffle, so every page shows the same one and a reload
// does not reshuffle early.
var SHUFFLE_MIN_MS = 5 * 60 * 1000;
var SHUFFLE_MAX_MS = 10 * 60 * 1000;
var shuffling = false;
var shuffleTimer = null;

function themeKnown(name) {
    for (var i = 0; i < THEMES.length; i++) { if (THEMES[i][0] === name) return true; }
    return false;
}

function applyTheme(name) {
    if (!themeKnown(name) || name === 'dark') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', name);
}

function currentTheme() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
}

function shuffleStep(fromTimer) {
    var now = Date.now();
    var st = null;
    try { st = JSON.parse(localStorage.getItem('confetti-butler-theme-shuffle')); } catch (e) {}
    if (!st || !themeKnown(st.theme) || !(st.until > now)) {
        var avoid = st && themeKnown(st.theme) ? st.theme : currentTheme();
        var pick;
        do { pick = THEMES[Math.floor(Math.random() * THEMES.length)][0]; } while (pick === avoid);
        st = { theme: pick, until: now + SHUFFLE_MIN_MS + Math.random() * (SHUFFLE_MAX_MS - SHUFFLE_MIN_MS) };
        try { localStorage.setItem('confetti-butler-theme-shuffle', JSON.stringify(st)); } catch (e) {}
    }
    applyTheme(st.theme);
    // Shuffle picks a layout with the theme (defined below; not yet at first load).
    if (typeof shuffleLayout === 'function' && LAYOUTS) shuffleLayout(st);
    // The timer's change gets a confetti rain (as in confetti-traffic's hub); a
    // page load or a pick from the selector does not.
    if (fromTimer && typeof confettiBlast === 'function' && document.body) confettiBlast();
    clearTimeout(shuffleTimer);
    shuffleTimer = setTimeout(function () {
        if (!shuffling) return;
        shuffleStep(true);
        document.dispatchEvent(new Event('themechange'));
    }, Math.max(1000, st.until - now));
}

// The project was called lab-butler until 2026-10-08: carry a saved theme and
// layout over to the new keys once, so nobody's choice resets.
try {
    ['theme', 'layout'].forEach(function (k) {
        var old = localStorage.getItem('lab-butler-' + k);
        if (old !== null && localStorage.getItem('confetti-butler-' + k) === null) {
            localStorage.setItem('confetti-butler-' + k, old);
        }
        localStorage.removeItem('lab-butler-' + k);
    });
    localStorage.removeItem('lab-butler-theme-shuffle');
} catch (e) {}

try {
    var savedTheme = localStorage.getItem('confetti-butler-theme');
    if (savedTheme === 'shuffle') { shuffling = true; shuffleStep(); }
    else applyTheme(savedTheme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'));
} catch (e) {}

function setTheme(name) {
    clearTimeout(shuffleTimer);
    shuffling = (name === 'shuffle');
    if (shuffling) {
        // A fresh pick now, not the tail of an earlier shuffle.
        try { localStorage.removeItem('confetti-butler-theme-shuffle'); } catch (e) {}
        shuffleStep();
    } else {
        applyTheme(name);
    }
    try { localStorage.setItem('confetti-butler-theme', shuffling ? 'shuffle' : currentTheme()); } catch (e) {}
    syncThemeSelect();
    document.dispatchEvent(new Event('themechange'));
}

function syncThemeSelect() {
    var sel = document.getElementById('theme-select');
    if (!sel) return;
    if (!sel.options.length) {
        THEMES.concat([['shuffle', 'Shuffle']]).forEach(function (t) {
            var o = document.createElement('option');
            o.value = t[0];
            o.textContent = t[1];
            sel.appendChild(o);
        });
    }
    sel.value = shuffling ? 'shuffle' : currentTheme();
}

// Read a CSS variable's current value, for JS that needs a literal colour.
function themeColor(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}


// Layout picker. A layout is CSS keyed on :root[data-layout=NAME] (layout.css)
// plus a few elements only it shows; the data and scripts are the same in all
// of them. Classic is the default and has no attribute. Third field: the
// layout brings its own colours, so the theme picker is disabled while it is
// on (the saved theme is kept for the other layouts).
var LAYOUTS = [
    ['classic', 'Classic'],
    ['modern', 'Modern'],
    ['retro95', 'Retro 95', true],
    ['msdos', 'MS-DOS', true],
    ['greencrt', 'Neon Green CRT', true],
];

function applyLayout(name) {
    var known = LAYOUTS.some(function (l) { return l[0] === name; });
    if (!known || name === 'classic') document.documentElement.removeAttribute('data-layout');
    else document.documentElement.setAttribute('data-layout', name);
}
try { applyLayout(localStorage.getItem('confetti-butler-layout')); } catch (e) {}

function setLayout(name) {
    applyLayout(name);
    try { localStorage.setItem('confetti-butler-layout', document.documentElement.getAttribute('data-layout') || 'classic'); } catch (e) {}
    // While shuffling, a hand pick holds until the next change (and a reload).
    if (shuffling) {
        try {
            var st = JSON.parse(localStorage.getItem('confetti-butler-theme-shuffle'));
            if (st) { st.layout = document.documentElement.getAttribute('data-layout') || 'classic'; localStorage.setItem('confetti-butler-theme-shuffle', JSON.stringify(st)); }
        } catch (e) {}
    }
    syncLayoutSelect();
    document.dispatchEvent(new Event('themechange'));
}

function syncLayoutSelect() {
    var sel = document.getElementById('layout-select');
    if (!sel) return;
    if (!sel.options.length) {
        LAYOUTS.forEach(function (l) {
            var o = document.createElement('option');
            o.value = l[0];
            o.textContent = l[1];
            sel.appendChild(o);
        });
    }
    sel.value = document.documentElement.getAttribute('data-layout') || 'classic';
    var own = LAYOUTS.some(function (l) { return l[0] === sel.value && l[2]; });
    var themeSel = document.getElementById('theme-select');
    if (themeSel) {
        themeSel.disabled = own;
        themeSel.title = own ? 'This layout has its own colours' : '';
    }
}

// Shuffle also picks a layout with each theme, on the same timer. The pick is
// kept in the shared shuffle state so every page shows the same one. It is not
// saved as the viewer's own layout choice.
function shuffleLayout(st) {
    var names = LAYOUTS.map(function (l) { return l[0]; });
    if (names.indexOf(st.layout) < 0) {
        var cur = document.documentElement.getAttribute('data-layout') || 'classic';
        var pick;
        do { pick = names[Math.floor(Math.random() * names.length)]; } while (pick === cur);
        st.layout = pick;
        try { localStorage.setItem('confetti-butler-theme-shuffle', JSON.stringify(st)); } catch (e) {}
    }
    applyLayout(st.layout);
    syncLayoutSelect();
}
// The first shuffleStep ran before LAYOUTS existed: catch up now.
try {
    if (shuffling) { var st0 = JSON.parse(localStorage.getItem('confetti-butler-theme-shuffle')); if (st0) shuffleLayout(st0); }
} catch (e) {}

// "Make it rain!" button: a party-popper cannon pops in from a random side and
// fires confetti in the current theme's colours. Plain canvas, no library (the
// hub may be offline). Each click gets its own overlay canvas, removed when the
// last piece falls off. btn is unused; kept so the callers stay as they are.
function confettiBlast(btn) {
    var colors = ['--yellow', '--red', '--green', '--blue', '--cyan']
        .map(themeColor)
        .filter(Boolean);
    if (!colors.length) colors = ['#d29922', '#f85149', '#3fb950', '#58a6ff', '#39c5cf'];
    var dpr = window.devicePixelRatio || 1;
    var W = window.innerWidth, H = window.innerHeight;
    var cv = document.createElement('canvas');
    cv.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:9999';
    cv.width = W * dpr;
    cv.height = H * dpr;
    document.body.appendChild(cv);
    var ctx = cv.getContext('2d');
    ctx.scale(dpr, dpr);
    var bits = [];
    function add(x, y, vx, vy) {
        bits.push({ x: x, y: y, vx: vx, vy: vy,
            w: 6 + Math.random() * 6, h: 3 + Math.random() * 5,
            rot: Math.random() * 6.28, spin: (Math.random() - 0.5) * 0.3,
            sway: Math.random() * 6.28,
            color: colors[Math.floor(Math.random() * colors.length)] });
    }
    var fired = false;
    var left = Math.random() < 0.5, dir = left ? 1 : -1, size = 72;
    var gun = document.createElement('div');
    gun.textContent = '🎉';
    gun.style.cssText = 'position:fixed;bottom:12px;' + (left ? 'left' : 'right') + ':0;font-size:' + size +
        'px;line-height:1;pointer-events:none;z-index:10000;transition:transform .3s ease-out';
    function pose(x, extra) {
        gun.style.transform = 'translateX(' + x + ') ' + (left ? '' : 'scaleX(-1) ') + extra;
    }
    pose(left ? '-130%' : '130%', '');
    document.body.appendChild(gun);
    // The popper's mouth is its top inner corner; shoot up and toward the middle.
    function fire(n) {
        fired = true;
        var mx = left ? size * 0.85 : W - size * 0.85, my = H - 12 - size * 0.85;
        for (var i = 0; i < n; i++) {
            var a = (60 + (Math.random() - 0.5) * 120) * Math.PI / 180;
            var s = 24 + Math.random() * 20;
            add(mx, my, dir * Math.cos(a) * s, -Math.sin(a) * s);
        }
    }
    gun.offsetWidth;
    pose('0', '');
    setTimeout(function () { fire(1400); pose('0', 'rotate(' + (-8 * dir) + 'deg) scale(1.15)'); }, 350);
    setTimeout(function () { pose('0', ''); }, 500);
    setTimeout(function () { fire(400); }, 550);
    setTimeout(function () { pose(left ? '-130%' : '130%', ''); }, 4500);
    setTimeout(function () { gun.remove(); }, 5700);
    var start = performance.now(), last = start;
    function frame(now) {
        var t = now - start, dt = Math.min(2, (now - last) / 16.7);
        last = now;
        ctx.clearRect(0, 0, W, H);
        bits = bits.filter(function (b) {
            b.vy += 0.25 * dt;
            b.vx *= Math.pow(0.98, dt);
            b.vy *= Math.pow(0.98, dt);
            b.sway += 0.1 * dt;
            b.x += (b.vx + Math.sin(b.sway)) * dt;
            b.y += b.vy * dt;
            b.rot += b.spin * dt;
            ctx.save();
            ctx.translate(b.x, b.y);
            ctx.rotate(b.rot);
            ctx.fillStyle = b.color;
            ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h * Math.abs(Math.cos(b.sway)));
            ctx.restore();
            return b.y < H + 30;
        });
        if ((bits.length || !fired) && t < 7000) requestAnimationFrame(frame);
        else cv.remove();
    }
    requestAnimationFrame(frame);
}

// Uptime for the header: minutes under an hour, then 2 hr 05 min, then 1 day 3 hr 05 min.
function fmtUptime(s) {
    var m = Math.floor(s / 60), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), r = m % 60;
    if (m < 60) return m + ' min';
    var out = h + ' hr ' + (r < 10 ? '0' : '') + r + ' min';
    return d ? d + (d === 1 ? ' day ' : ' days ') + out : out;
}

// Header status dot, on every page: green and pulsing while /api/health
// answers, red when it doesn't. A page that wants the data too (the
// dashboard) defines window.onHealth(data) — data is null when unreachable.
function pollHealth() {
    var dot = document.getElementById('health-dot');
    var text = document.getElementById('health-text');
    fetch('/api/health').then(function (r) { return r.json(); }).then(function (data) {
        if (dot) dot.className = 'dot up';
        if (text) text.textContent = data.uptime_s ? 'up since ' + fmtUptime(data.uptime_s) : 'up';
        if (window.onHealth) window.onHealth(data);
    }).catch(function () {
        if (dot) dot.className = 'dot down';
        if (text) text.textContent = 'unreachable';
        if (window.onHealth) window.onHealth(null);
    });
}

// Retro 95's taskbar clock (the browser's local time, like the real one).
function tickTray() {
    var el = document.getElementById('tray-clock');
    if (el) el.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

document.addEventListener('DOMContentLoaded', function () {
    syncThemeSelect();
    syncLayoutSelect();
    pollHealth();
    setInterval(pollHealth, 15000);
    tickTray();
    setInterval(tickTray, 15000);
});

// MS-DOS keyboard layer: menu bar, function-key line, a cursor over table rows.
// Active only while the MS-DOS layout is on; the markup is in base.html. Same
// block in confetti-butler and confetti-topo (and the idea, not the matrix, from
// confetti-traffic's dashboard).
(function () {
    var menuAt = -1;        // index of the open menu, -1 = none
    var itemAt = -1;        // highlighted item in that menu
    var altOnly = false;    // Alt went down and nothing else happened yet
    var rowAt = -1;         // cursor row, by position (pages redraw their tables)
    var panelAt = -1;

    function isDos() { return document.documentElement.getAttribute('data-layout') === 'msdos'; }
    function byId(id) { return document.getElementById(id); }
    function menus() { return document.querySelectorAll('#dos-menubar .dos-menu'); }
    function itemsOf(menu) { return menu.querySelectorAll('.dos-drop > *'); }

    function markItem(i) {
        var items = menuAt >= 0 ? itemsOf(menus()[menuAt]) : [];
        for (var k = 0; k < items.length; k++) items[k].classList.toggle('on', k === i);
        itemAt = i;
    }
    function openMenu(i) {
        var all = menus();
        for (var k = 0; k < all.length; k++) all[k].classList.toggle('open', k === i);
        menuAt = i;
        markItem(i >= 0 ? 0 : -1);
    }
    function toggleHelp(open) {
        var box = byId('dos-help');
        if (box) box.classList.toggle('open', open === undefined ? !box.classList.contains('open') : open);
    }

    function rows() {
        var all = document.querySelectorAll('.page tbody tr');
        var out = [];
        for (var i = 0; i < all.length; i++) if (all[i].offsetParent !== null) out.push(all[i]);
        return out;
    }
    function setRow(list, i) {
        var old = document.querySelectorAll('tr.kbd-cursor');
        for (var k = 0; k < old.length; k++) old[k].classList.remove('kbd-cursor');
        rowAt = i;
        if (i < 0 || !list[i]) return;
        list[i].classList.add('kbd-cursor');
        list[i].scrollIntoView({ block: 'nearest' });
    }
    function moveRow(step, edge) {
        var list = rows();
        if (!list.length) return;
        var cur = document.querySelector('tr.kbd-cursor');
        var at = cur ? list.indexOf(cur) : rowAt;    // a redrawn table lost the class, not the place
        if (edge) at = edge < 0 ? 0 : list.length - 1;
        else at = at < 0 ? (step > 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, at + step));
        setRow(list, at);
    }
    function openRow() {
        var cur = document.querySelector('tr.kbd-cursor');
        var el = cur && cur.querySelector('a[href], button');
        if (el) el.click();
        return !!el;
    }

    function stepPanel(dir) {
        var all = document.querySelectorAll('.page .section');
        var list = [];
        for (var i = 0; i < all.length; i++) if (all[i].offsetParent !== null) list.push(all[i]);
        if (!list.length) return;
        var here = document.activeElement && document.activeElement.closest ? document.activeElement.closest('.section') : null;
        var at = list.indexOf(here);
        if (at < 0) at = panelAt;
        panelAt = (at + dir + list.length) % list.length;
        list[panelAt].setAttribute('tabindex', '-1');
        list[panelAt].scrollIntoView({ block: 'start' });
        list[panelAt].focus({ preventScroll: true });
    }

    // Esc: help, then menu, then the row cursor.
    function closeLayer() {
        var box = byId('dos-help');
        if (box && box.classList.contains('open')) { toggleHelp(false); return; }
        if (menuAt >= 0) { openMenu(-1); return; }
        setRow([], -1);
    }

    function act(el) {
        var what = el.getAttribute('data-act');
        openMenu(-1);
        if (what === 'refresh') window.location.reload();
        else if (what === 'rain') confettiBlast(el);
        else if (what === 'classic') setLayout('classic');
        else if (what === 'help') toggleHelp();
        else if (what === 'panel') stepPanel(1);
        else if (what === 'menu') openMenu(0);
        else if (what === 'esc') closeLayer();
        else if (el.tagName === 'A') window.location.href = el.getAttribute('href');
    }

    function menuKey(e) {
        var all = menus();
        var items = itemsOf(all[menuAt]);
        var k = e.key;
        if (k === 'ArrowLeft') openMenu((menuAt + all.length - 1) % all.length);
        else if (k === 'ArrowRight') openMenu((menuAt + 1) % all.length);
        else if (k === 'ArrowDown') markItem((itemAt + 1) % items.length);
        else if (k === 'ArrowUp') markItem((itemAt + items.length - 1) % items.length);
        else if (k === 'Enter' || k === ' ') { if (items[itemAt]) act(items[itemAt]); }
        else if (k === 'Escape' || k === 'F10') openMenu(-1);
        else if (k.length === 1) {
            var ch = k.toLowerCase();
            for (var i = 0; i < items.length; i++) {
                if (items[i].getAttribute('data-key') === ch) { act(items[i]); e.preventDefault(); return; }
            }
            for (var m = 0; m < all.length; m++) {
                if (all[m].getAttribute('data-menu') === ch) { openMenu(m); break; }
            }
        } else return;
        e.preventDefault();
    }

    document.addEventListener('keydown', function (e) {
        if (!isDos()) return;
        var t = e.target;
        var typing = t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || t.isContentEditable ||
                           (t.closest && t.closest('.CodeMirror')));
        if (e.key === 'Alt') { if (!e.repeat) altOnly = true; return; }
        altOnly = false;
        if (typing) { if (e.key === 'Escape' && t.blur) t.blur(); return; }
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        if (e.key === 'F5' && e.shiftKey) return;
        if (e.key === 'F1') { toggleHelp(); e.preventDefault(); return; }
        if (e.key === 'F5') { window.location.reload(); e.preventDefault(); return; }
        if (e.key === 'F6') { stepPanel(e.shiftKey ? -1 : 1); e.preventDefault(); return; }
        if (e.key === 'F10') { openMenu(menuAt >= 0 ? -1 : 0); e.preventDefault(); return; }
        if (menuAt >= 0) { menuKey(e); return; }
        if (e.key === 'Escape') { closeLayer(); return; }
        // Cursor keys work from the page itself, not from a focused button or link.
        if (t !== document.body) return;
        if (e.key === 'ArrowDown') { moveRow(1, 0); e.preventDefault(); }
        else if (e.key === 'ArrowUp') { moveRow(-1, 0); e.preventDefault(); }
        else if (e.key === 'Home') { moveRow(0, -1); e.preventDefault(); }
        else if (e.key === 'End') { moveRow(0, 1); e.preventDefault(); }
        else if ((e.key === 'Enter' || e.key === ' ') && openRow()) e.preventDefault();
    });
    // A lone Alt press-and-release opens the menu, like DOS programs and old Windows.
    document.addEventListener('keyup', function (e) {
        if (e.key !== 'Alt' || !altOnly || !isDos()) return;
        altOnly = false;
        openMenu(menuAt >= 0 ? -1 : 0);
        e.preventDefault();
    });
    document.addEventListener('click', function (e) {
        var bar = byId('dos-menubar');
        if (!bar) return;
        var top = e.target.closest('#dos-menubar .dos-menu > button');
        if (top) {
            var i = Array.prototype.indexOf.call(menus(), top.parentNode);
            openMenu(i === menuAt ? -1 : i);
            return;
        }
        var item = e.target.closest('#dos-menubar .dos-drop > *, #dos-fkeys button');
        if (item) { act(item); return; }
        if (menuAt >= 0) openMenu(-1);
    });
})();
