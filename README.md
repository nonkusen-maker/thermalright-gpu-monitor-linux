# Thermalright GPU Monitor for Linux

Unofficial Python 3 utility displaying AMD GPU utilization, edge temperature and VRAM usage on supported Thermalright digital LED cooler displays. Uses Linux sysfs and USB HID, with dynamic metric colors and an optional systemd service.

## Compatibility

USB ID 0416:8001; PA120 style 2, 84 LEDs (PM 16–31). Other layouts are not supported. USB ID alone is insufficient to establish compatibility. Requires Linux with AMD GPU sysfs metrics. The top row is repurposed for VRAM; the display has no dedicated VRAM label.

## Test

Run `python3 tr_display.py --frames 3` for a dry run. No USB writes occur by default. After confirming the model and setting appropriate USB permissions, use `python3 tr_display.py --write`. See `--help` for options. Do not run multiple display controllers simultaneously. Physical color/layout verification remains pending.

## Service

The included udev rule grants access to members of plugdev. Review it before installation. Install the script under /opt/thermalright-gpu-monitor and replace @USER@ in the systemd template with an appropriate local unprivileged user belonging to plugdev. The service checks the device model by default. Some firmware replies only once per power cycle; if no reply is received, the utility refuses to write. Only add `--skip-handshake` after independently confirming the exact supported 84-LED layout. Do not assume compatibility from USB ID alone.

## Credits and licensing

Protocol references used during development:
- https://github.com/Lexonight1/thermalright-trcc-linux
- https://github.com/rejeb/thermalright-lcd-control
- https://github.com/MathieuxHugo/digital_thermal_right_lcd

Distributed under GPL-3.0-only; see LICENSE and THIRD_PARTY_NOTICES.md. Upstream project licenses were checked on 2026-11-25. Hardware protocol information is based on community reverse engineering; no proprietary vendor software is included. Compatibility and third-party rights beyond the declared upstream licenses are not independently certified. This project is not affiliated with Thermalright.
