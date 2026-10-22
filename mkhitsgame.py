#!/usr/bin/env python3

# Hitsgame -- Build cards for a music game
# Copyright 2023 Ruud van Asseldonk

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.

from __future__ import annotations

import html
import json
import os
import os.path
import subprocess
import sys
import tomllib

import qrcode  # type: ignore

from typing import Dict, Iterable, List, Literal, NamedTuple, Tuple
from collections import Counter

from qrcode.image.svg import SvgPathImage  # type: ignore


def metaflac_get_tags(fname: str) -> Tuple[str, Dict[str, str]]:
    """
    Return the metadata tags (Vorbis comments) from the file. If a tag is
    repeated, only the last value is kept. Returns the audio data md5sum as
    well.
    """
    out = subprocess.check_output(
        ["metaflac", "--show-md5sum", "--export-tags-to=-", fname],
        encoding="utf-8",
    )
    lines = out.splitlines()
    md5sum = lines[0]
    if md5sum == "00000000000000000000000000000000":
        print(f"{fname} has no embedded md5sum, re-encode with -f8")
        sys.exit(1)
    tags = [line.split("=", maxsplit=1) for line in lines[1:]]
    tags = [t for t in tags if len(t) == 2]
    return md5sum, {k.upper(): v for k, v in tags}


def ffprobe_get_tags(fname: str) -> Tuple[str, Dict[str, str]]:
    """
    Return the metadata tags from any audio file that ffmpeg can read (mp3,
    m4a, ogg, opus, wav, ...). Like metaflac_get_tags, returns an md5sum of
    the decoded audio data as well, so retagging the file does not change the
    output file name.
    """
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format_tags:stream_tags",
            "-of",
            "json",
            fname,
        ],
        encoding="utf-8",
    )
    probe = json.loads(out)
    # Vorbis-style containers (ogg, opus) store the tags on the stream, the
    # others on the container. Merge them, container tags take precedence.
    tags: Dict[str, str] = {}
    for stream in probe.get("streams", []):
        tags.update(stream.get("tags", {}))
    tags.update(probe.get("format", {}).get("tags", {}))

    out = subprocess.check_output(
        ["ffmpeg", "-v", "error", "-i", fname, "-map", "0:a", "-f", "md5", "-"],
        encoding="utf-8",
    )
    md5sum = out.strip().removeprefix("MD5=")
    return md5sum, {k.upper(): v for k, v in tags.items()}


def ffprobe_duration(fname: str) -> float:
    """
    Return the duration of the audio file in seconds.
    """
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            fname,
        ],
        encoding="utf-8",
    )
    return float(out)


INPUT_EXTENSIONS = (".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav")


