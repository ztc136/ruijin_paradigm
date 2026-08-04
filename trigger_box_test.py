#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""向 ESP32-S3 打标盒发送诊断帧：[0x36, marker]。"""

import argparse
import time

import serial
from serial.tools import list_ports


HEADER = 0x36
TEST_CODES = [1, 2, 4, 8, 16, 32, 64, 128, 0x55, 0xAA, 99]


def available_ports():
    return list(list_ports.comports())


def marker_code(value):
    """接受十进制或 0x 前缀十六进制 marker。"""
    try:
        code = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("编码必须是整数，例如 1、85 或 0x55") from exc
    if not 0 <= code <= 255:
        raise argparse.ArgumentTypeError("编码范围必须为 0–255")
    return code


def main():
    parser = argparse.ArgumentParser(description="ESP32-S3 sEEG 打标盒串口测试")
    parser.add_argument("--port", help="例如 COM3；留空时仅列出端口")
    parser.add_argument("--interval", type=float, default=0.5, help="marker 间隔秒数")
    parser.add_argument(
        "--code", type=marker_code,
        help="重复发送单一编码，例如 1 或 0x55；留空时发送完整诊断序列",
    )
    parser.add_argument(
        "--count", type=int, default=20,
        help="使用 --code 时的重复次数（默认 20）",
    )
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count 必须大于或等于 1")
    if args.interval < 0.05:
        parser.error("--interval 不得小于 0.05 秒")

    ports = available_ports()
    print("检测到的串口：")
    if ports:
        for port in ports:
            print("  {}  {}  {}".format(port.device, port.description, port.hwid))
    else:
        print("  无")
    if not args.port:
        print("\n连接打标盒后执行：python trigger_box_test.py --port COM3")
        print("示波器固定码测试：python trigger_box_test.py --port COM3 --code 1 --count 20 --interval 1")
        return 0

    codes = [args.code] * args.count if args.code is not None else TEST_CODES
    print("\n打开 {}：115200, 8N1".format(args.port))
    with serial.Serial(args.port, 115200, bytesize=8, parity="N", stopbits=1,
                       timeout=0, write_timeout=1) as device:
        time.sleep(0.3)
        for index, code in enumerate(codes, 1):
            frame = bytes([HEADER, code])
            written = device.write(frame)
            device.flush()
            if written != len(frame):
                raise RuntimeError("写入不完整：{}/{} bytes".format(written, len(frame)))
            print("[{}/{}] marker {:3d} / 0x{:02X}，串口帧 36 {:02X}".format(
                index, len(codes), code, code, code))
            time.sleep(max(0.05, args.interval))
    print("发送完成；请核对数据位与 GPIO10 strobe 波形。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
