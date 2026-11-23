---
name: douyin-transcribe-text
description: 抖音视频 → 口播逐字稿 → Obsidian 结构化笔记。当用户发来抖音链接或本地 MP4，需要口播逐字稿、专有名词校正、疑词核实、分段整理，或需要把视频文件交付飞书时使用。只做口播逐字稿；画面里的 AI 生图提示词走 douyin-prompt-extract。完整流程：Playwright 渲染 → 网络拦截捕获 CDN 直链 → 下载 → ffmpeg 合成 → 音频转写 → 专有名词校正（疑词截帧验证）→ 分段输出。输出需发用户确认后才写入 Vault。
metadata:
  requires:
    python_pkgs: ["playwright", "requests", "PyYAML"]
    system_bins: ["ffmpeg", "ffprobe"]
    optional_bins: ["~/.agents/venvs/whisper/bin/whisper"]
    key_file: "~/.agents/.siliconflow_key"
---

# douyin-transcribe-text

抖音视频 → 口播逐字稿 → 结构化 Markdown。`scripts/transcribe.py` 和同目录模块不依赖其他 Skill。

> 2026-09-28 由 `douyin-ingest` + `douyin-transcribe-text` 合并而来。旧目录已归档为 `douyin-ingest.merged-away`，可回滚。
>
> **本 skill 是兄弟 skill 的公共库**：`xhs-transcribe-text` 复用这里的 `PROPER_NOUN_MAP`
> （`scripts/transcribe.py`）、`references/proper-noun-map.md`、`references/output-format.md`；
> `douyin-prompt-extract` 复用这里的下载器（`scripts/transcribe.py --download-only`）、
> `scripts/ocr_vision.swift`、`references/frame-reading.md`。
> 它们按 `<douyin-transcribe-text-skill-dir>/...` 路径引用 —— **这些文件不要改名或搬家**。

## 快速开始

本 skill 的脚本在 `scripts/` 下。下文的 `<douyin-transcribe-text-skill-dir>` 指本 skill 目录。
**不要写死绝对路径** —— skill 会迁移/重装，写死的路径必然失效（实测 `~/.agents/skills/...` 已全部失效）。
需要真实路径时：

```bash
find ~/.openclaw/agents ~/.agents/skills ~/.openclaw/skills \
  -maxdepth 5 -type d -name douyin-transcribe-text 2>/dev/null | head -1
```

```bash
T="<douyin-transcribe-text-skill-dir>/scripts/transcribe.py"

python3 $T "<抖音链接>"                # 完整闭环
python3 $T --video /path/to/local.mp4  # 已有本地视频，跳过下载
python3 $T "<链接>" --check-link       # 只解析 video_id
python3 $T "<链接>" --headful          # 首次登录验证用（见「安全边界」）
python3 $T "<链接>" --download-only    # 只下载视频，不转写（只要画面的场景）
python3 $T --video in.mp4 --frames 68,231,253,285  # 按指定时间点抽帧（不校验文字）
```

疑词核验用 `scripts/verify_terms.py`（见「疑词截帧验证」），`--frames` 只是抽帧。

## 工作流

```
收到抖音链接（或本地 MP4）
  │
  ├─ 路径 A：链接
  │    resolve_video_id()      短链 302 跳转 → video_id
  │    capture_stream_urls()   Playwright 渲染 + 网络拦截 CDN 直链
  │    · 触发 play()           关键：抖音不自动播放，必须主动触发
  │    download()              CDN 直链下载（带 Referer，时效 10-30 分钟）
  │    mux()                   ffmpeg 合成 mp4
  │
  └─ 路径 B：本地 MP4
       _validate_mp4()         magic bytes 校验
                    │
                    ▼  （此后 A/B 合流）
  extract_audio()               ffmpeg 提取 16kHz mono WAV
  transcribe_result()           SenseVoice 或本地 Whisper，记录引擎与阶段耗时
  propose_corrections()         词表只提名疑词，原词保留
  find_suspect_terms()          补充启发式疑词
  verify_terms.py               按出现 ID 截帧，记录可核查的证据
  quality_gate.py               核对来源、原稿、证据与最终 Markdown
                    │
                    ▼
  发用户确认 ──「可以了」/「写入」──▶ 写入 04_inbox/openclaw-agent/
```

