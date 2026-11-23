# 转写引擎：默认 SenseVoice，失败降级本地 Whisper

> **读这里的条件**：转写结果异常、API 报错、要重装/重建 Whisper、或要评估换引擎的成本。
> 只想跑流程的话，`scripts/transcribe.py` 自己会选引擎，用不着看本文。

---

## 1. 默认走 API，Whisper 是 backup

`--engine auto` 的顺序为 SenseVoice → 本地 Whisper。缺凭证、云端失败、空结果或
音频超过 50 MB 都会转入本地分支。`--engine whisper` 跳过上传，
`--engine sensevoice` 将云端失败直接返回给调用方。

M4 实测（同一段 60s 音频）：

| 引擎 | 耗时 | 角色 |
|------|------|------|
| SiliconFlow SenseVoiceSmall | **30.6s** | **默认** |
| 本地 Whisper large-v3-turbo | 37.9s | backup（API 不可用时自动接管） |

**默认走 API 的两个理由：**

1. `FunAudioLLM/SenseVoiceSmall` 在硅基流动价格页标注**免费**（2026-08、2026-09 两次核实），不存在 API 成本
2. 本地并不更快（37.9s vs 30.6s）

词表主要来自 SenseVoice 的历史误听。两条引擎路径都只生成候选，需要音频或画面证据才能应用。
记录实际引擎及各段耗时到 `asr_engine` 和 `timings_seconds`。SenseVoice 当前接口只返回全文。
Whisper 同一次识别保留分段时间戳，无需为抽帧重跑转写。

---

## 2. API key 解析

优先级：**环境变量 → `~/.agents/.siliconflow_key`**。

环境变量名（任一命中即可）：`API_KEY` / `DOUYIN_API_KEY` / `SILICONFLOW_API_KEY`。

key 文件权限 `chmod 600`。脚本内**不硬编码任何 key**。

---

## 3. 本地 Whisper 环境

**不要用 Homebrew 装的 openai-whisper。** 它的 libtorch 硬链到特定版本 abseil，
Homebrew 一升级就 `dlopen` 失败。脚本因此**优先用自建 venv**
（`~/.agents/venvs/whisper`，PyPI 的 torch wheel，不受此影响）。

重建 venv：

```bash
python3 -m venv ~/.agents/venvs/whisper
~/.agents/venvs/whisper/bin/pip install openai-whisper
```

可覆盖：

- `DOUYIN_WHISPER_MODEL`（默认 `large-v3-turbo`）
- `DOUYIN_WHISPER_LANGUAGE`（默认 `Chinese`）
