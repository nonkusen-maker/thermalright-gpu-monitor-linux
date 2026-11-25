#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Protocol and layout adapted from upstream projects; see THIRD_PARTY_NOTICES.md.
# Modified for AMD GPU metrics, dynamic colors and Linux service, 2026-11-25.
"""
Show AMD GPU busy %, edge temperature and VRAM usage % on a Thermalright
digital (7-segment LED) cooler display, USB HID 0416:8001 ("Winbond/Nuvoton
HID Transfer").

DEFAULT IS DRY-RUN: nothing is written to the device unless --write is given.

Protocol (cross-checked against three independent open-source projects):
  * Lexonight1/thermalright-trcc-linux  doc/PROTOCOL_USBLED.md, core/led_protocol.py,
    services/led_segment.py  (decompiled from the official Windows TRCC FormLED.cs)
  * rejeb/thermalright-lcd-control       device_controller/led/protocol.py
  * MathieuxHugo/digital_thermal_right_lcd src/controller.py, conf/pearless_assasin_120.json

  Transport : 64-byte HID output reports, no report IDs (hidraw: prefix 0x00).
  Handshake : DA DB DC DD, 8x00, 0x01 (cmd init), zero pad to 64 bytes.
              Reply byte[5] = PM (product model), byte[4] = SUB.
              Firmware answers only ONCE per power cycle.
  Frame     : 20-byte header: DA DB DC DD, 8x00, 0x02 (cmd data), 3x00,
              uint16-LE payload length, 2x00; then N x (R,G,B), each value
              scaled by 0.4. Split into 64-byte reports, last zero-padded.
              >= 30 ms between frames.

LED layout implemented: style 2 "PA120 DIGITAL" (PM 16..31, 84 LEDs) only.
This covers Peerless Assassin 120/140 Digital, RK120 Digital and similar
two-row (CPU/GPU) panels. Other 0416:8001 models (AX120, AK120, LC1, LF8, ...)
use different LED counts/layouts; --write refuses them.

No dependencies beyond the Python 3 standard library.
"""

import argparse
import errno
import glob
import os
import select
import struct
import sys
import time

VID, PID = 0x0416, 0x8001

# --------------------------------------------------------------------------
# Wire protocol
# --------------------------------------------------------------------------
MAGIC = bytes([0xDA, 0xDB, 0xDC, 0xDD])
REPORT_SIZE = 64
HEADER_SIZE = 20
CMD_INIT = 0x01
CMD_DATA = 0x02
COLOR_SCALE = 0.4          # FormLED.cs SendHidVal: (float)color * 0.4f
MIN_FRAME_INTERVAL = 0.030  # Thread.Sleep(30) after each frame

# The report descriptor this protocol expects (vendor page 0xFF00, one
# 64-byte input + one 64-byte output report, no report ID).  Read from sysfs
# and compared, so we never talk to an unexpected device.
EXPECTED_REPORT_DESCRIPTOR = bytes.fromhex(
    "0600ff0901a10119012940150026ff0075089540810019012940910" "0c0"
)

PA120_PM = set(range(16, 32))  # PM bytes mapped to style 2 by TRCC


def build_init_report():
    r = bytearray(REPORT_SIZE)
    r[0:4] = MAGIC
    r[12] = CMD_INIT
    return bytes(r)


def build_frame(wire_colors):
    """wire_colors: list of (r,g,b) in physical wire order (already masked)."""
    body = bytearray()
    for r, g, b in wire_colors:
        body += bytes(max(0, min(255, int(c * COLOR_SCALE))) for c in (r, g, b))
    hdr = bytearray(HEADER_SIZE)
    hdr[0:4] = MAGIC
    hdr[12] = CMD_DATA
    struct.pack_into("<H", hdr, 16, len(body))
    return bytes(hdr) + bytes(body)


def chunk_reports(frame):
    out = []
    for i in range(0, len(frame), REPORT_SIZE):
        c = frame[i:i + REPORT_SIZE]
        out.append(c + b"\x00" * (REPORT_SIZE - len(c)))
    return out


