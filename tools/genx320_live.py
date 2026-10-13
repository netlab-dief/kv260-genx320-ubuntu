#!/usr/bin/env python3

import argparse
import os
import select
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

WIDTH = 320
HEIGHT = 320

# Small native V4L2 capture helper.
# It sends [4-byte payload size][payload] to Python for each buffer.
CAPTURE_C = r"""
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <unistd.h>

#define NBUF 2
#define PSE2 0x32455350U

struct mapping {
    void *ptr;
    size_t length;
};

static int xioctl(int fd, unsigned long cmd, void *arg)
{
    int ret;
    do {
        ret = ioctl(fd, cmd, arg);
    } while (ret < 0 && errno == EINTR);
    return ret;
}

static int write_all(int fd, const void *data, size_t len)
{
    const unsigned char *p = data;

    while (len) {
        ssize_t n = write(fd, p, len);

        if (n < 0 && errno == EINTR)
            continue;

        if (n <= 0)
            return -1;

        p += n;
        len -= n;
    }

    return 0;
}

int main(int argc, char **argv)
{
    const char *device = argc > 1 ? argv[1] : "/dev/video0";
    struct mapping maps[NBUF] = {{0}};
    struct v4l2_format fmt = {0};
    struct v4l2_requestbuffers req = {0};
    enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    int fd, streaming = 0, ret = 1;

    fd = open(device, O_RDWR | O_NONBLOCK);

    if (fd < 0) {
        perror("open video device");
        return 1;
    }

    fmt.type = type;

    if (xioctl(fd, VIDIOC_G_FMT, &fmt) < 0) {
        perror("VIDIOC_G_FMT");
        goto done;
    }

    fprintf(stderr,
        "Capture format: %ux%u FourCC=0x%08x\n",
        fmt.fmt.pix.width,
        fmt.fmt.pix.height,
        fmt.fmt.pix.pixelformat);

    if (fmt.fmt.pix.pixelformat != PSE2 ||
        fmt.fmt.pix.width != 320 ||
        fmt.fmt.pix.height != 320) {
        fprintf(stderr,
            "Expected PSE2 320x320. Configure media pads first.\n");
        goto done;
    }

    req.count = NBUF;
    req.type = type;
    req.memory = V4L2_MEMORY_MMAP;

    if (xioctl(fd, VIDIOC_REQBUFS, &req) < 0) {
        perror("VIDIOC_REQBUFS");
        goto done;
    }

    if (req.count < NBUF) {
        fprintf(stderr, "Insufficient V4L2 buffers\n");
        goto done;
    }

    for (unsigned int i = 0; i < NBUF; i++) {
        struct v4l2_buffer buf = {0};

        buf.type = type;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;

        if (xioctl(fd, VIDIOC_QUERYBUF, &buf) < 0) {
            perror("VIDIOC_QUERYBUF");
            goto done;
        }

        maps[i].length = buf.length;
        maps[i].ptr = mmap(NULL, buf.length,
                           PROT_READ | PROT_WRITE,
                           MAP_SHARED, fd, buf.m.offset);

        if (maps[i].ptr == MAP_FAILED) {
            maps[i].ptr = NULL;
            perror("mmap");
            goto done;
        }

        if (xioctl(fd, VIDIOC_QBUF, &buf) < 0) {
            perror("VIDIOC_QBUF");
            goto done;
        }
    }

    if (xioctl(fd, VIDIOC_STREAMON, &type) < 0) {
        perror("VIDIOC_STREAMON");
        goto done;
    }

    streaming = 1;
    fprintf(stderr, "GenX320 streaming started\n");

    while (1) {
        fd_set fds;
        struct timeval tv = {.tv_sec = 1, .tv_usec = 0};
        struct v4l2_buffer buf = {0};

        FD_ZERO(&fds);
        FD_SET(fd, &fds);

        int ready = select(fd + 1, &fds, NULL, NULL, &tv);

        if (ready < 0) {
            if (errno == EINTR)
                continue;
            perror("select");
            break;
        }

        if (ready == 0)
            continue;

        buf.type = type;
        buf.memory = V4L2_MEMORY_MMAP;

        if (xioctl(fd, VIDIOC_DQBUF, &buf) < 0) {
            if (errno == EAGAIN)
                continue;
            perror("VIDIOC_DQBUF");
            break;
        }

        if (buf.index >= NBUF ||
            buf.bytesused > maps[buf.index].length) {
            fprintf(stderr, "Invalid V4L2 buffer\n");
            break;
        }

        if (!(buf.flags & V4L2_BUF_FLAG_ERROR)) {
            uint32_t size = buf.bytesused;

            if (write_all(STDOUT_FILENO, &size, 4) < 0 ||
                write_all(STDOUT_FILENO,
                          maps[buf.index].ptr, size) < 0)
                break;
        }

        if (xioctl(fd, VIDIOC_QBUF, &buf) < 0) {
            perror("VIDIOC_QBUF");
            break;
        }
    }

done:
    if (streaming)
        xioctl(fd, VIDIOC_STREAMOFF, &type);

    for (unsigned int i = 0; i < NBUF; i++) {
        if (maps[i].ptr)
            munmap(maps[i].ptr, maps[i].length);
    }

    close(fd);
    return ret;
}
"""


def build_helper(directory):
    source = directory / "genx320_capture.c"
    binary = directory / "genx320_capture"

    source.write_text(CAPTURE_C)

    subprocess.run(
        ["gcc", "-O2", "-Wall", "-Wextra",
         str(source), "-o", str(binary)],
        check=True
    )

    return binary