## 安全边界（准确表述，勿简化）

这一节措辞很讲究，**不要图省事改成「不读 Cookie」** —— 那个说法会误导下一个人。

| 说法 | 是否属实 |
|------|---------|
| 不接触用户日常浏览器的任何数据 | ✅ 属实 |
| 完全不使用任何 cookie | ❌ **不实**。每次匿名访问抖音都会发一套自己的会话 cookie，脚本下次跑会复用 |
| 靠 cookie 才下得下来 | ❌ **不实**，见下 |

**为什么必须用浏览器：** 抖音媒体接口需要 `a_bogus` / `msToken` 等**动态签名**，由页面 JS 实时计算。纯 HTTP 下载器（yt-dlp 等）没有 JS 引擎，算不出来，因此匿名下载必然 403（实测：`HTTP Error 403: Forbidden` + `Fresh cookies are needed`）。本脚本开真实 Chromium，让抖音自己的 JS 算出签名，媒体流请求才发得出来。

**所以准确的说法是：**

> 使用**独立的匿名浏览器 profile**（`~/.cache/openclaw/chrome_profiles/douyin`），不接触用户日常浏览器的任何数据。抖音风控靠的是动态签名而非登录态，因此浏览器渲染是必要条件，这不等同于使用登录凭据。

**关于 `--headful`：** 抖音真要登录验证时，该参数打开**可见窗口，由用户本人手动登录**一次，会话存进同一个独立 profile 复用。属于本人可见操作，不是脚本偷取凭据。**必须在使用前向用户说明**，不得静默触发。

**其他铁律：**
- 不绕过任何访问控制；受限即停
- 登录验证只能由用户本人在可见窗口完成
- 失败时如实报错，不编造来源、不伪造转写结果
- 没有原始转发文本时注明「原始转发文本未提供」，**不杜撰链接**

## 依赖

| 组件 | 安装 |
|------|------|
| playwright | `python3 -m pip install playwright --break-system-packages` |
| chromium | `python3 -m playwright install chromium` |
| ffmpeg / ffprobe | `brew install ffmpeg` |
| PyYAML | `python3 -m pip install PyYAML`（交付检查） |
| 硅基流动 key | `~/.agents/.siliconflow_key`（chmod 600）；也可用 `API_KEY` / `SILICONFLOW_API_KEY` 环境变量 |
| 本地 Whisper（**backup**） | `~/.agents/venvs/whisper`（已装好，torch 2.14.0） |

脚本内**不硬编码任何 key**。key 读取优先级、引擎选择与重建方式 → `references/transcription-engine.md`。

## 转写引擎：默认 SenseVoice，失败降级本地 Whisper

`transcribe()` 自己会选引擎，跑流程不用管。要点只有两条：

- `--engine auto` 优先 SenseVoice，缺凭证、超限或云端出错时尝试本地 Whisper。需要限定引擎时使用 `--engine whisper` 或 `--engine sensevoice`。
- 遇到任务慢，先看输出中的 `timings_seconds` 或 stderr 的 `[耗时]`，定位是浏览器取源、下载、云端等待还是本地识别。

引擎基准、API key 优先级、Whisper venv 重建（含「Homebrew 装的 openai-whisper 不能用」）→ `references/transcription-engine.md`。

## 专有名词校正

`PROPER_NOUN_MAP` 按长度降序匹配，命中项输出到 `correction_candidates`。
`transcript.md` 保留原词。结合原音及同一时点的字幕、UI 或幻灯片核实后再改稿，并记录原词、改词、时点和证据。

**加词条、查已验证条目、看子串切分陷阱 → 读 `references/proper-noun-map.md`。**
（那是词表维护的唯一权威说明，含恒等护栏机制、加词硬规矩、已验证条目表。）

一条规则记住就够：**新增词条必须走截帧验证，不能凭上下文猜。**

## 疑词截帧验证（必跑，不可跳过）