# --------------------------------------------------------------------------
# PA120 (style 2) layout, logical indices (TRCC services/led_segment.py)
# --------------------------------------------------------------------------
CPU1, CPU2, GPU1, GPU2 = 0, 1, 2, 3          # "CPU" / "GPU" label LEDs
CPU_C, CPU_F, CPU_PCT = 4, 5, 6               # top row °C, °F, %
GPU_C, GPU_F, GPU_PCT = 7, 8, 9               # bottom row °C, °F, %
CPU_TEMP_DIGITS = (tuple(range(10, 17)), tuple(range(17, 24)), tuple(range(24, 31)))
CPU_USE_DIGITS = (tuple(range(31, 38)), tuple(range(38, 45)))
CPU_USE_HUNDRED = (80, 81)                    # partial "1" for 100 %
GPU_TEMP_DIGITS = (tuple(range(45, 52)), tuple(range(52, 59)), tuple(range(59, 66)))
GPU_USE_DIGITS = (tuple(range(66, 73)), tuple(range(73, 80)))
GPU_USE_HUNDRED = (82, 83)
NUM_LEDS = 84

# physical wire index -> logical index (TRCC core/led_protocol.py _REMAP_STYLE_PA120)
REMAP_PA120 = (
    1, 0, 15, 10, 11, 16, 14, 13, 12,
    22, 17, 18, 23, 21, 20, 19,
    29, 24, 25, 30, 28, 27, 26,
    4, 5, 81, 80,
    36, 31, 32, 37, 35, 34, 33,
    43, 38, 39, 44, 42, 41, 40,
    6, 9,
    75, 76, 77, 79, 74, 73, 78,
    68, 69, 70, 72, 67, 66, 71,
    82, 83, 7, 8,
    61, 62, 63, 65, 60, 59, 64,
    54, 55, 56, 58, 53, 52, 57,
    47, 48, 49, 51, 46, 45, 50,
    2, 3,
)

SEG_ORDER = "abcdefg"  # logical order inside each digit
CHAR_7SEG = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg",
    "5": "acdfg", "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg",
    " ": "", "-": "g", "C": "adef", "F": "aefg", "H": "bcefg",
}


def self_check():
    """Verify the layout tables against each other and against the
    independently-derived MathieuxHugo wire map. Raises on mismatch."""
    assert sorted(REMAP_PA120) == list(range(NUM_LEDS)), "remap not a permutation"
    w = {logical: wire for wire, logical in enumerate(REMAP_PA120)}
    # MathieuxHugo conf/pearless_assasin_120.json (wire indices)
    assert sorted((w[CPU1], w[CPU2])) == [0, 1]
    assert w[CPU_C] == 23 and w[CPU_F] == 24 and w[CPU_PCT] == 41
    assert w[GPU_PCT] == 42 and w[GPU_C] == 59 and w[GPU_F] == 60
    assert sorted((w[GPU1], w[GPU2])) == [82, 83]
    # MathieuxHugo digit wire order is f,a,b,g,e,d,c ; cpu_temp wires 2..22
    mh_order = "fabgedc"
    for d, digit in enumerate(CPU_TEMP_DIGITS):
        for k, seg in enumerate(mh_order):
            assert w[digit[SEG_ORDER.index(seg)]] == 2 + d * 7 + k
    # gpu_temp is wires 81 down to 61 in the same per-digit order
    for d, digit in enumerate(GPU_TEMP_DIGITS):
        for k, seg in enumerate(mh_order):
            assert w[digit[SEG_ORDER.index(seg)]] == 81 - (d * 7 + k)


def encode_digits(mask, digits, text):
    for leds, ch in zip(digits, text):
        for seg in CHAR_7SEG.get(ch, ""):
            mask[leds[SEG_ORDER.index(seg)]] = True


def encode_value(mask, digits, value, hundred_leds=None):
    """Right-aligned number, leading zeros blanked. None -> dashes."""
    n = len(digits)
    if value is None:
        encode_digits(mask, digits, "-" * n)
        return
    v = max(0, int(round(value)))
    if hundred_leds is not None:          # 2 digits + partial "1" (0..199)
        v = min(v, 199)
        if v >= 100:
            mask[hundred_leds[0]] = mask[hundred_leds[1]] = True
            encode_digits(mask, digits, f"{v - 100:02d}")
            return
    v = min(v, 10 ** n - 1)
    encode_digits(mask, digits, f"{v:>{n}d}")


