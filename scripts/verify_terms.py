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
    frames/<term>_<ts>.jpg        全帧（读 UI 用）
    frames/<term>_<ts>_sub.jpg     底部字幕条（读字幕用）
    frames/manifest.json           疑词 → 帧列表映射
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

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
        center = pos / total * dur
        lo = max(0, center - window)
        hi = min(dur, center + window)
        ts = []
        t = lo
        while t <= hi:
            ts.append(round(t, 1))
            t += step
        out[term] = ts
    return out


def grab(video: Path, ts: float, out_dir: Path, slug: str,
         width: int, height: int) -> list[Path]:
    """抽全帧 + 底部字幕条。

    out_dir 不存在时自建：被当作库直接调用时（如单测）通常不会预先
    mkdir，缺这行会让 ffmpeg 因打不开输出文件而抛 CalledProcessError。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    full = out_dir / f"{slug}_{int(ts)}.jpg"
    sub = out_dir / f"{slug}_{int(ts)}_sub.jpg"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(ts), "-i", str(video),
         "-frames:v", "1", "-q:v", "2", str(full), "-y"],
        capture_output=True, check=True)
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
        ["ffmpeg", "-v", "error", "-ss", str(ts), "-i", str(video),
         "-frames:v", "1",
         "-vf", f"crop={width}:{band}:0:{height - band},scale=720:-1",
         "-q:v", "2", str(sub), "-y"],
        capture_output=True, check=True)
    return [full, sub]


def slugify(term: str) -> str:
    s = re.sub(r"[^\w一-鿿]+", "_", term).strip("_")
    return s or "term"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True, type=Path)
    ap.add_argument("--text", type=Path, help="transcript_raw.md")
    ap.add_argument("--terms", help="逗号分隔的疑词")
    ap.add_argument("--times", help="逗号分隔的秒数，跳过估算")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--window", type=int, default=12, help="估算点 ±N 秒")
    ap.add_argument("--step", type=int, default=1, help="采样步长秒")
    a = ap.parse_args()

    if not a.video.exists():
        print(f"❌ 视频不存在: {a.video}", file=sys.stderr)
        return 1
    a.out.mkdir(parents=True, exist_ok=True)

    dur = probe_duration(a.video)
    w, h = probe_size(a.video)
    print(f"🎞  {a.video.name}  {dur:.0f}s  {w}x{h}")

    plan: dict[str, list[float]]
    if a.times:
        ts_list = [float(x) for x in a.times.split(",") if x.strip()]
        plan = {f"t{int(t)}": [t] for t in ts_list}
    else:
        if not (a.text and a.terms):
            print("❌ 需要 --text+--terms 或 --times", file=sys.stderr)
            return 1
        text = a.text.read_text(encoding="utf-8")
        terms = [t for t in a.terms.split(",") if t.strip()]
        plan = estimate_times(terms, text, dur, a.window, a.step)

    manifest = {"video": str(a.video), "duration": dur,
                "size": [w, h], "terms": {}}
    n = 0
    for term, ts_list in plan.items():
        slug = slugify(term)
        files = []
        for ts in ts_list:
            try:
                got = grab(a.video, ts, a.out, slug, w, h)
                files.extend(str(p) for p in got)
                n += 1
            except subprocess.CalledProcessError:
                continue
        manifest["terms"][term] = {
            "frames": files,
            "hints": sorted({int(t) for t in ts_list}),
        }
        print(f"  {term:<18} {len(files)//2} 帧  → {slug}_*.jpg")

    mf = a.out / "manifest.json"
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    print(f"\n✅ 共 {n} 个时间点，manifest: {mf}")
    print("⚠️  读帧时用严格 OCR 提示，别问『画面里有什么』：")
    print('    "只逐字抄录底部黑色字幕条的文字，不要描述画面"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
