#!/usr/bin/env python3
"""
疑词截帧验证辅助 —— 为 find_suspect_terms() 报出的疑词批量准备验证帧。

设计依据（2026-09-29 实测教训）：

1. **字符位置换算时间轴不准**。实测同一条视频，按 pos/len*dur 估算抽 3 帧，
   有 2 帧落空（🎼 音乐段与停顿让语速不均匀，误差可达 ±7s）。
   → 改为在估算点**密集采样**（默认 ±12s，步长 1s），宁可多抽也不漏。

2. **VLM 会编造**。实测对 215s 帧问"画面里有什么"，VLM 返回了一整篇
   《教父》电影分析 —— 画面里根本没有。必须用**严格 OCR 提示**
   （"只抄录字幕，不要描述画面"）才稳定。

3. **屏幕录制类视频，UI 文字是最强证据**。实测 Codex 教程横屏 1280x720，
   界面上的 NotebookLM / 创建项目 直接定案，胜过任何上下文推断。

用法：
    # 从 transcript_raw.md 读文本，自己传疑词
    python3 verify_terms.py --video V.mp4 --text transcript_raw.md \
        --terms "申屠电网,n twogaM,重庆HD" --out ./frames

    # 直接给时间点（已知位置时）
    python3 verify_terms.py --video V.mp4 --times 62,130,430 --out ./frames

输出：
    frames/frame_<ts>.jpg        全帧（读 UI 用）
    frames/frame_<ts>_sub.jpg    底部字幕条（读字幕用）
    frames/manifest.json           疑词 → 帧列表映射
"""

import argparse
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path

from review import REVIEW_FILE, file_sha256, json_object, transcript_basis

# 字幕条在画面底部的比例（竖屏 0.16-0.2，横屏黑条更矮）
SUB_RATIO = 0.22
# 字幕带额外向上延伸的安全边：避开底部常驻目录条 / 水印 / 进度条。
# 必须按画面高度取比例，不能用固定像素 —— 固定 150px 在 240px 高的视频上
# 占 62% 画高，字幕带退化成整帧（实测裁出 720x455），证据就不可读了。
SUB_SAFE_RATIO = 0.08


def probe_duration(video: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def probe_size(video: Path) -> tuple[int, int]:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True)
    w, h = r.stdout.strip().split(",")[:2]
    return int(w), int(h)


def estimate_times(terms: list[str], text: str, dur: float,
                   window: int, step: int) -> dict[str, list[float]]:
    """按字符位置估算时间点，并在估算点周围密集采样。"""
    total = max(len(text), 1)
    out = {}
    for term in terms:
        term = term.strip()
        if not term:
            continue
        pos = text.find(term)
        if pos < 0:
            # 找不到就均匀铺开全片，交给人眼判断
            out[term] = [round(dur * i / (len(terms) + 1), 1)
                         for i in range(1, len(terms) + 1)]
            continue
        out[term] = estimate_window(pos / total * dur, dur, window, step)
    return out


def estimate_window(center: float, duration: float, window: int,
                    step: int) -> list[float]:
    lo = max(0, center - window)
    hi = min(duration, center + window)
    return [round(lo + i * step, 1) for i in range(math.floor((hi - lo) / step) + 1)]


def segment_times(term: str, segments: list[dict], duration: float,
                  step: int) -> list[float]:
    """Use the ASR intervals that contain the original wording."""
    times = set()
    for segment in segments:
        if term not in (segment.get("text") or ""):
            continue
        start, end = segment.get("start"), segment.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration + 1):
            continue
        # Include the borders: captions sometimes appear before or after
        # the corresponding sound. Whole-second points can be shared by terms.
        stop = min(math.ceil(end) + 1, math.ceil(duration) - 1)
        times.update(range(max(0, math.floor(start) - 1), stop + 1, step))
    return sorted(times)