从 `transcript_review.json` 读取疑词的出现 ID。逐处回听或对照帧，记录在
`review_decisions.json`。输出「已核实 / 仍未核实」两张表。待核验的草稿标明状态。

| 文件 | 什么时候读 |
|------|-----------|
| `references/frame-verification.md` | 疑词核验**流程**（怎么跑、怎么定案、录屏类怎么读幻灯片） |
| `references/frame-reading.md` | **读帧通用规矩**（串行、严格 OCR 提示、整帧 vs 字幕条、Vision OCR、帧拷进工作区）—— 所有读帧任务通用，读一次就够 |

- 优先按相邻的 `transcript_segments.json` 定位。云端 TXT、旧稿或疑词未命中分段时，仍用估算窗密集采样。
- 回听确定说了什么。字幕、UI 或幻灯片确认拼写，证据须与该次口播对应。无法确认时保留原词。
两条的完整说明与实测依据 → 见 `references/frame-verification.md`。

```bash
python3 "<douyin-transcribe-text-skill-dir>/scripts/verify_terms.py" \
  --video <mp4> --text <transcript_raw.md> --occurrences "25:28,71:74" --out <frames_dir>
```

**为什么不能只靠上下文推断：** 实测上下文推断错误率 **29%**（逐条证据见
`references/frame-verification.md`）。这些词看起来全都"能从上下文推出来"，
**自查发现不了**，只能靠画面定案。

## 踩坑记录

1. **抖音不自动播放** —— 必须 `play()` / 空格键触发，否则拦截到 0 条流
2. **音/视频/合流三条流，content-type 全是 `video/mp4`** —— 抖音把音频也装在 MP4 容器里，**靠 content-type 区分音视频无效**（实测 2026-09-29 三条流 ct 完全相同）。必须下载后用 ffprobe 判定真实流构成
3. **content-length 不可信** —— 脚本按 header 体积降序尝试，并打印偏差警告。单次 ffprobe 取得音视频轨与时长，已排除的候选不会重试。
4. **CDN 签名有 10-30 分钟时效** —— 捕获后立即下载
5. **yt-dlp 路径已死** —— 无 JS 引擎算签名，必然 403。代码中已全部移除，仅在文档注释里保留原因说明
6. **CDN 返回音频替身** —— Douyin CDN 有时在 `/video/` 路径下返回纯音频流
（Content-Type 报 video，但实际内容是 audio/aac）。
   现状：脚本下载后用 ffprobe 判定每条流的真实构成（`_probe_streams`），
   纯视频流 + 纯音频流会**合并**（`mux`）；若只剩音频流，产出的是无视频轨的 mp4，
   转写照常但**截帧验证不可用**。
   表现：`⚠️  本次未取到视频轨（抖音未下发），截帧验证不可用`
7. **页面会预载并自动播放别的视频** —— 抖音单视频页里，推荐视频同样是「音视频合一」且体积最大，
   按体积排序会**优先选中错的**（实测 2026-10-27：目标 114.218s，却下到另一条 107.8s 的配音视频）。
   现状：脚本从页面 DOM 的 `<video>.duration` 取目标时长，**逐条候选流校验时长**（容差 3s）；
   全部不符就**直接报错拒交**，不返回任何视频

## 目录命名

目录名用 `<主题>_<作者>_<日期>`，不用 video_id —— 让下载的对应文件夹一眼看出内容主题。
video_id 记在目录内 `meta.json`，不丢可追溯性。

**共享约定（媒体落盘位置、目录命名规则、批量重命名命令、exFAT 删文件坑）见
README《媒体落盘位置》一节** —— 那是唯一权威处，抖音和小红书两边共用。
本 skill 特有的只有元数据来源与页面身份校验：

- 抖音页面 `description` 的格式是：
  ```
  <标题> - <作者>于20261025发布在抖音，已经收获了4008个喜欢，来抖音，记录美好生活！
  ```
  `og:title` 常为空（实测 15/15 全空），**必须从 description 解析**：
  主题 / 作者 / 日期的解析规则实现在 `scripts/naming.py`
  （`derive_title` / `extract_author` / `extract_date`）——
  规则要改就改那里，**不要在别处再抄一份**（两处实现必然漂移）。

  可覆盖：`--title` / `--author` / `--date`。Agent 读完稿子知道更准的主题时，显式传参覆盖。