class Track(NamedTuple):
    year: int
    fname: str
    title: str
    artist: str
    md5sum: str
    url: str

    @staticmethod
    def load(config: Config, fname: str) -> Track:
        """
        Create a track from an input filename.
        """
        if fname.endswith(".flac"):
            md5sum, tags = metaflac_get_tags(fname)
        else:
            md5sum, tags = ffprobe_get_tags(fname)
        title = tags.get("TITLE")
        artist = tags.get("ARTIST")
        date = tags.get("ORIGINALDATE", tags.get("DATE"))
        if title is None:
            print(f"{fname}: No TITLE tag present.")
            sys.exit(1)
        if artist is None:
            print(f"{fname}: No ARTIST tag present.")
            sys.exit(1)
        if date is None:
            print(f"{fname}: No ORIGINALDATE or DATE tag present.")
            sys.exit(1)

        url = config.url_prefix + md5sum + ".mp4"
        year = int(date[0:4])

        return Track(year, fname, title, artist, md5sum, url)

    def out_fname(self) -> str:
        return self.md5sum + ".mp4"

    def clip_args(self, config: Config) -> Tuple[List[str], List[str], List[str]]:
        """
        Return the ffmpeg input arguments, output arguments, and audio filters
        that cut a clip of `config.clip_seconds` out of the track, with a short
        fade in and out. The start of the clip is derived from the md5sum, so
        it is the same on every run, but differs per track.
        """
        if config.clip_seconds <= 0:
            return [], [], []

        duration = ffprobe_duration(self.fname)
        clip_s = min(config.clip_seconds, duration)
        fade_s = 1.0

        # Start somewhere between 15% and 70% into the track, to skip quiet
        # intros and outros, but make sure the clip still fits.
        frac = int(self.md5sum[:8], 16) / 0xFFFFFFFF
        max_start = min(0.70 * duration, duration - clip_s)
        min_start = min(0.15 * duration, max_start)
        start_s = min_start + frac * (max_start - min_start)

        in_args = ["-ss", f"{start_s:.3f}"]
        out_args = ["-t", f"{clip_s:.3f}"]
        filters = [
            f"afade=t=in:d={fade_s}",
            f"afade=t=out:st={clip_s - fade_s:.3f}:d={fade_s}",
        ]
        return in_args, out_args, filters

    def loudnorm_filters(
        self, config: Config, in_args: List[str], out_args: List[str]
    ) -> List[str]:
        """
        Return audio filters that normalize the loudness of the (clipped,
        mono) track to `config.loudness` LUFS. This measures the track in a
        first pass, so the second pass can apply a plain gain instead of
        dynamic compression.
        """
        if config.loudness == 0:
            return []

        target = f"loudnorm=I={config.loudness}:TP=-1.5:LRA=11"
        # Measure the mono downmix, that is what ends up in the output.
        mono = "aformat=channel_layouts=mono"
        # fmt:off
        result = subprocess.run([
            "ffmpeg", "-hide_banner", "-nostats",
            *in_args,
            "-i", self.fname,
            "-map", "0:a",
            *out_args,
            "-af", f"{mono},{target}:print_format=json",
            "-f", "null", "-",
        ], stderr=subprocess.PIPE, encoding="utf-8", check=True)
        # fmt:on
        # The measurements are printed as a json object near the end of stderr,
        # followed by ffmpeg's usual summary lines.
        m, _ = json.JSONDecoder().raw_decode(result.stderr, result.stderr.rindex("{"))
        return [
            mono,
            f"{target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
            f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
            f":offset={m['target_offset']}:linear=true",
        ]

    def encode_to_out(self, config: Config) -> None:
        """
        Encode the input audio file to an mp4 file in the output directory,
        under an unpredictable (but reproducible) name based on the audio
        md5sum. The resulting file has all metadata removed on purpose.
        """
        out_fname = os.path.join("out", self.out_fname())
        if os.path.isfile(out_fname):
            return
        clip_in_args, clip_out_args, fade_filters = self.clip_args(config)
        filters = self.loudnorm_filters(config, clip_in_args, clip_out_args)
        filters += fade_filters
        filter_args = ["-af", ",".join(filters)] if filters else []
        # fmt:off
        subprocess.check_call([
            "ffmpeg",
            *clip_in_args,
            "-i", self.fname,
            # Copy the audio stream, and no other stream (no cover art).
            "-map", "0:a",
            # By default ffmpeg copies metadata from the input file (file 0).
            # Disable this by copying from the non-existing file -1 instead.
            "-map_metadata", "-1",
            # Really disable metadata writing, including the encoder tag.
            "-fflags", "+bitexact",
            "-flags", "+bitexact",
            # Put the index at the start of the file, so browsers can start
            # playing before the whole file is downloaded.
            "-movflags", "+faststart",
            # Downmix stereo to mono (audio channels = 1). When we play the
            # game we listen on a phone speaker or bluetooth speaker anyway.
            "-ac", "1",
            # The loudnorm filter outputs 192kHz, bring that back to normal.
            "-ar", "48000",
            # Encode as AAC at 192kbps.
            "-b:a", "192k",
            "-c:a", "aac",
            *clip_out_args,
            *filter_args,
            out_fname,
        ])
        # fmt:on

    def qr_svg(self) -> Tuple[str, int]:
        """
        Render a QR code for the URL as SVG path, return also the side length
        (in SVG units, which by convention we map to mm).
        """
        from qrcode.compat.etree import ET  # type: ignore

        # A box size of 10 means that every "pixel" in the code is 1mm, but we
        # don't know how many pixels wide and tall the code is, so return that
        # too, the "pixel size". Note, it is independent of the specified box
        # size, we always have to divide by 10.
        qr = qrcode.make(self.url, image_factory=SvgPathImage, box_size=8)
        return ET.tostring(qr.path).decode("ascii"), qr.pixel_size / 10


class Config(NamedTuple):
    url_prefix: str
    font: str

    # Whether to include a grid in the output. This is good for inspecting the
    # output on a computer, but for print, unless you want to use the grid as
    # a guide for scissors to cut, you probably want to enable crop marks and
    # disable the grid, so a slight misalignment when cutting does not result
    # in a line near the edge of the card.
    grid: bool

    # Whether to include crop marks in the output that indicate where to cut.
    crop_marks: bool

    # Length of the clip to cut out of every track, in seconds. Set to 0 to
    # encode the entire track.
    clip_seconds: float = 0

    # Target loudness in LUFS (EBU R128) to normalize every track to, e.g.
    # -14 like YouTube and Spotify. Set to 0 to leave the volume as-is.
    loudness: float = 0

    @staticmethod
    def load(fname: str) -> Config:
        with open(fname, "rb") as f:
            toml = tomllib.load(f)
            return Config(**toml)


