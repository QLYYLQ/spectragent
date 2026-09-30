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
