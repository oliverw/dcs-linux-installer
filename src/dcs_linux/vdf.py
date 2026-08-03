"""Valve KeyValues, the format Steam keeps its library and app state in.

`libraryfolders.vdf`, `appmanifest_*.acf` and `config.vdf` are all this one
format: quoted keys, quoted values, braces for nesting. Steam is not
consistent about capitalisation between files, so keys are lower-cased on the
way in and every lookup here is case-insensitive.

Parsing is deliberately forgiving. These files belong to another program and
may be half-written when we read them; an unparseable config means "no
information", never an exception.
"""

from __future__ import annotations

import re

KeyValues = dict[str, "str | KeyValues"]

# A quoted string, a brace, or a comment to the end of the line. The string
# alternative comes first, so `//` inside a quoted path stays part of it.
_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*')

_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def parse(text: str) -> KeyValues:
    """The document as nested dicts, with lower-cased keys."""
    root: KeyValues = {}
    stack: list[KeyValues] = [root]
    key: str | None = None

    for match in _TOKEN.finditer(text):
        string, brace = match.group(1), match.group(2)
        if string is not None:
            if key is None:
                key = string
            else:
                stack[-1][_unescape(key).lower()] = _unescape(string)
                key = None
        elif brace == "{":
            child: KeyValues = {}
            stack[-1][_unescape(key or "").lower()] = child
            stack.append(child)
            key = None
        elif brace == "}":
            # A stray closing brace would otherwise pop the root away.
            if len(stack) > 1:
                stack.pop()
            key = None

    return root


def dig(node: object, *keys: str) -> str | None:
    """The string at a path of keys, or None if it is missing or a block."""
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key.lower())
    return node if isinstance(node, str) else None


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda match: _ESCAPES.get(match.group(1), match.group(1)), value)
