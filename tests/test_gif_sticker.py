"""Fit a GIF into the sticker limits without losing the animation."""

import io
import random

from conftest import module
from PIL import Image, ImageSequence

MAX_BYTES = 1024 * 1024


def sticker_gif(frames=4, size=(64, 64), duration=80, seed=7, delays=None):
    """Build one deterministic animated GIF from noisy frames."""
    random.seed(seed)
    images = []
    for _ in range(frames):
        frame = Image.new("RGB", size)
        frame.putdata(
            [
                (random.randrange(256), random.randrange(256), random.randrange(256))
                for _ in range(size[0] * size[1])
            ]
        )
        images.append(frame.quantize(colors=256, method=Image.FASTOCTREE))
    buffer = io.BytesIO()
    images[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=delays or duration,
        loop=0,
    )
    return buffer.getvalue()


def oversized_gif():
    """Return a GIF well above the 1 MiB sticker limit."""
    return sticker_gif(frames=20, size=(240, 240))


def durations(data):
    with Image.open(io.BytesIO(data)) as image:
        return [int(frame.info.get("duration") or 0) for frame in ImageSequence.Iterator(image)]


def test_gif_within_limits_is_sent_unchanged():
    data = sticker_gif()
    assert len(data) < MAX_BYTES
    compress = module("gif_compress")
    assert compress.fit_sticker_gif(data, "auto") is data
    assert compress.fit_sticker_gif(data, "conservative") is data


def test_compression_can_be_off_or_unknown():
    compress = module("gif_compress")
    data = oversized_gif()
    assert compress.fit_sticker_gif(data, "none") is None
    assert compress.fit_sticker_gif(data, "fastest") is None


def test_oversized_gif_is_reencoded_within_the_sticker_limits():
    data = oversized_gif()
    assert len(data) > MAX_BYTES
    compress = module("gif_compress")
    fitted = compress.fit_sticker_gif(data, "auto")
    assert fitted is not None and fitted is not data
    assert len(fitted) <= MAX_BYTES
    assert module("native_client").validate_image_bytes(fitted) == "image/gif"
    delays = durations(fitted)
    assert len(delays) <= 200
    assert sum(delays) <= 10_000
    with Image.open(io.BytesIO(fitted)) as image:
        assert max(image.size) <= 1024
    assert len(delays) > 1


def test_conservative_keeps_every_frame():
    data = oversized_gif()
    compress = module("gif_compress")
    fitted = compress.fit_sticker_gif(data, "conservative")
    assert fitted is not None
    assert len(fitted) <= MAX_BYTES
    assert len(durations(fitted)) == len(durations(data))


def test_a_long_timeline_is_shortened_without_dropping_frames():
    # 20 frames of 700 ms run 14 s, past the 10 s sticker limit.
    data = sticker_gif(frames=20, size=(64, 64), duration=700)
    compress = module("gif_compress")
    for mode in ("auto", "conservative"):
        fitted = compress.fit_sticker_gif(data, mode)
        assert fitted is not None, mode
        delays = durations(fitted)
        assert len(delays) == 20, mode
        assert sum(delays) <= 10_000, mode


def test_uneven_delays_cannot_run_past_the_limit():
    # Keeping only every other frame would have left 6 s + 6 s + 6 s of delays.
    delays = [6000, 20] * 3
    data = sticker_gif(frames=6, size=(64, 64), delays=delays)
    compress = module("gif_compress")
    fitted = compress.fit_sticker_gif(data, "auto")
    assert fitted is not None
    assert sum(durations(fitted)) <= 10_000


def test_a_single_frame_is_not_worth_a_sticker():
    # Nothing can be sampled down, and a still sticker looks like a still image,
    # so the source stays with the ordinary image send instead.
    data = sticker_gif(frames=1, size=(64, 64), duration=12000)
    compress = module("gif_compress")
    assert compress.fit_sticker_gif(data, "auto") is None


def test_more_than_two_hundred_frames_are_sampled_down_only_when_allowed():
    data = sticker_gif(frames=210, size=(32, 32), duration=20)
    compress = module("gif_compress")
    assert compress.fit_sticker_gif(data, "conservative") is None
    fitted = compress.fit_sticker_gif(data, "auto")
    assert fitted is not None
    delays = durations(fitted)
    assert len(delays) <= 200
    assert sum(delays) <= 10_000


def test_the_gear_is_chosen_from_the_source_shape():
    compress = module("gif_compress")
    small = compress._gear((200, 200), 30, compress.GEARS)
    large = compress._gear((1000, 1000), 30, compress.GEARS)
    assert small == compress.GEARS[0]
    assert large[0] < compress.GEARS[0][0]


def test_every_gear_palette_has_a_budget():
    # A missing entry surfaces as a KeyError inside the encoder, which the
    # caller can only see as "compression failed" for every source at that gear.
    compress = module("gif_compress")
    palettes = {colours for _, colours in compress.GEARS + compress.CONSERVATIVE_GEARS}
    assert palettes <= set(compress.BUDGET)


def test_a_busy_gif_drops_frames_instead_of_failing():
    # 199 frames of 900 px each stay under the frame cap, so nothing else would
    # bring this source under 1 MiB.
    compress = module("gif_compress")
    size, durations_ms = (900, 900), [40] * 199
    stride = compress._fitted_stride(size, durations_ms, 1, compress.GEARS)
    assert stride is not None and stride > 1


def test_a_single_oversized_frame_is_not_turned_into_a_sticker():
    # Dropping frames is the only lever left, and dropping down to one frame
    # would send a still picture through the sticker channel.
    data = sticker_gif(frames=1, size=(1400, 1400))
    assert len(data) > MAX_BYTES
    compress = module("gif_compress")
    assert compress.fit_sticker_gif(data, "auto") is None


def test_an_unreadable_source_is_left_alone():
    compress = module("gif_compress")
    assert compress.fit_sticker_gif(b"not a gif at all", "auto") is None
    assert compress.fit_sticker_gif(b"", "auto") is None
