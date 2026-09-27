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

try { applyTheme(localStorage.getItem('lab-butler-theme')); } catch (e) {}

function setTheme(name) {
    applyTheme(name);
    try { localStorage.setItem('lab-butler-theme', currentTheme()); } catch (e) {}
    syncThemeSelect();
    document.dispatchEvent(new Event('themechange'));
}

function syncThemeSelect() {
    var sel = document.getElementById('theme-select');
    if (!sel) return;
    if (!sel.options.length) {
        THEMES.forEach(function (t) {
            var o = document.createElement('option');
            o.value = t[0];
            o.textContent = t[1];
            sel.appendChild(o);
        });
    }
    sel.value = currentTheme();
}

// Read a CSS variable's current value, for JS that needs a literal colour.
function themeColor(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

document.addEventListener('DOMContentLoaded', syncThemeSelect);
