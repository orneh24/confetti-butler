// Colour theme picker, shared by every page. The palettes live in theme.css
// as :root[data-theme="NAME"]; dark is the default and has no attribute.
// Loaded synchronously in <head> so a saved choice is applied before first
// paint (no dark flash). Pages that draw with JS-held colours (the topology
// graph) listen for the 'themechange' event and redraw; a layout change fires
// it too, since Retro 95 and Amber CRT swap the whole palette.
var THEMES = [
    ['dark', 'Dark'],
    ['light', 'Light'],
    ['dracula', 'Dracula'],
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
    else applyTheme(savedTheme);
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
    ['amber', 'Amber CRT', true],
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

// "Confetti!" button: a burst from the button, then rain from the top, in the
// current theme's colours. Plain canvas, no library (the server may be offline).
// Each click gets its own overlay canvas, removed when the last piece falls off.
function confettiBlast(btn) {
    var colors = ['--yellow', '--red', '--green', '--blue', '--cyan']
        .map(themeColor)
        .filter(Boolean);
    if (!colors.length) colors = ['#d29922', '#f85149', '#3fb950', '#58a6ff', '#39c5cf'];
    var calm = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
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
    if (!calm && btn) {
        var r = btn.getBoundingClientRect();
        var bx = r.left + r.width / 2, by = r.top + r.height / 2;
        for (var i = 0; i < 600; i++) {
            var a = -Math.PI / 2 + (Math.random() - 0.5) * Math.PI * 1.2;
            var s = 8 + Math.random() * 14;
            add(bx, by, Math.cos(a) * s, Math.sin(a) * s);
        }
    }
    var rainTotal = calm ? 320 : 1000, rainLeft = rainTotal;
    var start = performance.now(), last = start;
    function frame(now) {
        var t = now - start, dt = Math.min(2, (now - last) / 16.7);
        // Rain is spread over the first 2 s: this frame's share of the total.
        var due = Math.max(1, Math.round(rainTotal * (now - last) / 2000));
        last = now;
        for (; due > 0 && rainLeft > 0; due--, rainLeft--) {
            add(Math.random() * W, -20 - Math.random() * 40, (Math.random() - 0.5) * 2, 2 + Math.random() * 3);
        }
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
        if ((bits.length || rainLeft > 0) && t < 7000) requestAnimationFrame(frame);
        else cv.remove();
    }
    requestAnimationFrame(frame);
}

// Header status dot, on every page: green and pulsing while /api/health
// answers, red when it doesn't. A page that wants the data too (the
// dashboard) defines window.onHealth(data) — data is null when unreachable.
function pollHealth() {
    var dot = document.getElementById('health-dot');
    var text = document.getElementById('health-text');
    fetch('/api/health').then(function (r) { return r.json(); }).then(function (data) {
        if (dot) dot.className = 'dot up';
        if (text) text.textContent = data.uptime_s ? 'up since ' + Math.floor(data.uptime_s / 60) + 'm' : 'up';
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
