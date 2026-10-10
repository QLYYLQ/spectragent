# Replay site rules

Per-site rules for offline replay (`network_source: { source_id: archive }`,
`docs/architecture_design.md` §4.14 "站点规则"). Point an archive source at this
directory with `matcher: { site_rules: configs/replay_rules }`.

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
