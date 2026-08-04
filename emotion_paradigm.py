#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""情绪诱发范式：PsychoPy 呈现、评分、随机抽样和事件打标。"""

import argparse
import bisect
import csv
import itertools
import json
import random
import re
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
VIDEO_DURATION_CACHE_PATH = APP_DIR / "video_durations.json"
HELPER_FILES = {
    "start": Path("使用图片/图片/开始.png"),
    "fixation": Path("使用图片/图片/注视.png"),
    "rating": Path("使用图片/图片/打分图片.png"),
    "rest": Path("使用图片/图片/休息.png"),
    "end": Path("使用图片/图片/结束.png"),
}
CATEGORY_CN = {"positive": "正性", "negative": "负性", "neutral": "中性", "unknown": "未知"}
# 自动视频短测会由 os._exit 结束；在此之前保留 MovieStim 引用，避免 Python
# 在函数返回时同步析构仍在解码的长视频。
SMOKE_MOVIE_KEEPALIVE = []


class ExperimentAbort(Exception):
    pass


class MarkerError(RuntimeError):
    pass


class CameraError(RuntimeError):
    pass


def load_config(path=CONFIG_PATH):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_asset_root(config):
    return (APP_DIR / config["asset_root"]).resolve()


def safe_filename(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value).strip())
    return value or "anonymous"


def category_for(path, paradigm_config, paradigm_root):
    relative = path.relative_to(paradigm_root).as_posix()
    for category, patterns in paradigm_config.get("category_rules", {}).items():
        if any(re.search(pattern, relative, flags=re.IGNORECASE) for pattern in patterns):
            return category
    return "unknown"


def discover_stimuli(asset_root, paradigm_id, paradigm_config):
    folder = asset_root / paradigm_config["folder"]
    if not folder.is_dir():
        raise FileNotFoundError("材料目录不存在：{}".format(folder))
    extensions = {suffix.lower() for suffix in paradigm_config["extensions"]}
    paths = sorted(
        (path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() in extensions),
        key=lambda p: p.as_posix().lower(),
    )
    return [
        {"path": path, "relative": path.relative_to(asset_root).as_posix(),
         "category": category_for(path, paradigm_config, folder)}
        for path in paths
    ]


def sample_stimuli(stimuli, count, rng, balanced=True):
    if len(stimuli) < count:
        raise ValueError("可用材料只有 {} 个，少于需要的 {} 个".format(len(stimuli), count))
    if not balanced:
        result = rng.sample(stimuli, count)
        rng.shuffle(result)
        return result

    groups = defaultdict(list)
    for item in stimuli:
        groups[item["category"]].append(item)
    known_groups = {key: value for key, value in groups.items() if key != "unknown"}
    if len(known_groups) < 2:
        result = rng.sample(stimuli, count)
        rng.shuffle(result)
        return result

    categories = list(known_groups)
    rng.shuffle(categories)
    selected = []
    # 每轮从每类取一个，达到尽量均衡；某一类耗尽时自动从其余类别补足。
    while len(selected) < count:
        progressed = False
        for category in categories:
            available = known_groups[category]
            if available and len(selected) < count:
                index = rng.randrange(len(available))
                selected.append(available.pop(index))
                progressed = True
        if not progressed:
            break
        rng.shuffle(categories)
    if len(selected) < count:
        used = {item["path"] for item in selected}
        remainder = [item for item in stimuli if item["path"] not in used]
        selected.extend(rng.sample(remainder, count - len(selected)))
    rng.shuffle(selected)
    return selected


def balanced_category_targets(count, rng):
    categories = ["positive", "neutral", "negative"]
    targets = {category: count // len(categories) for category in categories}
    remainder_order = list(categories)
    rng.shuffle(remainder_order)
    for category in remainder_order[:count % len(categories)]:
        targets[category] += 1
    return targets


def sample_category_targets_prefer_unused(stimuli, targets, rng, used_paths=None):
    """按指定类别配额抽样；每类优先取该被试历史session未使用的材料。"""
    used_paths = used_paths or set()
    selected = []
    for category, target in targets.items():
        target = int(target)
        category_items = [item for item in stimuli if item["category"] == category]
        if len(category_items) < target:
            raise ValueError("{}类材料只有{}个，无法抽取{}个".format(
                CATEGORY_CN.get(category, category), len(category_items), target))
        unused = [item for item in category_items if item["relative"] not in used_paths]
        reused = [item for item in category_items if item["relative"] in used_paths]
        rng.shuffle(unused)
        rng.shuffle(reused)
        chosen = unused[:target]
        if len(chosen) < target:
            chosen.extend(reused[:target - len(chosen)])
        selected.extend(dict(item, reused=(item["relative"] in used_paths)) for item in chosen)
    rng.shuffle(selected)
    return selected


def prior_stimulus_paths(participant):
    """读取该被试以前生成的计划，跨 session 优先避免材料重复。"""
    output_dir = APP_DIR / "data"
    if not output_dir.is_dir():
        return set()
    prefix = safe_filename(participant) + "_"
    used = set()
    for path in output_dir.glob(prefix + "*_stimulus_plan.csv"):
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    stimulus = str(row.get("stimulus", "")).strip()
                    if stimulus:
                        used.add(stimulus)
        except (OSError, csv.Error):
            continue
    return used


def find_ffprobe():
    candidates = [
        Path(sys.executable).resolve().parent / "share/ffpyplayer/ffmpeg/bin/ffprobe.exe",
        Path(r"C:/Program Files/PsychoPy/share/ffpyplayer/ffmpeg/bin/ffprobe.exe"),
    ]
    command = shutil.which("ffprobe")
    if command:
        candidates.append(Path(command))
    return next((path for path in candidates if path.is_file()), None)


def load_video_duration_cache():
    try:
        with VIDEO_DURATION_CACHE_PATH.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_video_duration_cache(cache):
    with VIDEO_DURATION_CACHE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(cache, handle, ensure_ascii=False, indent=2, sort_keys=True)


def video_duration_s(item, cache, ffprobe):
    path = item["path"]
    stat = path.stat()
    key = item["relative"]
    cached = cache.get(key, {})
    if (cached.get("size") == stat.st_size and cached.get("mtime_ns") == stat.st_mtime_ns
            and float(cached.get("duration_s", 0)) > 0):
        return float(cached["duration_s"])
    if ffprobe is None:
        raise RuntimeError("找不到 ffprobe，无法计算视频 session 时长")
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=True,
    )
    duration = float(result.stdout.strip())
    cache[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                  "duration_s": round(duration, 6)}
    return duration


def balanced_video_candidates(stimuli, targets, used_paths, durations, rng):
    options = []
    for category, per_category in targets.items():
        per_category = int(per_category)
        items = [item for item in stimuli if item["category"] == category]
        if len(items) < per_category:
            raise ValueError("视频{}类材料不足：{} < {}".format(
                CATEGORY_CN[category], len(items), per_category))
        options.append(list(itertools.combinations(items, per_category)))
    candidates = []
    for combination_group in itertools.product(*options):
        items = [item for combination in combination_group for item in combination]
        duration = sum(durations[item["relative"]] for item in items)
        reuse_count = sum(item["relative"] in used_paths for item in items)
        candidates.append({"items": items, "duration_s": duration,
                           "reuse_count": reuse_count, "tie": rng.random()})
    return candidates


def estimate_trial_s(pconfig, item, expected_rating_s):
    stimulus_s = float(item.get("duration_s", pconfig.get("stimulus_s") or 0.0))
    rating_s = expected_rating_s if pconfig.get("rating", True) else 0.0
    return float(pconfig["fixation_s"]) + stimulus_s + rating_s + float(pconfig.get("rest_s", 0.0))


def session_rotation_index(session, block_count):
    match = re.search(r"\d+", str(session))
    if match:
        return max(0, int(match.group()) - 1) % block_count
    return sum(ord(char) for char in str(session)) % block_count


