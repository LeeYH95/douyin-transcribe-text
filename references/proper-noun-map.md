# 专有名词校正表（PROPER_NOUN_MAP）维护指南

> **本文件是词表的唯一权威说明。** 词表本体在 `scripts/transcribe.py` 的
> `PROPER_NOUN_MAP`（随验证持续增加）。
> `xhs-transcribe-text` 复用同一张表（两端转写引擎都是 SenseVoiceSmall，错误指纹一致），
> 所以**改词表 = 改这个文件描述的机制**，不是改小红书专属规则。

抖音主流程通过 `propose_corrections()` 将命中项放入 JSON 的
`correction_candidates`。草稿保留说话原词，核实后才应用修订。
`correct_proper_nouns()` 保留给已有显式调用方。

---

## 1. 机制（不要改回别的实现）

- **长度降序 + 单次扫描**。`PROPER_NOUN_SORTED` 按词长降序排，拼成一个正则
  `_PROPER_NOUN_RE`，一次 `re.sub` 扫全篇。
- **单次扫描，不做级联替换**。逐条 `text.replace()` 会产生级联：
  `scul → skill` 之后规则 `kill→skill` 又命中新生成的 `skill`，产出 `ssskillll`。
  单次扫描后替换文本不再被回扫，此问题消失。
- **单次扫描仍不够**：规则 `ski` / `kill` 是 `skill` 的子串，会切开本来正确的
  `skill`。必须配 `PROPER_NOUN_MAP` 里的**恒等条目**（`"skill": "skill"`）。
- **恒等条目绝对不能删**。它不是冗余，是护栏。删掉会让已正确的词被切开。

### 恒等护栏的两个已知实例

| 恒等条目 | 保护的正确写法 | 不加会产出 |
|---------|--------------|-----------|
| `skill` → `skill` | `skill` / `skills` | `skillll` / `skillskill` |
| `声图像素` → `声图像素` | `声图像素`（小红书路径实测） | `生图像素` |

---

## 2. 加词条的两条硬规矩

1. **只加非真实词。** 听错形式本身必须是垃圾词。
   `声图 → 生图` 可以加；`开箱 → 看向` **不能加** —— 「开箱测评」「开箱视频」
   是正确用法，全局单次扫描会把它们改坏。这类词靠 `find_suspect_terms()` 人工判。
2. **撞到真实词就加恒等护栏。** 加了 `声图→生图` 后发现「声图像素」被改坏，
   于是加 `声图像素→声图像素` 恒等条目。校正表按词长降序匹配，长词优先，
   与 `skill→skill` 同一机制。

---

## 3. 子串切分陷阱（加词前必读）

新词条若**包含**旧词，必须让新词更长（排序会自动优先命中），否则旧的短词
会先把长词切开：

| raw | 只加 `scu→skill` | 后果 |
|---|---|---|
| `scukill` | `skill` + `kill`→`skill` | **`skillskill`**（坏） |
| `cukill` | `cuki`→`skill` + `ll` | **`skillll`**（坏） |

加词条前必须做两件事：

1. **枚举 raw 里的真实形态**（别照抄转写稿 —— 转写稿可能已经被老词表切坏了）
2. **同一语义的所有变体都要加**，且长的必须更长：
   `scukill` / `cukill` / `scull` / `scuki` / `cuki`

**验证方法**：拿 raw 跑一遍 `correct_proper_nouns()`，然后 grep 产物里有没有
`skillll` / `skillskill` / `agentt` / `aagent` 这类**重复字** —— 有就是切坏了。

保留 raw 和各次修订记录。从 raw 及已核实的修订重建正文。历史样本成立的组合，
在新视频里仍需要对应证据，尤其是数字、金额和可能属于其他领域的真实词。

---

## 4. 新增词条必须走截帧验证

**不能凭上下文猜。** 新增条目要有画面/字幕证据，方法见
`references/frame-verification.md`。

校正优先看**视频烧录字幕**（比用量表帧更直接）。证据强度：
字幕 + UI 双证据 > 单一烧录字幕 > 上下文推断（实测错误率 29%）。

---

## 5. 已验证条目（含依据）

下表按证据强度分两类：**✅ = 有截帧/字幕实证**，可信；**依据列写「上下文」的只靠推断**，
可信度较低 —— 加新词条时不要拿上下文推断当证据。

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
| **open car** | **OpenClaw** | ✅ 586s 终端 `for C in 'clawhub','openclaw','hermes'` |
| **fiers** | **Hermes** | ✅ 2087s 烧录字幕 |
| **howco** | **Claude Code** | ✅ 2088s 烧录字幕 |
| **A整 / aggen / agian / gian** | **agent** | ✅ 2104s 幻灯片 THE STABLE STACK |
| **coded S app** | **Codex App** | ✅ 2086s 幻灯片 CODEX APP FIRST |
| **再asone** | **awesome** | ✅ 617s 包名 `awesome-openclaw-skills` |
| **FFMEPG** | **FFmpeg** | ✅ 617s `@ffmpeg-video-editor` |
| **9就3个** | **993** | ✅ 617s「安装量：993 installs」 |
| **影视剧风** | **影视飓风** | ✅ 2092s 画面 T 恤印 STORM |
| **劣制角色** | **内置角色** | ✅ 253s 烧录字幕 |
| **outtra模式** | **Ultra 模式** | ✅ 68s 烧录字幕 |
| **febo** | **Fable** | ✅ 285s 烧录字幕 |

---

## 6. 词表规模

随验证持续增长。**不要在文档里写死条数** —— 条数会变，
写死的数字必然过期。要说规模就说「随验证持续增加」。

规模本身不是重点，重点是：**这整张表是按 SenseVoice 的错误指纹逐条攒出来的**。
换转写引擎等于整套校正层作废、需重标定。
