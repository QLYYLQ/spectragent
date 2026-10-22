# Replay request rules

Rules for offline replay (`network_source: {source_id: archive}`), described in
`docs/architecture_design.md` §4.14. The shipped replay and branch configurations
select this directory with `matcher: {site_rules: configs/replay_rules}`.

Every `*.yaml` file contains a `rules` list. Filenames organize the evidence;
they do not select a host or imply a domain suffix. Each rule declares its own
scope, identity, priority, response exhaustion policy and its policy for a
recorded request without a body. All files form one rule set.

A request uses the matching rule with the greatest integer `priority`. Two
matching rules at that priority are a configuration conflict, naming both
rules. File order and filename order never break a tie. Rules do not merge:
a narrower rule must repeat every behavior it needs, including `exhaustion`
and `unavailable`.
Recorded and live requests must select the same rule identity to become matching
candidates, including fuzzy and Range matching. Equal normalized keys from
different scopes do not authorize exchanging their responses.

The published `baseline.yaml` explicitly chooses `repeat_last` and
`serve_identical` at priority `-100` for all requests. The endpoint rules use
priority `0` and make the same choices explicitly. This preserves this
experiment's selected response reuse behavior. It is not a framework default.
To make a stateful endpoint miss after its recorded sequence is consumed, give
it a matching rule with `exhaustion: miss`. Requests without an applicable
policy are refused; the framework does not silently supply one.

## Records without a body

A recording keeps some requests without a response body: the page cancelled
them (most often images scrolled past), they were still in flight when the run
ended, the browser served them from its memory cache, or the recorder's body
read timed out. Such a record keeps its place in its response sequence. With
`unavailable: miss` the miss policy answers it (a subresource gets 404). With
`unavailable: serve_identical` an available record of the same sequence
answers for it, but only when every available record of that sequence has the
same status and the same recorded body, and the consumed record's own status,
when the recorder saw one, is that status (a recorded 500 is never answered
with a 200). A request whose source failed on the
network (for example `net::ERR_BLOCKED_BY_ORB`) is never answered from another
record; that is fixed, not a rule setting. The replay log records each such
answer as `substituted`, with both record ids and how the source request
ended (`source_outcome`), so answering a request the source page cancelled is
a visible deviation from the recording. `repeat_last` repeats the last
available response; a sequence with no available response stays a
`body_unavailable` miss.

## Evidence and scope

The original rules were written on 2026-10-14 from twelve Chinese-site
recordings by comparing replay misses with recorded requests and response
bodies. On 2026-10-15 the parameter rules were narrowed to the documented
endpoints, with exact paths and methods checked through the recordings'
committed network indexes. File comments retain the parameter rationale.
That set contained 25 site files plus the explicit baseline. On 2026-10-19,
`xiaohongshu.com.yaml` added three exact search endpoints after an authenticated
record/replay comparison: notes ignores body `search_id`/`session_id`, filter ignores
query `search_id`, and onebox ignores body `search_id`/`request_id`. The same saved
login seed and search inputs were held constant; the recorded responses did not echo
these identifiers. All other content selectors remain in the matching key.

The failing search replay showed an empty result page. Two candidate replays restored
the same 19 result links, titles and order from the archived DOM, and reduced the
search checkpoint's screenshot distance from 28 to 0. The two earlier warnings were
a search-history panel's open/closed state; the recommendation cards were unchanged.
They remain recorded warnings, with task relevance assessed separately. These are
repeats of one source task, not evidence across accounts or every search variant;
negative request mutations verify that keywords, pages, sorting and filters do not merge.

