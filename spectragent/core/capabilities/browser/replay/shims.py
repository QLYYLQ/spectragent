"""The ``pinned`` tier's page/worker shim (§4.14).

Ported from Web Page Replay's ``deterministic.js``
(https://chromium.googlesource.com/webpagereplay @
6a31bb0937dccd01bedae8bd6cdeb8c113937cfb, BSD-3-Clause, "Copyright 2017 The
Chromium Authors"; license text in THIRD_PARTY_NOTICES.md). What it keeps
from WPR, and what af changed, is stated in the template's own header so
the notice travels with the source into every page it is injected into.

The template is a Python string with two placeholders, ``__AF_T0_MS__`` and
``__AF_SEED__``, replaced by integers. :data:`SHIM_SHA256` is the digest of
the *template* — one value per shim version, whatever T0 and seed a run
uses — and is what the environment fingerprint records next to them.
"""

import hashlib

SHIM_TEMPLATE = r"""// Copyright 2017 The Chromium Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.
//
// Ported from Web Page Replay deterministic.js
// (chromium.googlesource.com/webpagereplay @ 6a31bb0937dccd01bedae8bd6cdeb8c113937cfb).
// Modified by af (All4One) for replayability: pinned. Kept: Date and Date.now
// start at T0 and advance 50 ms every 25 constructions (never frozen, so a
// busy-wait on Date.now ends), and a re-injection guard. Changed: Math.random
// and crypto.getRandomValues draw from seeded mulberry32 streams instead of a
// fixed step; crypto.randomUUID returns RFC 4122 version-4 UUIDs from the
// seeded bytes; ServiceWorkerContainer.prototype.register rejects; every API
// is feature-tested so the same source runs in pages and workers. Dropped:
// the getTimezoneOffset, navigator.onLine and navigator.connection overrides
// (time zone and locale are CDP overrides). performance.now and timers are
// untouched, as in WPR.
(function () {
  'use strict';
  const g = typeof globalThis === 'object' ? globalThis : self;
  const MARK = Symbol.for('af.replayability.pinned');
  if (g[MARK]) {
    return;
  }
  Object.defineProperty(g, MARK, { value: true });

  const T0_MS = __AF_T0_MS__;
  const SEED = __AF_SEED__ >>> 0;

  function mulberry32(state) {
    return function () {
      state = (state + 0x6D2B79F5) | 0;
      let t = Math.imul(state ^ (state >>> 15), 1 | state);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const nextRandom = mulberry32(SEED);
  const nextUnit = mulberry32((SEED ^ 0x9E3779B9) >>> 0);
  function nextByte() {
    return Math.floor(nextUnit() * 256);
  }

  // Math.random
  Math.random = function random() {
    return nextRandom();
  };

  // crypto.getRandomValues / crypto.randomUUID
  if (typeof g.crypto === 'object' && g.crypto !== null) {
    const crypto = g.crypto;
    if (typeof crypto.getRandomValues === 'function') {
      const nativeGetRandomValues = crypto.getRandomValues;
      const floatTypes = ['Float16Array', 'Float32Array', 'Float64Array']
        .map(function (name) { return g[name]; })
        .filter(function (type) { return typeof type === 'function'; });
      crypto.getRandomValues = function getRandomValues(array) {
        const isFloat = floatTypes.some(function (type) { return array instanceof type; });
        if (!ArrayBuffer.isView(array) || array instanceof DataView || isFloat
            || array.byteLength > 65536) {
          // Not ours to answer: the native implementation throws the right error.
          return nativeGetRandomValues.call(crypto, array);
        }
        const bytes = new Uint8Array(array.buffer, array.byteOffset, array.byteLength);
        for (let i = 0; i < bytes.length; i++) {
          bytes[i] = nextByte();
        }
        return array;
      };
    }
    if (typeof crypto.randomUUID === 'function') {
      crypto.randomUUID = function randomUUID() {
        const b = [];
        for (let i = 0; i < 16; i++) {
          b.push(nextByte());
        }
        b[6] = (b[6] & 0x0f) | 0x40;
        b[8] = (b[8] & 0x3f) | 0x80;
        const h = b.map(function (x) { return (x + 0x100).toString(16).slice(1); });
        return h.slice(0, 4).join('') + '-' + h.slice(4, 6).join('') + '-'
          + h.slice(6, 8).join('') + '-' + h.slice(8, 10).join('') + '-'
          + h.slice(10, 16).join('');
      };
    }
  }

  // Date
  const NativeDate = g.Date;
  let now = T0_MS;
  let constructions = 0;
  function tick() {
    constructions += 1;
    if (constructions > 25) {
      now += 50;
      constructions = 1;
    }
    return now;
  }
  function PinnedDate() {
    if (new.target === undefined) {
      return new NativeDate(tick()).toString();
    }
    if (arguments.length === 0) {
      return Reflect.construct(NativeDate, [tick()], new.target);
    }
    return Reflect.construct(NativeDate, Array.prototype.slice.call(arguments), new.target);
  }
  Object.defineProperty(PinnedDate, 'name', { value: 'Date' });
  Object.defineProperty(PinnedDate, 'length', { value: 7 });
  Object.setPrototypeOf(PinnedDate, NativeDate);
  PinnedDate.prototype = NativeDate.prototype;
  Object.defineProperty(NativeDate.prototype, 'constructor', {
    value: PinnedDate, writable: true, configurable: true, enumerable: false,
  });
  PinnedDate.now = function now() {
    return tick();
  };
  g.Date = PinnedDate;

  // Service workers: registration is refused (with setBypassServiceWorker).
  if (typeof g.ServiceWorkerContainer === 'function' && g.ServiceWorkerContainer.prototype
      && typeof g.ServiceWorkerContainer.prototype.register === 'function') {
    g.ServiceWorkerContainer.prototype.register = function register() {
      return Promise.reject(new DOMException(
        'Service worker registration is blocked by replayability: pinned', 'SecurityError'));
    };
  }
})();
"""

#: Digest of the template source; changes exactly when the shim's code does.
SHIM_SHA256 = hashlib.sha256(SHIM_TEMPLATE.encode("utf-8")).hexdigest()


def render_shim(*, t0_ms: int, seed: int) -> str:
    """The shim for one run: the template with T0 and the seed filled in."""
    if not isinstance(t0_ms, int) or isinstance(t0_ms, bool):
        raise TypeError(f"t0_ms must be an int, got {type(t0_ms).__name__}")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError(f"seed must be an int, got {type(seed).__name__}")
    return SHIM_TEMPLATE.replace("__AF_T0_MS__", str(t0_ms)).replace("__AF_SEED__", str(seed))


#: The replay server's own service-worker block (M30), injected into every
#: frame of a replaying run whatever the tier: a service worker would answer
#: navigations itself (they would pause on its session as ``XHR``, and a
#: main-frame document miss could not be told apart). With
#: ``Network.setBypassServiceWorker``. Idempotent by construction — it only
#: replaces ``register`` — so it composes with ``pinned``'s shim, which
#: refuses registration too.
SERVICE_WORKER_BLOCK = r"""(() => {
  const g = globalThis;
  if (typeof g.ServiceWorkerContainer === 'function' && g.ServiceWorkerContainer.prototype
      && typeof g.ServiceWorkerContainer.prototype.register === 'function') {
    g.ServiceWorkerContainer.prototype.register = function register() {
      return Promise.reject(new DOMException(
        'Service worker registration is blocked while replaying an archive', 'SecurityError'));
    };
  }
})();
"""
