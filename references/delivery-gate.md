# 交付前的确定性检查

> **读这里的条件**：逐字稿已生成，准备发给用户之前。
> 交付形态（frontmatter / 分段 / 飞书发送方式）见 `output-format.md`；
> 疑词的截帧核实见 `frame-verification.md`；读帧本身的通用规矩见 `frame-reading.md`。

交付前要跑的两件事，都是**确定性规则，不靠模型自评**：

1. **幻觉重复检测** —— ASR 复读 / 幻觉，脚本已自动标注，你只需判断标注是否合理
2. **质量闸 `quality_gate.py`** —— 确认原稿、媒体、逐处证据和最终稿相符

---

## 1. 幻觉重复检测

`find_repeats()` 检测 ASR 复读/幻觉，**只标注不删改**（铁律「原文一字不改」）。

阈值按单元长度自适应：

| 单元长度 | 阈值 | 理由 |
|---------|------|------|
| ≤2 字符 | 5 次 | 「哈哈哈」「他他他」重复 3-4 次属正常，不报 |
| ≥3 字符 | 3 次 | 长句复读 3 遍基本就是幻觉（实测 "I should keep on down" × 3） |

字符类**排除换行与句末标点**（。！？；：，、… 及半角 `, . ! ? ; :`），避免跨句误配；
单元上限 40 字符（「I should keep on down」长 21，设 20 会整条漏检）。

标注格式：`〔⚠️ 疑似 ASR 幻觉重复 N 次，待确认〕`，插在重复段末尾，**文字原样保留**。
`transcript_raw.md` 永远存未标注原文，判断权在用户。

**标注只存在于交付给用户的对话里，不写进 Vault 正文** —— 写入前把标注去掉，
只保留核对过的原文。（理由与「疑词标注」的硬闸同源，见
`output-format.md`《写入 Vault 前的硬闸》）

---

## 2. 质量闸

```bash
python3 "<douyin-transcribe-text-skill-dir>/scripts/quality_gate.py" <transcript.md> --type speech --json
```

口播任务的入口状态为 `needs_review`、`needs_source_confirmation` 或 `no_speech`。
取不到媒体时入口返回 `needs_media`。质量闸检查通过后才返回 `ready`。
任何阻断项都会给出 JSON 的 `blockers`，退出码为 2。
句长、书面词和朗读性只给提示。需要试听时显式提供 `--tts --out <音频输出目录>`。

### 记录核验决定

将下列格式保存为稿件旁的 `review_decisions.json`。指纹和出现 ID 从
`transcript_review.json` 复制。每一处出现都要分别查证。`keep` 保留，`replace` 指定修订词。

```json
{
  "raw_sha256": "<原稿 SHA-256>",
  "media_sha256": "<媒体 SHA-256>",
  "occurrences": {
    "25:28": {
      "action": "keep",
      "evidence": {"kind": "audio", "seconds": 3.5, "reason": "回听确认这里说的是 IPS"}
    },
    "71:74": {
      "action": "replace",
      "replacement": "FPS",
      "evidence": {"kind": "frame", "seconds": 31, "manifest": "frames/manifest.json", "reason": "这一处口播与画面计数器一致"}
    }
  }
}
```

当来源待确认时，核对具体媒体与原作品后，额外记录
`"source_confirmation": {"source": "<核验记录中的来源>", "reason": "<对应依据>"}`。
对分离流或 MSE，作品页与时长匹配只能作为线索。源文件、原稿或抽帧清单不一致会阻断。

用 `quality_gate.py <transcript.md> --render-reviewed` 获取按出现位置应用修订的正文，
添加分段标题并保存后，再对最终 Markdown 执行 `--json` 检查。
核验决定必须来自实际回听或查看帧。程序检查记录和文件的一致性。

---

## 3. 自检

完整自检清单见 `output-format.md`《输出前自检》（含疑词两张表、质量闸无阻断项、
标注不进 Vault 三项）—— 那里是唯一清单，本文件不重复列。
