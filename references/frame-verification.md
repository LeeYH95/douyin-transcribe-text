# 疑词截帧验证流程

> **读这里的条件**：逐字稿已生成，`find_suspect_terms()` 报了疑词，需要核实。
> 读帧本身怎么读（串行、严格 OCR 提示、整帧 vs 字幕条、本地 Vision OCR、帧拷进工作区）
> 见 `references/frame-reading.md` —— 那是所有读帧任务通用的规矩，读一次就够。

---

## 为什么这一步不能跳

上下文推断看起来都"合理"，但**无法自查**。2026-09-29 Codex 教程的实测：

| 转写原文 | 上下文推断 | 截帧实证 | 结论 |
|---|---|---|---|
| `n twogaM` | Notion | `NotebookLM (EXPERIMENTAL)` | **推断错** |
| `重庆HD` | Claude HD | `然后现在从ChatGPT和Codex的` | **推断错** |
| `申屠电网` | 无法判断 | 字幕`深度调研`+UI 面板标题 | 推断不出 |
| 深度洞裁 | 深度洞察 | `一个深度洞察的结果` | 对 |
| 外coing | vibecoding | `带你从0到1学习AI和Vibe Coding` | 对（大小写有差） |

**铁律：交付前必须跑截帧验证，输出「已核实 / 仍未核实」两张表。不允许只列待确认就交付。**

---

## 根因：滚动会让 Playwright 拦截到推荐视频的 CDN 流

`capture_stream_urls` 曾经有「向下滚动 3 次」的逻辑（用于触发更多内容加载）。实测 2026-10-01 发现：

> 抖音单视频页往下滚 → 页面**预载推荐视频流** → 滚动期间的 CDN 拦截请求属于推荐视频，**不是目标视频**

后果：下载到的媒体是推荐视频（比如天安门升旗），目标视频内容完全丢失。

**修复**：删除所有滚动逻辑，只触发目标视频播放。不滚动 = 不引入推荐视频 CDN 污染。

如果截帧验证时发现时间轴完全错位（抽的帧内容和疑词上下文不匹配），优先怀疑下载到的不是目标视频，而非时间轴估算误差。

---

## 两条核实硬规则

| 规则 | 说明 |
|------|------|
| **优先按 ASR 分段定位** | 检查该词出现的分段与前后边缘。有多个匹配段时保留各段。无分段时间戳或词未命中时，按字符位置估算，默认 **±12s、步长 1s** |
| **核对同一时点的口播与画面** | 回听确认说的是哪一个词。用字幕、UI 或幻灯片确认拼写。画面里的词与该句口播无法对应时保持未核实 |

「怎么读帧」的具体做法（禁问「画面里有什么」、严格 OCR 提示、串行、Vision OCR、路径白名单）
→ **`references/frame-reading.md`**

---

## 怎么跑

```bash
T="<douyin-transcribe-text-skill-dir>/scripts/verify_terms.py"
D="<媒体根目录>/抖音/<主题>_<作者>_<日期>"

python3 $T --video "$D/<video_id>.mp4" --text "$D/transcript_raw.md" \
  --occurrences "25:28,71:74" \
  --out "$D/frames"
```

> 目录名用 `<主题>_<作者>_<日期>`（见 README《媒体落盘位置》），视频文件名仍是 `<video_id>.mp4`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--window` | 12 | 估算点 ±N 秒 |
| `--step` | **1** | 采样步长秒；设为 2 会漏掉比 2 秒短的字幕（如片尾短句） |
| `--times` | — | 直接给秒数，跳过估算（已知位置时用） |
| `--segments` | 相邻 `transcript_segments.json` | 可显式指定转写分段时间戳 |

从 `transcript_review.json` 选出现 ID。同一个词的不同位置分别定位。落在同一个 ASR
分段内时，回听区分各次出现。

每个时间点一次生成 `frame_秒.jpg` 和 `frame_秒_sub.jpg`。多个疑词共享同一时间点的
全帧与字幕条。`manifest.json` 记录每处出现的帧、时点和 SHA-256。
`--terms` 与 `--times` 适合探索画面。用于交付依据时使用出现 ID 生成绑定清单。

---

## 交付格式

```
✅ 已核实（截帧）
| 转写原文 | 正确写法 | 证据 |
|---|---|---|
| n twogaM | NotebookLM | 259s 界面 NotebookLM (EXPERIMENTAL) |

⚠️ 仍未核实
| 词 | 状态 |
|---|---|
| 菲尔专属知识 school | 抽帧 3 处均未命中，保留原文 |
```

**没验完也要说**，不能默默交付。这和「待确认」列表不是一回事——待确认是我没查，仍未核实是我查了但没查到。

---

## 录屏/幻灯片类视频：没有烧录字幕 ≠ 无法核实

**无烧录字幕的视频不能直接跳过核实** —— 画面上的幻灯片/UI 文字就是证据。
实测 2026-10-30 的 RAG 录屏（1280x720，1429s）：无烧录字幕，但按幻灯片切换点
采样 OCR 后，能读出「切片→向量检索」「语义切分」等幻灯片文字，疑词校正就锚在
这些帧上。

### 步骤 1：找幻灯片切换点（帧差分）

`ffmpeg → pipe → 16x16 灰度 raw → 帧间像素差`。切换点的帧差远大于静止段，
用「中位帧差 × 2.5」作阈值（实测中位 0.40 → 阈值 1.00，1429s 视频切出 84 个切换点）：

```bash
ffmpeg -v error -i "$V" -vf "fps=1,scale=16:16,format=gray" -f rawvideo - | python3 -c "
import sys, statistics
d = sys.stdin.buffer.read(); n = 256
fr = [d[i:i+n] for i in range(0, len(d)-n+1, n)]
df = [sum(abs(x-y) for x, y in zip(fr[i-1], fr[i]))/n for i in range(1, len(fr))]
thr = max(statistics.median(df)*2.5, 3.0)
print('切换秒点:', [i+1 for i, v in enumerate(df) if v > thr])
"
```

### 步骤 2：串行 OCR 建文本索引

用本地 Vision OCR（`scripts/ocr_vision.swift`），一次传多张即可，它内部逐张处理
（并发会静默返回空结果，见 `references/frame-reading.md`）：

```bash
swiftc -O "<douyin-transcribe-text-skill-dir>/scripts/ocr_vision.swift" -o /tmp/ocr_vision
/tmp/ocr_vision <frame_*.jpg> > /tmp/slide_text.txt
```

拿到文本索引后，逐词在索引里比对 —— 命中即证据，等价于烧录字幕的核实。

---

## 何时可以跳过

只有一种情况：视频**既无烧录字幕、画面也无任何可读文字**（纯真人口播）。
此时抽帧无信息量，标注「本片无画面文字可核实」。

其余一律必跑 —— 包括「无烧录字幕」的录屏类视频（走上面的幻灯片 OCR 路线）。
