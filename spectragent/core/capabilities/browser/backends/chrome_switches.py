"""Chrome command-line switches: merging and contradiction detection.

Chrome keeps only the **last** occurrence of most switches. For three of
them that silently drops configuration rather than overriding it, because
their values are lists:

- ``--js-flags`` carries V8 flags (``--random-seed=42 --expose-gc``);
- ``--disable-features`` / ``--enable-features`` carry comma-separated
  feature names.

``CHROME_DEFAULT_ARGS`` and ``disable_security`` each carried a
``--disable-features`` list, so turning ``disable_security`` on threw the
default list away. And ``replayability: pinned`` adds its own
``--js-flags`` and ``--disable-features``, which a provider's
``extra_args`` must not erase. :func:`merge_chrome_switches` folds every
occurrence of those three into one switch each.

This module belongs to the Chrome command-line layer, not to one provider
(``docs/architecture_design.md`` §4.9.x): any provider that launches Chrome
itself builds its argv through it.
"""

from collections.abc import Sequence

#: The switches that cut a replaying browser off from the network (M30, S2a):
#: every host name resolves to nothing except exact ``localhost``, and no
#: proxy — Chrome on macOS uses the system proxy and ignores ``http_proxy``,
#: and with a proxy the resolver rules cover only the proxy host. Two argv
#: elements, no shell quoting.
NETWORK_ISOLATION_ARGS: tuple[str, ...] = (
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
    "--no-proxy-server",
)
#: Switches that decide where the browser's traffic goes. They are left out
#: of the launch-flag digest on both sides of a replay — a recording's
#: fixture ``MAP`` rules and the replay's isolation switches would otherwise
#: never compare equal — and a provider that sets one of its own undoes the
#: isolation.
NETWORK_ROUTING_SWITCHES = frozenset(
    {
        "--host-resolver-rules",
        "--no-proxy-server",
        "--proxy-server",
        "--proxy-pac-url",
        "--proxy-auto-detect",
        "--proxy-bypass-list",
    }
)


#: Switches whose value is a space-separated list of V8 flags.
_SPACE_LISTS = frozenset({"--js-flags"})
#: Switches whose value is a comma-separated list of feature names.
_COMMA_LISTS = frozenset({"--disable-features", "--enable-features"})
#: The pairs where one list switches off what the other switches on.
_OPPOSITE_FEATURES = {
    "--disable-features": "--enable-features",
    "--enable-features": "--disable-features",
}


def split_switch(arg: str) -> tuple[str, str | None]:
    """``"--lang=en-US"`` → ``("--lang", "en-US")``; ``"--mute-audio"`` → ``(…, None)``."""
    name, sep, value = arg.partition("=")
    return name, (value if sep else None)


def without_routing_switches(flags: Sequence[str]) -> list[str]:
    """*flags* minus every network routing switch (:data:`NETWORK_ROUTING_SWITCHES`)."""
    return [flag for flag in flags if split_switch(flag)[0] not in NETWORK_ROUTING_SWITCHES]


def _space_items(value: str | None) -> list[str]:
    return [item for item in (value or "").split() if item]


def _comma_items(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def merge_chrome_switches(args: Sequence[str]) -> list[str]:
    """Fold every ``--js-flags`` / ``--disable-features`` / ``--enable-features``.

    Each of the three becomes one switch at the position of its first
    occurrence: V8 flags space-joined in order, feature names comma-joined
    and de-duplicated (first occurrence wins the position). Every other
    argument is kept exactly as given and in order. A list switch whose
    merged value is empty is dropped.
    """
    merged: dict[str, list[str]] = {}
    out: list[str | tuple[str]] = []
    for arg in args:
        name, value = split_switch(arg)
        if name in _SPACE_LISTS or name in _COMMA_LISTS:
            items = _space_items(value) if name in _SPACE_LISTS else _comma_items(value)
            if name not in merged:
                merged[name] = []
                out.append((name,))
            bucket = merged[name]
            if name in _SPACE_LISTS:
                bucket.extend(items)
            else:
                bucket.extend(item for item in items if item not in bucket)
            continue
        out.append(arg)

    result: list[str] = []
    for entry in out:
        if isinstance(entry, tuple):
            name = entry[0]
            items = merged[name]
            if items:
                joiner = " " if name in _SPACE_LISTS else ","
                result.append(f"{name}={joiner.join(items)}")
        else:
            result.append(entry)
    return result


def v8_flags(args: Sequence[str]) -> dict[str, str | None]:
    """V8 flag name → value across every ``--js-flags`` in ``args`` (last wins)."""
    flags: dict[str, str | None] = {}
    for arg in args:
        name, value = split_switch(arg)
        if name in _SPACE_LISTS:
            for flag in _space_items(value):
                flag_name, flag_value = split_switch(flag)
                flags[flag_name] = flag_value
    return flags


def _features(args: Sequence[str], switch: str) -> set[str]:
    found: set[str] = set()
    for arg in args:
        name, value = split_switch(arg)
        if name == switch:
            found.update(_comma_items(value))
    return found


def contradicting_switch(arg: str, owned: Sequence[str]) -> str | None:
    """The switch in ``owned`` that ``arg`` contradicts, or ``None``.

    ``owned`` is a set of switches some other party is the only source of
    (``replayability: pinned``'s launch args). ``arg`` contradicts it when:

    - it is an ordinary switch ``owned`` also sets, with another value;
    - it is a ``--js-flags`` setting a V8 flag ``owned`` sets differently;
    - it is a feature list naming a feature ``owned`` lists the other way
      (enabling what ``owned`` disables, or the reverse).

    Repeating an owned switch with the *same* value is not a contradiction,
    and neither is adding unrelated flags or features to a list switch:
    :func:`merge_chrome_switches` folds those into one.
    """
    name, value = split_switch(arg)
    if name in _SPACE_LISTS:
        owned_flags = v8_flags(owned)
        for flag_name, flag_value in v8_flags([arg]).items():
            if flag_name in owned_flags and not _same_value(owned_flags[flag_name], flag_value):
                return f"--js-flags={flag_name}" + (
                    f"={owned_flags[flag_name]}" if owned_flags[flag_name] is not None else ""
                )
        return None
    if name in _COMMA_LISTS:
        opposite = _OPPOSITE_FEATURES[name]
        clash = sorted(_features([arg], name) & _features(owned, opposite))
        return f"{opposite}={','.join(clash)}" if clash else None
    for owned_arg in owned:
        owned_name, owned_value = split_switch(owned_arg)
        if owned_name == name and not _same_value(owned_value, value):
            return owned_arg
    return None


def _same_value(a: str | None, b: str | None) -> bool:
    """Equal switch values; numbers compare numerically (``1`` == ``1.0``)."""
    if a == b:
        return True
    if a is None or b is None:
        return False
    try:
        return float(a) == float(b)
    except ValueError:
        return False