def build_integrated_plan(config, participant, session, seed, test_mode=False):
    settings = config.get("integrated_session", {})
    paradigm_ids = [str(value) for value in settings.get("paradigms", list("123456"))]
    target_s = float(settings.get("target_duration_s", 3600))
    expected_rating_s = float(settings.get("expected_rating_s", 8.0))
    rng = random.Random(seed)
    asset_root = resolve_asset_root(config)
    used_paths = prior_stimulus_paths(participant) if settings.get("prefer_unused_materials", True) else set()
    targets_by_pid = {}
    for pid in paradigm_ids:
        if test_mode:
            targets_by_pid[pid] = {"positive": 1, "neutral": 1, "negative": 1}
        else:
            configured = config["paradigms"][pid].get("category_trial_counts")
            if not configured:
                count = int(settings.get("block_trial_counts", {}).get(
                    pid, config["paradigms"][pid]["trial_count"]))
                configured = balanced_category_targets(count, random.Random(seed + int(pid)))
            targets_by_pid[pid] = {str(category): int(count)
                                   for category, count in configured.items()}
    counts = {pid: sum(targets_by_pid[pid].values()) for pid in paradigm_ids}

    pools = {pid: discover_stimuli(asset_root, pid, config["paradigms"][pid])
             for pid in paradigm_ids}
    selected_by_pid = {}
    duration_cache = load_video_duration_cache()
    ffprobe = find_ffprobe()
    durations = {}
    for pid in paradigm_ids:
        if config["paradigms"][pid]["kind"] == "video":
            for item in pools[pid]:
                durations[item["relative"]] = video_duration_s(item, duration_cache, ffprobe)
    if durations:
        save_video_duration_cache(duration_cache)

    # 先选图片区块，其耗时固定；再联合选择两个视频区块，使总时长最接近目标。
    for pid in paradigm_ids:
        if config["paradigms"][pid]["kind"] != "video":
            selected_by_pid[pid] = sample_category_targets_prefer_unused(
                pools[pid], targets_by_pid[pid], rng, used_paths)

    fixed_estimate = 0.0
    for pid, selected in selected_by_pid.items():
        pconfig = config["paradigms"][pid]
        fixed_estimate += sum(estimate_trial_s(pconfig, item, expected_rating_s)
                              for item in selected)

    video_ids = [pid for pid in paradigm_ids if config["paradigms"][pid]["kind"] == "video"]
    candidate_lists = []
    for pid in video_ids:
        candidate_lists.append((pid, balanced_video_candidates(
            pools[pid], targets_by_pid[pid], used_paths, durations, rng)))
    video_overhead = sum(counts[pid] * (
        float(config["paradigms"][pid]["fixation_s"]) +
        float(config["paradigms"][pid].get("rest_s", 0.0)) + expected_rating_s)
        for pid in video_ids)
    if len(candidate_lists) == 2:
        pid_a, candidates_a = candidate_lists[0]
        pid_b, candidates_b = candidate_lists[1]
        best = None
        for candidate_a in candidates_a:
            for candidate_b in candidates_b:
                estimated = (fixed_estimate + video_overhead +
                             candidate_a["duration_s"] + candidate_b["duration_s"])
                reused = candidate_a["reuse_count"] + candidate_b["reuse_count"]
                score = abs(estimated - target_s) + reused * 300.0
                key = (score, abs(estimated - target_s), candidate_a["tie"] + candidate_b["tie"])
                if best is None or key < best[0]:
                    best = (key, candidate_a, candidate_b)
        for pid, candidate in ((pid_a, best[1]), (pid_b, best[2])):
            items = []
            for item in candidate["items"]:
                items.append(dict(item, reused=(item["relative"] in used_paths),
                                  duration_s=durations[item["relative"]]))
            rng.shuffle(items)
            selected_by_pid[pid] = items
    else:
        for pid, candidates in candidate_lists:
            def candidate_key(value):
                estimated = fixed_estimate + video_overhead + value["duration_s"]
                distance = abs(estimated - target_s)
                return (distance + value["reuse_count"] * 300.0,
                        distance, value["tie"])
            candidate = min(candidates, key=candidate_key)
            selected_by_pid[pid] = [dict(item,
                reused=(item["relative"] in used_paths),
                duration_s=durations[item["relative"]]) for item in candidate["items"]]

    blocks = []
    for pid in paradigm_ids:
        pconfig = config["paradigms"][pid]
        selected = selected_by_pid[pid]
        estimated = sum(estimate_trial_s(pconfig, item, expected_rating_s) for item in selected)
        blocks.append({"paradigm": pid, "config": pconfig, "stimuli": selected,
                       "estimated_s": estimated})
    if settings.get("rotate_block_order_by_session", True) and blocks:
        offset = session_rotation_index(session, len(blocks))
        blocks = blocks[offset:] + blocks[:offset]
    total_estimated = sum(block["estimated_s"] for block in blocks)
    return {"blocks": blocks, "estimated_s": total_estimated, "target_s": target_s,
            "seed": seed, "previously_used_count": len(used_paths)}


def compact_demo_plan(config, session_plan):
    """演示版使用正式类别配额，但缩短固定阶段并允许跳过素材。"""
    expected_rating_s = float(config.get("integrated_session", {}).get(
        "expected_rating_s", 8.0))
    asset_root = resolve_asset_root(config)
    rng = random.Random(int(session_plan["seed"]) ^ 0xD3E0)
    duration_cache = load_video_duration_cache()
    blocks = []
    for block in session_plan["blocks"]:
        pconfig = block["config"]
        pool = discover_stimuli(asset_root, block["paradigm"], pconfig)
        targets = {str(category): int(count) for category, count in
                   pconfig["category_trial_counts"].items()}
        selected = []
        for category, count in targets.items():
            candidates = [item for item in pool if item["category"] == category]
            if len(candidates) < count:
                raise ValueError("范式{}的{}材料只有{}个，演示版需要{}个".format(
                    block["paradigm"], CATEGORY_CN.get(category, category),
                    len(candidates), count))
            chosen = rng.sample(candidates, count)
            for item in chosen:
                item = dict(item, reused=False)
                cached = duration_cache.get(item["relative"], {})
                if pconfig["kind"] == "video" and float(cached.get("duration_s", 0)) > 0:
                    item["duration_s"] = float(cached["duration_s"])
                selected.append(item)
        rng.shuffle(selected)
        stimulus_s = 3.0 if pconfig["kind"] == "video" else 0.5
        trial_s = 0.30 + stimulus_s
        if pconfig.get("rating", True):
            trial_s += expected_rating_s
        if float(pconfig.get("rest_s", 0.0)) > 0:
            trial_s += 0.30
        blocks.append(dict(block, stimuli=selected, estimated_s=trial_s * len(selected)))
    result = dict(session_plan)
    result["blocks"] = blocks
    result["estimated_s"] = sum(block["estimated_s"] for block in blocks)
    return result


def validate_assets(config, print_report=True):
    asset_root = resolve_asset_root(config)
    errors = []
    if not asset_root.is_dir():
        errors.append("素材根目录不存在：{}".format(asset_root))
    for name, relative in HELPER_FILES.items():
        path = asset_root / relative
        if not path.is_file():
            errors.append("缺少界面图片 {}：{}".format(name, path))
    report = []
    if asset_root.is_dir():
        for paradigm_id, pconfig in config["paradigms"].items():
            try:
                stimuli = discover_stimuli(asset_root, paradigm_id, pconfig)
                counts = defaultdict(int)
                for item in stimuli:
                    counts[item["category"]] += 1
                if len(stimuli) < int(pconfig["trial_count"]):
                    errors.append("范式{}材料不足：{} < {}".format(
                        paradigm_id, len(stimuli), pconfig["trial_count"]))
                for category, target in pconfig.get("category_trial_counts", {}).items():
                    if counts[category] < int(target):
                        errors.append("范式{}的{}材料不足：{} < {}".format(
                            paradigm_id, CATEGORY_CN.get(category, category),
                            counts[category], target))
                report.append((paradigm_id, pconfig["name"], len(stimuli), dict(counts)))
            except Exception as exc:
                errors.append("范式{}：{}".format(paradigm_id, exc))
    if print_report:
        print("素材根目录：{}".format(asset_root))
        for paradigm_id, name, total, counts in report:
            details = ", ".join("{}={}".format(CATEGORY_CN.get(k, k), v)
                                for k, v in sorted(counts.items()))
            print("  {} {}：共 {} 个（{}）".format(paradigm_id, name, total, details))
        if errors:
            print("\n校验失败：")
            for error in errors:
                print("  - {}".format(error))
        else:
            print("\n校验通过。")
    return errors


def build_serial_payload(marker_config, code):
    """按配置构造串口负载；旧 ESP32 打标盒要求 [0x36, marker]。"""
    protocol = str(marker_config.get("serial_protocol", "raw_byte")).strip().lower()
    if protocol == "raw_byte":
        return bytes([code])
    if protocol == "esp32_header":
        header = int(marker_config.get("serial_header_byte", 0x36))
        if not 0 <= header <= 255:
            raise MarkerError("串口帧头必须在 0–255 范围内：{}".format(header))
        return bytes([header, code])
    raise MarkerError("不支持的串口协议：{}".format(protocol))


class CsvTable:
    def __init__(self, path, fieldnames):
        self.path = Path(path)
        self.handle = self.path.open("w", newline="", encoding="utf-8-sig")
        self.writer = csv.DictWriter(self.handle, fieldnames=fieldnames, extrasaction="ignore")
        self.writer.writeheader()
        self.handle.flush()

    def write(self, row):
        self.writer.writerow(row)
        self.handle.flush()

    def close(self):
        if not self.handle.closed:
            self.handle.close()


