#!/usr/bin/env python3
"""Remove the connected black background from preset avatars and downscale.

The preset art is RGB on a pure-black background. We flood-fill from the
borders so only the background (the black region connected to the edges) becomes
transparent, while the character's own black outlines/spots/eyes stay intact.
"""
from __future__ import annotations

import os
from collections import deque

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
PRESETS_DIR = os.path.join(HERE, "..", "src", "assets", "presets")
OUT_SIZE = 256
BLACK_THRESH = 60  # max channel value still treated as background black


def remove_black_bg(im: Image.Image) -> Image.Image:
    im = im.convert("RGBA")
    w, h = im.size
    px = im.load()

    def is_bg(x: int, y: int) -> bool:
        r, g, b, a = px[x, y]
        return r <= BLACK_THRESH and g <= BLACK_THRESH and b <= BLACK_THRESH

    visited = bytearray(w * h)
    q: deque[tuple[int, int]] = deque()

    # Seed from every border pixel that is black.
    for x in range(w):
        for y in (0, h - 1):
            if not visited[y * w + x] and is_bg(x, y):
                visited[y * w + x] = 1
                q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if not visited[y * w + x] and is_bg(x, y):
                visited[y * w + x] = 1
                q.append((x, y))

    while q:
        x, y = q.popleft()
        px[x, y] = (0, 0, 0, 0)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h:
                idx = ny * w + nx
                if not visited[idx] and is_bg(nx, ny):
                    visited[idx] = 1
                    q.append((nx, ny))

    return im


def main() -> None:
    for name in sorted(os.listdir(PRESETS_DIR)):
        if not name.lower().endswith(".png"):
            continue
        path = os.path.join(PRESETS_DIR, name)
        im = Image.open(path)
        im = remove_black_bg(im)
        im = im.resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)
        im.save(path)
        print(f"processed {name}: {im.mode} {im.size}")


if __name__ == "__main__":
    main()
