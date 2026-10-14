# Replay site rules

Per-site rules for offline replay (`network_source: { source_id: archive }`,
`docs/architecture_design.md` §4.14 "站点规则"). Point an archive source at this
directory with `matcher: { site_rules: configs/replay_rules }`; the shipped
replay and branch configurations in `configs/flywheel/` do.

- One file per domain: `<domain>.yaml` (only `*.yaml` files are read; this README is
  ignored). A request's host picks the file of its **longest suffix** on label
  boundaries — `a.b.example.com` tries `a.b.example.com.yaml`, `b.example.com.yaml`,
  `example.com.yaml`, `com.yaml` — so a file named after the registrable domain
  covers its subdomains, with no Public Suffix List involved.
- At most one file applies to a request; rules never merge across files.
- The directory's digest (each file's name and sha256, sorted) is recorded in the
  replay run's fingerprint, so two replays under different rules are told apart.
- An unknown key, a value of the wrong type or a file that is not a YAML mapping
  fails the replay's setup, naming the file. An empty file is a rule that changes
  nothing.

## The shipped rules

The 25 files here were written on 2026-10-14 from twelve recordings of Chinese
sites (Qidian, Ctrip, 21st Century Business Herald, NetEase news and tech,
East Money, Douban Books, AcFun, Bank of China, Jiemian, LeTV, The Paper,
V2EX), by comparing each replay miss with the recorded request nearest to it,
and committed as one set: the analytics rules change which requests a page
retries and so how its random draws and request counters line up, so a subset
can replay worse than none.

- One file per registrable domain. Each comment says what a parameter is and
  gives its evidence; `A vs B` pairs the two values one request carried in a
  recording and in its replay.
- `ignore_query` lists only values that change from visit to visit and select
  nothing: random numbers, timestamps, visitor, session and trace ids, cache
  busters, JSONP callback names. A parameter that selects content (an article
  id, a page size, a search term, an ad slot) is never ignored; analytics
  pixels whose event fields differ are answered 204 with `synthesize_204`
  instead.
- **Risk kept on purpose**: `ctrip.com.yaml` ignores the top-level `id` of
  every ctrip.com request body (the ad endpoint's random request id). A rule
  covers its whole domain, so an endpoint that selects content by a top-level
  `id` would be served any recorded call of that endpoint. The file says so.

## JSONP callback parameters

A JSONP request names the function its answer calls (`?callback=NAME`), and
the page invents the name: jQuery's `jQuery<random digits>_<counter>`, or a
random suffix (`erebor_<32 hex>`, `vjs_<time><2 digits>`). When the archive
serves a recorded JSONP answer to a request that asks for another name, the
replay writes the requested name over the recorded one at the call the answer
opens with, and records it in the replay log (`callback_rewrite`;
`docs/architecture_design.md` §4.14 "JSONP 回调名"). So a callback parameter
with a random part may be ignored here: `douban.com.yaml` (`callback`) and
`le.com.yaml` (`jsonp`). An answer that never uses the parameter, a static file
calling a fixed name, is the same bytes whatever the request asks
(`eastmoney.com.yaml`, `callback`). jQuery's names need no rule: the built-in
normalisation already leaves them out of the match key.

## Schema

Every key is optional.

| Key | Type | Effect |
|---|---|---|
| `ignore_query` | list of name globs | Query parameters left out of the normalised key and the fuzzy score (`session*`). |
| `ignore_body_paths` | list of path globs | Body parameters left out of the key: form field names, flattened JSON paths (`client.ts`, `items.*.nonce`), GraphQL variables matched within one operation (`variables.cursor`). |
| `ignore_headers` | list of name globs | Request headers that never count in the header tie-break (case-insensitive). |
| `fuzzy` | bool | `on` / `true`: the fuzzy level for this site even for documents and non-GET requests, and even when `matcher.levels` lacks `fuzzy`. YAML 1.1 reads a bare `on` as `true`. |
| `synthesize_204` | list of URL globs | Misses of these URLs are benign: the miss policy answers 204 (analytics, telemetry). |
| `response_echo` | list of `{url, request_path, response_path}` | On a hit for a URL matching `url`, copy the values at `request_path` in the request's JSON (or a form parameter holding JSON) into the positions at `response_path` of the served JSON — a request id the page checks for. A `*` segment walks every element; one request value is written to every position. |

Globs are `fnmatch` patterns (`*` also crosses dots); URL globs match the URL
without its fragment.

## Example

`example.com.yaml`:

```yaml
ignore_query: [session_id, "trk_*"]
ignore_body_paths: [client.ts, "variables.cursor"]
ignore_headers: [x-request-id]
fuzzy: on
synthesize_204:
  - "https://stats.example.com/*"
response_echo:
  - url: "https://example.com/aura*"
    request_path: message.actions.*.id
    response_path: actions.*.id
```
