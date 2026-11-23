# douyin-transcribe-text

> 抖音视频 → 口播逐字稿 → Obsidian 结构化笔记

一个 OpenClaw / Claude Code **Skill**：把抖音链接或本地 MP4 变成一份可信的口播逐字稿，
关键难点（动态签名、专有名词误听、疑词定案）都已经封在脚本里。

```
Playwright 渲染 → 网络拦截捕获 CDN 直链 → 下载 → ffmpeg 合成
  → SenseVoice 转写 → 专有名词校正 → 疑词截帧验证 → 分段输出
```

---

## 它解决什么

抖音视频转写有三个真实的坑，这个 skill 逐个填掉：

| 坑 | 现象 | 本 skill 的做法 |
|---|---|---|
| **动态签名** | `curl` / `yt-dlp` 直连必然 403 | 开真实 Chromium，让抖音自己的 JS 算出 `a_bogus` / `msToken`，再从网络层拦截 CDN 直链 |
| **专有名词误听** | 「Claude Code」→「cloud code」，「LVR」→「IY2」 | 维护一张按长度降序替换的专名校正表（`PROPER_NOUN_MAP`），纯本地、无网络 |
| **疑词无处对证** | 看起来「能从上下文推出来」，实测错误率 **29%** | 交付前必跑截帧验证，用画面定案，输出「已核实 / 仍未核实」两张表 |

## 快速开始

```bash
# 安装
git clone https://github.com/LeeYH95/douyin-transcribe-text.git \
  ~/.openclaw/skills/douyin-transcribe-text

# 依赖
python3 -m pip install playwright requests --break-system-packages
python3 -m playwright install chromium
brew install ffmpeg

# 转写（完整闭环：下载 → 转写 → 校正 → 分段）
T=~/.openclaw/skills/douyin-transcribe-text/scripts/transcribe.py
python3 $T "<抖音链接>"
```

其他用法：

```bash
python3 $T --video /path/to/local.mp4        # 已有本地视频，跳过下载
python3 $T "<链接>" --check-link             # 只解析 video_id
python3 $T "<链接>" --headful                # 首次登录验证用（可见窗口，人工登录）
python3 $T "<链接>" --download-only          # 只下载视频，不转写
python3 $T --video in.mp4 --frames 68,231    # 按指定时间点抽帧
```

## 依赖

| 组件 | 安装 | 必需 |
|---|---|---|
| playwright | `python3 -m pip install playwright --break-system-packages` | ✅ |
| chromium | `python3 -m playwright install chromium` | ✅ |
| ffmpeg / ffprobe | `brew install ffmpeg` | ✅ |
| 硅基流动 key | 见下 | ✅（可降级） |
| 本地 Whisper | `python3 -m venv ~/.agents/venvs/whisper && ~/.agents/venvs/whisper/bin/pip install openai-whisper` | ⭕ 备份引擎 |

**转写 key**：写入 `~/.agents/.siliconflow_key`（chmod 600），或用环境变量
`API_KEY` / `DOUYIN_API_KEY` / `SILICONFLOW_API_KEY`。脚本内**不硬编码任何 key**。

**引擎降级**：默认走硅基流动 SenseVoice，API 不可用时自动切本地 Whisper，流程不中断。
但换引擎 = 整套专名校正层作废（`PROPER_NOUN_MAP` 是按 SenseVoice 的错误指纹逐条攒的）。

## 目录结构

```
douyin-transcribe-text/
├── SKILL.md                    入口：触发条件、工作流、铁律
├── references/                 按需加载的参考文档
│   ├── output-format.md        输出规范（frontmatter 字段表 / 分段 / 交付前自检清单）
│   ├── proper-noun-map.md      专名校正表维护（唯一权威说明）
│   ├── frame-verification.md   疑词截帧验证流程
│   ├── frame-reading.md        读帧通用规矩
│   ├── transcription-engine.md  转写引擎选择与 key 优先级
│   ├── delivery-gate.md        交付前质量闸与幻觉重复检测
│   └── feishu-file-delivery.md  大文件发飞书的正确姿势
├── scripts/
│   ├── transcribe.py           主程序（下载 + 转写 + 校正，单文件自包含）
│   ├── verify_terms.py         疑词截帧验证
│   ├── quality_gate.py         出稿前质量门禁（确定性规则）
│   ├── naming.py               目录命名（主题_作者_日期）
│   ├── rename_folders.py       批量重命名（幂等）
│   ├── feishu_send_file.py     大文件交付飞书
│   └── ocr_vision.swift        本地 Vision OCR
└── tests/test_transcribe.py    83 个离线测试
```

`scripts/transcribe.py` **单文件自包含**，不 import 其他 skill —— skill 会迁移或重装，
拆成多个互相 import 的模块，相对路径必然失效。

## 质量闸

```bash
python3 scripts/quality_gate.py <transcript.md> --type speech
```

**类型要选对**：口播稿用 `--type speech`；画面实录/整理稿用 `--type note`
（表格与加粗本身就是内容结构，按口播稿判会误报）。

结论里出现阻断项就改完再交付。闸门是确定性规则，不靠模型自评。

## 测试

```bash
python3 -m pytest tests/ -v
```

