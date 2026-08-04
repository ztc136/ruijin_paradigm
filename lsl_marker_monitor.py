#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在另一台电脑上监听并打印本范式的 LSL marker，用于接线前联调。"""

from datetime import datetime

from pylsl import StreamInlet, resolve_byprop


STREAM_NAME = "EmotionParadigmMarkers"


def main():
    print("正在查找 LSL 流 {!r}（10 秒超时）……".format(STREAM_NAME))
    streams = resolve_byprop("name", STREAM_NAME, timeout=10.0)
    if not streams:
        raise SystemExit("未发现流。请确认两台电脑在同一网络且范式已选择 LSL 输出。")
    inlet = StreamInlet(streams[0], max_buflen=60)
    info = inlet.info()
    print("已连接：name={} type={} source_id={}".format(
        info.name(), info.type(), info.source_id()))
    print("按 Ctrl+C 停止。")
    try:
        while True:
            sample, timestamp = inlet.pull_sample(timeout=1.0)
            if sample is not None:
                print("{}  lsl_time={:.6f}  marker={}".format(
                    datetime.now().isoformat(timespec="milliseconds"), timestamp, int(sample[0])))
    except KeyboardInterrupt:
        print("\n监听结束。")


if __name__ == "__main__":
    main()
