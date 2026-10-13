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
  statement), the `browser.network` archiver.
- **Kept:** pairing `Fetch.requestPaused` with `Network.requestWillBeSent` by the
  network request id in either arrival order; one record per redirect hop, closed by
  the next hop's `redirectResponse` and linked by `redirectedFrom`; worker requests
  reported on the worker session but paused on the page session; `*ExtraInfo` events
  paired with responses per request id, responses served from cache treated as having
  none (crbug 1340398); `data:` URLs skipped; the WebSocket event set and its
  wall-clock baseline.
- **Modified by af:** re-implemented in Python as a passive accumulator for an archive
  instead of a live request router — no routing, interception overrides,
  authentication, CORS preflight synthesis or frame manager. Added: deduplication
  across two recorder sessions of one target; settle / pending bookkeeping (an
  exchange whose body was read at the Fetch Response stage settles without
  `loadingFinished`); the browser redaction rules applied as each event is stored.
  The port of Playwright's refetch of an oversized body (`Network.loadNetworkResource`
  then `IO.read`) was removed on 2026-10-13: a large body is now streamed while the
  page itself downloads it (`Network.streamResourceContent`), af's own code.

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

## cdxj-indexer — `postquery.py`

- **Upstream:** cdxj-indexer, https://github.com/webrecorder/cdxj-indexer (by Ilya Kreymer)
- **Revision:** `a65917b12fbef23e58ee3e66878b852923811fb6` (2026-10-01, v1.5.0), file
  `cdxj_indexer/postquery.py`
