"""Page-side DOM extraction for the Skyvern-style cleaner.

This is an original, self-contained script. It does not load or copy Skyvern's
AGPL ``domUtils.js``. Every evaluation builds its tree and node-reference
array in one JS world, so navigation cannot leave a stale bootstrap behind.
The Python capture resolves references to CDP backend node IDs before the
remote object group is released.
"""

from __future__ import annotations

EXTRACTION_SCRIPT = r"""
(() => {
  const refs = [];
  const interactiveTags = new Set([
    'A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'OPTION', 'SUMMARY', 'DETAILS'
  ]);
  const interactiveRoles = new Set([
    'button', 'link', 'checkbox', 'radio', 'tab', 'combobox', 'listbox',
    'option', 'switch', 'menuitem', 'textbox', 'slider', 'spinbutton'
  ]);
  const skipTags = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);

  function ownText(el) {
    let text = '';
    for (const child of el.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) text += child.nodeValue || '';
    }
    return text.trim();
  }

  function visitChild(child) {
    if (child.nodeType === Node.TEXT_NODE) {
      const text = child.nodeValue || '';
      return text.trim() ? {tagName: '#text', nodeType: 3, text} : null;
    }
    return child instanceof Element ? visit(child) : null;
  }

  function visit(el) {
    if (!(el instanceof Element) || skipTags.has(el.tagName)) return null;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' ||
        style.visibility === 'collapse' || Number(style.opacity) === 0) return null;

    const tag = el.tagName.toUpperCase();
    const box = el.getBoundingClientRect();
    const attrs = {};
    for (const attr of el.attributes) attrs[attr.name] = attr.value;

    // Never put secrets into the tree that reaches prompt and RunTrace.
    const type = String(attrs.type || '').toLowerCase();
    const autocomplete = String(attrs.autocomplete || '').toLowerCase();
    const formControl = ['INPUT', 'TEXTAREA', 'SELECT'].includes(tag);
    const sensitiveHint = /(?:otp|one.?time|verification|auth.?code|security.?code|pass.?code|two.?factor|2fa|mfa|pin)/i
      .test([attrs.id, attrs.name, autocomplete].join(' '));
    const sensitive = formControl && (
      (tag === 'INPUT' && ['password', 'file', 'hidden'].includes(type)) ||
      autocomplete.startsWith('cc-') || autocomplete === 'one-time-code' ||
      sensitiveHint
    );
    if (sensitive) {
      delete attrs.value;
      attrs['data-af-redacted'] = 'true';
    } else if (formControl) {
      attrs.value = String(el.value ?? '');
      if (tag === 'INPUT' && ['checkbox', 'radio'].includes(type)) {
        if (el.checked) attrs.checked = 'true'; else delete attrs.checked;
      }
    }

    const role = String(attrs.role || '').toLowerCase();
    const interactive = interactiveTags.has(tag) || interactiveRoles.has(role) ||
      el.hasAttribute('onclick') || el.tabIndex >= 0 || style.cursor === 'pointer';
    const frame = tag === 'IFRAME' || tag === 'FRAME';
    const ref = interactive || frame ? refs.push(el) - 1 : null;
    const children = [];
    if (!sensitive) {
      for (const child of el.childNodes) {
        const built = visitChild(child);
        if (built !== null) children.push(built);
      }
    }
    const shadowRoots = [];
    if (el.shadowRoot) {
      const shadowChildren = [];
      for (const child of el.shadowRoot.childNodes) {
        const built = visitChild(child);
        if (built !== null) shadowChildren.push(built);
      }
      shadowRoots.push({tagName: '#shadow-root', children: shadowChildren,
                        shadowRootType: el.shadowRoot.mode});
    }

    return {
      tagName: tag.toLowerCase(), attributes: attrs, text: sensitive ? '' : ownText(el),
      interactive, ref, children, shadowRoots,
      rect: {x: box.x, y: box.y, width: box.width, height: box.height},
      clientRect: {x: el.clientLeft, y: el.clientTop,
                   width: el.clientWidth, height: el.clientHeight},
      scrollRects: {x: el.scrollLeft, y: el.scrollTop,
                    width: el.scrollWidth, height: el.scrollHeight},
      contentOffset: {
        x: el.clientLeft + (parseFloat(style.paddingLeft) || 0),
        y: el.clientTop + (parseFloat(style.paddingTop) || 0)
      }
    };
  }

  return {
    tree: visit(document.documentElement), refs,
    scroll: {
      x: window.scrollX, y: window.scrollY,
      width: document.documentElement.scrollWidth,
      height: document.documentElement.scrollHeight,
      viewportWidth: window.innerWidth, viewportHeight: window.innerHeight
    }
  };
})()
"""

