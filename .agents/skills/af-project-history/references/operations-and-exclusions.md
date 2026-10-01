# Operations, local assets, and excluded memories

## Localhost HTTP clients

The development machine has historically exported HTTP(S) proxy variables. A client
probing a loopback service can accidentally send `127.0.0.1` through that proxy,
turning an immediate connection failure into a read timeout.

For repository-owned clients that must talk directly to localhost, explicitly disable
environment proxy inheritance and catch the transport-level error family appropriate
to the client. Remote LLM clients are a different case and may need the proxy. Verify
the current environment rather than assuming a particular port or proxy product.

## Remote execution aliases

Historical Claude hooks exposed `@local`, `@dev`, and `@hub` routing aliases. They are
not Codex primitives. If a task still needs those machines, inspect the currently
available execution tooling first; do not embed host addresses or credential paths in
skills or documentation.

## Ignored visual assets are intentional

`assets/` is the accepted Spectragent icon and visual-identity source set. It is
deliberately ignored by git and is not disposable cleanup residue.

Two accepted visual invariants:

1. Spectrum colors use discrete hard-edged bands, not hue gradients.
2. Every SVG color has a literal-hex presentation fallback; CSS variables may enhance
   it only behind feature detection so non-browser rasterizers do not render black.

Run the asset verifier before changing the set. Because the directory is ignored,
avoid cleanup or reset operations that could erase the only local copy.

## Memories intentionally not promoted as current knowledge

- **"Codex has no image-generation tool"** was invalidated by the current image
  generation capability. Choose tools from the current session, not this old claim.
- **Framework-flow snapshot** was stale about memory negotiation, structured output,
  and available strategies; current architecture and tests replace it.
- **Provider price/cache tables** are dated research. Preserve the modeling lesson,
  not numeric prices or model allowlists.
- **Credential locations, private addresses, Claude permission allowlists, and raw
  session/tool logs** are excluded to avoid leaking local security state.
- **A historical conscribe release preference** is not standing permission to push a
  repository or upload to PyPI. Obtain current, explicit authorization for each
  outward-facing release operation.
