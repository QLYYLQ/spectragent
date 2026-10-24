# Replay request rules

Rules for offline replay (`network_source: {source_id: archive}`), described in
`docs/architecture_design.md` §4.14. The shipped replay and branch configurations
select this directory with `matcher: {site_rules: configs/replay_rules}`.

Every `*.yaml` file contains a `rules` list of request rules, a `page_urls` list of
page-URL rules (see "Page URLs in restoration" below), a `media_areas` list of media-area
rules (see "Media areas in restoration" below), or any of them together. Filenames organize the evidence;
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

Also on 2026-10-23, rules could first parse a field's value (see "Fields that hold fields"),
and two player requests got rules, from the offline branch replays of
`lookahead_15task_freeze_20261023` and the live requests of three of them, dumped and compared
with the recorded ones:

- `qq.com.yaml`: the player's video info request (`POST vd6.l.qq.com/proxyhttp`) parses
  `vinfoparam` and `adparam` and ignores the encrypted key, the client time and the random
  player and play ids inside them. The video, definition, encryption version, page and player
  fields stay. It also ignores `adparam.sspKey` and the ad context `sspAdParam`: the answer puts
  its ad part under the request's random `sspKey`, so a replayed page never reads a recorded ad
  answer, and what the ad context selects cannot reach it.
- `iqiyi.com.yaml`: the player's stream address request (`GET cache.video.iqiyi.com/dash`)
  ignores `tm`, `authKey` (md5 over `tm` and `tvid`, recomputed), the signature `vf`, the device
  id `k_uid` and the device fingerprint `dfp`, also inside the JSON field `bop` (`bop.dfp`).
  `tvid`, `bid` and the other fields stay. Video segments on `*.71edge.com/videos/vts/*` and the
  bandwidth probe `data.video.iqiyi.com/v.f4v` ignore only `random`, the client time the player
  adds as a cache buster; the byte range fields stay.

Across the 15 recordings of that root (35,943 recorded requests), these rules change the keys of
39 player info and 5 stream address requests and nothing else. Every group of records they join
has one HTTP status, the same video and definition; the joined video info answers differ only in
the server time, signature, test bucket and CDN tokens, and the joined ad answers sit under
different `sspKey` names. The segment and probe rules join no recorded requests.

Restoration was compared on the same recordings with the same branch configuration, only
`site_rules` changed, one fork per checkpoint: Tencent Video 23 forks, iQIYI 6 forks whose
checkpoints had failed the media check. Restorer `ok` verdicts did not change: Tencent Video 0 of
23 before and after, iQIYI 0 of 6, and every fork still fails the media check. What changed:

- Tencent Video: no player info request was served before; after, they were on all 23 forks. On
  8 forks (5 play page checkpoints of 28 and 3 home page checkpoints of 128 and 263) the restored
  page now builds the player as the recording did, and one of them (28, step 5) is now within the
  screenshot threshold (distance 10 → 3). On the search page of 28
  (step 25) the two recorded preview videos were missing before; after, both correspond and one
  plays, but the seek to the recorded time stops at 25.2 s of 42.4 s: the segments after that come
  from a CDN host the recording never contacted.
- The Tencent Video media check cannot pass on 22 of these 23 checkpoints whatever the requests:
  every page but the search page renders the player inside an iframe in a shadow root
  (`<fake-iframe-video>`), which the media reader lists as unreachable (`main/unplaced-0`), so the
  recorded step is not restorable (`docs/architecture_design.md` §4.4, accepted 2026-10-21 until a
  real site needed it).
- The play pages of 128 and 263 got their video info from `vinfo_proxy`, whose answer is
  encrypted; the replayed player then asks `proxyhttp` again with encryption version 9.2, which
  the recording never did, so no rule can serve it. A scratch run that also served the protobuf
  variant of `GetTabRemoteConfig` (keyed without its visitor id) changed nothing there, so that
  request is not the cause.
- iQIYI: every stream address request and every recorded segment request was served after, none
  before; the restored play page video still had no data, because the recording kept no body for
  its first segment (the page cancelled it while the body arrived), and the player then asked a
  host the recording never contacted.

On 2026-10-24 the Tencent Video forks of `lookahead_15task_20261024` were reviewed (recordings
28, 128 and 263; 30 of 70 checkpoints not restored). No rule or declaration was added; what
fails is not a request a rule can join:

- Play pages failing on screenshot distance alone (128 at steps 5 and 24, 28 at step 20): the
  restored page, whose player does not play offline, went quiet and was captured after a
  settle wait of 0.5 to 1.3 s, with the side panel and the header still placeholders. The
  recorded one waited the 5 s settle cap, because the playing video kept changing the page. The forks' own later frames match it (128 at step 24: distance 0).
  On play pages, 86 of the 108 checks taken right after a navigation fail on distance, against 44
  of 516 on a page already open. At step 20 of 28 the recorded page also opened a login dialog the
  replay never shows; the `vinfo_proxy` comment in `qq.com.yaml` says why its watch history fields
  stay in the key.