On 2026-10-20, `bilibili.com.yaml` added the space page's profile request
(`GET api.bilibili.com/x/space/wbi/acc/info`). It ignores query `dm_img_inter`, which
holds window size and scroll offset mixed with fresh random numbers, and `w_rid`, the
request signature computed over all other query fields. All eight recorded signatures
were recomputed from those fields, so the signature carries nothing else. The profile
is selected by `mid`, which stays in the key with `wts` and every other field. Without
the rule, the profile page showed an error, a later click missed its target and the
replay ended behind a login dialog. In two candidate replays every profile request hit
in recorded order and restoration passed 24 and 25 of 30 checks, against 2 and 3 without it. Those
replays ran before rules declared `unavailable`; the rule now repeats the baseline's `serve_identical`,
which changes nothing for this request, because all eight recorded profile responses have a body. The evidence
is one recording of one account; a changed `mid` still misses. (A changed `wts` also
missed until 2026-10-23, when the shared replay clock made `wts` differ on every replay;
see below.) The
profile's video-list request (`/x/space/wbi/arc/search`) changes the same two fields,
but its recorded answer was an empty list: serving it changed no restoration check or
page text, so a higher hit count alone did not justify a rule and it is not covered.

On 2026-10-22, `qq.com.yaml` (Tencent Video), the new `iqiyi.com.yaml` and the new
`10jqka.com.cn.yaml` (Tonghuashun) added endpoint rules. The evidence is the offline
branch replays of five anonymous recordings in `lookahead_15task_pixels_20261022`:
Tencent Video 128, 263 and 28, iQIYI 18 and Tonghuashun 233. Each rule covers a value
that the replayed page makes fresh: a random visitor, panel, chat or search-box id, a
trace id, a jQuery callback name and cache buster, or a client time with a signature
over it. The live requests were captured during replay. Each differed from its nearest
recorded request only in the fields its rule ignores, and each file says why per rule.
Every recorded iQIYI signature was recomputed from the other fields. Across all 15
recordings, requests that a new rule joins share their status. Their recorded answers
differ only in tracking strings, ranking order or lists that change between page loads.
The tests check that every content field still misses.

Restoration checks used the restorer's `ok` verdict on the 64 branch forks of these
recordings. Before the rules, 4 of 64 forks were `ok`. With the rules they were 17 of 64
under `pace: none` and 20 of 64 under `pace: recorded_llm_latency`. The forks whose
checkpoint is on the recorded URL rose from 12 to 40 and 52. The four step-29 Tencent
Video forks need more than the 900 s task timeout when paced. Most of the remaining
Tencent Video and iQIYI forks fail only the media and screenshot checks: the video does
not play. The player's stream requests put encrypted keys, times and flow ids inside one
field (`vinfoparam` in `proxyhttp`, `cKey` on `getvinfo`, `authKey`/`vf` on iQIYI's
`dash`), and a rule cannot ignore only part of a field. The protobuf variant of
`GetTabRemoteConfig` stays matched on its whole body because its cached-version field
chooses between a full config and a not-modified answer.

On 2026-10-23, pinned recording's clock became one shared clock for every tab (T0 at run
start, then real time) instead of a per-document counter that replayed identical `Date`
values. Client times in URLs and bodies now differ between recording and replay, so the
fields derived from them (signatures, ids built from the clock or from random draws whose
order the timing changes) differ too. The rules added that day were found with the
flywheel's near-miss report (`decision_inputs.site_rule_candidates`, which now groups misses
by endpoint, counts query and body fields apart and lists the fields that change together)
over the 304 branch replays of `lookahead_15task_20261023`, recorded and branched under the
shared clock:

- `bilibili.com.yaml`: the WBI-signed content APIs ignore `wts` (client seconds) and `w_rid`
  (md5 over the other fields and a key; 106 of 127 recorded signatures recompute), plus `qv_id`
  (random per search), `session` (random per play) and `dm_img_inter` (telemetry) where
  they occur; the visitor ticket ignores `context[ts]` and its HMAC `hexsign` (all 8
  recomputed). Search results, the combined results, placeholder, trending, suggestions,
  page slots, video detail, player info, play URL, profile and follow state; then, after
  replaying with those, the video page's danmaku, danmaku settings, subtitles and summary
  button.
- `iqiyi.com.yaml`: the replayed page makes a new visitor device id (none of the 56 values
  seen equals the recorded one), sent as `device`, `deviceId`, `device_id`, `deviceID`,
  `qyid`, `u` or the module-grading body's `cid`; 20 home page, search and player endpoints
  ignore it (with `timestamp`/`sign` where present), the video details rule now ignores it
  too, the search and player module configs ignore their cache buster `t`, and the poster
  badge request ignores `timestamp`/`sign` (found after replaying with the others).