def time_label(ts: float) -> str:
    return f"{ts:.3f}".rstrip("0").rstrip(".")


def grab(video: Path, ts: float, out_dir: Path, slug: str,
         width: int, height: int) -> list[Path]:
    """抽全帧 + 底部字幕条。

    out_dir 不存在时自建：被当作库直接调用时（如单测）通常不会预先
    mkdir，缺这行会让 ffmpeg 因打不开输出文件而抛 CalledProcessError。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    label = time_label(ts)
    full = out_dir / f"{slug}_{label}.jpg"
    sub = out_dir / f"{slug}_{label}_sub.jpg"
    # 字幕带：底部 SUB_RATIO 高度 + SUB_SAFE_RATIO 安全边。
    # 安全边的作用是避开底部常驻目录条 —— 窄裁剪会只框到目录条，
    # 每个时间点读数都一样（假证据）。
    # 横向**不裁**：中文烧录字幕水平居中，裁左边会把字幕右半截切掉
    # （实测 1280 宽视频上「就是把资料一股脑丢给AI」被裁成「就是把资料一股月」）。
    # 整幅缩到 720 宽即可，竖屏 1080 宽也只缩不裁，不会丢字。
    ch = int(height * SUB_RATIO)
    safe = int(height * SUB_SAFE_RATIO)
    band = ch + safe
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", str(ts), "-i", str(video),
         "-filter_complex",
         f"[0:v:0]split[full][bottom];"
         f"[bottom]crop={width}:{band}:0:{height - band},scale=720:-1[sub]",
         "-map", "[full]", "-frames:v", "1", "-q:v", "2", str(full),
         "-map", "[sub]", "-frames:v", "1", "-q:v", "2", str(sub)],
        capture_output=True, check=True)
    return [full, sub]


def slugify(term: str) -> str:
    s = re.sub(r"[^\w一-鿿]+", "_", term).strip("_")
    return s or "term"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True, type=Path)
    ap.add_argument("--text", type=Path, help="transcript_raw.md")
    ap.add_argument("--segments", type=Path, help="transcript_segments.json，已有 ASR 时间戳时优先定位")
    ap.add_argument("--terms", help="逗号分隔的疑词")
    ap.add_argument("--occurrences", help="逗号分隔的疑词出现 ID，例如 25:28,71:74")
    ap.add_argument("--times", help="逗号分隔的秒数，跳过估算")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--window", type=int, default=12, help="估算点 ±N 秒")
    ap.add_argument("--step", type=int, default=1, help="采样步长秒")
    a = ap.parse_args()
    if a.step <= 0 or a.window < 0:
        ap.error("--step 必须大于 0，--window 必须非负")
    if sum(bool(mode) for mode in (a.times, a.terms, a.occurrences)) != 1:
        ap.error("请选一种定位方式：--times、--terms 或 --occurrences")

    if not a.video.exists():
        print(f"❌ 视频不存在: {a.video}", file=sys.stderr)
        return 1
    a.out.mkdir(parents=True, exist_ok=True)

    dur = probe_duration(a.video)
    w, h = probe_size(a.video)
    print(f"🎞  {a.video.name}  {dur:.0f}s  {w}x{h}")

    plan: dict[str, list[float]]
    methods = {}
    provenance = {}
    records = {}
    if a.occurrences:
        if not a.text:
            ap.error("--occurrences 需要 --text transcript_raw.md")
        try:
            review = json_object(a.text.with_name(REVIEW_FILE))
            provenance = {key: review[key] for key in ("raw_sha256", "media_sha256")}
            if (file_sha256(a.text) != provenance["raw_sha256"]
                    or file_sha256(a.video) != provenance["media_sha256"]):
                raise ValueError("原稿或媒体与核验记录不一致")
            known = {entry["id"]: entry for entry in review["occurrences"]}
            records = {key: known[key] for key in
                       (item.strip() for item in a.occurrences.split(","))}
            text = transcript_basis(a.text)
        except (OSError, ValueError, KeyError) as exc:
            ap.error(f"无法定位疑词出现：{exc}")
        plan = {}
        for key, occurrence in records.items():
            interval = occurrence["interval_seconds"]
            if interval:
                start, end = interval
                segment = {"start": start, "end": end, "text": occurrence["term"]}
                anchored = segment_times(occurrence["term"], [segment], dur, a.step)
            else:
                anchored = []
            plan[key] = anchored or estimate_window(
                occurrence["start"] / max(len(text), 1) * dur, dur, a.window, a.step)
            methods[key] = "asr_segments" if anchored else "text_estimate"
    elif a.times:
        ts_list = [float(x) for x in a.times.split(",") if x.strip()]
        if not ts_list or any(not math.isfinite(t) or not 0 <= t < dur for t in ts_list):
            ap.error("--times 必须是视频时长范围内的秒数")
        plan = {f"t{time_label(t)}": [t] for t in ts_list}
    else:
        if not (a.text and a.terms):
            print("❌ 需要 --text+--terms 或 --times", file=sys.stderr)
            return 1
        text = a.text.read_text(encoding="utf-8")
        terms = [t.strip() for t in a.terms.split(",") if t.strip()]
        segments_path = a.segments or a.text.with_name("transcript_segments.json")
        segments = (json.loads(segments_path.read_text(encoding="utf-8")).get("segments", [])
                    if segments_path.exists() else [])
        if a.segments and not a.segments.exists():
            ap.error(f"时间戳文件不存在：{a.segments}")
        plan = estimate_times(terms, text, dur, a.window, a.step)
        for term in terms:
            anchored = segment_times(term, segments, dur, a.step)
            if anchored:
                plan[term] = anchored
                methods[term] = "asr_segments"
            else:
                methods[term] = "text_estimate"

    manifest = {"video": str(a.video.resolve()), "duration": dur,
                "size": [w, h], **provenance}
    entries = {}
    manifest["occurrences" if a.occurrences else "terms"] = entries
    started = time.perf_counter()
    frames_by_time = {}
    hashes_by_time = {}
    for term, ts_list in plan.items():
        files = []
        actual_times = []
        captures = []
        for ts in sorted({round(t, 3) for t in ts_list if 0 <= t < dur}):
            try:
                if ts not in frames_by_time:
                    frames_by_time[ts] = grab(a.video, ts, a.out, "frame", w, h)
                got = frames_by_time[ts]
                if all(path.is_file() for path in got):
                    frame_paths = [str(path.resolve()) for path in got]
                    files.extend(frame_paths)
                    actual_times.append(ts)
                    if a.occurrences:
                        if ts not in hashes_by_time:
                            hashes_by_time[ts] = [file_sha256(path) for path in got]
                        captures.append({"seconds": ts, "frames": frame_paths,
                                         "sha256": hashes_by_time[ts]})
            except subprocess.CalledProcessError:
                print(f"⚠️  {ts:g}s 抽帧失败", file=sys.stderr)
                continue
        entries[term] = {
            "frames": files,
            "hints": ts_list,
            "captured_times": actual_times,
            "method": methods.get(term, "explicit"),
        }
        if a.occurrences:
            entries[term].update(term=records[term]["term"], captures=captures)
        print(f"  {term:<18} {len(actual_times)} 个时间点")

    manifest["extract_seconds"] = round(time.perf_counter() - started, 3)
    mf = a.out / "manifest.json"
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    captured = {ts for entry in entries.values() for ts in entry["captured_times"]}
    print(f"\n共取得 {len(captured)} 个时间点，manifest: {mf}")
    print("⚠️  读帧时用严格 OCR 提示，别问『画面里有什么』：")
    print('    "只逐字抄录底部黑色字幕条的文字，不要描述画面"')
    return 0 if captured else 2


if __name__ == "__main__":
    sys.exit(main())