#: The ``skyvern_hit_test`` cleaner's in-page hit test (arch §4.12), called with
#: ``Runtime.callFunctionOn`` on the object ``EXTRACTION_SCRIPT`` returned, in
#: the same JS world, right after the tree is read — so ``this.refs`` are the
#: very elements the tree's ``ref`` numbers name.
#:
#: For each ref whose box (``getBoundingClientRect``) meets this frame's
#: viewport, ``elementFromPoint`` at the centre of the visible part (the
#: element's shadow root's own ``elementFromPoint`` when it lives in one);
#: when the centre is covered, a 5x5 grid inside the visible part, each edge
#: inset 2 CSS px, row by row. A point *hits* when the topmost element there
#: is the element, one of its descendants (walking ``parentNode`` and
#: ``ShadowRoot.host`` up), or a ``<label>`` whose ``control`` it is — a click
#: there reaches it. The result, by value, maps each ref to
#: ``{center_topmost, offset}``: ``offset`` is the hit point relative to the
#: element's own box (the page script cannot know where its frame sits in the
#: top-level viewport; the cleaner adds that), or ``null`` when nothing hit.
#: A ref outside the viewport is absent: off-screen is not covered.
HIT_TEST_FUNCTION = r"""
function() {
  const refs = this.refs || [];
  const root = document.documentElement;
  const vw = (root && root.clientWidth) || window.innerWidth;
  const vh = (root && root.clientHeight) || window.innerHeight;
  const GRID = 5, INSET = 2;
  const out = {};

  function reaches(el, top) {
    for (let node = top; node; node = node.parentNode || node.host || null) {
      if (node === el) return true;
      if (node instanceof HTMLLabelElement && node.control === el) return true;
    }
    return false;
  }

  function topAt(el, x, y) {
    const scope = el.getRootNode();
    const finder = scope instanceof ShadowRoot && scope.elementFromPoint ? scope : document;
    return finder.elementFromPoint(x, y);
  }

  refs.forEach((el, ref) => {
    if (!(el instanceof Element) || !el.isConnected) return;
    const box = el.getBoundingClientRect();
    const x1 = Math.max(box.left, 0), y1 = Math.max(box.top, 0);
    const x2 = Math.min(box.right, vw), y2 = Math.min(box.bottom, vh);
    if (x2 <= x1 || y2 <= y1) return;
    const cx = (x1 + x2) / 2, cy = (y1 + y2) / 2;
    if (reaches(el, topAt(el, cx, cy))) {
      out[ref] = {center_topmost: true, offset: [cx - box.left, cy - box.top]};
      return;
    }
    const ix = Math.min(INSET, (x2 - x1) / 2), iy = Math.min(INSET, (y2 - y1) / 2);
    const sx = (x2 - x1 - 2 * ix) / (GRID - 1), sy = (y2 - y1 - 2 * iy) / (GRID - 1);
    for (let row = 0; row < GRID; row++) {
      for (let column = 0; column < GRID; column++) {
        const px = x1 + ix + column * sx, py = y1 + iy + row * sy;
        if (reaches(el, topAt(el, px, py))) {
          out[ref] = {center_topmost: false, offset: [px - box.left, py - box.top]};
          return;
        }
      }
    }
    out[ref] = {center_topmost: false, offset: null};
  });
  return out;
}
"""