def line_break_text(s: str) -> List[str]:
    """
    Line break the artist and title so they (hopefully) fit on a card. This
    is a hack based on string lengths, but it's good enough for most cases.
    """
    if len(s) < 24:
        return [s]

    words = s.split(" ")
    char_count = sum(len(word) for word in words)

    # The starting situation is everything on the first line. We'll try
    # out every possible line break and pick the one with the most even
    # distribution (by characters in the string, not true text width).
    top, bot = " ".join(words), ""
    diff = char_count

    # Try line-breaking between every word.
    for i in range(1, len(words)):
        w1, w2 = words[:i], words[i:]
        t, b = " ".join(w1), " ".join(w2)
        d = abs(len(t) - len(b))
        if d < diff:
            top, bot, diff = t, b, d

    return [top, bot]


def render_text_svg(x_mm: float, y_mm: float, s: str, class_: str) -> Iterable[str]:
    """
    Render the artist or title, broken across lines if needed.
    """
    lines = line_break_text(s)
    line_height_mm = 6
    h_mm = line_height_mm * len(lines)

    for i, line in enumerate(lines):
        dy_mm = line_height_mm * (1 + i) - h_mm / 2
        yield (
            f'<text x="{x_mm}" y="{y_mm + dy_mm}" text-anchor="middle" '
            f'class="{class_}">{html.escape(line)}</text>'
        )


class Table(NamedTuple):
    """
    A table of cards laid out on two-sided paper.
    """

    cells: List[Track]

    # Hitster cards are 65mm wide, so on a 210mm wide A4 paper, we can fit
    # 3 columns and still have 7mm margin on both sides. That may be a bit
    # tight but either way, let's do 3 columns.
    width: int = 3

    # In the 297mm A4 paper, if we put 4 rows of 65mm that leaves 37mm of
    # margin, about 20mm top and bottom.
    height: int = 4

    @staticmethod
    def new() -> Table:
        return Table(cells=[])

    def append(self, track: Track) -> None:
        self.cells.append(track)

    def is_empty(self) -> bool:
        return len(self.cells) == 0

    def is_full(self) -> bool:
        return len(self.cells) >= self.width * self.height

    def render_svg(
        self, config: Config, mode: Literal["qr"] | Literal["title"], page_footer: str
    ) -> str:
        """
        Render the front of the page as svg. The units are in millimeters.
        """
        # Size of the page.
        w_mm = 210
        h_mm = 297
        # Size of the cards / table cells. In the Hitster game I have, the
        # cards have a side length of 65mm. But then fitting the table on A4
        # paper, it is possible, but the margins get very small to the point
        # where the crop marks may fall into the non-printable region. So make
        # the cards slightly smaller so they are safe to print.
        side_mm = 62

        tw_mm = side_mm * self.width
        th_mm = side_mm * self.height
        hmargin_mm = (w_mm - tw_mm) / 2
        vmargin_mm = (h_mm - th_mm) / 2
        # Align the table top-left with a fixed margin and leave more space at
        # the bottom, so we can put a page number there.
        vmargin_mm = hmargin_mm

        parts: List[str] = []
        parts.append(
            '<svg version="1.1" width="210mm" height="297mm" '
            'viewBox="0 0 210 297" '
            'xmlns="http://www.w3.org/2000/svg">'
        )
        parts.append(
            f"""
            <style>
            text {{ font-family: {config.font!r}; }}
            .year {{ font-size: 18px; font-weight: 900; }}
            .title, .artist, .footer {{ font-size: 5.2px; font-weight: 400; }}
            .title {{ font-style: italic; }}
            rect, line {{ stroke: black; stroke-width: 0.2; }}
            </style>
            """
        )
        if config.grid:
            parts.append(
                f'<rect x="{hmargin_mm}" y="{vmargin_mm}" '
                f'width="{tw_mm}" height="{th_mm}" '
                'fill="transparent" stroke-linejoin="miter"/>'
            )
        for ix in range(0, self.width + 1):
            x_mm = hmargin_mm + ix * side_mm
            if config.grid and ix > 0 and ix <= self.width:
                parts.append(
                    f'<line x1="{x_mm}" y1="{vmargin_mm}" '
                    f'x2="{x_mm}" y2="{vmargin_mm + th_mm}" />'
                )
            if config.crop_marks:
                parts.append(
                    f'<line x1="{x_mm}" y1="{vmargin_mm - 5}" x2="{x_mm}" y2="{vmargin_mm - 1}" />'
                    f'<line x1="{x_mm}" y1="{vmargin_mm + th_mm + 1}" x2="{x_mm}" y2="{vmargin_mm + th_mm + 5}" />'
                )

        for iy in range(0, self.height + 1):
            y_mm = vmargin_mm + iy * side_mm
            if config.grid and iy > 0 and iy <= self.height:
                parts.append(
                    f'<line x1="{hmargin_mm}" y1="{y_mm}" '
                    f'x2="{hmargin_mm + tw_mm}" y2="{y_mm}" />'
                )
            if config.crop_marks:
                parts.append(
                    f'<line x1="{hmargin_mm - 5}" y1="{y_mm}" x2="{hmargin_mm - 1}" y2="{y_mm}" />'
                    f'<line x1="{hmargin_mm + tw_mm + 1}" y1="{y_mm}" x2="{hmargin_mm + tw_mm + 5}" y2="{y_mm}" />'
                )

        for i, track in enumerate(self.cells):
            if mode == "qr":
                # Note, we mirror over the x-axis, to match the titles codes
                # when printed double-sided.
                ix = self.width - 1 - (i % self.width)
                iy = i // self.width
                qr_path, qr_mm = track.qr_svg()
                # I'm lazy so we center the QR codes, we don't resize them. If
                # the urls get longer, then the QR codes will cover a larger
                # area of the cards.
                x_mm = hmargin_mm + ix * side_mm + (side_mm - qr_mm) / 2
                y_mm = vmargin_mm + iy * side_mm + (side_mm - qr_mm) / 2
                parts.append(f'<g transform="translate({x_mm}, {y_mm})">')
                parts.append(qr_path)
                parts.append(f"</g>")

            if mode == "title":
                ix = i % self.width
                iy = i // self.width
                x_mm = hmargin_mm + (ix + 0.5) * side_mm
                y_mm = vmargin_mm + (iy + 0.5) * side_mm
                parts.append(
                    f'<text x="{x_mm}" y="{y_mm + 6.5}" text-anchor="middle" '
                    f'class="year">{track.year}</text>'
                )
                for part in render_text_svg(x_mm, y_mm - 19, track.artist, "artist"):
                    parts.append(part)
                for part in render_text_svg(x_mm, y_mm + 18, track.title, "title"):
                    parts.append(part)

        parts.append(
            f'<text x="{w_mm - hmargin_mm}" y="{h_mm - hmargin_mm}" text-anchor="end" '
            f'class="footer">{html.escape(page_footer)}</text>'
        )

        parts.append("</svg>")

        return "\n".join(parts)