83 个测试全部离线，不触网、不调用转写 API。覆盖专名校正、疑词检测、mp4 校验、
密钥解析、流构成探测、字幕条几何、语速与无口播判定、标题解析。

## 媒体落盘位置

**所有转写下载的视频/图片优先放外接盘，不占内置盘。** 根目录用环境变量指定：

```bash
export OPENCLAW_MEDIA_ROOT="/Volumes/YourSSD/06_媒体转写"
```

未设置或目标不可写时自动回退 `~/Downloads`，不会崩。

| 类别 | 路径 |
|---|---|
| 抖音转写（视频 + 逐字稿） | `<媒体根目录>/抖音/<主题>_<作者>_<日期>/` |
| 小红书转写（视频 / 图文） | `<媒体根目录>/小红书/<主题>_<作者>_<日期>/` |
| 单独下载的图片素材 | `<媒体根目录>/图片素材/` |
| 历史零散产物 | `<媒体根目录>/_归档/` |

**目录命名**：`<主题>_<作者>_<日期>`，不用 video_id —— Finder 里扫一眼就知道是什么内容。
video_id 记在目录内的 `meta.json`，不丢可追溯性。

```bash
python3 scripts/rename_folders.py <抖音根目录> [--apply]   # 幂等，重复跑不会造 _2
```

> ⚠️ 抖音页面在并发 / 风控下可能渲染出**别的视频**。`canonical` / `location` 必须仍指向
> 目标 video_id，否则不采信任何 meta，目录退回 video_id —— 宁可名字难看，也不能名不副实。

## 笔记属性规范

写入 Vault 时每份笔记必须包含完整 YAML frontmatter，字段按此顺序：

```yaml
---
title: 文档标题，简洁准确
description: 一句话描述内容概要（25字内）
source: 原始来源链接
tags: [标签1, 标签2]
created: YYYY-MM-DD
type: speech/transcript
confidence: high
speaker: 演讲者/作者（无@符号）
organization: 组织机构（视情况）
event: 会议/活动名称（视情况）
authors: 作者列表（视情况）
---
```

`title` / `description` / `source` / `tags` / `created` / `type` / `confidence` 必填；
其余**视情况**字段无值时整行省略，不写空占位。正文标题一律用一级标题 `# 标题`。

## 铁律

1. **原文一字不改** —— 只做结构化分段，不增删改任何内容
2. **未经确认不写 Vault** —— 「可以了」/「写入」才操作；数字、表情、简短肯定**不算确认**
3. **视频保留本地** —— 不把下载的视频丢进 Vault
4. **存疑必标注** ⚠️ —— 无法核实的词保留原文并注明，标注只进交付对话，不进 Vault 正文
5. **来源不编造** —— 没有原始转发文本就注明未提供
6. **疑词必核** —— 交付前跑 `verify_terms.py`，输出「已核实 / 仍未核实」两张表
7. **目录名必须名副其实** —— 页面身份校验不过就不采信 meta
8. **无口播不硬凑** —— `no_speech: true` 时不要把幻觉碎片当逐字稿交付

## 安全边界

| 说法 | 是否属实 |
|------|---------|
| 不接触用户日常浏览器的任何数据 | ✅ 属实 |
| 完全不使用任何 cookie | ❌ **不实**。每次匿名访问抖音都会发一套自己的会话 cookie，脚本下次跑会复用 |
| 靠 cookie 才下得下来 | ❌ **不实** |

**准确的说法是：**

> 使用**独立的匿名浏览器 profile**（`~/.cache/openclaw/chrome_profiles/douyin`），
> 不接触用户日常浏览器的任何数据。抖音风控靠的是动态签名而非登录态，
> 因此浏览器渲染是必要条件，这不等同于使用登录凭据。

`--headful` 会打开**可见窗口**，由用户本人手动登录一次，会话存进同一个独立 profile 复用。
属于本人可见操作，不是脚本偷取凭据。**使用前必须向用户说明**，不得静默触发。

其他：不绕过任何访问控制，受限即停；失败时如实报错，不编造来源、不伪造转写结果。

## 踩坑记录

1. **抖音不自动播放** —— 必须 `play()` / 空格键触发，否则拦截到 0 条流
2. **音/视频/合流三条流 content-type 全是 `video/mp4`** —— 抖音把音频也装在 MP4 容器里，
   靠 content-type 区分无效，必须下载后用 ffprobe 判定真实流构成
3. **content-length 不可信** —— 实测声称 0.6MB 实收 7.6MB（偏差 12×）。脚本按 header 体积降序尝试
   并打印偏差警告；首轮无合流时按实收体积重试一轮
4. **CDN 签名有 10-30 分钟时效** —— 捕获后立即下载
5. **yt-dlp 路径已死** —— 无 JS 引擎算签名，必然 403
6. **CDN 返回音频替身** —— 有时在 `/video/` 路径下返回纯音频流；
   若只剩音频流，产出无视频轨 mp4，转写照常但**截帧验证不可用**
7. **页面会预载并自动播放别的视频** —— 按体积排序会优先选中错的。
   脚本从 DOM 的 `<video>.duration` 取目标时长，逐条候选流校验时长（容差 3s），
   全部不符就**直接报错拒交**

## License

MIT
