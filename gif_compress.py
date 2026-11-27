"""Fit a GIF into the sticker channel's size, frame and duration limits.

WeChat only renders a sticker bubble for content sent through the sticker
channel. Native prepares one sticker from an image source, and the prepared
output has hard limits: 1 MiB, 200 frames, 10 s and a 1024 px longest edge.
Most chat GIFs are far above that, so the source is re-encoded here first;
Native then keeps the uploaded bytes unchanged, because a GIF source prepared
without an edge limit and without a trim is preserved rather than re-encoded.
This module is therefore the only place the animation is altered.

The gear is chosen from the source's own shape instead of by trial encoding: a
measured bytes-per-pixel budget predicts the output size at each candidate edge
length and palette, and the first gear that fits under 1 MiB is encoded once.
The budget comes from measuring 186 chat GIFs: 256-colour frames stayed below
0.22 bytes per pixel, 128-colour frames below 0.26, and 64-colour frames below
0.31, because a smaller palette spends proportionally more of each frame on its
header.

Two properties a re-encode cannot keep: an originally transparent frame is
composited onto white, and a timeline longer than 10 s is scaled down, so a long
animation plays slightly faster instead of losing its ending.
"""

import io
import math
import time

from PIL import Image, ImageSequence

MODES = ("none", "auto", "conservative")

MAX_STICKER_BYTES = 1024 * 1024
MAX_STICKER_FRAMES = 200
MAX_STICKER_DURATION_MS = 10_000
MAX_STICKER_EDGE = 1024
MAX_STICKER_PIXELS = 16_000_000
MIN_FRAME_MS = 20
DEFAULT_FRAME_MS = 100
MAX_ENCODE_PIXELS = 48_000_000
COMPRESSION_TIMEOUT = 30.0

# (longest edge, palette colours), best output first. "auto" may also drop
# frames; "conservative" keeps every frame and only trades size and colour.
GEARS = (
    (1024, 256),
    (512, 256),
    (320, 256),
    (240, 128),
    (160, 64),
)
CONSERVATIVE_GEARS = (
    (1024, 256),
    (768, 256),
    (512, 128),
    (384, 128),
    (256, 64),
    (192, 32),
    (128, 16),
)

# Upper bound on output bytes per (scaled pixel x kept frame), by palette size.
# Fewer colours cost more per pixel, because each frame header stays the same
# size while the frame body shrinks, so the bound rises with the palette.
BUDGET = {256: 0.25, 128: 0.30, 64: 0.36, 32: 0.40, 16: 0.46}


def fit_sticker_gif(
    data: bytes, mode: str, *, timeout: float = COMPRESSION_TIMEOUT
) -> bytes | None:
    """Return GIF bytes the sticker channel can prepare, or None to leave the send alone.

    None means the caller keeps its previous behaviour: compression is off, the
    source is not a readable GIF, or the source cannot be fitted at all (a frame
    above 16 Mi pixels, or a timeline too long for 200 frames of 20 ms).
    """
    if mode not in ("auto", "conservative"):
        return None
    try:
        size, durations = _scan(data)
    except Exception:
        return None
    if not durations or size[0] * size[1] > MAX_STICKER_PIXELS:
        return None
    if _fits(data, size, durations):
        return data
    stride = _stride(len(durations), mode)
    if stride is None:
        return None
    gears = GEARS if mode == "auto" else CONSERVATIVE_GEARS
    if mode == "auto":
        # A short but very busy GIF can exceed 1 MiB at every edge length while
        # staying under the frame cap; dropping frames is then the only lever.
        stride = _fitted_stride(size, durations, stride, gears)
        if stride is None:
            return None
    timeline = _timeline(durations, stride)
    if timeline is None:
        return None
    deadline = time.monotonic() + timeout
    gear = _gear(size, len(timeline), gears)
    output = _attempt(data, size, stride, timeline, gear, deadline)
    if output is None and gear != gears[-1]:
        # The prediction is a bound from real sources, not a proof, so one
        # cheap retry at the smallest gear keeps a bad guess from failing the
        # whole send. Enumeration stops here; it never walks the ladder.
        output = _attempt(data, size, stride, timeline, gears[-1], deadline)
    return output


def _fits(data: bytes, size: tuple[int, int], durations: list[int]) -> bool:
    return (
        len(data) <= MAX_STICKER_BYTES
        and len(durations) <= MAX_STICKER_FRAMES
        and sum(durations) <= MAX_STICKER_DURATION_MS
        and max(size) <= MAX_STICKER_EDGE
    )


def _stride(frames: int, mode: str) -> int | None:
    """Return how many source frames each kept frame replaces, or None if refused."""
    if frames <= MAX_STICKER_FRAMES:
        return 1
    if mode == "conservative":
        # This mode promises to keep every frame, so a source that only fits by
        # dropping some is left alone for the caller's ordinary image send.
        return None
    return math.ceil(frames / MAX_STICKER_FRAMES)


