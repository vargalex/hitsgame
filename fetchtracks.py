#!/usr/bin/env python3

# Hitsgame -- Fetch source tracks from YouTube for mkhitsgame.py
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.

"""
Usage:

    fetchtracks.py "Artist | Title | Year [| url]" ...
    fetchtracks.py list.txt

Every argument is either a track description, or a file with one track
description per line. The fields are separated by "|", so artists and titles
may contain " - ". A track description looks like

    Szécsi Pál | Kismadár | 1971
    Queen | Bohemian Rhapsody | 1975 | https://www.youtube.com/watch?v=fJ9rUzIMcZQ

When no url is given, the first YouTube search hit for "Artist Title" is used.
The audio is downloaded without re-encoding, tagged with TITLE, ARTIST and
DATE, and written to the tracks directory as "Artist - Title.<ext>", ready
for mkhitsgame.py. Tracks that already exist there are skipped.

Lines starting with # and empty lines are ignored. The year has to be given,
because the upload date on YouTube is not the release year.
"""

from __future__ import annotations

import os
import os.path
import subprocess
import sys
import tempfile

from typing import List, NamedTuple, Optional


class Item(NamedTuple):
    artist: str
    title: str
    year: int
    url: Optional[str]

    @staticmethod
    def parse(line: str) -> Item:
        fields = [f.strip() for f in line.split("|")]
        if len(fields) not in (3, 4) or not fields[2].isdigit():
            raise ValueError(f"Expected 'Artist | Title | Year [| url]', got: {line}")
        artist, title, year = fields[:3]
        url = fields[3] if len(fields) == 4 else None
        return Item(artist, title, int(year), url)

    def source(self) -> str:
        return self.url if self.url is not None else f"ytsearch1:{self.artist} {self.title}"

    def out_basename(self) -> str:
        name = f"{self.artist} - {self.title}"
        return name.replace("/", "_").lstrip(".")


def read_items(args: List[str]) -> List[Item]:
    items: List[Item] = []
    for arg in args:
        if os.path.isfile(arg):
            with open(arg, encoding="utf-8") as f:
                lines = [ln.strip() for ln in f]
            lines = [ln for ln in lines if ln and not ln.startswith("#")]
        else:
            lines = [arg]
        items.extend(Item.parse(ln) for ln in lines)
    return items


def fetch(item: Item, track_dir: str, tmp_dir: str) -> None:
    existing = [
        f for f in os.listdir(track_dir) if os.path.splitext(f)[0] == item.out_basename()
    ]
    if existing:
        print(f"skip   {item.artist} - {item.title}: {existing[0]} exists")
        return

    # Download the best audio stream as-is (usually opus or m4a), and let
    # yt-dlp tell us the resulting file name and what video it picked.
    # fmt:off
    out = subprocess.check_output([
        "yt-dlp",
        "--quiet", "--no-warnings", "--no-playlist", "--no-simulate",
        "-f", "bestaudio",
        "-x", "--audio-format", "best",
        "-o", os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "--print", "after_move:filepath",
        "--print", "after_move:%(webpage_url)s %(title)s",
        item.source(),
    ], encoding="utf-8")
    # fmt:on
    dl_fname, picked = out.strip().split("\n", 1)
    ext = os.path.splitext(dl_fname)[1]
    out_fname = os.path.join(track_dir, item.out_basename() + ext)

    # Copy the audio stream without re-encoding, drop YouTube's metadata and
    # write only the tags that mkhitsgame.py needs.
    # fmt:off
    subprocess.check_call([
        "ffmpeg", "-loglevel", "error", "-y",
        "-i", dl_fname,
        "-map", "0:a",
        "-c", "copy",
        "-map_metadata", "-1",
        "-metadata", f"title={item.title}",
        "-metadata", f"artist={item.artist}",
        "-metadata", f"date={item.year}",
        out_fname,
    ])
    # fmt:on
    os.remove(dl_fname)
    print(f"ok     {item.artist} - {item.title} - {item.year}: {picked}")


def main() -> None:
    if len(sys.argv) < 2 or "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__.strip())
        sys.exit(1)

    try:
        items = read_items(sys.argv[1:])
    except ValueError as exc:
        print(exc)
        sys.exit(1)
    track_dir = "tracks"
    os.makedirs(track_dir, exist_ok=True)

    failed: List[Item] = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        for item in items:
            try:
                fetch(item, track_dir, tmp_dir)
            except subprocess.CalledProcessError as exc:
                print(f"FAILED {item.artist} - {item.title}: {exc}")
                failed.append(item)

    if failed:
        print(f"\n{len(failed)} of {len(items)} tracks failed:")
        for item in failed:
            print(f"  {item.artist} - {item.title} - {item.year}")
        sys.exit(1)


if __name__ == "__main__":
    main()