### ⚠️ 采信前必须校验页面身份

抖音页面在并发 / 风控下可能渲染出**别的视频**（实测 2026-10-28：两个进程共用同一 profile 时，页面被渲染成另一条视频，导致目录名与下载内容完全对不上）。

**判据**：`canonical` / `location` 必须仍指向目标 video_id（`og:url` 实测为空，不可用）。校验不过就**不采信任何 meta**，目录退回 video_id 并告警 —— 宁可名字难看，也不能名不副实。

**页面标题同样不能采信**：身份校验失败时 `page_title` 属于被渲染出来的**另一个**视频。实测 2026-10-30：校验失败后 `meta.json` 写进了「天地之大 困住你的是什么」，而实际下载的是「给自己的网站下了一场大雨」。标题对不上比标题难看更糟 —— 它会让按标题检索的人找错文件。

## 输出格式

**交付形态（frontmatter 字段表、分段规范、飞书发送方式、自检清单、疑词处理决策表、
写入 Vault 前的硬闸）全在 `references/output-format.md`** —— 那里是唯一权威处。

### 交付前跑质量闸（确定性规则，不靠模型自评）

```bash
python3 "<douyin-transcribe-text-skill-dir>/scripts/quality_gate.py" <transcript.md> --type speech --json
```

**类型必须选对**：口播稿用 `--type speech`；**画面实录/整理稿用 `--type note`** ——
表格与加粗本身就是内容结构，按口播稿判会误报「markdown 残留」。
`ok: true` 仅表示处理完成。先用质量闸检查，并且只有 `status=ready` 才交付终稿。
页面与媒体无法直接关联时先核实来源。Vault 写入仍需用户明确确认。

**幻觉重复检测 + 质量闸的完整用法、标注格式、以及「标注不进 Vault」的边界
→ 读 `references/delivery-gate.md`**（交付前检查阶段的唯一权威处）。

### 要发视频文件（.mp4）时 → 读 `references/feishu-file-delivery.md`

`MEDIA:` 指令发 .mp4 会被飞书拒收（实测落成 `msg_type=nonsupport`，或直接静默丢弃）。
可用路线是 `im/v1/files` 上传 + `msg_type=file` 发送，脚本 `scripts/feishu_send_file.py` 已封装。

## 铁律

1. **原文一字不改** —— 只做结构化分段，不增删改任何内容
2. **未经确认不写 Vault** —— 「可以了」/「写入」才操作；数字、表情、简短肯定**不算确认**
3. **视频保留本地** —— 落盘位置与目录命名见「目录命名」一节
4. **存疑必标注** ⚠️ —— 无法核实的词保留原文并注明。标注只进交付对话，
   **不进 Vault 正文**（硬闸见 `references/output-format.md`《写入 Vault 前的硬闸》）
5. **来源不编造** —— 没有原始转发文本就注明未提供
6. **疑词必核** —— 交付前跑 `verify_terms.py`，输出「已核实 / 仍未核实」两张表。只列待确认就交付 = 未完成
7. **目录名必须名副其实** —— 目录名用 `<主题>_<作者>_<日期>`；页面身份校验不过就不采信 meta，目录退回 video_id
8. **无口播不硬凑** —— 结果 `no_speech: true` 时不要把幻觉碎片当逐字稿交付，只交视频并说明

## 测试

```bash
python3 -m pytest "<douyin-transcribe-text-skill-dir>/tests/" -v
```

覆盖：专有名词校正、疑词检测、错误分类、mp4 校验、密钥解析、结构化输出、
幻觉重复检测、流构成探测、字幕条几何、语速与无口播判定、标题解析。

## 已知局限

- 16 kHz 单声道 PCM 音频约 26 分钟就可能到达 50 MB 上传上限。`auto` 超限时尝试本地 Whisper。
- **ASR 识别上限**：专名和生僻字需要复核。云端只有全文时，疑词时间点仍靠估算。
- **content-length 排序不可靠**：按 header 体积排序可能多下载几条流。抓取和下载耗时以实际日志为准。
