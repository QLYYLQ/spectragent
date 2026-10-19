# Replay request rules

Rules for offline replay (`network_source: {source_id: archive}`), described in
`docs/architecture_design.md` §4.14. The shipped replay and branch configurations
select this directory with `matcher: {site_rules: configs/replay_rules}`.

Every `*.yaml` file contains a `rules` list. Filenames organize the evidence;
they do not select a host or imply a domain suffix. Each rule declares its own
scope, identity, priority and response exhaustion policy. All files form one
rule set.

A request uses the matching rule with the greatest integer `priority`. Two
matching rules at that priority are a configuration conflict, naming both
rules. File order and filename order never break a tie. Rules do not merge:
a narrower rule must repeat every behavior it needs, including `exhaustion`.
Recorded and live requests must select the same rule identity to become matching
candidates, including fuzzy and Range matching. Equal normalized keys from
different scopes do not authorize exchanging their responses.

The published `baseline.yaml` explicitly chooses `repeat_last` at priority
`-100` for all requests. The endpoint rules use priority `0` and make the same
choice explicitly. This preserves this experiment's selected response reuse
behavior. It is not a framework default. To make a stateful endpoint miss after
its recorded sequence is consumed, give it a matching rule with
`exhaustion: miss`. Requests without an applicable policy are refused; the
framework does not silently supply one.

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
| `exhaustion` | `repeat_last` or `miss` | Required. Reuse the last response of the selected sequence, or report `sequence_exhausted`. |
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

  - id: order-status
    scope:
      host: api.example.com
      path: /orders/status
      method: POST
      port: 443
    priority: 0
    exhaustion: miss
    levels: [exact, normalized]
    ignore_query: [request_nonce]
    ignore_body_paths: [client.trace_id]
    keep_body_paths: [id]
```

This endpoint rule is complete: it chooses `miss` even though the broader rule
chooses `repeat_last`. Neither rule authorizes ignoring `id` in an order body.

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
fetches a missing response from the real site. Synthetic responses and sequence
reuse remain distinguishable from recorded responses in the replay log.