class FaceCameraRecorder:
    """后台录制面部视频，并为每个成功写入的视频帧记录同源实验时钟。"""

    FRAME_FIELDS = ["frame_index", "time_s", "wall_time", "width", "height"]

    def __init__(self, camera_config, camera_index, width, height, fps,
                 video_path, frames_path, info_path, clock):
        try:
            import cv2
        except ImportError as exc:
            raise CameraError("PsychoPy环境缺少OpenCV，无法录制摄像头") from exc
        self.cv2 = cv2
        self.config = camera_config
        self.camera_index = int(camera_index)
        self.requested_width = int(width)
        self.requested_height = int(height)
        self.requested_fps = float(fps)
        self.video_path = Path(video_path)
        self.frames_path = Path(frames_path)
        self.info_path = Path(info_path)
        self.clock = clock
        self.capture = None
        self.writer = None
        self.frames_handle = None
        self.frames_writer = None
        self.thread = None
        self.stop_event = threading.Event()
        self.error = None
        self.frame_count = 0
        self.first_frame_time = None
        self.last_frame_time = None
        self.actual_width = 0
        self.actual_height = 0
        self.writer_fps = 0.0
        self.reported_fps = 0.0
        self.calibration_fps = 0.0
        self.backend_name = ""
        self.free_disk_start_gb = 0.0
        self.latest_frame = None
        self.frame_lock = threading.Lock()

    def start(self):
        cv2 = self.cv2
        self.free_disk_start_gb = shutil.disk_usage(self.video_path.parent).free / (1024 ** 3)
        minimum_free_gb = float(self.config.get("minimum_free_disk_gb", 20.0))
        if self.free_disk_start_gb < minimum_free_gb:
            raise CameraError("录像磁盘剩余{:.1f}GB，低于最低要求{:.1f}GB".format(
                self.free_disk_start_gb, minimum_free_gb))
        capture = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not capture.isOpened():
            capture.release()
            capture = cv2.VideoCapture(self.camera_index)
        if not capture.isOpened():
            raise CameraError("无法打开摄像头{}；请检查连接、编号和占用状态".format(
                self.camera_index))
        self.capture = capture
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.requested_width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.requested_height)
        capture.set(cv2.CAP_PROP_FPS, self.requested_fps)
        try:
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        calibration_frames = []
        calibration_count = max(15, min(30, int(round(self.requested_fps / 2.0))))
        for _ in range(calibration_count):
            ok, frame = capture.read()
            frame_time = self.clock.getTime()
            if ok and frame is not None:
                calibration_frames.append((frame, frame_time))
        ok = bool(calibration_frames)
        if ok:
            frame, frame_time = calibration_frames[-1]
        if not ok or frame is None:
            capture.release()
            raise CameraError("摄像头{}已打开但无法读取画面".format(self.camera_index))
        self.actual_height, self.actual_width = frame.shape[:2]
        self.reported_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        calibration_duration = (calibration_frames[-1][1] - calibration_frames[0][1]
                                if len(calibration_frames) > 1 else 0.0)
        self.calibration_fps = ((len(calibration_frames) - 1) / calibration_duration
                                if calibration_duration > 0 else 0.0)
        self.writer_fps = (round(self.calibration_fps, 3)
                           if self.calibration_fps > 1.0 else
                           (self.reported_fps if self.reported_fps > 1.0
                            else self.requested_fps))
        minimum_fps = float(self.config.get("minimum_acceptable_fps", 25.0))
        if self.calibration_fps > 0 and self.calibration_fps < minimum_fps:
            capture.release()
            raise CameraError(
                "摄像头{}实测仅{:.2f}fps，低于最低要求{:.2f}fps".format(
                    self.camera_index, self.calibration_fps, minimum_fps))
        try:
            self.backend_name = capture.getBackendName()
        except Exception:
            self.backend_name = "unknown"
        codec = str(self.config.get("codec", "MJPG"))[:4].ljust(4)
        writer = cv2.VideoWriter(
            str(self.video_path), cv2.VideoWriter_fourcc(*codec), self.writer_fps,
            (self.actual_width, self.actual_height))
        if not writer.isOpened():
            capture.release()
            raise CameraError("无法创建摄像头视频：{}（编码{}）".format(
                self.video_path, codec))
        self.writer = writer
        self.frames_handle = self.frames_path.open("w", newline="", encoding="utf-8-sig")
        self.frames_writer = csv.DictWriter(self.frames_handle, fieldnames=self.FRAME_FIELDS)
        self.frames_writer.writeheader()
        self._write_frame(frame, frame_time)
        self.thread = threading.Thread(target=self._capture_loop,
                                       name="face-camera-recorder", daemon=True)
        self.thread.start()
        print("摄像头录像已开始：设备{}，{}x{}，请求{:.2f}/报告{:.2f}/校准{:.2f}fps，编码{}".format(
            self.camera_index, self.actual_width, self.actual_height,
            self.requested_fps, self.reported_fps, self.calibration_fps, codec))
        if self.calibration_fps < self.requested_fps * 0.8:
            print("警告：摄像头实际帧率明显低于请求值；微表情采集前请更换模式或摄像头。",
                  file=sys.stderr)

    def _write_frame(self, frame, frame_time):
        self.writer.write(frame)
        with self.frame_lock:
            self.latest_frame = frame.copy()
        self.frame_count += 1
        if self.first_frame_time is None:
            self.first_frame_time = float(frame_time)
        self.last_frame_time = float(frame_time)
        self.frames_writer.writerow({
            "frame_index": self.frame_count,
            "time_s": "{:.6f}".format(frame_time),
            "wall_time": datetime.now().isoformat(timespec="milliseconds"),
            "width": self.actual_width,
            "height": self.actual_height,
        })
        if self.frame_count % 30 == 0:
            self.frames_handle.flush()

    def _capture_loop(self):
        try:
            consecutive_failures = 0
            while not self.stop_event.is_set():
                ok, frame = self.capture.read()
                frame_time = self.clock.getTime()
                if not ok or frame is None:
                    consecutive_failures += 1
                    if consecutive_failures >= 5:
                        raise CameraError("摄像头连续5次读取失败，录像已中断")
                    continue
                consecutive_failures = 0
                self._write_frame(frame, frame_time)
        except Exception as exc:
            self.error = exc

    def ensure_healthy(self):
        if self.error is not None:
            raise CameraError(str(self.error))
        if self.thread is not None and not self.thread.is_alive() and not self.stop_event.is_set():
            raise CameraError("摄像头录像线程意外停止")

    def preview_frame(self):
        with self.frame_lock:
            if self.latest_frame is None:
                return None
            frame = self.latest_frame.copy()
        # 仅预览镜像，保存的视频保持摄像头原始方向。
        return self.cv2.cvtColor(frame[:, ::-1], self.cv2.COLOR_BGR2RGB)

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=3.0)
        if self.thread is not None and self.thread.is_alive() and self.capture is not None:
            self.capture.release()
            self.thread.join(timeout=2.0)
        if self.capture is not None:
            self.capture.release()
            self.capture = None
        if self.writer is not None:
            self.writer.release()
            self.writer = None
        if self.frames_handle is not None and not self.frames_handle.closed:
            self.frames_handle.flush()
            self.frames_handle.close()
        duration_s = 0.0
        if self.first_frame_time is not None and self.last_frame_time is not None:
            duration_s = max(0.0, self.last_frame_time - self.first_frame_time)
        measured_fps = ((self.frame_count - 1) / duration_s
                        if duration_s > 0 and self.frame_count > 1 else 0.0)
        video_bytes = self.video_path.stat().st_size if self.video_path.is_file() else 0
        estimated_gb_per_hour = ((video_bytes / duration_s) * 3600.0 / (1024 ** 3)
                                 if duration_s > 0 else 0.0)
        info = {
            "camera_index": self.camera_index,
            "backend": self.backend_name,
            "requested_width": self.requested_width,
            "requested_height": self.requested_height,
            "requested_fps": self.requested_fps,
            "actual_width": self.actual_width,
            "actual_height": self.actual_height,
            "reported_fps": self.reported_fps,
            "calibration_fps": self.calibration_fps,
            "writer_fps": self.writer_fps,
            "frame_count": self.frame_count,
            "first_frame_time_s": self.first_frame_time,
            "last_frame_time_s": self.last_frame_time,
            "duration_s": duration_s,
            "measured_fps": measured_fps,
            "video_bytes": video_bytes,
            "estimated_gb_per_hour": estimated_gb_per_hour,
            "free_disk_start_gb": self.free_disk_start_gb,
            "error": "" if self.error is None else str(self.error),
        }
        self.info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print("摄像头录像已结束：{}帧，实测{:.2f}fps，时长{:.2f}s，估算{:.1f}GB/小时".format(
            self.frame_count, measured_fps, duration_s, estimated_gb_per_hour))
        return info