def _fitted_stride(size, durations, stride: int, gears) -> int | None:
    """Raise the stride until the smallest gear is predicted to fit, or None.

    Judging a gear needs the kept frames, and dropping frames is what changes
    them, so the search here is over the stride rather than over the gears.
    """
    edge, colours = gears[-1]
    target = _target_size(size, edge)
    per_frame = target[0] * target[1]
    for candidate in range(stride, len(durations) + 1):
        kept = len(durations) // candidate + (1 if len(durations) % candidate else 0)
        if kept < 2:
            return None  # A single frame is a picture, not an animation.
        if kept * MIN_FRAME_MS > MAX_STICKER_DURATION_MS:
            continue
        if per_frame * kept > MAX_ENCODE_PIXELS:
            continue
        if per_frame * kept * BUDGET[colours] <= MAX_STICKER_BYTES:
            return candidate
    return None


def _timeline(durations: list[int], stride: int) -> list[int] | None:
    """Return one delay per kept frame, with the total inside the 10 s limit.

    Sampling every nth frame is not enough on its own: GIF delays differ per
    frame, so the kept frames can still run past 10 s, and a single frame with a
    12 s delay exceeds it by itself. Each kept frame absorbs the delays of the
    frames it replaces, then the whole timeline is scaled down proportionally,
    which keeps every frame and the relative pacing.
    """
    kept = [
        sum(durations[index : index + stride])
        for index in range(0, len(durations), stride)
    ]
    if not kept or len(kept) * MIN_FRAME_MS > MAX_STICKER_DURATION_MS:
        return None
    if not any(kept):
        # An animated GIF without any delay has no timeline to fit; give it the
        # delay a viewer would apply by default so it does not flash past.
        kept = [DEFAULT_FRAME_MS] * len(kept)
    total = sum(kept)
    if total > MAX_STICKER_DURATION_MS:
        scale = MAX_STICKER_DURATION_MS / total
        kept = [max(MIN_FRAME_MS, int(round(value * scale))) for value in kept]
        _trim(kept)
    return kept


def _trim(values: list[int]) -> None:
    """Remove the rounding residue, taking it from the longest frames first."""
    excess = sum(values) - MAX_STICKER_DURATION_MS
    for index in sorted(range(len(values)), key=lambda item: values[item], reverse=True):
        if excess <= 0:
            break
        take = min(excess, values[index] - MIN_FRAME_MS)
        values[index] -= take
        excess -= take


def _gear(
    size: tuple[int, int], frames: int, gears: tuple[tuple[int, int], ...]
) -> tuple[int, int]:
    """Pick the largest gear whose predicted output and decode cost both fit."""
    for edge, colours in gears:
        target = _target_size(size, edge)
        pixels = target[0] * target[1] * frames
        if pixels > MAX_ENCODE_PIXELS:
            continue
        if pixels * BUDGET[colours] <= MAX_STICKER_BYTES:
            return edge, colours
    return gears[-1]


def _target_size(size: tuple[int, int], edge: int) -> tuple[int, int]:
    width, height = size
    scale = min(1.0, edge / max(width, height))
    return (max(1, round(width * scale)), max(1, round(height * scale)))


def _attempt(data, size, stride, timeline, gear, deadline) -> bytes | None:
    edge, colours = gear
    if time.monotonic() > deadline:
        return None
    try:
        frames = _frames(data, len(timeline), stride, _target_size(size, edge), deadline)
        if frames is None:
            return None
        output = _encode(frames, timeline, colours)
    except Exception:
        return None
    return output if len(output) <= MAX_STICKER_BYTES else None


def _scan(data: bytes) -> tuple[tuple[int, int], list[int]]:
    """Read the canvas size and per-frame delays without keeping pixel data."""
    durations = []
    with Image.open(io.BytesIO(data)) as image:
        size = image.size
        for frame in ImageSequence.Iterator(image):
            durations.append(int(frame.info.get("duration") or 0))
    return size, durations


def _frames(data, count, stride, size, deadline) -> list[Image.Image] | None:
    """Decode only the kept frames, already scaled, so peak memory stays bounded."""
    frames = []
    with Image.open(io.BytesIO(data)) as image:
        for index, frame in enumerate(ImageSequence.Iterator(image)):
            if index % stride:
                continue
            if time.monotonic() > deadline:
                return None
            frames.append(_render(frame, size))
            if len(frames) == count:
                break
    return frames if len(frames) == count else None


def _render(frame: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Composite one frame onto white and scale it before it is held in memory."""
    layer = frame.convert("RGBA")
    if layer.size != size:
        layer = layer.resize(size, Image.Resampling.BILINEAR)
    background = Image.new("RGB", size, (255, 255, 255))
    background.paste(layer, mask=layer.getchannel("A"))
    return background


def _encode(frames, durations, colours) -> bytes:
    images = []
    for frame in frames:
        image = frame.quantize(colors=colours, method=Image.FASTOCTREE)
        # A stray transparency entry breaks Pillow's local header writer, and a
        # re-quantized frame has no alpha channel left to describe.
        image.info.clear()
        images.append(image)
    buffer = io.BytesIO()
    images[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    return buffer.getvalue()