def compose(metrics, mode, phase):
    """Return (logical on/off mask, human description)."""
    m = [False] * NUM_LEDS
    temp, busy, vram = metrics["edge_c"], metrics["busy_pct"], metrics["vram_pct"]
    # Bottom row (GPU label): edge temp °C + busy %
    if mode == "split" or phase == 0:
        m[GPU1] = m[GPU2] = m[GPU_C] = m[GPU_PCT] = True
        encode_value(m, GPU_TEMP_DIGITS, temp)
        encode_value(m, GPU_USE_DIGITS, busy, GPU_USE_HUNDRED)
    if mode == "split":
        # Top row usage field = VRAM %. CPU label + top temp stay dark so it
        # is not mistaken for a CPU reading.
        m[CPU_PCT] = True
        encode_value(m, CPU_USE_DIGITS, vram, CPU_USE_HUNDRED)
        desc = f"top: VRAM {fmt(vram)}%   bottom: GPU {fmt(temp)}°C {fmt(busy)}%"
    elif phase == 0:
        desc = f"bottom: GPU {fmt(temp)}°C busy {fmt(busy)}%"
    else:
        # Bottom row shows VRAM % in usage field, temp digits blank.
        m[GPU1] = m[GPU2] = m[GPU_PCT] = True
        encode_value(m, GPU_USE_DIGITS, vram, GPU_USE_HUNDRED)
        desc = f"bottom: GPU VRAM {fmt(vram)}%"
    return m, desc


def fmt(v):
    return "--" if v is None else f"{v:.0f}"


def to_wire(mask, rgb):
    return [rgb if mask[REMAP_PA120[i]] else (0, 0, 0) for i in range(NUM_LEDS)]


# ---- ASCII preview -------------------------------------------------------
def gradient(value, low=0, high=100):
    # Blue -> cyan -> green -> yellow -> red; linear RGB interpolation.
    if value is None:
        return (110, 110, 110)
    x = max(0.0, min(1.0, (value-low)/(high-low))) * 4
    palette = [(0,0,255), (0,255,255), (0,255,0), (255,255,0), (255,0,0)]
    i = min(3, int(x))
    return tuple(round(a+(b-a)*(x-i)) for a,b in zip(palette[i],palette[i+1]))


def dynamic_wire(mask, metrics, mode, phase, brightness):
    temp = gradient(metrics['edge_c'], 35, 85)
    busy = gradient(metrics['busy_pct'])
    vram = gradient(metrics['vram_pct'])
    colors = [(255,255,255)] * NUM_LEDS
    def paint(indices, color):
        for index in indices:
            colors[index] = color
    paint([GPU_C, GPU_F] + [i for d in GPU_TEMP_DIGITS for i in d], temp)
    paint([GPU1, GPU2, GPU_PCT] + list(GPU_USE_HUNDRED) + [i for d in GPU_USE_DIGITS for i in d], vram if mode == 'cycle' and phase else busy)
    paint([CPU_PCT] + list(CPU_USE_HUNDRED) + [i for d in CPU_USE_DIGITS for i in d], vram)
    scale = max(0,min(100,brightness))/100
    return [tuple(round(c*scale) for c in colors[i]) if mask[i] else (0,0,0) for i in REMAP_PA120]


def _digit_art(mask, leds):
    on = {s: mask[leds[i]] for i, s in enumerate(SEG_ORDER)}
    h = lambda s: "__" if on[s] else "  "
    v = lambda s, c="|": c if on[s] else " "
    return [f" {h('a')} ", f"{v('f')}{h('g')}{v('b')}", f"{v('e')}{h('d')}{v('c')}"]


def render_row(mask, label_on, label, temp_digits, c_led, use_digits, hundred, pct_led):
    cols = [_digit_art(mask, d) for d in temp_digits]
    unit = ["  ", "°C" if mask[c_led] else "  ", "  "]
    hund = [" ", "|" if mask[hundred[0]] else " ", "|" if mask[hundred[1]] else " "]
    use = [_digit_art(mask, d) for d in use_digits]
    pct = ["  ", " %" if mask[pct_led] else "  ", "  "]
    lab = label if label_on else " " * len(label)
    lines = []
    for r in range(3):
        lines.append(("    " if r != 1 else lab + " ") + "".join(c[r] for c in cols)
                     + unit[r] + "  " + hund[r] + "".join(u[r] for u in use) + pct[r])
    return lines


