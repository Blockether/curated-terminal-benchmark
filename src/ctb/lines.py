"""Read an agent log in parts: a runaway agent can write lines and files larger than the memory."""

from collections.abc import Iterator
from pathlib import Path

#: Bytes that one read takes.
CHUNK = 1 << 20


def lines(path: Path, errors: str = "strict", prefix: str = "") -> Iterator[str]:
    """The lines of a UTF-8 file that start with prefix, without their line end.

    A line without the prefix is dropped before it is read whole. Lines end only at \\n;
    str.splitlines() also splits at \\x85 or \\u2028 inside a string.
    """
    start = prefix.encode()
    parts: list[bytes] = []
    size, known, keep = 0, False, False
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            pos = 0
            while pos < len(chunk):
                end = chunk.find(b"\n", pos)
                stop = len(chunk) if end < 0 else end
                if keep or not known:
                    parts.append(chunk[pos:stop])
                    size += stop - pos
                    if not known and (size >= len(start) or end >= 0):
                        known, keep = True, b"".join(parts).startswith(start)
                if end < 0:
                    break
                if keep:
                    yield b"".join(parts).decode("utf-8", errors)
                parts, size, known, keep = [], 0, False, False
                pos = end + 1
    if parts and b"".join(parts).startswith(start):
        yield b"".join(parts).decode("utf-8", errors)