def decode_events(payload, frame):
    """Decode EVT2.1 ON/OFF words into a BGR preview frame."""
    if len(payload) % 8:
        return 0

    count = 0

    for (word,) in struct.iter_unpack("<Q", payload):
        typ = word >> 60

        if typ not in (0, 1):
            continue

        xbase = (word >> 43) & 0x7FF
        y = (word >> 32) & 0x7FF
        mask = word & 0xFFFFFFFF

        if y >= HEIGHT:
            continue

        while mask:
            bit_value = mask & -mask
            bit = bit_value.bit_length() - 1
            x = xbase + bit

            if x < WIDTH:
                if typ == 1:
                    # ON: green
                    frame[y, x] = (0, 255, 0)
                else:
                    # OFF: magenta
                    frame[y, x] = (255, 0, 255)

                count += 1

            mask ^= bit_value

    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="/dev/video0")
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--scale", type=int, default=2)
    parser.add_argument("--persistence", type=float, default=0.80)
    args = parser.parse_args()

    if not (0 <= args.persistence < 1):
        parser.error("--persistence must be >= 0 and < 1")

    if args.scale < 1 or args.seconds < 0:
        parser.error("--scale must be >= 1 and --seconds >= 0")

    if not os.environ.get("DISPLAY") and not os.environ.get(
        "WAYLAND_DISPLAY"
    ):
        raise SystemExit(
            "No graphical desktop found. Run this in the KV260 desktop."
        )

    directory = Path(tempfile.mkdtemp(prefix="genx320-preview-"))
    proc = None

    try:
        binary = build_helper(directory)

        proc = subprocess.Popen(
            [str(binary), args.device],
            stdout=subprocess.PIPE,
            bufsize=0
        )

        os.set_blocking(proc.stdout.fileno(), False)

        pending = bytearray()
        image = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)

        decoded_count = 0
        buffer_count = 0
        started = time.monotonic()
        last_draw = started

        # GENX320_RATE_DIAG
        last_report = started
        previous_buffers = 0
        previous_events = 0
        zero_event_buffers = 0

        # GENX320_WORD_DIAG
        from collections import Counter
        word_counts = Counter()
        bytes_received = 0
        previous_bytes = 0
        previous_word_counts = Counter()
        last_time_high = None

        print("Live GenX320 preview")
        print("Press Q or ESC to quit.")
        print("Duration:", args.seconds or "unlimited", "seconds")

        cv2.namedWindow("GenX320 Live EVT2.1", cv2.WINDOW_NORMAL)

        while True:
            now = time.monotonic()

            if args.seconds and now - started >= args.seconds:
                break

            if proc.poll() is not None:
                print("Capture helper exited:", proc.returncode)
                break

            ready, _, _ = select.select(
                [proc.stdout.fileno()], [], [], 0.01
            )

            if ready:
                try:
                    chunk = os.read(proc.stdout.fileno(), 262144)
                except BlockingIOError:
                    chunk = b""

                if chunk:
                    pending.extend(chunk)

            # A buffer is a 4-byte length followed by event data.
            while len(pending) >= 4:
                length = struct.unpack_from("<I", pending, 0)[0]

                if length > 1048576:
                    raise RuntimeError(
                        f"Invalid V4L2 payload length: {length}"
                    )

                if len(pending) < length + 4:
                    break

                payload = bytes(pending[4:4 + length])
                del pending[:4 + length]

                # GENX320_WORD_DIAG
                bytes_received += len(payload)

                for (word,) in struct.iter_unpack(
                    "<Q", payload[:len(payload) // 8 * 8]
                ):
                    typ = word >> 60
                    word_counts[typ] += 1

                    if typ == 8:
                        last_time_high = (
                            (word >> 32) & 0x0FFFFFFF
                        )

                new_events = decode_events(payload, image)
                decoded_count += new_events
                buffer_count += 1

                if new_events == 0:
                    zero_event_buffers += 1

            # GENX320_RATE_DIAG: print one report per second.
            if now - last_report >= 1.0:
                print(
                    f"Buffers/s={buffer_count - previous_buffers} "
                    f"Events/s={decoded_count - previous_events} "
                    f"Total buffers={buffer_count} "
                    f"Zero-event buffers={zero_event_buffers}",
                    flush=True
                )
                delta_types = word_counts - previous_word_counts

                print(
                    "WORD_DIAG "
                    f"bytes/s={bytes_received - previous_bytes} "
                    f"ON={delta_types[1]} "
                    f"OFF={delta_types[0]} "
                    f"TIME_HIGH={delta_types[8]} "
                    f"OTHER_E={delta_types[14]} "
                    f"last_time_high={last_time_high}",
                    flush=True
                )

                previous_word_counts = word_counts.copy()
                previous_bytes = bytes_received
                previous_buffers = buffer_count
                previous_events = decoded_count
                last_report = now

            if now - last_draw >= 1 / 30:
                display = cv2.resize(
                    image,
                    (WIDTH * args.scale, HEIGHT * args.scale),
                    interpolation=cv2.INTER_NEAREST
                )

                cv2.putText(
                    display,
                    f"Buffers: {buffer_count}  Events: {decoded_count}",
                    (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (255, 255, 255),
                    1,
                    cv2.LINE_AA
                )

                cv2.imshow("GenX320 Live EVT2.1", display)

                key = cv2.waitKey(1) & 0xFF

                if key in (ord("q"), 27):
                    break

                # Fade old events while retaining recent activity.
                cv2.convertScaleAbs(
                    image, dst=image,
                    alpha=args.persistence,
                    beta=0
                )

                last_draw = now

    except KeyboardInterrupt:
        pass

    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        cv2.destroyAllWindows()
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    main()
