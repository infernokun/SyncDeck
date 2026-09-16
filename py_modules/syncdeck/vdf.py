"""Minimal reader for Valve's text KeyValues format (.acf / .vdf).

Only what appmanifest and libraryfolders files actually use: quoted keys,
quoted string values, nested braces, // comments. No binary VDF, no macros,
no conditionals -- adding them would be dead code here.
"""

from __future__ import annotations

from typing import Any


def loads(text: str) -> dict:
    tokens = _tokenize(text)
    index = 0
    root: dict = {}

    def parse_block(start: int, into: dict) -> int:
        i = start
        while i < len(tokens):
            token = tokens[i]
            if token == "}":
                return i + 1
            key = token
            i += 1
            if i >= len(tokens):
                break
            if tokens[i] == "{":
                child: dict = {}
                i = parse_block(i + 1, child)
                into[key] = child
            else:
                into[key] = tokens[i]
                i += 1
        return i

    # A top-level file is one or more `"key" { ... }` pairs.
    parse_block(index, root)
    return root


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return loads(handle.read())


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if char in " \t\r\n":
            i += 1
        elif char == "/" and i + 1 < length and text[i + 1] == "/":
            newline = text.find("\n", i)
            i = length if newline == -1 else newline + 1
        elif char in "{}":
            tokens.append(char)
            i += 1
        elif char == '"':
            i += 1
            chunks: list[str] = []
            while i < length and text[i] != '"':
                if text[i] == "\\" and i + 1 < length:
                    chunks.append(_unescape(text[i + 1]))
                    i += 2
                else:
                    chunks.append(text[i])
                    i += 1
            tokens.append("".join(chunks))
            i += 1
        else:
            start = i
            while i < length and text[i] not in ' \t\r\n"{}':
                i += 1
            tokens.append(text[start:i])
    return tokens


def _unescape(char: str) -> str:
    return {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(char, char)


def get_path(data: dict, *keys: str, default: Any = None) -> Any:
    """Case-insensitive nested lookup; Valve is inconsistent about casing."""
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        lowered = {k.lower(): v for k, v in current.items()}
        if key.lower() not in lowered:
            return default
        current = lowered[key.lower()]
    return current