class MarkerHub:
    """统一 marker 输出；支持硬件与 LSL 双路发送，并始终写事件 CSV。"""

    EVENT_FIELDS = ["event_index", "time_s", "wall_time", "code", "label",
                    "outputs", "paradigm", "block_index", "trial_index",
                    "category", "stimulus", "detail"]

    def __init__(self, mode, endpoint, marker_config, clock, event_path):
        self.mode = str(mode).strip().lower()
        self.modes = [] if self.mode == "log" else self.mode.split("+")
        allowed = {"parallel", "serial", "lsl"}
        if (any(part not in allowed for part in self.modes)
                or len(self.modes) != len(set(self.modes))):
            raise MarkerError("不支持的 marker 模式：{}".format(self.mode))
        self.endpoint = str(endpoint).strip()
        self.config = marker_config
        self.clock = clock
        self.events = CsvTable(event_path, self.EVENT_FIELDS)
        self.event_index = 0
        self.devices = {}
        self.timers = []
        self.closed = False
        try:
            self._open_device()
        except Exception:
            self.events.close()
            raise

    def _open_device(self):
        if not self.modes:
            return
        try:
            for output in self.modes:
                if output == "parallel":
                    from psychopy import parallel
                    address = int(self.endpoint or "0x0378", 0)
                    device = parallel.ParallelPort(address=address)
                    device.setData(0)
                    self.devices[output] = device
                elif output == "serial":
                    import serial
                    if not self.endpoint or self.endpoint.lower() == "auto":
                        from serial.tools import list_ports
                        ports = list(list_ports.comports())
                        # 当前打标盒使用 WCH CH343/CH9102；优先按厂商VID识别。
                        preferred = [port for port in ports if port.vid == 0x1A86]
                        candidates = preferred if preferred else ports
                        if not candidates:
                            raise MarkerError("未检测到串口，请连接打标盒后重试")
                        if len(candidates) > 1:
                            descriptions = ", ".join(
                                "{} ({})".format(port.device, port.description)
                                for port in candidates)
                            raise MarkerError(
                                "检测到多个候选串口：{}；请明确填写COM口".format(descriptions))
                        self.endpoint = candidates[0].device
                        print("自动选择打标盒串口：{} ({})".format(
                            candidates[0].device, candidates[0].description))
                    self.devices[output] = serial.Serial(
                        port=self.endpoint,
                        baudrate=int(self.config.get("serial_baudrate", 115200)),
                        bytesize=8,
                        parity="N",
                        stopbits=1,
                        timeout=0,
                    )
                elif output == "lsl":
                    from pylsl import StreamInfo, StreamOutlet
                    info = StreamInfo(
                        self.config.get("lsl_stream_name", "EmotionParadigmMarkers"),
                        self.config.get("lsl_stream_type", "Markers"),
                        1, 0, "int32", "emotion-paradigm-markers",
                    )
                    self.devices[output] = StreamOutlet(info)
        except MarkerError:
            self._close_devices()
            raise
        except Exception as exc:
            self._close_devices()
            raise MarkerError("无法初始化 {} 打标：{}".format(self.mode, exc))

    def _reset_device(self, output):
        if self.closed or output not in self.devices:
            return
        try:
            if output == "parallel":
                self.devices[output].setData(0)
            elif output == "serial" and self.config.get("serial_zero_reset", False):
                self.devices[output].write(bytes([0]))
        except Exception:
            pass

    def _close_devices(self):
        for output, device in list(self.devices.items()):
            if output == "parallel":
                try:
                    device.setData(0)
                except Exception:
                    pass
            if output == "serial":
                try:
                    device.close()
                except Exception:
                    pass
        self.devices.clear()

    def send(self, code, label, paradigm="", block_index="", trial_index="",
             category="", stimulus="", detail=""):
        code = int(code)
        if not 0 <= code <= 255:
            raise MarkerError("marker 必须在 0–255 范围内：{}".format(code))
        timestamp = self.clock.getTime()
        try:
            for output in self.modes:
                device = self.devices[output]
                if output == "parallel":
                    device.setData(code)
                elif output == "serial":
                    payload = build_serial_payload(self.config, code)
                    written = device.write(payload)
                    if written != len(payload):
                        raise MarkerError("串口 marker 未完整写出：{}/{} bytes".format(
                            written, len(payload)))
                elif output == "lsl":
                    device.push_sample([code])
                needs_reset = (output == "parallel" or
                               (output == "serial" and
                                self.config.get("serial_zero_reset", False)))
                if needs_reset:
                    pulse = float(self.config.get("pulse_width_s", 0.01))
                    timer = threading.Timer(pulse, self._reset_device, args=(output,))
                    timer.daemon = True
                    timer.start()
                    self.timers.append(timer)
        finally:
            self.event_index += 1
            self.events.write({
                "event_index": self.event_index,
                "time_s": "{:.6f}".format(timestamp),
                "wall_time": datetime.now().isoformat(timespec="milliseconds"),
                "code": code,
                "label": label,
                "outputs": self.mode,
                "paradigm": paradigm,
                "block_index": block_index,
                "trial_index": trial_index,
                "category": category,
                "stimulus": stimulus,
                "detail": detail,
            })
        return timestamp

    def close(self):
        if self.closed:
            return
        for output in self.modes:
            self._reset_device(output)
        self.closed = True
        self._close_devices()
        self.events.close()


def fit_size(path, max_width=1.72, max_height=0.96):
    try:
        from PIL import Image
        with Image.open(str(path)) as image:
            width, height = image.size
        aspect = float(width) / float(height)
        out_height = min(max_height, max_width / aspect)
        return (out_height * aspect, out_height)
    except Exception:
        return (1.60, 0.90)


def check_abort(event_module):
    if event_module.getKeys(keyList=["escape"]):
        raise ExperimentAbort()


def schedule_marker(win, marker, row, field, code, label, **metadata):
    def callback():
        row[field] = "{:.6f}".format(marker.send(code, label, **metadata))
    win.callOnFlip(callback)


def make_image_stimulus(win, visual, image_path):
    return visual.ImageStim(win, image=str(image_path), size=fit_size(image_path),
                            units="height", interpolate=True, autoLog=False)


def show_fixed_image(win, event, core, stimulus, duration, marker, row,
                     onset_field, code, label, metadata=None, after_first_flip=None):
    metadata = metadata or {}
    stage_clock = core.Clock()
    event.clearEvents()
    stimulus.draw()
    schedule_marker(win, marker, row, onset_field, code, label, **metadata)
    win.callOnFlip(stage_clock.reset)
    win.flip()
    if after_first_flip is not None:
        after_first_flip()
    while stage_clock.getTime() < duration:
        check_abort(event)
        stimulus.draw()
        win.flip()


def show_start(win, event, core, stimulus, auto_continue=False):
    event.clearEvents()
    auto_clock = core.Clock()
    while True:
        stimulus.draw()
        win.flip()
        if auto_continue and auto_clock.getTime() >= 0.25:
            return
        keys = event.getKeys(keyList=["space", "escape"])
        if "escape" in keys:
            raise ExperimentAbort()
        if "space" in keys:
            return


def show_still_stimulus(win, event, core, stimulus, item, duration, marker, row, code,
                        allow_skip=False):
    stage_clock = core.Clock()
    event.clearEvents()
    stimulus.draw()
    metadata = {"trial_index": row["trial_index"], "category": item["category"],
                "stimulus": item["relative"]}
    schedule_marker(win, marker, row, "stimulus_onset_s", code,
                    "stimulus_{}".format(item["category"]), **metadata)
    win.callOnFlip(stage_clock.reset)
    win.flip()
    while stage_clock.getTime() < duration:
        keys = event.getKeys(keyList=["space", "escape"])
        if "escape" in keys:
            raise ExperimentAbort()
        if allow_skip and "space" in keys:
            break
        stimulus.draw()
        win.flip()


def prepare_movie_stimulus(win, visual, item, test_mode=False):
    kwargs = dict(win=win, filename=str(item["path"]), loop=False, noAudio=False,
                  autoLog=False, units="height", size=(1.60, 0.90), autoStart=False)
    try:
        movie = visual.MovieStim(**kwargs)
    except TypeError:
        kwargs.pop("autoStart", None)
        movie = visual.MovieStim(**kwargs)
    try:
        video_size = movie.getVideoSize()
        if video_size and video_size[0] and video_size[1]:
            aspect = float(video_size[0]) / float(video_size[1])
            height = min(0.90, 1.70 / aspect)
            movie.size = (height * aspect, height)
    except Exception:
        pass
    return movie


