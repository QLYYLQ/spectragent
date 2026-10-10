# Third-party notices

Code in this repository that was ported from, or closely follows, a third-party
source. Each entry names the upstream, the exact revision, its license, where the
port lives, and what was changed. Only permissively licensed code is ported; the
copyleft references the data-flywheel work studied (pywb, wabac.js) are design
references only and contribute no code.

## Web Page Replay — `deterministic.js`

- **Upstream:** Web Page Replay (WPR-go), https://chromium.googlesource.com/webpagereplay
- **Revision:** `6a31bb0937dccd01bedae8bd6cdeb8c113937cfb` (2026-10-08), file `deterministic.js`
- **License:** BSD-3-Clause (text below). The file itself carries
  "Copyright 2017 The Chromium Authors. All rights reserved."
- **Ported into:** `spectragent/core/capabilities/browser/replay/shims.py` (`SHIM_TEMPLATE`),
  the shim the `replayability: pinned` tier injects into every page and worker.
- **Kept:** `Date` and `Date.now()` start at a fixed time and advance 50 ms every
  25 constructions (never frozen); a guard against injecting twice.
- **Modified by af:** `Math.random` and `crypto.getRandomValues` draw from seeded
  mulberry32 streams instead of WPR's fixed 0.13297 step or constant value;
  `crypto.randomUUID` returns RFC 4122 version-4 UUIDs from the seeded bytes;
  `ServiceWorkerContainer.prototype.register` rejects; the `Date` replacement is
  built with `Reflect.construct` so subclasses and `instanceof` keep working; every
  API is feature-tested so one source runs in pages and workers; the template takes
  T0 and the seed as placeholders. **Dropped:** the hard-coded `getTimezoneOffset`
  (time zone is a CDP override), and the `navigator.onLine` / `navigator.connection`
  overrides.

```
Copyright 2020 The Chromium Authors
Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.
* Neither the name of webpagereplay nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.
THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Playwright — `crNetworkManager.ts`

- **Upstream:** Playwright, https://github.com/microsoft/playwright
- **Revision:** `d9f2fd3e2232ace8e317a84eda6cb86e25bcf49c` (2026-10-09),
  file `packages/playwright-core/src/server/chromium/crNetworkManager.ts`
- **License:** Apache License 2.0. The file carries "Copyright 2017 Google Inc. All
  rights reserved. Modifications copyright (c) Microsoft Corporation." Playwright's
  `NOTICE` reads: "Playwright. Copyright (c) Microsoft Corporation. This software
  contains code derived from the Puppeteer project
  (https://github.com/puppeteer/puppeteer), available under the Apache 2.0 license
  (https://github.com/puppeteer/puppeteer/blob/master/LICENSE)."
- **Ported into:** `spectragent/core/capabilities/browser/archive/network_model.py`
  (the event model; the file keeps the upstream license header and a modification
  statement) and `spectragent/core/capabilities/browser/archive/network.py`
  (`NetworkSession._refetch`, `REFETCH_SAFE_TYPES`), the `browser.network` archiver.
- **Kept:** pairing `Fetch.requestPaused` with `Network.requestWillBeSent` by the
  network request id in either arrival order; one record per redirect hop, closed by
  the next hop's `redirectResponse` and linked by `redirectedFrom`; worker requests
  reported on the worker session but paused on the page session; `*ExtraInfo` events
  paired with responses per request id, responses served from cache treated as having
  none (crbug 1340398); `data:` URLs skipped; the WebSocket event set and its
  wall-clock baseline; the refetch of an oversized body — only a `GET` of a
  refetch-safe static type (font, image, manifest, media, script, stylesheet,
  texttrack), `Network.loadNetworkResource` then `IO.read` until `eof`, then `IO.close`.
- **Modified by af:** re-implemented in Python as a passive accumulator for an archive
  instead of a live request router — no routing, interception overrides,
  authentication, CORS preflight synthesis or frame manager. Added: deduplication
  across two recorder sessions of one target; settle / pending bookkeeping (an
  exchange whose body was read at the Fetch Response stage settles without
  `loadingFinished`); the browser redaction rules applied as each event is stored;
  the refetch runs after the response was released, reads in ≤ 1 MiB chunks and stops
  at a byte budget.

```
                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
```

The full Apache-2.0 text is at https://www.apache.org/licenses/LICENSE-2.0.txt (and in
Playwright's `LICENSE` at the revision above).