- Search pages (128 at step 13, 28 at step 1): the recorded observation shows the page still
  searching; the live search answer arrived 3.6 s and 1.5 s after the capture, the replayed one at
  once. Their document misses are a double navigation: pressing Enter in the search box navigated
  twice, 10 to 20 ms apart (the recordings hold one document request per search). The first
  request was gone when its answer was sent (`Invalid InterceptionId`), which counts as a
  document miss, and it had consumed a recorded document, so the second navigation got the next
  one. All 32 failed main-frame answers of that root are this case, on `/x/search/`.
- The front page and `/channel/movie`: the focus-map preview is a frame inside a shadow root
  covering the viewport (100% and 87% of it), so every check fails `media_mismatch`, and a
  media-area rule would never apply its mask. Re-verified with `v.qq.com/` declared, 0 of 116
  front-page checks pass before and 46 after (step 0 of 128 and 28, before the recorded preview
  started), no checkpoint among them; declaring `/channel/*` changes no verdict. It is not
  declared: the 2026-10-23 ruling covers video and search pages.
- 263 from step 15 is on a Meituan page: at step 14 the teacher clicked "了解详情" on an ad the
  recorded player showed (a 15 s `<video>` that started after its click on the player at step
  13). Offline the player shows its network error (it asks `proxyhttp` again with encryption
  version 9.2, see above) and plays no ad, so the click lands on the player's canvas and the tab
  stays on the play page.

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
| `parse_query` | list of `{field, format}` | Query parameters whose value holds more fields; see "Fields that hold fields". |
| `ignore_query` | list of name globs | Query fields omitted from the normalized key and fuzzy score. |
| `keep_query` | list of name globs | Fields retained even when an ignore pattern matches. |
| `parse_body_paths` | list of `{field, format}` | Body fields (form names, flattened JSON paths, GraphQL variable paths) whose value holds more fields. |
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

## Fields that hold fields

Some requests pack several fields into one field's value. Tencent Video's player sends
`proxyhttp` a JSON body whose `vinfoparam` is a URL-encoded query string holding the video id,
the definition, an encrypted key, the client time and random ids; iQIYI's `dash` request has a
query field `bop` holding a JSON document. Ignoring the whole field would drop the video id
too. A rule can declare such a field and the format of its value:

```yaml
    parse_body_paths:
    - field: vinfoparam      # an exact body path, not a glob
      format: urlencoded     # or json
    ignore_body_paths:
    - vinfoparam.cKey
    - vinfoparam.tm
    parse_query:
    - field: bop
      format: json
    ignore_query:
    - bop.dfp
```

The field is then keyed by its sub-fields, named `<field>.<name>`: `vinfoparam.vid`,
`vinfoparam.cKey`. A JSON value is flattened by full path below the field, as if it were
written inline (`bop.dfp`, `ssp.pre_ad_params.video.flow_id`). The `ignore_*` and `keep_*`
globs address sub-fields by these names, and every sub-field they do not ignore stays in the
key. The near-miss report lists differing sub-fields under the same names once a replay used
a rule that parses the field.

- `urlencoded` reads `name=value` parts joined by `&`, decoded like a query string (`+` and
  `%20` are the same space). `json` reads an object or an array.
- A value of another shape stays one field under its own name: an empty value, a plain token,
  a part without `=` or with an empty name, a JSON string or number.
- Parsing happens in the one canonicaliser, for recorded and live requests alike, and before
  the body's 4096-character cap and its digest. The exact level still compares the original
  bytes; the normalized level no longer distinguishes sub-field order or encoding.
- One level only: a sub-field of a parsed field is not parsed again.
- Refused when the rules load, naming the file and the rule: an unknown format or key, an
  empty field name, the same field declared twice, a field inside another parsed field, and a
  field the same rule ignores whole (parsing it would have no effect).
- Protobuf is not parsed: without a schema a length-delimited field may be a string, bytes or
  a nested message, and protobuf is usually the whole body rather than one field.

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

## Page URLs in restoration

Some pages write a parameter into their own address after loading, with
`history.replaceState` or client-side routing and no request, and its value changes on
every replay. Restoration verification then fails with `url_mismatch` although the page
is the same. A `page_urls` rule names such parameters for one site's pages; when a
branch or same-path replay compares a restored page URL with the recorded one, it leaves
them out. Request matching never reads `page_urls`. The rules are frozen and digested
with the request rules, so the replay's own `site_rules_snapshot` records which version
a check used. A run with `network_source: live` has no rule set, and its URLs compare
exactly. Architecture §4.14 "恢复：重定位与校验" describes the comparison.

| Key | Type | Meaning |
|---|---|---|
| `id` | string | Rule identity, unique within the whole set (request rules included). |
| `scope.host` | string | Required host glob, matched like a request rule's `host`; never implies subdomains. |
| `scope.path` | string or null | Path glob; null covers every path of the host. |
| `ignore_query` | list of name globs | At least one. Query parameters, by decoded name, left out of both URLs. |

A rule applies when its scope matches both URLs; every applicable rule contributes. The
comparison drops each `&`-separated query segment whose name a rule lists, whatever its
value and whether one side or both have it, and compares everything else byte for byte:
scheme, host and port, path, the remaining segments in their order and spelling, and the
fragment. The check's evidence keeps both URLs as observed and adds `url_ignored_query`
(the names left out) and `url_rule_ids` (the rules that left them out).

