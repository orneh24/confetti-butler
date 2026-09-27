// Dark/light theme toggle, same behaviour as mesh-flux's dashboard.
// Loaded synchronously in <head> so a saved light choice is applied before
// first paint (no dark flash). Pages that draw with JS-held colors (the
// topology graph) listen for the 'themechange' event and redraw.
try {
    if (localStorage.getItem('lab-butler-theme') === 'light') {
        document.documentElement.setAttribute('data-theme', 'light');
    }
} catch (e) {}

function toggleTheme() {
    var root = document.documentElement;
    var light = root.getAttribute('data-theme') !== 'light';
    if (light) root.setAttribute('data-theme', 'light');
    else root.removeAttribute('data-theme');
    try { localStorage.setItem('lab-butler-theme', light ? 'light' : 'dark'); } catch (e) {}
    syncThemeButton();
    document.dispatchEvent(new Event('themechange'));
}

function syncThemeButton() {
    var btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent =
        document.documentElement.getAttribute('data-theme') === 'light' ? 'Dark mode' : 'Light mode';
}

// Read a CSS variable's current value, for JS that needs a literal color.
function themeColor(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

document.addEventListener('DOMContentLoaded', syncThemeButton);
