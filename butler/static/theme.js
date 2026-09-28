// Colour theme picker, shared by every page. The palettes live in theme.css
// as :root[data-theme="NAME"]; dark is the default and has no attribute.
// Loaded synchronously in <head> so a saved choice is applied before first
// paint (no dark flash). Pages that draw with JS-held colours (the topology
// graph) listen for the 'themechange' event and redraw.
var THEMES = [
    ['dark', 'Dark'],
    ['light', 'Light'],
    ['catppuccin', 'Catppuccin Mocha'],
    ['gruvbox', 'Gruvbox'],
    ['terminal', 'Terminal green'],
];

// "Shuffle" is a mode, not a palette: it shows a random palette from THEMES
// and moves to a different one every 5-10 minutes. The saved choice is
// 'shuffle'; the palette on screen and when it next changes live under
// lab-butler-theme-shuffle, so every page shows the same one and a reload
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

function shuffleStep() {
    var now = Date.now();
    var st = null;
    try { st = JSON.parse(localStorage.getItem('lab-butler-theme-shuffle')); } catch (e) {}
    if (!st || !themeKnown(st.theme) || !(st.until > now)) {
        var avoid = st && themeKnown(st.theme) ? st.theme : currentTheme();
        var pick;
        do { pick = THEMES[Math.floor(Math.random() * THEMES.length)][0]; } while (pick === avoid);
        st = { theme: pick, until: now + SHUFFLE_MIN_MS + Math.random() * (SHUFFLE_MAX_MS - SHUFFLE_MIN_MS) };
        try { localStorage.setItem('lab-butler-theme-shuffle', JSON.stringify(st)); } catch (e) {}
    }
    applyTheme(st.theme);
    clearTimeout(shuffleTimer);
    shuffleTimer = setTimeout(function () {
        if (!shuffling) return;
        shuffleStep();
        document.dispatchEvent(new Event('themechange'));
    }, Math.max(1000, st.until - now));
}

try {
    var savedTheme = localStorage.getItem('lab-butler-theme');
    if (savedTheme === 'shuffle') { shuffling = true; shuffleStep(); }
    else applyTheme(savedTheme);
} catch (e) {}

function setTheme(name) {
    clearTimeout(shuffleTimer);
    shuffling = (name === 'shuffle');
    if (shuffling) {
        // A fresh pick now, not the tail of an earlier shuffle.
        try { localStorage.removeItem('lab-butler-theme-shuffle'); } catch (e) {}
        shuffleStep();
    } else {
        applyTheme(name);
    }
    try { localStorage.setItem('lab-butler-theme', shuffling ? 'shuffle' : currentTheme()); } catch (e) {}
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

document.addEventListener('DOMContentLoaded', syncThemeSelect);