- `qq.com.yaml`: the play page's fan community recommendations ignore the visitor id in
  `guid`/`video_omgid`.

No rule was added for IGN or Tonghuashun: their near misses inside restored prefixes were
ads, telemetry, responsive image sizes and, on Tonghuashun, the K-line request's
`end_time` (the client time on the latest-bar request, but a paging bound on others, which
a rule cannot tell apart). Across all 15 recordings of that root, and the 15 re-recordings
of `lookahead_15task_freeze_20261023` as a holdout, every group of requests a new rule joins
has one HTTP status (records without any response stay unanswered); the joined answers differ
only in live counters, server times, tickets, rotating placeholders and ranking order.

Restoration was compared on the re-recordings (made with `background_tabs: freeze`, which
the old recordings predate), with the same branch configuration and only `site_rules`
changed, one fork per checkpoint. Restorer `ok` verdicts, before → after: Bilibili 2 → 4 of
12, iQIYI 1 → 3 of 8, Tencent Video 0 → 0 of 4. iQIYI forks that reached the recorded URL
rose from 2 to 8 of 8 (before, the replay stayed on the home or search page), and Bilibili
forks within the screenshot threshold from 3 to 6 of 12. Of the Bilibili forks still
failing, two restore a URL that differs only in the search page's `vt` parameter (one also
fails the media check). The page writes `vt` into its own URL without requesting it, so this
is a restoration criterion, not a request rule. The four of task 6 replay a recording whose
home page requests had failed or been cancelled while recording. Tencent Video's forks and most remaining iQIYI ones fail the media and
screenshot checks: the video does not play (see 2026-10-22). Replaying the second-round
rules (video page overlays, poster badges) changed no verdict on 8 of those forks; they
are request-equivalence changes, each checked by matching and counterexample tests.

These rules are experimental content assumptions. A visitor id on one
statistics endpoint does not authorize ignoring the same parameter on another
endpoint. A change must check incorrect matches as well as newly matched
requests. In particular:

- Ctrip ignores the top-level body `id` only on `POST tripAds.json`, where the
  evidence identifies a random ad request id. Other APIs keep their `id`.
  App-config and destination visitor fields each have their own endpoint.
- Baidu, Douban, East Money and LeTV parameters are split across their observed
  pixels, scripts, ad calls and API paths. Search and business endpoints do
  not inherit statistics rules. LeTV search page size `ps` remains meaningful.
- AcFun's search rule does not cover the home page. Qidian's CSRF omission is
  limited to the two API endpoints identified by the original evidence.
- Host-wide Tencent telemetry synthesis is narrowed to reporting endpoints;
  whitelist/configuration endpoints keep their recorded behavior. The old
  `h.trace.qq.com` entry lacks endpoint evidence and is removed.
- There is no global removal of timestamps, cache parameters, random-looking
  numbers, dimensions or callback names. Parameters previously removed by
  heuristics remain in keys unless an applicable rule explicitly ignores them.
  Douban's ad `ts`, for example, is retained by this rule set.

Removing assumptions can lower hit rates. That does not justify extending a
rule without evidence that the requests select the same content. Preserve
failed requests, candidates and the selected rule when investigating a change.

## Schema