def show_movie_stimulus(win, event, item, movie, marker, row, code, test_mode=False,
                        allow_skip=False):

    metadata = {"trial_index": row["trial_index"], "category": item["category"],
                "stimulus": item["relative"]}
    event.clearEvents()
    movie.draw()
    schedule_marker(win, marker, row, "stimulus_onset_s", code,
                    "stimulus_{}".format(item["category"]), **metadata)
    # 当前 PsychoPy 可将播放启动与首帧翻转同步；旧版已自动启动时 play() 也安全。
    win.callOnFlip(movie.play)
    win.flip()
    test_clock = None
    if test_mode:
        from psychopy import core
        test_clock = core.Clock()
    while not bool(getattr(movie, "isFinished", False)):
        keys = event.getKeys(keyList=["space", "escape"])
        if "escape" in keys:
            raise ExperimentAbort()
        if allow_skip and "space" in keys:
            break
        if test_clock is not None and test_clock.getTime() >= 3.0:
            break
        movie.draw()
        win.flip()
    if (test_clock is not None or allow_skip) and not bool(getattr(movie, "isFinished", False)):
        try:
            movie.pause()
        except Exception:
            pass


def collect_rating(win, visual, event, core, background, marker, row, codes, metadata,
                   auto_response=False):
    pointer = visual.ShapeStim(
        win,
        vertices=[(-0.022, -0.025), (0.022, -0.025), (0.0, 0.022)],
        closeShape=True,
        fillColor=(255, 215, 0),
        lineColor=(20, 20, 20),
        lineWidth=2,
        colorSpace="rgb255",
        units="height",
        autoLog=False,
    )
    selection_text = visual.TextStim(win, text="", pos=(0, -0.34), height=0.04,
                                     color=(20, 120, 20), colorSpace="rgb255",
                                     font="SimHei", units="height", autoLog=False)
    rating_clock = core.Clock()
    selected = 5
    selection_rt = None
    event.clearEvents()
    background.draw()
    schedule_marker(win, marker, row, "rating_onset_s", codes["rating_onset"],
                    "rating_onset", **metadata)
    win.callOnFlip(rating_clock.reset)
    win.flip()
    while True:
        background.draw()
        pointer.pos = (-0.696 + (selected - 1) * 0.174, -0.015)
        pointer.draw()
        selection_text.text = "当前选择：{}　← / → 移动　空格或回车确认".format(selected)
        selection_text.color = (20, 120, 20)
        selection_text.draw()
        win.flip()
        if auto_response and rating_clock.getTime() >= 0.25:
            selected = 5
            selection_rt = rating_clock.getTime()
            confirmation_rt = rating_clock.getTime()
            marker.send(codes["rating_5"], "rating_select_5",
                        detail="rt={:.6f};automated=1;confirmed=1".format(confirmation_rt),
                        **metadata)
            row["rating"] = selected
            row["rating_rt_s"] = "{:.6f}".format(selection_rt)
            row["confirmation_rt_s"] = "{:.6f}".format(confirmation_rt)
            return
        keys = event.getKeys(keyList=[
            "1", "2", "3", "4", "5", "6", "7", "8", "9",
            "num_1", "num_2", "num_3", "num_4", "num_5", "num_6", "num_7", "num_8", "num_9",
            "left", "right", "a", "d", "space", "return", "enter", "num_enter", "escape",
        ])
        for key in keys:
            if key == "escape":
                raise ExperimentAbort()
            if key in ("left", "a"):
                selected = max(1, selected - 1)
                selection_rt = rating_clock.getTime()
                continue
            if key in ("right", "d"):
                selected = min(9, selected + 1)
                selection_rt = rating_clock.getTime()
                continue
            match = re.search(r"([1-9])$", key)
            if match:
                selected = int(match.group(1))
                selection_rt = rating_clock.getTime()
                continue
            if key in ("space", "return", "enter", "num_enter"):
                confirmation_rt = rating_clock.getTime()
                default_confirmed = selection_rt is None
                if selection_rt is None:
                    selection_rt = confirmation_rt
                marker.send(
                    codes["rating_{}".format(selected)],
                    "rating_select_{}".format(selected),
                    detail=("selection_rt={:.6f};confirmation_rt={:.6f};"
                            "default_confirmed={};confirmed=1").format(
                                selection_rt, confirmation_rt, int(default_confirmed)),
                    **metadata
                )
                row["rating"] = selected
                row["rating_rt_s"] = "{:.6f}".format(selection_rt)
                row["confirmation_rt_s"] = "{:.6f}".format(confirmation_rt)
                return


def make_session_paths(participant, paradigm_id, session, camera_enabled=False):
    output_dir = APP_DIR / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = "{}_p{}_s{}_{}".format(safe_filename(participant), paradigm_id,
                                  safe_filename(session), stamp)
    paths = {
        "events": output_dir / (stem + "_events.csv"),
        "trials": output_dir / (stem + "_trials.csv"),
        "plan": output_dir / (stem + "_stimulus_plan.csv"),
    }
    if camera_enabled:
        paths.update({
            "face_video": output_dir / (stem + "_face.avi"),
            "face_frames": output_dir / (stem + "_face_frames.csv"),
            "face_info": output_dir / (stem + "_face_info.json"),
        })
    return paths


def start_face_camera(config, options, clock, paths):
    if not options.camera:
        return None
    camera_config = config.get("camera", {})
    recorder = FaceCameraRecorder(
        camera_config, options.camera_index, options.camera_width,
        options.camera_height, options.camera_fps,
        paths["face_video"], paths["face_frames"], paths["face_info"], clock)
    recorder.start()
    return recorder


def show_camera_alignment(win, visual, event, core, recorder, auto_continue=False):
    """实验前显示实时面部取景，确认后进入范式。"""
    from PIL import Image
    preview = visual.ImageStim(win, units="height", size=(1.20, 0.675),
                               autoLog=False, interpolate=True)
    fps_warning = ("\n警告：实际帧率低于请求值，正式微表情采集前请确认设备能力"
                   if recorder.calibration_fps < recorder.requested_fps * 0.8 else "")
    instruction = visual.TextStim(
        win, text=("摄像头取景检查：设备{}，{}×{}，实测{:.1f}fps{}\n"
                   "请确认面部完整、清晰、光线均匀；按空格继续，Esc中止").format(
                       recorder.camera_index, recorder.actual_width,
                       recorder.actual_height, recorder.calibration_fps, fps_warning),
        pos=(0, 0.43), height=0.035, color="white", font="SimHei",
        units="height", wrapWidth=1.55, autoLog=False)
    event.clearEvents()
    auto_clock = core.Clock()
    while True:
        recorder.ensure_healthy()
        frame = recorder.preview_frame()
        if frame is not None:
            preview.image = Image.fromarray(frame)
            preview.draw()
        instruction.draw()
        win.flip()
        if auto_continue and auto_clock.getTime() >= 0.5:
            return
        keys = event.getKeys(keyList=["space", "escape"])
        if "escape" in keys:
            raise ExperimentAbort()
        if "space" in keys:
            return


TRIAL_FIELDS = [
    "participant", "session", "paradigm", "seed", "block_index",
    "block_trial_index", "global_trial_index", "trial_index", "category",
    "category_cn", "stimulus", "fixation_onset_s", "stimulus_onset_s",
    "stimulus_offset_s", "rating_onset_s", "rating", "rating_rt_s",
    "confirmation_rt_s", "rest_onset_s", "trial_end_s", "planned_duration_s",
    "reused", "status",
]


PLAN_FIELDS = [
    "block_index", "block_trial_index", "global_trial_index", "paradigm",
    "category", "category_cn", "stimulus", "video_duration_s",
    "planned_duration_s", "reused", "seed",
]


def show_block_transition(win, visual, event, core, marker, codes, block, block_index,
                          block_count, estimated_total_s, wait_for_space, auto_continue=False,
                          demo_mode=False):
    pid = block["paradigm"]
    title = "第 {}/{} 部分\n{}".format(block_index, block_count, block["config"]["name"])
    if wait_for_space:
        message = title + "\n\n可以休息，准备好后按空格继续"
        stimulus = visual.TextStim(win, text=message, height=0.055, color="white",
                                   font="SimHei", units="height", autoLog=False,
                                   wrapWidth=1.55)
        event.clearEvents()
        wait_clock = core.Clock()
        while True:
            stimulus.draw()
            win.flip()
            if auto_continue and wait_clock.getTime() >= 0.25:
                break
            keys = event.getKeys(keyList=["space", "escape"])
            if "escape" in keys:
                raise ExperimentAbort()
            if "space" in keys:
                break

    ready_text = title + "\n\n即将开始"
    if demo_mode:
        ready_text += "\n流程演示：素材呈现期间可按空格跳过"
    if block_index == 1:
        ready_text += "\n本次预计约 {:.1f} 分钟（不含自定休息）".format(estimated_total_s / 60.0)
    ready = visual.TextStim(win, text=ready_text, height=0.055, color="white",
                            font="SimHei", units="height", autoLog=False,
                            wrapWidth=1.55)
    ready_clock = core.Clock()
    ready.draw()
    win.callOnFlip(marker.send, codes["block_start_{}".format(pid)],
                   "block_start_{}".format(pid), paradigm=pid, block_index=block_index,
                   detail="estimated_block_s={:.3f}".format(block["estimated_s"]))
    win.callOnFlip(ready_clock.reset)
    win.flip()
    duration = 0.25 if auto_continue else 1.0
    while ready_clock.getTime() < duration:
        check_abort(event)
        ready.draw()
        win.flip()


