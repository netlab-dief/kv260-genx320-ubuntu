#!/usr/bin/env python3
import argparse
import struct
import subprocess
from pathlib import Path

W, H = 320, 320
# GenX320 EVT2.1 timestamp tick period
# EVT2.1 counter runs at ~9.48 kHz
GENX320_TICK_US = 105.5

parser = argparse.ArgumentParser(
    description="GenX320 EVT2.1 raw to MP4 visualization"
)
parser.add_argument("input", help="Headerless EVT2.1 raw file")
parser.add_argument("output", help="Output MP4")
parser.add_argument(
    "--window-us", type=int, default=1000,
    help="Sensor-time accumulation per video frame (default: 1000 us)"
)
parser.add_argument(
    "--fps", type=int, default=30,
    help="Output video frame rate (default: 30)"
)
parser.add_argument(
    "--scale", type=int, default=2,
    help="Output enlargement factor (default: 2)"
)
args = parser.parse_args()

if args.window_us <= 0 or args.fps <= 0 or args.scale <= 0:
    parser.error("window-us, fps and scale must be positive")

data = Path(args.input).read_bytes()

if len(data) % 8:
    raise SystemExit("Input is not aligned to 64-bit EVT2.1 words")

# Accumulate event coordinates by sensor timestamp windows.
# Each output frame is regenerated from scratch.
events = []
time_high = None

for (word,) in struct.iter_unpack("<Q", data):
    typ = word >> 60

    if typ == 8:
        time_high = (word >> 32) & 0x0FFFFFFF
        continue

    if typ not in (0, 1) or time_high is None:
        continue

    low = (word >> 54) & 0x3F
    t = int(((time_high << 6) | low) * GENX320_TICK_US)

    xbase = (word >> 43) & 0x7FF
    y = (word >> 32) & 0x7FF
    mask = word & 0xFFFFFFFF

    if y >= H:
        continue

    while mask:
        lowest = mask & -mask
        bit = lowest.bit_length() - 1
        x = xbase + bit

        if x < W:
            events.append((t, x, y, typ))

        mask ^= lowest

if not events:
    raise SystemExit("No pixel events decoded")

# Preserve stream order, sorting only when necessary.
if any(events[i][0] < events[i-1][0]
       for i in range(1, len(events))):
    print("Warning: some event timestamps are out of order")
    events.sort(key=lambda e: e[0])

t0 = events[0][0]
t1 = events[-1][0]
duration = t1 - t0

print(f"Wall-clock equivalent: {duration/1_000_000:.3f} s")
frame_count = duration // args.window_us + 1

print(f"Decoded events: {len(events)}")
print(f"Sensor-time span: {duration} us")
print(f"Visualization frames: {frame_count}")
print(f"Playback duration: {frame_count / args.fps:.2f} s")

if frame_count > 10000:
    raise SystemExit(
        "Too many frames; increase --window-us"
    )

cmd = [
    "ffmpeg", "-y",
    "-loglevel", "error",
    "-f", "rawvideo",
    "-pixel_format", "rgb24",
    "-video_size", f"{W}x{H}",
    "-framerate", str(args.fps),
    "-i", "-",
    "-vf", f"scale={W*args.scale}:{H*args.scale}:flags=neighbor",
    "-c:v", "mpeg4",
    "-q:v", "3",
    "-pix_fmt", "yuv420p",
    args.output
]

print("Encoding video...")

proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

try:
    index = 0

    for frame_index in range(frame_count):
        frame = bytearray(W * H * 3)

        end_time = t0 + (frame_index + 1) * args.window_us

        while index < len(events) and events[index][0] < end_time:
            _, x, y, polarity = events[index]
            pos = (y * W + x) * 3

            if polarity == 1:
                frame[pos:pos+3] = b'\x00\xff\x00'  # ON green
            else:
                frame[pos:pos+3] = b'\xff\x00\xff'  # OFF magenta

            index += 1

        proc.stdin.write(frame)

    proc.stdin.close()
    result = proc.wait()

    if result:
        raise RuntimeError(f"ffmpeg exited with code {result}")

except (BrokenPipeError, Exception):
    if proc.stdin and not proc.stdin.closed:
        proc.stdin.close()
    proc.wait()
    raise

print(f"Saved: {args.output}")
