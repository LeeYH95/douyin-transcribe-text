---
name: douyin-transcribe-text
description: 抖音视频下载 + 语音转写 + 专有名词校正 + 结构化输出。当用户发来抖音链接，需要提取口播逐字稿并整理成 Obsidian 格式时使用。完整流程：Playwright 浏览器渲染 → 网络拦截捕获 CDN 直链 → 下载 → ffmpeg 合成 → 音频转写 → 专有名词校正（疑词截帧验证）→ 分段输出。输出需发用户确认后才写入 Vault。
metadata:
  requires:
    python_pkgs: ["playwright", "requests"]
    system_bins: ["ffmpeg", "ffprobe"]
    optional_bins: ["~/.agents/venvs/whisper/bin/whisper"]
    key_file: "~/.agents/.siliconflow_key"
---

# douyin-transcribe-text

抖音视频 → 口播逐字稿 → 结构化 Markdown。**单文件自包含**，`scripts/transcribe.py` 不依赖任何其他 Skill。

> 2026-09-28 由 `douyin-ingest` + `douyin-transcribe-text` 合并而来。旧目录已归档为 `douyin-ingest.merged-away`，可回滚。

## 快速开始

```bash
T=~/.agents/skills/douyin-transcribe-text/scripts/transcribe.py

python3 $T "<抖音链接>"                    # 完整闭环
python3 $T --video /path/to/local.mp4      # 已有本地视频，跳过下载
python3 $T "<链接>" --check-link           # 只解析 video_id
python3 $T "<链接>" --headful              # 首次登录验证用（见「安全边界」）
python3 $T --video in.mp4 --frames 68,231,253,285   # 疑词截帧验证
```

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
  transcribe()                  硅基流动 SenseVoice（失败降级本地 Whisper）
  correct_proper_nouns()        已知专有名词批量替换（纯本地）
  find_suspect_terms()          标记仍存疑的词
  format_output()               YAML frontmatter + 一级标题分段
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

> 使用**独立的匿名浏览器 profile**（`~/Downloads/douyin_videos/.chrome_profile`），不接触用户日常浏览器的任何数据。抖音风控靠的是动态签名而非登录态，因此浏览器渲染是必要条件，这不等同于使用登录凭据。

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
| 硅基流动 key | `~/.agents/.siliconflow_key`（chmod 600）；也可用 `API_KEY` / `SILICONFLOW_API_KEY` 环境变量 |
| 本地 Whisper（**backup**） | `~/.agents/venvs/whisper`（已装好，torch 2.14.0） |

Key 读取优先级：环境变量 → `~/.agents/.siliconflow_key`。脚本内**不硬编码任何 key**。

## 转写引擎：快的默认，慢的 backup

M4 实测（同一段 60s 音频）：

| 引擎 | 耗时 | 角色 |
|------|------|------|
| SiliconFlow SenseVoiceSmall | **30.6s** | **默认** |
| 本地 Whisper large-v3-turbo | 37.9s | backup（API 不可用时自动接管） |

**默认走 API 的原因：**
- `FunAudioLLM/SenseVoiceSmall` 在硅基流动价格页标注**免费**（2026-08、2026-09 两次核实），不存在 API 成本
- `PROPER_NOUN_MAP` 的 54 条规则是按 **SenseVoice 的错误指纹**逐条攒出来的，换引擎等于整套校正层作废、需重标定
- 本地并不更快（37.9s vs 30.6s）

**注意：Homebrew 装的 openai-whisper 不能用。** 它的 libtorch 硬链到特定版本 abseil，Homebrew 一升级就 `dlopen` 失败。脚本因此**优先用自建 venv**（`~/.agents/venvs/whisper`，PyPI 的 torch wheel，不受此影响）。

重装 venv：
```bash
python3 -m venv ~/.agents/venvs/whisper
~/.agents/venvs/whisper/bin/pip install openai-whisper
```

可覆盖：`DOUYIN_WHISPER_MODEL`（默认 `large-v3-turbo`）、`DOUYIN_WHISPER_LANGUAGE`（默认 `Chinese`）。

## 专有名词校正

`PROPER_NOUN_MAP` 按长度降序替换，纯本地无需网络。已验证条目（含依据）：

| 听错词 | 正确写法 | 依据 |
|--------|---------|------|
| codeex | Codex | 上下文 |
| GBT5.6 / GBT6 | GPT-5.6 / GPT-6 | 上下文 |
| GPT6色 / GPT5.6艘 | gpt-6-sol | 上下文 |
| GPT6ustra / GPT6Atra | gpt-6-astra | 上下文 |
| GBT6luar / GPT6lu娜 | gpt-6-luna | 上下文 |
| 子H / AIH / 主A卷 | 子 agent / agent / 主 agent | 上下文 |
| scul / scu / ski | skill | 上下文 |
| expllo / exQ / expl | Explore | 上下文 |
| simplified | Simplify | 上下文 |
| cloud / snet / oppos | Claude / Sonnet / Opus | 上下文 |
| **experad** | **Expert Advisor** | ✅ 231s 烧录字幕角色列表 |
| **劣制角色** | **内置角色** | ✅ 253s 烧录字幕 |
| **outtra 模式** | **Ultra 模式** | ✅ 68s 烧录字幕 |
| **febo** | **Fable** | ✅ 285s 烧录字幕 |