def execute_blocks(config, options, blocks, seed, paths, estimated_total_s):
    from psychopy import core, event, visual

    expected_rating_s = float(config.get("integrated_session", {}).get("expected_rating_s", 8.0))
    plan_table = CsvTable(paths["plan"], PLAN_FIELDS)
    global_index = 0
    for block_index, block in enumerate(blocks, start=1):
        pid = block["paradigm"]
        pconfig = block["config"]
        for block_trial_index, item in enumerate(block["stimuli"], start=1):
            global_index += 1
            planned = estimate_trial_s(pconfig, item, expected_rating_s)
            plan_table.write({
                "block_index": block_index, "block_trial_index": block_trial_index,
                "global_trial_index": global_index, "paradigm": pid,
                "category": item["category"],
                "category_cn": CATEGORY_CN.get(item["category"], item["category"]),
                "stimulus": item["relative"],
                "video_duration_s": ("{:.6f}".format(item["duration_s"])
                                     if "duration_s" in item else ""),
                "planned_duration_s": "{:.6f}".format(planned),
                "reused": int(bool(item.get("reused", False))), "seed": seed,
            })
    plan_table.close()

    trial_table = CsvTable(paths["trials"], TRIAL_FIELDS)
    global_clock = core.Clock()
    marker = None
    camera = None
    win = None
    codes = config["marker_codes"]
    completed = False
    active_row = None
    current_prepared = []
    try:
        marker = MarkerHub(options.marker, options.endpoint, config["marker"],
                           global_clock, paths["events"])
        win = visual.Window(
            size=(1280, 720), fullscr=not options.windowed,
            screen=int(config.get("screen", 0)), allowGUI=bool(options.windowed),
            color=config.get("background_rgb255", [185, 183, 183]),
            colorSpace="rgb255", units="height", waitBlanking=True,
        )
        asset_root = resolve_asset_root(config)
        helper_paths = {name: asset_root / relative for name, relative in HELPER_FILES.items()}
        helpers = {name: make_image_stimulus(win, visual, path)
                   for name, path in helper_paths.items()}
        loading = visual.TextStim(win, text="正在加载实验材料，请稍候……", height=0.055,
                                  color="white", font="SimHei", units="height", autoLog=False)
        camera = start_face_camera(config, options, global_clock, paths)
        if camera is not None:
            show_camera_alignment(win, visual, event, core, camera, options.smoke_test)

        show_start(win, event, core, helpers["start"], options.smoke_test)
        if camera is not None:
            camera.ensure_healthy()
        marker.send(codes["experiment_start"], "experiment_start",
                    detail="blocks={};estimated_s={:.3f};seed={}".format(
                        ",".join(block["paradigm"] for block in blocks), estimated_total_s, seed))

        global_trial_index = 0
        for block_index, block in enumerate(blocks, start=1):
            pid = block["paradigm"]
            pconfig = block["config"]
            stimuli = block["stimuli"]
            loading.text = "正在加载第 {}/{} 部分材料，请稍候……".format(block_index, len(blocks))
            loading.draw()
            win.flip()
            if pconfig["kind"] == "video":
                current_prepared = [None] * len(stimuli)
                current_prepared[0] = prepare_movie_stimulus(
                    win, visual, stimuli[0], options.test)
            else:
                current_prepared = [make_image_stimulus(win, visual, item["path"])
                                    for item in stimuli]

            show_block_transition(
                win, visual, event, core, marker, codes, block, block_index, len(blocks),
                estimated_total_s, wait_for_space=(block_index > 1),
                auto_continue=options.smoke_test,
                demo_mode=options.demo,
            )

            for block_trial_index, item in enumerate(stimuli, start=1):
                if camera is not None:
                    camera.ensure_healthy()
                global_trial_index += 1
                prepared_stimulus = current_prepared[block_trial_index - 1]
                if prepared_stimulus is None:
                    loading.draw()
                    win.flip()
                    prepared_stimulus = prepare_movie_stimulus(
                        win, visual, item, options.test)
                    current_prepared[block_trial_index - 1] = prepared_stimulus
                metadata = {
                    "trial_index": global_trial_index, "category": item["category"],
                    "stimulus": item["relative"], "paradigm": pid,
                    "block_index": block_index,
                }
                row = {field: "" for field in TRIAL_FIELDS}
                planned = estimate_trial_s(pconfig, item, expected_rating_s)
                row.update({
                    "participant": options.participant, "session": options.session,
                    "paradigm": pid, "seed": seed, "block_index": block_index,
                    "block_trial_index": block_trial_index,
                    "global_trial_index": global_trial_index,
                    "trial_index": global_trial_index, "category": item["category"],
                    "category_cn": CATEGORY_CN.get(item["category"], item["category"]),
                    "stimulus": item["relative"],
                    "planned_duration_s": "{:.6f}".format(planned),
                    "reused": int(bool(item.get("reused", False))), "status": "running",
                })
                active_row = row
                fixation_s = 0.30 if options.test else float(pconfig["fixation_s"])
                show_fixed_image(win, event, core, helpers["fixation"], fixation_s,
                                 marker, row, "fixation_onset_s", codes["fixation"],
                                 "fixation", metadata)

                stim_code = codes.get("stimulus_{}".format(item["category"]),
                                      codes["stimulus_unknown"])
                if pconfig["kind"] == "video":
                    show_movie_stimulus(win, event, item, prepared_stimulus,
                                        marker, row, stim_code, options.test,
                                        allow_skip=options.demo)
                else:
                    stimulus_s = 0.50 if options.test else float(pconfig["stimulus_s"])
                    show_still_stimulus(win, event, core, prepared_stimulus, item,
                                        stimulus_s, marker, row, stim_code,
                                        allow_skip=options.demo)

                if pconfig.get("rating", True):
                    collect_rating(win, visual, event, core, helpers["rating"], marker,
                                   row, codes, metadata, options.smoke_test)
                    row["stimulus_offset_s"] = row["rating_onset_s"]

                rest_s = float(pconfig.get("rest_s", 0.0))
                if rest_s > 0:
                    if options.test:
                        rest_s = 0.30
                    rest_loader = None
                    if (pconfig["kind"] == "video" and not options.test
                            and block_trial_index < len(stimuli)):
                        next_index = block_trial_index

                        def rest_loader(current=prepared_stimulus, index=next_index):
                            try:
                                current.unload(log=False)
                            except Exception:
                                pass
                            current_prepared[index] = prepare_movie_stimulus(
                                win, visual, stimuli[index], False)

                    show_fixed_image(win, event, core, helpers["rest"], rest_s,
                                     marker, row, "rest_onset_s", codes["rest"], "rest",
                                     metadata, rest_loader)
                row["trial_end_s"] = "{:.6f}".format(
                    marker.send(codes["trial_end"], "trial_end", **metadata))
                row["status"] = "completed"
                trial_table.write(row)
                active_row = None
                if camera is not None:
                    camera.ensure_healthy()

            marker.send(codes["block_end_{}".format(pid)], "block_end_{}".format(pid),
                        paradigm=pid, block_index=block_index,
                        detail="completed_trials={}".format(len(stimuli)))
            if options.smoke_test and pconfig["kind"] == "video":
                SMOKE_MOVIE_KEEPALIVE.extend(
                    stimulus for stimulus in current_prepared if stimulus is not None)
            else:
                for stimulus in current_prepared:
                    if stimulus is not None and hasattr(stimulus, "unload"):
                        try:
                            stimulus.unload(log=False)
                        except Exception:
                            pass
            current_prepared = []

        end_row = {}
        ending_s = 0.30 if options.test else 2.0
        show_fixed_image(win, event, core, helpers["end"], ending_s,
                         marker, end_row, "end_onset_s", codes["experiment_end"],
                         "experiment_end", {"detail": "completed"})
        completed = True
    except ExperimentAbort:
        if active_row is not None:
            active_row["status"] = "aborted"
            trial_table.write(active_row)
            active_row = None
        if marker is not None:
            marker.send(codes["aborted"], "aborted", detail="escape")
    except Exception:
        if active_row is not None:
            active_row["status"] = "error"
            trial_table.write(active_row)
            active_row = None
        raise
    finally:
        trial_table.close()
        if camera is not None:
            try:
                camera.stop()
            except Exception as exc:
                print("摄像头停止/保存失败：{}".format(exc), file=sys.stderr)
        if marker is not None:
            marker.close()
        for stimulus in current_prepared:
            if stimulus is not None and hasattr(stimulus, "unload"):
                try:
                    stimulus.unload(log=False)
                except Exception:
                    pass
        if win is not None:
            win.close()
    return {"completed": completed, "paths": paths, "seed": seed,
            "estimated_s": estimated_total_s, "integrated": len(blocks) > 1}


