// Dashboard widgets you can move and resize. Loaded by dashboard.html only.
//
// Move: drag a widget by its title bar; the other widgets shift to make room (the grid order changes, nothing floats).
// Drop it on another widget (that widget gets an outline; on a full-width widget only its middle band counts) and the two, with
// any widget already on that row, share the row: the columns are split between them. Dropped above or below a full-width
// widget it only changes the order.
// Resize: drag the corner. The width snaps to whole grid columns (the full row at most); the height is free and
// the widget scrolls inside when it is smaller than its content. Double-click the corner to give one widget its size back.
// Remembered in this browser only (localStorage), so a new browser sees the default layout. "Reset layout" clears it.
//
// A widget is a `.section` with a `data-box="<id>"` and a `.section-header`; the page's own code only touches the
// elements inside the widgets, so its refresh never undoes the arrangement. Pointer events, so touch works too.
// The SAME file is used by confetti-topo, confetti-butler and confetti-traffic: keep the three identical.
var DashGrid = (function () {
    var box, cfg, state = { order: [], span: {}, height: {}, row: {} };

    var CSS =
        '.dg-bar { display: flex; justify-content: flex-end; padding: 8px 24px 0; }' +
        '.dg-bar button { font-size: 11px; }' +
        '.dg-on .section { position: relative; }' +
        '.dg-on .section-header { cursor: grab; touch-action: none; user-select: none; -webkit-user-select: none; }' +
        // The widget being dragged stays in the grid as the drop place: its content is hidden and it shows a grid pattern.
        '.dg-on .section.dg-drag { outline: 2px dashed var(--text-dim); outline-offset: -2px;' +
        '  background-image: repeating-linear-gradient(0deg, transparent 0 19px, rgba(128,128,128,.35) 19px 20px),' +
        '  repeating-linear-gradient(90deg, transparent 0 19px, rgba(128,128,128,.35) 19px 20px); }' +
        '.dg-on .section.dg-drag > * { visibility: hidden; }' +
        // A copy of it follows the pointer.
        '.dg-ghost { position: fixed; z-index: 10000; margin: 0; opacity: .85; pointer-events: none; box-shadow: 0 8px 24px rgba(0,0,0,.4); }' +
        '.dg-on .section.dg-join { outline: 2px solid var(--blue, var(--text-dim)); outline-offset: -2px; }' +
        'body.dg-moving, body.dg-moving * { user-select: none; -webkit-user-select: none; cursor: grabbing !important; }' +
        'body.dg-sizing, body.dg-sizing * { user-select: none; -webkit-user-select: none; cursor: nwse-resize !important; }' +
        '.dg-resize { position: absolute; right: 0; bottom: 0; width: 16px; height: 16px; cursor: nwse-resize; touch-action: none;' +
        '  background: linear-gradient(135deg, transparent 0 45%, var(--text-dim) 45% 55%, transparent 55% 70%, var(--text-dim) 70% 80%, transparent 80%); opacity: .7; }' +
        '.dg-resize:hover { opacity: 1; }';

    function key() { return cfg.prefix + '-dashboard-layout'; }

    function load() {
        try {
            var s = JSON.parse(localStorage.getItem(key()));
            if (s && typeof s === 'object') {
                state = { order: Array.isArray(s.order) ? s.order : [], span: s.span || {}, height: s.height || {}, row: s.row || {} };
            }
        } catch (e) { /* not remembered */ }
    }

    function save() {
        try { localStorage.setItem(key(), JSON.stringify(state)); } catch (e) { /* not remembered */ }
    }

    function boxes() { return Array.prototype.slice.call(box.querySelectorAll(':scope > .section[data-box]')); }

    function columns() {
        var cols = getComputedStyle(box).gridTemplateColumns.split(' ').filter(Boolean);
        return Math.max(1, cols.length);
    }

    // Put the saved size on one box. A span wider than the columns now on screen is the full row (a span past the
    // last column would make the grid add columns of its own).
    // A widget that starts a row (state.row: the first of widgets that were joined on one row) is pinned to column 1, so the
    // row it was joined on is not shared with the widget before it.
    function colValue(id, span, cols) {
        if (!span) { return ''; }
        if (span >= cols) { return '1 / -1'; }
        return state.row[id] ? '1 / span ' + span : 'span ' + span;
    }

    function applySize(el) {
        var id = el.getAttribute('data-box');
        var cols = columns();
        el.style.gridColumn = colValue(id, state.span[id], cols);
        var body = el.querySelector('.section-body');
        if (body) {
            body.style.height = state.height[id] ? state.height[id] + 'px' : '';
        }
    }

    function applyAll() {
        var byId = {};
        boxes().forEach(function (el) { byId[el.getAttribute('data-box')] = el; });
        // Saved order first; widgets it does not know (a new one) stay after, in page order.
        var seen = {};
        state.order.forEach(function (id) {
            if (byId[id] && !seen[id]) { box.appendChild(byId[id]); seen[id] = true; }
        });
        boxes().forEach(function (el) {
            if (!seen[el.getAttribute('data-box')]) { box.appendChild(el); }
        });
        boxes().forEach(applySize);
    }

    function saveOrder() {
        state.order = boxes().map(function (el) { return el.getAttribute('data-box'); });
        save();
    }

    // ---- move ----------------------------------------------------------------------------------------------
    // The dragged widget was dropped on the middle of `target`: the widgets on target's row (same top edge) and the
    // dragged one share the columns of that row, first ones getting the extra column. Too many for the columns
    // (or a one-column grid, e.g. a phone) means nothing is resized.
    function joinRow(el, target) {
        var cols = columns();
        var top = target.getBoundingClientRect().top;
        var group = boxes().filter(function (b) { return b === el || Math.abs(b.getBoundingClientRect().top - top) < 6; });
        if (cols < 2 || group.length < 2 || group.length > cols) { return; }
        var base = Math.floor(cols / group.length), extra = cols % group.length;
        group.forEach(function (b, i) {
            var id = b.getAttribute('data-box');
            state.span[id] = base + (i < extra ? 1 : 0);
            if (i === 0) { state.row[id] = true; } else { delete state.row[id]; }
            applySize(b);
        });
    }

    function startMove(ev, el) {
        var x0 = ev.clientX, y0 = ev.clientY, moving = false, join = null, ghost = null, gx = 0, gy = 0;

        function setJoin(t) {
            if (join === t) { return; }
            if (join) { join.classList.remove('dg-join'); }
            join = t;
            if (join) { join.classList.add('dg-join'); }
        }

        // The pointer is over no widget: if it is level with a row, it is in that row's empty space (right of the last
        // widget or left of the first). Put the dragged widget next to them there and the grid fills the free columns.
        function emptySpace(e) {
            var row = boxes().filter(function (b) {
                var r = b.getBoundingClientRect();
                return e.clientY >= r.top && e.clientY <= r.bottom;
            });
            if (!row.length) { return; }
            setJoin(null);
            var last = row[row.length - 1], first = row[0];
            if (e.clientX > last.getBoundingClientRect().right) {
                if (last !== el) { box.insertBefore(el, last.nextSibling); }
            } else if (e.clientX < first.getBoundingClientRect().left) {
                if (first !== el) { box.insertBefore(el, first); }
            }
        }

        function over(e) {
            var t = document.elementFromPoint(e.clientX, e.clientY);
            var target = t && t.closest ? t.closest('.section[data-box]') : null;
            if (!target) { emptySpace(e); return; }
            if (target === el || target.parentNode !== box) { return; }
            var r = target.getBoundingClientRect();
            var fy = (e.clientY - r.top) / r.height;
            var full = r.width > box.clientWidth * 0.8;
            // Share this widget's row: most of a widget that is already beside another one, the middle band of a full-width widget
            // (its top and bottom edges there mean "above" and "below").
            setJoin(full ? (fy > 0.3 && fy < 0.7 ? target : null) : (fy > 0.15 && fy < 0.85 ? target : null));
            // A widget that fills the row is passed on the way down; widgets side by side on the way across.
            var after = full ? e.clientY > r.top + r.height / 2 : e.clientX > r.left + r.width / 2;
            box.insertBefore(el, after ? target.nextSibling : target);
        }

        function move(e) {
            if (!moving) {
                if (Math.abs(e.clientX - x0) + Math.abs(e.clientY - y0) < 5) { return; }
                moving = true;
                var r = el.getBoundingClientRect();
                gx = x0 - r.left; gy = y0 - r.top;
                ghost = el.cloneNode(true);
                ghost.classList.add('dg-ghost');
                ghost.style.width = r.width + 'px';
                ghost.style.height = r.height + 'px';
                ghost.style.gridColumn = '';
                document.body.appendChild(ghost);
                el.classList.add('dg-drag');
                document.body.classList.add('dg-moving');
            }
            ghost.style.left = (e.clientX - gx) + 'px';
            ghost.style.top = (e.clientY - gy) + 'px';
            over(e);   // before scrolling: the scroll moves the widgets under the pointer
            if (e.clientY < 70) { window.scrollBy(0, -14); }
            else if (e.clientY > window.innerHeight - 70) { window.scrollBy(0, 14); }
        }

        function end() {
            document.removeEventListener('pointermove', move);
            document.removeEventListener('pointerup', end);
            document.removeEventListener('pointercancel', end);
            if (moving) {
                el.classList.remove('dg-drag');
                if (ghost) { ghost.remove(); ghost = null; }
                document.body.classList.remove('dg-moving');
                var target = join;
                setJoin(null);
                delete state.row[el.getAttribute('data-box')];   // it moved: no longer pinned to a row start
                if (target) { joinRow(el, target); }
                saveOrder();
            }
        }

        // On the document, not the header with pointer capture: moving the widget in the DOM (insertBefore) makes the
        // browser drop the capture, and the drag would never see its pointerup.
        document.addEventListener('pointermove', move);
        document.addEventListener('pointerup', end);
        document.addEventListener('pointercancel', end);
    }

    // ---- resize --------------------------------------------------------------------------------------------
    function startSize(ev, el, grip) {
        var id = el.getAttribute('data-box');
        var r0 = el.getBoundingClientRect();
        var body = el.querySelector('.section-body');
        var head = el.querySelector('.section-header');
        var x0 = ev.clientX, y0 = ev.clientY;
        var cs = getComputedStyle(box);
        var gap = parseFloat(cs.columnGap) || 0;
        var unit = parseFloat(cs.gridTemplateColumns.split(' ')[0]) || r0.width;
        var cols = columns();
        document.body.classList.add('dg-sizing');

        function move(e) {
            var w = r0.width + (e.clientX - x0);
            var span = Math.max(1, Math.min(cols, Math.round((w + gap) / (unit + gap))));
            state.span[id] = span;
            el.style.gridColumn = colValue(id, span, cols);
            if (body) {
                var h = Math.max(60, r0.height + (e.clientY - y0) - (head ? head.offsetHeight : 0) - 2);
                state.height[id] = Math.round(h);
                body.style.height = state.height[id] + 'px';
            }
        }

        function end() {
            grip.removeEventListener('pointermove', move);
            grip.removeEventListener('pointerup', end);
            grip.removeEventListener('pointercancel', end);
            document.body.classList.remove('dg-sizing');
            save();
        }

        grip.setPointerCapture(ev.pointerId);
        grip.addEventListener('pointermove', move);
        grip.addEventListener('pointerup', end);
        grip.addEventListener('pointercancel', end);
    }

    function reset() {
        state = { order: [], span: {}, height: {}, row: {} };
        try { localStorage.removeItem(key()); } catch (e) { /* nothing saved anyway */ }
        // Back to the page's own order: the order the widgets had when the page was built.
        original.forEach(function (el) { box.appendChild(el); });
        boxes().forEach(applySize);
    }

    var original = [];

    function init(options) {
        cfg = options || {};
        box = document.querySelector(cfg.container || '.container');
        if (!box || box.getAttribute('data-dg')) { return; }
        box.setAttribute('data-dg', '1');
        box.classList.add('dg-on');
        var style = document.createElement('style');
        style.textContent = CSS;
        document.head.appendChild(style);
        original = boxes();

        boxes().forEach(function (el) {
            var header = el.querySelector('.section-header');
            if (!header) { return; }
            header.title = 'Drag to move this widget';
            header.addEventListener('pointerdown', function (ev) {
                if (ev.button !== 0 || (ev.target.closest && ev.target.closest('a, button, input, select, textarea'))) { return; }
                startMove(ev, el);
            });
            var grip = document.createElement('div');
            grip.className = 'dg-resize';
            grip.title = 'Drag to resize (width in columns, height free). Double-click: original size';
            grip.addEventListener('pointerdown', function (ev) {
                if (ev.button !== 0) { return; }
                ev.preventDefault();
                startSize(ev, el, grip);
            });
            grip.addEventListener('dblclick', function () {
                var id = el.getAttribute('data-box');
                delete state.span[id];
                delete state.height[id];
                delete state.row[id];
                applySize(el);
                save();
            });
            el.appendChild(grip);
        });

        var bar = document.createElement('div');
        bar.className = 'dg-bar';
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = 'Reset layout';
        btn.title = 'Put the widgets back in their original order and size';
        btn.addEventListener('click', reset);
        bar.appendChild(btn);
        box.parentNode.insertBefore(bar, box);

        load();
        applyAll();
        var timer = null;
        window.addEventListener('resize', function () {
            clearTimeout(timer);
            timer = setTimeout(function () { boxes().forEach(applySize); }, 100);
        });
    }

    return { init: init };
})();