def main() -> None:
    config = Config.load("mkhitsgame.toml")
    os.makedirs("out", exist_ok=True)
    os.makedirs("build", exist_ok=True)
    track_dir = "tracks"

    table = Table.new()
    tables: List[Table] = []
    tracks: List[Track] = []

    year_counts: Counter[int] = Counter()
    decade_counts: Counter[int] = Counter()

    for fname in os.listdir(track_dir):
        if not fname.endswith(INPUT_EXTENSIONS):
            continue
        fname_full = os.path.join(track_dir, fname)
        track = Track.load(config, fname_full)
        track.encode_to_out(config)
        tracks.append(track)

    tracks.sort()
    for track in tracks:
        table.append(track)
        year_counts[track.year] += 1
        decade_counts[10 * (track.year // 10)] += 1

        if table.is_full():
            tables.append(table)
            table = Table.new()

    # Append the final table, which may not be full.
    if not table.is_empty():
        tables.append(table)

    # Print statistics about how many tracks we have per year and per decade,
    # so you can tweak the track selection to make the distribution somewhat
    # more even.
    print("YEAR STATISTICS")
    for year, count in sorted(year_counts.items()):
        print(f"{year}: {count:2} {'#' * count}")

    print("\nDECADE STATISTICS")
    for decade, count in sorted(decade_counts.items()):
        print(f"{decade}s: {count:2} {'#' * count}")

    print("\nTOTAL")
    print(f"{sum(decade_counts.values())} tracks")

    # For every table, write the two pages as svg.
    pdf_inputs: List[str] = []
    for i, table in enumerate(tables):
        p = i + 1
        pdf_inputs.append(f"build/{p}a.svg")
        pdf_inputs.append(f"build/{p}b.svg")
        with open(pdf_inputs[-2], "w", encoding="utf-8") as f:
            f.write(table.render_svg(config, "title", f"{p}a"))
        with open(pdf_inputs[-1], "w", encoding="utf-8") as f:
            f.write(table.render_svg(config, "qr", f"{p}b"))

    # Combine the svgs into a single pdf for easy printing.
    cmd = ["rsvg-convert", "--format=pdf", "--output=build/cards.pdf", *pdf_inputs]
    subprocess.check_call(cmd)


if __name__ == "__main__":
    main()