def run_experiment(config, options):
    try:
        from psychopy import core, event, gui, visual
    except ImportError as exc:
        raise RuntimeError("未找到 PsychoPy。请用 PsychoPy Standalone 打开本文件，或先安装 psychopy。") from exc

    if not options.no_gui:
        integrated_minutes = int(round(float(config.get("integrated_session", {}).get(
            "target_duration_s", 1800)) / 60.0))
        paradigm_choices = ["{} - {}".format(pid, pcfg["name"])
                            for pid, pcfg in config["paradigms"].items()]
        default_choice = next((choice for choice in paradigm_choices
                               if choice.startswith(str(options.paradigm) + " -")), paradigm_choices[0])
        info = {
            "被试编号": options.participant,
            "场次": options.session,
            "实验模式": ["整合实验（推荐，范式1–3，自动约{}分钟）".format(
                integrated_minutes), "单范式调试"],
            "单范式（仅调试使用）": [default_choice] + [c for c in paradigm_choices if c != default_choice],
            "全屏": not options.windowed,
            "打标方式": [options.marker] + [m for m in [
                "log", "lsl", "serial", "parallel", "serial+lsl", "parallel+lsl"
            ] if m != options.marker],
            "端口或地址": options.endpoint,
            "随机种子（留空则自动）": "" if options.seed is None else str(options.seed),
            "运行版本": (["流程演示版（正式配额、缩短时长、可跳过素材）", "正式实验"]
                         if options.demo else
                         ["正式实验", "流程演示版（正式配额、缩短时长、可跳过素材）"]),
            "工程短测（减少素材并缩短时长）": bool(options.test),
            "摄像头录像": bool(options.camera or config.get("camera", {}).get(
                "enabled_by_default", True)),
            "摄像头编号": str(options.camera_index),
            "摄像头分辨率": "{}x{}".format(options.camera_width, options.camera_height),
            "摄像头帧率": str(options.camera_fps),
        }
        dialog = gui.DlgFromDict(info, title="情绪诱发范式", sortKeys=False)
        if not dialog.OK:
            return None
        options.participant = str(info["被试编号"]).strip() or "anonymous"
        options.session = str(info["场次"]).strip() or "1"
        options.integrated = str(info["实验模式"]).startswith("整合实验")
        options.paradigm = str(info["单范式（仅调试使用）"]).split(" -", 1)[0]
        options.windowed = not bool(info["全屏"])
        options.marker = str(info["打标方式"])
        options.endpoint = str(info["端口或地址"])
        seed_text = str(info["随机种子（留空则自动）"]).strip()
        options.seed = int(seed_text) if seed_text else None
        options.demo = str(info["运行版本"]).startswith("流程演示版")
        options.test = bool(info["工程短测（减少素材并缩短时长）"])
        options.camera = bool(info["摄像头录像"])
        options.camera_index = int(str(info["摄像头编号"]).strip())
        resolution_match = re.fullmatch(
            r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", str(info["摄像头分辨率"]))
        if not resolution_match:
            raise ValueError("摄像头分辨率应为宽x高，例如1280x720")
        options.camera_width = int(resolution_match.group(1))
        options.camera_height = int(resolution_match.group(2))
        options.camera_fps = float(str(info["摄像头帧率"]).strip())

    if options.demo:
        options.integrated = True
        options.test = True

    if options.camera:
        if options.camera_index < 0:
            raise ValueError("摄像头编号不得小于0")
        if options.camera_width < 160 or options.camera_height < 120:
            raise ValueError("摄像头分辨率过小")
        if options.camera_fps <= 0:
            raise ValueError("摄像头帧率必须大于0")

    if not options.integrated and options.paradigm not in config["paradigms"]:
        raise ValueError("范式必须为 1–6")
    errors = validate_assets(config, print_report=False)
    if errors:
        raise RuntimeError("素材校验失败：\n" + "\n".join(errors))

    seed = options.seed if options.seed is not None else random.SystemRandom().randrange(1, 2 ** 31)
    if options.integrated:
        session_plan = build_integrated_plan(
            config, options.participant, options.session, seed,
            options.test and not options.demo)
        if options.demo:
            session_plan = compact_demo_plan(config, session_plan)
        paths = make_session_paths(options.participant, "ALL", options.session,
                                   options.camera)
        if options.demo:
            print("流程演示计划：{} 个区块，预计约 {:.1f} 分钟".format(
                len(session_plan["blocks"]), session_plan["estimated_s"] / 60.0))
        else:
            print("整合实验计划：{} 个区块，预计 {:.1f} 分钟，目标 {:.1f} 分钟".format(
                len(session_plan["blocks"]), session_plan["estimated_s"] / 60.0,
                session_plan["target_s"] / 60.0))
        for index, block in enumerate(session_plan["blocks"], start=1):
            counts = {category: sum(item["category"] == category for item in block["stimuli"])
                      for category in ("positive", "neutral", "negative")}
            print("  区块{} 范式{}：{} trials，正/中/负={}/{}/{}，预计{:.1f}分钟".format(
                index, block["paradigm"], len(block["stimuli"]),
                counts["positive"], counts["neutral"], counts["negative"],
                block["estimated_s"] / 60.0))
        return execute_blocks(config, options, session_plan["blocks"], seed, paths,
                              session_plan["estimated_s"])

    asset_root = resolve_asset_root(config)
    pconfig = config["paradigms"][options.paradigm]
    rng = random.Random(seed)
    all_stimuli = discover_stimuli(asset_root, options.paradigm, pconfig)
    if options.test:
        trial_count = min(2, int(pconfig["trial_count"]))
        stimuli = sample_stimuli(all_stimuli, trial_count, rng,
                                  bool(pconfig.get("balanced_by_category", True)))
    elif pconfig.get("category_trial_counts"):
        stimuli = sample_category_targets_prefer_unused(
            all_stimuli, pconfig["category_trial_counts"], rng)
    else:
        stimuli = sample_stimuli(all_stimuli, int(pconfig["trial_count"]), rng,
                                  bool(pconfig.get("balanced_by_category", True)))
    paths = make_session_paths(options.participant, options.paradigm, options.session,
                               options.camera)

    plan_table = CsvTable(paths["plan"], ["trial_index", "category", "category_cn", "stimulus", "seed"])
    for index, item in enumerate(stimuli, start=1):
        plan_table.write({"trial_index": index, "category": item["category"],
                          "category_cn": CATEGORY_CN.get(item["category"], item["category"]),
                          "stimulus": item["relative"], "seed": seed})
    plan_table.close()

    trial_table = CsvTable(paths["trials"], TRIAL_FIELDS)
    global_clock = core.Clock()
    marker = None
    camera = None
    win = None
    prepared_stimuli = []
    codes = config["marker_codes"]
    completed = False
    active_row = None
    try:
        marker = MarkerHub(options.marker, options.endpoint, config["marker"],
                           global_clock, paths["events"])
        win = visual.Window(
            size=(1280, 720), fullscr=not options.windowed, screen=int(config.get("screen", 0)),
            allowGUI=bool(options.windowed), color=config.get("background_rgb255", [185, 183, 183]),
            colorSpace="rgb255", units="height", waitBlanking=True,
        )
        helper_paths = {name: asset_root / relative for name, relative in HELPER_FILES.items()}
        helpers = {name: make_image_stimulus(win, visual, path)
                   for name, path in helper_paths.items()}
        loading = visual.TextStim(win, text="正在加载实验材料，请稍候……", height=0.055,
                                  color="white", font="SimHei", units="height", autoLog=False)
        camera = start_face_camera(config, options, global_clock, paths)
        if camera is not None:
            show_camera_alignment(win, visual, event, core, camera, options.smoke_test)
        loading.draw()
        win.flip()
        if pconfig["kind"] == "video":
            # 第一段在开始提示前加载。正式实验的后续视频会利用上一 trial
            # 的 12 秒休息画面加载，避免延长注视阶段，也避免同时打开六个播放器。
            prepared_stimuli = [None] * len(stimuli)
            prepared_stimuli[0] = prepare_movie_stimulus(win, visual, stimuli[0], options.test)
        else:
            prepared_stimuli = [make_image_stimulus(win, visual, item["path"])
                                for item in stimuli]

        show_start(win, event, core, helpers["start"], options.smoke_test)
        if camera is not None:
            camera.ensure_healthy()
        marker.send(codes["experiment_start"], "experiment_start",
                    detail="paradigm={};seed={}".format(options.paradigm, seed))

        for trial_index, item in enumerate(stimuli, start=1):
            if camera is not None:
                camera.ensure_healthy()
            prepared_stimulus = prepared_stimuli[trial_index - 1]
            if prepared_stimulus is None:
                loading.draw()
                win.flip()
                prepared_stimulus = prepare_movie_stimulus(win, visual, item, options.test)
                prepared_stimuli[trial_index - 1] = prepared_stimulus
            metadata = {"trial_index": trial_index, "category": item["category"],
                        "stimulus": item["relative"]}
            row = {field: "" for field in TRIAL_FIELDS}
            row.update({
                "participant": options.participant, "session": options.session,
                "paradigm": options.paradigm, "seed": seed, "trial_index": trial_index,
                "category": item["category"],
                "category_cn": CATEGORY_CN.get(item["category"], item["category"]),
                "stimulus": item["relative"], "status": "running",
            })
            active_row = row
            fixation_s = 0.30 if options.test else float(pconfig["fixation_s"])
            show_fixed_image(win, event, core, helpers["fixation"], fixation_s,
                             marker, row, "fixation_onset_s", codes["fixation"], "fixation", metadata)

            stim_code = codes.get("stimulus_{}".format(item["category"]), codes["stimulus_unknown"])
            if pconfig["kind"] == "video":
                show_movie_stimulus(win, event, item, prepared_stimulus,
                                    marker, row, stim_code, options.test,
                                    allow_skip=options.demo)
            else:
                stimulus_s = 0.50 if options.test else float(pconfig["stimulus_s"])
                show_still_stimulus(win, event, core, prepared_stimulus, item, stimulus_s,
                                    marker, row, stim_code, allow_skip=options.demo)

            if pconfig.get("rating", True):
                collect_rating(win, visual, event, core, helpers["rating"], marker, row,
                               codes, metadata, options.smoke_test)
                # 材料真正从屏幕消失的时刻，就是评分界面的首次翻屏时刻。
                row["stimulus_offset_s"] = row["rating_onset_s"]

            rest_s = float(pconfig.get("rest_s", 0.0))
            if rest_s > 0:
                if options.test:
                    rest_s = 0.30
                rest_loader = None
                if (pconfig["kind"] == "video" and not options.test
                        and trial_index < len(stimuli)):
                    next_index = trial_index

                    def rest_loader(current=prepared_stimulus, index=next_index):
                        try:
                            current.unload(log=False)
                        except Exception:
                            pass
                        prepared_stimuli[index] = prepare_movie_stimulus(
                            win, visual, stimuli[index], False)

                show_fixed_image(win, event, core, helpers["rest"], rest_s,
                                 marker, row, "rest_onset_s", codes["rest"], "rest",
                                 metadata, rest_loader)
            row["trial_end_s"] = "{:.6f}".format(
                marker.send(codes["trial_end"], "trial_end", **metadata))
            row["status"] = "completed"
            trial_table.write(row)
            active_row = None
            if camera is not None:
                camera.ensure_healthy()

        end_row = {}
        ending_s = 0.30 if options.test else 2.0
        show_fixed_image(win, event, core, helpers["end"], ending_s,
                         marker, end_row, "end_onset_s", codes["experiment_end"],
                         "experiment_end", {"detail": "completed"})
        completed = True
    except ExperimentAbort:
        if active_row is not None:
            active_row["status"] = "aborted"
            trial_table.write(active_row)
            active_row = None
        if marker is not None:
            marker.send(codes["aborted"], "aborted", detail="escape")
    except Exception:
        if active_row is not None:
            active_row["status"] = "error"
            trial_table.write(active_row)
            active_row = None
        raise
    finally:
        trial_table.close()
        if camera is not None:
            try:
                camera.stop()
            except Exception as exc:
                print("摄像头停止/保存失败：{}".format(exc), file=sys.stderr)
        if marker is not None:
            marker.close()
        if options.smoke_test and pconfig["kind"] == "video":
            SMOKE_MOVIE_KEEPALIVE.extend(s for s in prepared_stimuli if s is not None)
        else:
            for stimulus in prepared_stimuli:
                # MovieStim.stop() 会“关闭后重新载入”影片，不适合退出清理；
                # unload() 才是只释放解码器、音频和纹理资源的接口。
                if stimulus is not None and hasattr(stimulus, "unload"):
                    try:
                        stimulus.unload(log=False)
                    except Exception:
                        pass
        if win is not None:
            win.close()
    return {"completed": completed, "paths": paths, "seed": seed}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="情绪诱发范式")
    parser.add_argument("--validate", action="store_true", help="只校验素材，不启动 PsychoPy")
    parser.add_argument("--self-test", action="store_true", help="校验并测试六套抽样逻辑")
    parser.add_argument("--no-gui", action="store_true", help="跳过启动参数对话框")
    parser.add_argument("--participant", default="test", help="被试编号")
    parser.add_argument("--session", default="1", help="场次")
    parser.add_argument("--integrated", action="store_true",
                        help="一次连续运行六个范式区块，自动规划约60分钟")
    parser.add_argument("--paradigm", choices=list("123456"), default="1")
    parser.add_argument(
        "--marker",
        choices=["log", "lsl", "parallel", "serial", "parallel+lsl", "serial+lsl"],
        default="log",
    )
    parser.add_argument("--endpoint", default="",
                        help="串口COM3/auto，或并口地址0x0378")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--windowed", action="store_true")
    camera_defaults = load_config().get("camera", {})
    parser.add_argument("--camera", action="store_true", help="录制被试面部视频")
    parser.add_argument("--camera-index", type=int,
                        default=int(camera_defaults.get("index", 0)))
    parser.add_argument("--camera-width", type=int,
                        default=int(camera_defaults.get("width", 1280)))
    parser.add_argument("--camera-height", type=int,
                        default=int(camera_defaults.get("height", 720)))
    parser.add_argument("--camera-fps", type=float,
                        default=float(camera_defaults.get("fps", 60)))
    parser.add_argument("--test", action="store_true", help="减少素材数量并缩短固定时长（工程测试）")
    parser.add_argument("--demo", action="store_true",
                        help="流程演示版：使用正式素材配额，但缩短并可跳过素材")
    parser.add_argument("--smoke-test", action="store_true",
                        help="自动开始并评分5；仅用于无人值守验收")
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    config = load_config()
    if options.validate or options.self_test:
        errors = validate_assets(config, print_report=True)
        if not errors and options.self_test:
            root = resolve_asset_root(config)
            for pid, pconfig in config["paradigms"].items():
                stimuli = discover_stimuli(root, pid, pconfig)
                selected = sample_category_targets_prefer_unused(
                    stimuli, pconfig["category_trial_counts"], random.Random(20260803))
                assert len(selected) == len({item["path"] for item in selected})
                actual = {category: sum(item["category"] == category for item in selected)
                          for category in ("positive", "neutral", "negative")}
                assert actual == pconfig["category_trial_counts"]
                print("  范式{}抽样测试通过：{} 个且无重复".format(pid, len(selected)))
            integrated = build_integrated_plan(
                config, "__INTEGRATED_SELFTEST__", "1", 20260803, False)
            active_paradigms = [str(pid) for pid in
                                config["integrated_session"]["paradigms"]]
            assert len(integrated["blocks"]) == len(active_paradigms)
            assert [block["paradigm"] for block in integrated["blocks"]] == active_paradigms
            all_paths = []
            for block in integrated["blocks"]:
                category_counts = {category: sum(item["category"] == category
                                                 for item in block["stimuli"])
                                   for category in ("positive", "neutral", "negative")}
                assert category_counts == block["config"]["category_trial_counts"]
                all_paths.extend(item["relative"] for item in block["stimuli"])
            assert len(all_paths) == len(set(all_paths))
            assert abs(integrated["estimated_s"] - integrated["target_s"]) <= 300
            demo = compact_demo_plan(config, build_integrated_plan(
                config, "__DEMO_SELFTEST__", "1", 20260803, False))
            assert len(demo["blocks"]) == len(active_paradigms)
            for block in demo["blocks"]:
                block_counts = {category: sum(item["category"] == category
                                              for item in block["stimuli"])
                                for category in ("positive", "neutral", "negative")}
                assert block_counts == block["config"]["category_trial_counts"]
            marker_values = list(config["marker_codes"].values())
            assert len(marker_values) == len(set(marker_values))
            print("  整合session测试通过：{}区块、类别配额正确、无重复、预计{:.2f}分钟".format(
                len(active_paradigms), integrated["estimated_s"] / 60.0))
            print("  流程演示测试通过：素材配额与正式版一致")
        return 1 if errors else 0
    try:
        result = run_experiment(config, options)
        if result is not None:
            status = "完成" if result["completed"] else "已中止"
            print("实验{}；随机种子：{}".format(status, result["seed"]))
            for key, path in result["paths"].items():
                print("{}: {}".format(key, path))
            if options.smoke_test and (options.integrated or options.paradigm in ("1", "4")):
                # 自动短测故意在视频中途结束；强制退出可避免 ffpyplayer
                # 等待长视频解码线程，同时所有 CSV/marker 已在上面关闭并刷新。
                sys.stdout.flush()
                sys.stderr.flush()
                import os
                os._exit(0)
        return 0
    except Exception as exc:
        print("错误：{}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