**新增词条必须走截帧验证，不能凭上下文猜。** 校正优先看**视频烧录字幕**（比用量表帧更直接），方法见 `--frames`。

## 踩坑记录

1. **抖音不自动播放** —— 必须 `play()` / 空格键触发，否则拦截到 0 条流
2. **音/视频/合流三条流，content-type 全是 `video/mp4`** —— 抖音把音频也装在 MP4 容器里，**靠 content-type 区分音视频无效**（实测 2026-09-29 三条流 ct 完全相同）。必须下载后用 ffprobe 判定真实流构成
3. **content-length 不可信** —— 抖音对部分流返回 0 或错误长度（实测声称 0.6MB 实收 7.6MB，偏差 12×）。脚本按 header 体积降序尝试，并**打印偏差警告**；首轮无合流时按实收体积重试一轮
4. **CDN 签名有 10-30 分钟时效** —— 捕获后立即下载
5. **yt-dlp 路径已死** —— 无 JS 引擎算签名，必然 403。代码中已全部移除，仅在文档注释里保留原因说明
5. **CDN 返回音频替身** —— Douyin CDN 有时在 `/video/` 路径下返回纯音频流
（Content-Type 报 video，但实际内容是 audio/aac），导致产出的 mp4 无视频轨、截帧验证全废。
修复：在 download 后用 ffprobe probe 视频流，为空时自动 fallback 到 audio 直链下载。
表现：`⚠️  CDN 直链只含音频流（无视频轨），尝试 audio 直链…`

## 输出格式

见 `references/output-format.md`（frontmatter 字段表、分段规范、自检清单）。

### 飞书输出必须用 ```markdown 代码块（2026-09-29 用户明确要求）

发给用户时，**整份文档包在 ```` ```markdown ```` 围栏里**，不要直接甩裸 markdown。

原因：飞书对裸 markdown 的渲染会挤压长段落，密排成一坨，很难读（用户原话「一股脑把所有信息对在卡片消息上，很难看」）；代码块保证层级和断行原样显示。

配套要求：

| 做 | 不做 |
|----|------|
| 只发文档本身 | ❌ 在正文前后堆解释、对比表格、过程复盘 |
| 用代码块包裹 | ❌ 裸发 markdown 让飞书渲染 |
| 待确认词用文末 `⚠️` 一行 | ❌ 散落在正文里打断阅读 |
| — | ❌ 用 `MEDIA:` 发 .md 文件（飞书会变成下载附件，用户看不到内容） |

**注意围栏嵌套：** 文档内含 `---` 分隔线时，外层用**四个反引号** ```` ```` ```` 包裹，避免内部围栏提前闭合。

已验证的反面案例（2026-09-29 踩过）：
- 裸发 markdown → 用户嫌乱
- 包三反引号但正文含 ``` → 提前闭合、渲染错乱
- `MEDIA:` 发 .md → 变成附件下载，不是预览

骨架：

```markdown
---
title: 文档标题
description: 一句话描述（25字内）
source: 原始来源链接
tags: [标签1, 标签2]
created: YYYY-MM-DD
type: transcript
confidence: medium
speaker: 演讲者
---

# 标题

# 一、小标题

正文...

---

原始转发文本：...
```

`confidence` 未经逐段核对**不要填 high**。

## 铁律

1. **原文一字不改** —— 只做结构化分段，不增删改任何内容
2. **未经确认不写 Vault** —— 「可以了」/「写入」才操作；数字、表情、简短肯定**不算确认**
3. **视频保留本地** —— `~/Downloads/douyin_videos/<video_id>/`
4. **存疑必标注** ⚠️ —— 无法核实的词保留原文并注明
5. **来源不编造** —— 没有原始转发文本就注明未提供

## 测试

```bash
python3 -m pytest ~/.agents/skills/douyin-transcribe-text/tests/ -v
```

覆盖：专有名词校正、疑词检测、错误分类、mp4 校验、密钥解析、结构化输出。

## 幻觉重复检测

`find_repeats()` 检测 ASR 复读/幻觉，**只标注不删改**（铁律「原文一字不改」）。

阈值按单元长度自适应：

| 单元长度 | 阈值 | 理由 |
|---------|------|------|
| ≤2 字符 | 5 次 | 「哈哈哈」「他他他」重复 3-4 次属正常，不报 |
| ≥3 字符 | 3 次 | 长句复读 3 遍基本就是幻觉（实测 "I should keep on down" × 3） |

字符类**排除句末标点**（。！？；：，、），避免跨句误配；上限 40 字符（「I should keep on down」长 21，设 20 会整条漏检）。

标注格式：`〔⚠️ 疑似 ASR 幻觉重复 N 次，待确认〕`，插在重复段末尾，**文字原样保留**。
`transcript_raw.md` 永远存未标注原文，判断权在用户。

## 已知局限

- 超过 30 分钟的视频可能触及 API 大小限制，需分段转写
- API 免费属当前策略，官方可随时调整；调整后 backup（本地 Whisper）自动接管，流程不中断
- **ASR 识别上限**：SenseVoice 对专有名词、生僻字会出错（如「恩比德」→「恩彼得」），词表救不了。换引擎更糟——59 条词表全按 SenseVoice 错误指纹标定
- **content-length 排序不可靠**：按 header 体积排序只是启发式，已加偏差警告 + 实收重试兜底，但极端情况下仍可能多下载几条流