- **License:** Apache License 2.0 (the repository's `LICENSE`; no `NOTICE` file). Only
  this file's request-body logic is ported. cdxj-indexer itself is **not** a dependency:
  it requires `surt` (AGPL-3.0), and the sorting `surt` supplies upstream is our own code.
- **Ported into:** `spectragent/core/capabilities/browser/replay/canonical.py`
  (`body_params` and its helpers; the file keeps the upstream license header and a
  modification statement), the canonicaliser offline replay keys requests with.
- **Kept:** the `__wb_method=<METHOD>` parameter; urlencoded bodies read as their decoded
  parameters; multipart bodies read as their parts' names and values, never the
  boundary; JSON flattened into parameters, newline-delimited JSON line by line;
  `text/plain` tried as JSON first; anything unparsable as one `__wb_post_data` value.
- **Modified by af:** a pure key function instead of a CDX index writer; every method
  with a body is keyed (upstream: POST and PUT); the parameters are sorted and merged
  with the query here (upstream leaves that to SURT); JSON keys are full paths
  (`a.b.0.c`; upstream: the leaf name with `.N_` suffixes); `+json` media types and
  form-shaped untyped bodies included, empty containers kept; a multipart file part is
  its sha256 (upstream: raw bytes) and multipart is parsed without the `multipart`
  package; an unparsable body is its sha256 (upstream: base64); past 4096 characters the
  body is replaced by its sha256 (upstream: truncated); AMF dropped; GraphQL operations
  keyed by operation; site-rule paths removed.

```
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

## Playwright — `harBackend.ts`

- **Upstream:** Playwright, https://github.com/microsoft/playwright
- **Revision:** `d9f2fd3e2232ace8e317a84eda6cb86e25bcf49c` (2026-10-09), file
  `packages/playwright-core/src/server/harBackend.ts` (`_harFindResponse`,
  `countMatchingHeaders`, `multipartBoundary`)
- **License:** Apache License 2.0, with Playwright's `NOTICE` (quoted under
  `crNetworkManager.ts` above).
- **Closely followed in:** `spectragent/core/capabilities/browser/replay/canonical.py`
  (`exact_body_digest`: a multipart body compared with its boundary removed) and
  `spectragent/core/capabilities/browser/replay/matcher.py` (the request-header count
  that breaks a tie).
- **Kept:** the exact match on method, URL and body bytes with each side's multipart
  boundary removed; ranking tied candidates by matching `name:value` request headers.
- **Modified by af:** bodies compared for every method (upstream: POST only), as a
  sha256 of the redacted body; headers rank only otherwise-equal candidates, after the
  recording order; redirect chains are **not** followed inside the lookup — replay serves
  the recorded 3xx and lets Chrome follow it.

## Web Page Replay — `archive.go`

- **Upstream:** Web Page Replay (WPR-go), https://chromium.googlesource.com/webpagereplay
- **Revision:** `6a31bb0937dccd01bedae8bd6cdeb8c113937cfb` (2026-10-08), file
  `src/webpagereplay/archive.go` (`FindRequest`, `findBestMatchInArchivedRequestSet`)
- **License:** BSD-3-Clause (text under `deterministic.js` above).
- **Closely followed in:** `spectragent/core/capabilities/browser/replay/matcher.py`
  (`fuzzy_score`, the fuzzy level and its tie-break, the chronological cursor).
- **Kept:** same-host same-path fuzzy matching scored `2M/T` over query parameters; a tie
  between equally scored URLs goes to the lexicographically smaller URL; serving the
  responses of one request in recording order
  (`serve-response-in-chronological-sequence`).
- **Modified by af:** `M` counts from 0 (upstream: 1) and the canonical parameters are
  compared; a threshold (`fuzzy_threshold`) below which nothing matches, off for
  documents and non-GET requests unless a site rule turns it on (upstream: no threshold,
  never off); a request's canonical body parameters count too (upstream never reads the
  body); used records are tracked per replay run and, once every candidate is used, the
  last one repeats (upstream falls back to the best header match).

## Web Page Replay — `proxy.go`

- **Upstream:** Web Page Replay (WPR-go), https://chromium.googlesource.com/webpagereplay
- **Revision:** `6a31bb0937dccd01bedae8bd6cdeb8c113937cfb` (2026-10-08), file
  `src/webpagereplay/proxy.go` (`updateDate`, `updateDates`)
- **License:** BSD-3-Clause (text under `deterministic.js` above).
- **Closely followed in:** `spectragent/core/capabilities/browser/replay/responder.py`
  (`dated_headers`: the dates of a served response moved together).
- **Kept:** `Last-Modified` and `Expires` keep their distance from the recorded `Date`
  when `Date` becomes now; a value that does not parse is left as it is.
- **Modified by af:** re-implemented, not copied; one switch for everything
  (`shift_dates: none | all` — upstream always shifts); the cookie `Expires` attribute
  moves with the headers (upstream leaves cookies alone; see mitmproxy below); without a
  recorded `Date` the request's start time anchors the shift, and a `Date` is
  synthesised so Chrome's cookie-expiry rebasing has one.

## mitmproxy — `Response.refresh`

- **Upstream:** mitmproxy, https://github.com/mitmproxy/mitmproxy
- **Revision:** `5253dcbd1d8f0522de097bfe56918fe12a0f267a` (2026-10-05), files
  `mitmproxy/http.py` (`Response.refresh`) and `mitmproxy/net/http/cookies.py`
  (`refresh_set_cookie_header`)
- **License:** MIT (text under `serverplayback.py` below).
- **Closely followed in:** `spectragent/core/capabilities/browser/replay/responder.py`
  (`dated_headers`, `shifted_cookie_expiry`).
- **Kept:** one delta, now − the recorded time, applied to `Date`, `Expires`,
  `Last-Modified` and every `Set-Cookie` `Expires=` attribute.
- **Modified by af:** re-implemented, not copied; the delta is measured from the
  recorded `Date` when there is one (upstream: the response's recorded timestamp);
  shifting is off unless `shift_dates: all`; an unparsable cookie expiry is kept as
  recorded (upstream drops the attribute).

## mitmproxy — `serverplayback.py`

- **Upstream:** mitmproxy, https://github.com/mitmproxy/mitmproxy
- **Revision:** `5253dcbd1d8f0522de097bfe56918fe12a0f267a` (2026-10-05), file
  `mitmproxy/addons/serverplayback.py` (`next_flow`, the `server_replay_ignore_*` options)
- **License:** MIT (text below).
- **Closely followed in:** `spectragent/core/capabilities/browser/replay/matcher.py` (each
  recorded response served once, in recording order, per key) and
  `spectragent/core/capabilities/browser/replay/site_rules.py` (`ignore_query` /
  `ignore_body_paths` leave named parameters out of the key).
- **Modified by af:** the used set is shared across the three matching levels; a key
  whose records are all used repeats its last record and says `exhausted` (upstream
  deletes the key, so the next identical request misses); a miss is never forwarded to
  the network (upstream's default `server_replay_extra=forward`).

```
Copyright (c) 2013, Aldo Cortesi. All rights reserved.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