def render(mask):
    top = render_row(mask, mask[CPU1], "CPU", CPU_TEMP_DIGITS, CPU_C,
                     CPU_USE_DIGITS, CPU_USE_HUNDRED, CPU_PCT)
    bot = render_row(mask, mask[GPU1], "GPU", GPU_TEMP_DIGITS, GPU_C,
                     GPU_USE_DIGITS, GPU_USE_HUNDRED, GPU_PCT)
    return "\n".join(top + bot)


# --------------------------------------------------------------------------
# AMD sysfs metrics (read-only)
# --------------------------------------------------------------------------
def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def find_amd_card(card=None):
    cands = [f"/sys/class/drm/{card}"] if card else sorted(glob.glob("/sys/class/drm/card[0-9]*"))
    for c in cands:
        dev = os.path.join(c, "device")
        if _read(os.path.join(dev, "vendor")) == "0x1002" and \
                os.path.exists(os.path.join(dev, "gpu_busy_percent")):
            return dev
    return None


def find_edge_input(dev):
    for h in sorted(glob.glob(os.path.join(dev, "hwmon", "hwmon*"))):
        for lab in sorted(glob.glob(os.path.join(h, "temp*_label"))):
            if _read(lab) == "edge":
                return lab[:-len("_label")] + "_input"
    return None


def read_metrics(dev, edge_input):
    def num(p, scale=1.0):
        s = _read(p) if p else None
        try:
            return float(s) / scale
        except (TypeError, ValueError):
            return None
    busy = num(os.path.join(dev, "gpu_busy_percent"))
    used = num(os.path.join(dev, "mem_info_vram_used"))
    total = num(os.path.join(dev, "mem_info_vram_total"))
    vram = (100.0 * used / total) if used is not None and total else None
    return {"busy_pct": busy, "edge_c": num(edge_input, 1000.0),
            "vram_pct": vram, "vram_used": used, "vram_total": total}


# --------------------------------------------------------------------------
# hidraw device
# --------------------------------------------------------------------------
def find_hidraw():
    """Return (devnode, sysfs_dir) of the 0416:8001 hidraw node, or (None, None)."""
    want = f"HID_ID=0003:{VID:08X}:{PID:08X}"
    for h in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        ue = _read(os.path.join(h, "device", "uevent")) or ""
        if want in ue.splitlines():
            return "/dev/" + os.path.basename(h), os.path.join(h, "device")
    return None, None


def check_descriptor(sysdir):
    try:
        with open(os.path.join(sysdir, "report_descriptor"), "rb") as f:
            rd = f.read()
    except OSError as e:
        return False, f"cannot read report descriptor: {e}"
    if rd != EXPECTED_REPORT_DESCRIPTOR:
        return False, f"unexpected report descriptor {rd.hex()}"
    return True, "report descriptor matches (vendor 0xFF00, 64B in/out, no report ID)"