| Key | Type | Meaning |
|---|---|---|
| `id` | string | Rule identity, unique within the set; included in diagnostics. |
| `scope` | mapping | Request constraints below; an empty mapping explicitly matches every request. |
| `priority` | integer | Greatest matching priority wins; defaults to `0`. |
| `exhaustion` | `repeat_last` or `miss` | Required. Reuse the last available response of the selected sequence, or report `sequence_exhausted`. |
| `unavailable` | `miss` or `serve_identical` | Required. Answer a consumed record without a body through the miss policy, or with an available record of the same sequence when every available record there has the same status and body and the consumed record's recorded status, if any, is that status (never for a source network failure). |
| `levels` | list of `exact`, `normalized`, `fuzzy`, or null | Restrict the run's allowed matching levels; null uses the run's set. A rule cannot enable a level forbidden by `matcher.levels`. |
| `fuzzy` | boolean | Permit fuzzy matching for documents and non-GET requests, only when the run and rule allow `fuzzy`. Use `true` or `false`. |
| `ignore_query` | list of name globs | Query fields omitted from the normalized key and fuzzy score. |
| `keep_query` | list of name globs | Fields retained even when an ignore pattern matches. |
| `ignore_body_paths` | list of path globs | Body fields omitted from the normalized key: form names, flattened JSON paths, or GraphQL variable paths within an operation. |
| `keep_body_paths` | list of path globs | Body fields retained even when an ignore pattern matches. |
| `ignore_headers` | list of name globs | Request headers omitted from the header tie-break; case-insensitive. |
| `keep_headers` | list of name globs | Headers retained even when an ignore pattern matches; case-insensitive. |
| `synthesize_204` | list of URL globs | Classify matching non-document misses as benign for a synthetic 204. Document handling takes precedence. |
| `response_echo` | list of `{url, request_path, response_path}` | On a hit, copy request JSON values into existing response JSON positions; a `*` path segment traverses array/object members. |

List fields default to empty. `keep_*` takes precedence over the corresponding
`ignore_*`. It does not restore a field removed by another rule, since only one
rule is used.

All declared scope constraints must match:

| Scope key | Type | Matches |
|---|---|---|
| `host` | string or null | Host glob. `example.com` matches that host only; use `*.example.com` explicitly for subdomains. |
| `path` | string or null | Request path glob, without query or fragment. |
| `url` | string or null | Full URL glob, without fragment. |
| `method` | string or null | HTTP method constraint, for example `GET` or `POST`. |
| `port` | integer or null | Effective network port, including the scheme's default when the URL omits it. |

Omitted/null scope fields do not constrain that dimension. Globs use `fnmatch`
syntax; `*` crosses dots and slashes. Quote wildcard strings in YAML. A rule
with an exact `host` never implicitly includes subdomains.

Matching strength is fixed: `exact`, then `normalized`, then `fuzzy`.
`matcher.levels` and rule `levels` are allowed sets, not ordering controls.
Range requests respect the same boundary. Once the strongest permitted level
finds a sequence, exhaustion is handled there; it does not fall through to a
weaker match for another resource. `repeat_last` is logged as reuse, and
`miss` reports exhaustion separately from a request never recorded.

## Example

This file may have any name ending in `.yaml`:

```yaml
rules:
  - id: explicit-fallback
    scope: {}
    priority: -100
    exhaustion: repeat_last
    unavailable: serve_identical

  - id: order-status
    scope:
      host: api.example.com
      path: /orders/status
      method: POST
      port: 443
    priority: 0
    exhaustion: miss
    unavailable: miss
    levels: [exact, normalized]
    ignore_query: [request_nonce]
    ignore_body_paths: [client.trace_id]
    keep_body_paths: [id]
```

This endpoint rule is complete: it chooses `miss` for both policies even though
the broader rule chooses `repeat_last` and `serve_identical`. Neither rule
authorizes ignoring `id` in an order body.

## JSONP callbacks

Ignoring a callback name is endpoint-specific. On a recorded JSONP hit, replay
rewrites the opening callback to the name requested by the page and records
`callback_rewrite`, subject to the checks in the architecture's “JSONP 回调名”
section. The shipped examples are Douban's ad endpoint (`callback`) and LeTV's
two SSO endpoints (`jsonp`). East Money's `version.jsonp` ignores its query
callback because the recorded static answer always calls a fixed name.
jQuery-shaped callback names have no global matching exemption.

## Frozen versions and offline evidence

A run reads each rule file once and freezes the resulting set. The replay
fingerprint records the source, original file text, file identities, per-file
SHA-256 values, the set digest and effective matcher configuration. Those bytes
are committed through the environment archive and WAL. Editing or deleting the
source directory cannot change a running replay or its saved snapshot.
Historical rules can be reconstructed and checked from the committed run
without accessing the original directory.

Unknown fields, invalid types, duplicate identities and conflicting scopes
are errors with rule/file context. Miss synthesis remains offline: it never
fetches a missing response from the real site. Synthetic responses, sequence
reuse and answers from an identical record remain distinguishable from
recorded responses in the replay log.