List a parameter only when the recording shows the page writes it and it selects no
content: no recorded request carries it (a document request that does makes it part of
the navigation), and the requests that fetch the page's content select it by other
fields. Keyword, page, order, id and filter parameters are never listed.
`page_urls.yaml` holds the published rules, each with its evidence. On 2026-10-23 they
were written from the branch forks of `lookahead_15task_freeze_20261023`: Bilibili's
search page `vt` (the last eight digits of the page clock in milliseconds; 168 checks
differed in it alone) and Tonghuashun's screener result page `sign` (the page clock in
milliseconds when the question was asked; 8 checks). Both values change on every replay
because of the shared replay clock. The forks were replayed again with the same branch
configuration and only `page_urls.yaml` added, one fork per checkpoint. Restorer `ok`
checkpoint verdicts, before → after: Bilibili 0 → 4 of 5 (`118_180a3f31` at steps 17,
21, 25 and 29; `158_f5a61bf9` at step 9 still fails on media), Tonghuashun 0 → 2 of 2
(`44_5b851f00` and `134_d7b73ce7` at step 29). Over those seven forks the passing checks
rose from 110 to 148 of 166, and no check failed `url_mismatch` any more.

## Media areas in restoration

On video sites the playback of a recording and of its offline replay can rarely be made
the same: stream keys are encrypted per session, autoplay previews start at another
moment, a player does not load offline, and stacked `<video>` elements trade places.
Restoration verification then fails with `media_mismatch` and with a screenshot distance
that sits in the player, although the rest of the page is the same. A `media_areas` rule
declares such a page. Recording is unchanged (videos keep playing, so the data stays
realistic), and so is media alignment (`media: align`). When a branch or same-path replay
verifies a restored observation of a declared page:

1. the media playback state is still measured and kept in `restoration.evidence[*].media`,
   but the restorer does not judge it: no `media_mismatch` from comparisons, unreachable
   frames or alignment outcomes;
2. the boxes both screenshots' media facts measured are masked out of both images before
   the screenshot distance: every `<video>` / `<audio>` element and every embedded frame
   whose own media read did not answer (what it plays is unknown; Tencent Video's player is
   such a frame). The mask is the union of both sides' boxes, clipped to the viewport;
3. past 75% of the image masked (`MAX_MASKED_SHARE`), the rest says too little about the
   page: the mask is not applied and the whole images are compared, as on an undeclared
   page, never a pass by a nearly blank image;
4. the check's evidence gains `media_areas`: the rules (`rule_ids`), every masked box
   (`areas`: side, `media_element` or `unreachable_frame`, its name and box in CSS pixels),
   the share of the image masked (`masked_share`), the distance over the whole images
   (`unmasked_distance`) and after masking (`masked_distance`), and `mask_applied`.

Everything else still counts: URL, element signatures, document misses, action outcomes and
every pixel outside the masked boxes. A player drawn on a `<canvas>` has no box in the media
facts and is not masked. A run with `network_source: live` has no rule set.

| Key | Type | Meaning |
|---|---|---|
| `id` | string | Rule identity, unique within the whole set (request and page-URL rules included). |
| `scope.host` | string | Required host glob, matched like a page-URL rule's; never implies subdomains. |
| `scope.path` | string or null | Path glob; null covers every path of the host. |

A rule applies when its scope matches both page URLs. Declare only video and search pages
whose checks the recorded evidence shows failing on media state or on a screenshot distance
concentrated in player areas; never a site that is not a video site. Each rule carries a
comment naming the site, why and the evidence run.

`media_areas.yaml` holds the published rules: Tencent Video's play and search pages,
iQIYI's play and search pages, IGN's review articles and search page, Bilibili's video
pages and search host. They were written on 2026-10-23 from the forks of
`lookahead_15task_freeze_20261023`. Re-verifying the saved observations of all 334 forks
with this file added (same observations, new measurement and verdict), the checkpoint
checks that passed went, before → after: Tencent Video 0 → 52 of 70, IGN 22 → 40 of 70,
iQIYI 31 → 33 of 68 (most of its forks restore pages of other sites: Mango TV and a game
advertisement), Bilibili 34 → 38 of 54, Tonghuashun unchanged at 66 of 72; no check that
passed before failed after. Fourteen of the forks were replayed again with the same branch
configuration, once with main's rules and once with this file added. Restorer `ok` checks,
before → after: Tencent Video 0 → 16 of 30 (checkpoints 0 → 3 of 3), IGN 13 → 26 of 40
(0 → 3 of 4), iQIYI 1 → 5 of 16 (1 → 1 of 4), Bilibili 19 → 21 of 26 (0 → 2 of 3). The
masked share was at most 37% on Tencent Video, 61% on IGN, 67% on iQIYI and 33% on
Bilibili; every mask was applied. The checks that still fail differ outside the media
boxes (pages still loading, scrolled apart, other result lists) or on undeclared pages
(front pages, other sites).

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