class HidRaw:
    def __init__(self, node):
        self.fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)

    def write_report(self, report):
        assert len(report) == REPORT_SIZE
        # Leading 0x00 = report number for a device without report IDs.
        n = os.write(self.fd, b"\x00" + report)
        if n != REPORT_SIZE + 1:
            raise IOError(f"short write {n}")

    def read_report(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        return os.read(self.fd, REPORT_SIZE)

    def drain(self):
        while self.read_report(0) is not None:
            pass

    def close(self):
        os.close(self.fd)


def handshake(dev):
    dev.drain()
    time.sleep(0.05)
    dev.write_report(build_init_report())
    time.sleep(0.2)
    resp = dev.read_report(1.0)
    if not resp or len(resp) < 7:
        return None
    return {"raw": resp, "magic_ok": resp[0:4] == MAGIC, "sub": resp[4], "pm": resp[5]}


# --------------------------------------------------------------------------
def hexdump_reports(reports):
    return "\n".join(f"    report {i}: {r.hex(' ')}" for i, r in enumerate(reports))


def parse_color(s):
    s = s.lstrip("#")
    if len(s) != 6:
        raise argparse.ArgumentTypeError("color must be RRGGBB")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true",
                    help="actually send to the device (default: dry-run, no device access)")
    ap.add_argument("--skip-handshake", action="store_true",
                    help="with --write: don't require a PA120 PM reply (the firmware only "
                         "answers once per power cycle). Only use if you KNOW the cooler is a "
                         "PA120-style 84-LED panel.")
    ap.add_argument("--mode", choices=("split", "cycle"), default="split",
                    help="split: VRAM%% on top row, temp+busy on bottom (default); "
                         "cycle: bottom row alternates temp+busy / VRAM%%")
    ap.add_argument("--cycle-seconds", type=float, default=3.0)
    ap.add_argument("--interval", type=float, default=1.0, help="refresh seconds")
    ap.add_argument("--color", type=parse_color, default=None, help="fixed RRGGBB (default: dynamic metric colors)")
    ap.add_argument("--brightness", type=int, default=100, help="0-100")
    ap.add_argument("--card", help="DRM card name, e.g. card1 (auto-detect AMD by default)")
    ap.add_argument("--frames", type=int, default=0, help="stop after N frames (0 = forever)")
    ap.add_argument("--hex", action="store_true", help="dry-run: also print HID report bytes")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if a.interval <= 0 or a.cycle_seconds <= 0 or a.frames < 0:
        ap.error("interval and cycle-seconds must be positive; frames must be nonnegative")
    self_check()

    dev_dir = find_amd_card(a.card)
    if not dev_dir:
        sys.exit("No amdgpu card with gpu_busy_percent found in /sys/class/drm")
    edge = find_edge_input(dev_dir)
    print(f"GPU sysfs: {os.path.realpath(dev_dir)}  edge: {edge or 'not found'}")

    node, sysdir = find_hidraw()
    if node:
        ok, msg = check_descriptor(sysdir)
        print(f"Display: {node} ({msg})")
    else:
        ok = False
        print("Display: 0416:8001 hidraw node not found")

    rgb = tuple(int(c * max(0, min(100, a.brightness)) / 100) for c in a.color) if a.color is not None else None
    hid = None
    if a.write:
        if not node or not ok:
            sys.exit("Refusing to write: device missing or descriptor mismatch.")
        if not os.access(node, os.R_OK | os.W_OK):
            sys.exit(f"Refusing to write: no read/write permission on {node}.\n"
                     "This script will not use sudo. A udev rule (installed by you) such as\n"
                     '  SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0416", ATTRS{idProduct}=="8001", '
                     'TAG+="uaccess"\nis needed for user access.')
        hid = HidRaw(node)
        if not a.skip_handshake:
            hs = handshake(hid)
            if hs is None:
                hid.close()
                sys.exit("No handshake reply (firmware answers only once per power cycle).\n"
                         "Model could not be verified -> not sending frames. Re-plug/power-cycle "
                         "and retry, or use --skip-handshake if you are certain it is a PA120-style panel.")
            print(f"Handshake: magic_ok={hs['magic_ok']} PM={hs['pm']} SUB={hs['sub']}")
            if not hs["magic_ok"] or hs["pm"] not in PA120_PM:
                hid.close()
                sys.exit(f"PM={hs['pm']} is not a PA120-style (84-LED) panel; this layout "
                         "is unsupported -> not sending frames.")
        print("WRITE MODE: sending frames to the display.")
    else:
        print("DRY-RUN: no device access; showing what would be sent.")

    t0 = time.monotonic()
    n = 0
    try:
        while True:
            met = read_metrics(dev_dir, edge)
            phase = int((time.monotonic() - t0) / a.cycle_seconds) % 2
            mask, desc = compose(met, a.mode, phase)
            reports = chunk_reports(build_frame(to_wire(mask, rgb) if rgb is not None else dynamic_wire(mask, met, a.mode, phase, a.brightness)))
            if not a.quiet:
                used = met["vram_used"]
                tot = met["vram_total"]
                vtxt = f" ({used/2**30:.2f}/{tot/2**30:.2f} GiB)" if used and tot else ""
                print(f"\n[{time.strftime('%H:%M:%S')}] busy={fmt(met['busy_pct'])}% "
                      f"edge={fmt(met['edge_c'])}°C vram={fmt(met['vram_pct'])}%{vtxt}  -> {desc}")
                print(render(mask))
                if a.hex and not a.write:
                    print(hexdump_reports(reports))
            if hid:
                for r in reports:
                    hid.write_report(r)
                time.sleep(MIN_FRAME_INTERVAL)
            n += 1
            if a.frames and n >= a.frames:
                break
            time.sleep(max(a.interval, MIN_FRAME_INTERVAL))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        if e.errno in (errno.ENODEV, errno.EIO):
            print("Device disconnected.", file=sys.stderr)
        else:
            raise
    finally:
        if hid:
            hid.close()


if __name__ == "__main__":
    main()
