#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内容质量闸 (quality gate) —— YC 那套闭环里的 Quality Gate 环节。

不依赖模型自评，纯确定性规则 + TTS 实测，输出可回归的信号。
用途：判断一份逐字稿「能不能直接发」。
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# ── 书面语特征词（出现在口播稿里 = 翻译腔/AI味）────────────────────
WRITTEN_MARKERS = [
    "因此", "从而", "综上", "此外", "然而", "首先", "其次", "最后",
    "需要指出的是", "值得注意的是", "不难看出", "换言之", "与此同时",
    "也就是说", "总而言之", "众所周知", "毋庸置疑", "至关重要",
    "具有重要意义", "提供了", "起到了", "奠定了", "开辟了",
]

# ── 朗读不友好的结构 ───────────────────────────────────────────
PAREN = re.compile(r"[（(][^）)]{0,60}[）)]")
DASH = re.compile(r"——|—{2,}")
EN_MIX = re.compile(r"[A-Za-z]{3,}")

SENT_SPLIT = re.compile(r"[。！？!?；;]+")


def load_body(path: Path) -> tuple[str, str]:
    """返回 (正文, frontmatter)。剔除校正说明表——它不是要发的内容。"""
    raw = path.read_text(encoding="utf-8", errors="ignore")
    fm = ""
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            fm, raw = parts[1], parts[2]
    # 去掉一级标题行
    body = "\n".join(l for l in raw.split("\n") if not l.startswith("# "))
    # 去掉专名校正说明段
    idx = body.find("## 专有名词校正说明")
    if idx > 0:
        body = body[:idx]
    return body.strip(), fm.strip()


def split_sentences(body: str) -> list[str]:
    out = []
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        for s in SENT_SPLIT.split(line):
            s = s.strip()
            if s:
                out.append(s)
    return out


def analyze(path: Path) -> dict:
    body, fm = load_body(path)
    sents = split_sentences(body)
    nchars = len(re.sub(r"\s", "", body))
    nsent = len(sents)

    # 1. 书面词密度（每百字）
    hits = []
    for w in WRITTEN_MARKERS:
        c = body.count(w)
        if c:
            hits.append({"词": w, "次数": c})
    written_hits = sum(h["次数"] for h in hits)
    written_density = written_hits * 100 / max(nchars, 1)

    # 2. 句长分布
    lens = [len(re.sub(r"\s", "", s)) for s in sents]
    long_sents = [(i, s, l) for i, (s, l) in enumerate(zip(sents, lens)) if l > 40]
    short_sents = [(i, s, l) for i, (s, l) in enumerate(zip(sents, lens)) if l < 8]
    longest = max(lens) if lens else 0

    # 3. 朗读干扰项
    parens = PAREN.findall(body)
    dashes = DASH.findall(body)
    en_tokens = EN_MIX.findall(body)

    # 4. markdown 残留
    md_leak = re.findall(r"\*\*[^*\n]{2,50}\*\*|^\s*[-*]\s+|`[^`\n]+`", body, re.M)

    # 5. frontmatter 字段完整性
    fm_keys = [l.split(":")[0] for l in fm.split("\n") if ":" in l]
    required = ["title", "description", "source", "tags", "created", "type", "confidence"]
    fm_missing = [k for k in required if k not in fm_keys]

    return {
        "文件": str(path),
        "正文字数": nchars,
        "句数": nsent,
        "书面语": {
            "总命中": written_hits,
            "每百字密度": round(written_density, 2),
            "明细": sorted(hits, key=lambda x: -x["次数"])[:8],
        },
        "句长": {
            "最长句字数": longest,
            "长句(>40字)数": len(long_sents),
            "长句占比%": round(len(long_sents) * 100 / max(nsent, 1), 1),
            "碎句(<8字)数": len(short_sents),
            "碎句占比%": round(len(short_sents) * 100 / max(nsent, 1), 1),
            "最长句样本": max(sents, key=lambda s: len(re.sub(r"\s", "", s)))[:90] if sents else "",
        },
        "朗读干扰": {
            "括号插入语": len(parens),
            "破折号": len(dashes),
            "英文词": len(en_tokens),
            "英文词样本": sorted(set(en_tokens))[:10],
        },
        "格式残留": {"markdown符号": len(md_leak)},
        "frontmatter": {
            "字段": fm_keys,
            "缺失必填": fm_missing,
            "合规": not fm_missing,
        },
        "_sents": sents,
        "_body": body,
    }


def tts_probe(body: str, out_dir: Path, voice: str | None) -> dict:
    """用 macOS say 实际合成，验证可朗读性 + 产出音频供人工听。"""
    if not shutil_which("say"):
        return {"可用": False, "原因": "系统无 say 命令"}
    text = re.sub(r"[#*`>|\[\]()]", "", body)[:1500]
    out = out_dir / "tts_preview.aiff"
    cmd = ["say", "-o", str(out), "-r", "180"]
    if voice:
        cmd[1:1] = ["-v", voice]
    cmd.append(text)
    try:
        subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception as e:
        return {"可用": False, "原因": str(e)}
    if not out.exists():
        return {"可用": False, "原因": "say 未产出文件"}
    size = out.stat().st_size
    # 估算语速：字符/秒
    dur = 0.0
    try:
        r = subprocess.run(["afinfo", str(out)], capture_output=True, text=True, timeout=20)
        m = re.search(r"estimated duration: ([\d.]+) sec", r.stdout)
        if m:
            dur = float(m.group(1))
    except Exception:
        pass
    return {
        "可用": True,
        "音频": str(out),
        "字节": size,
        "时长秒": round(dur, 1),
        "语速(字/秒)": round(len(text) / dur, 2) if dur else None,
        "音色": voice or "系统默认",
    }


def shutil_which(cmd: str) -> str | None:
    from shutil import which
    return which(cmd)


def judge(r: dict, tts: dict, note_type: str = "speech") -> list[str]:
    """给出可执行的结论（不是打分，是「能不能发」的判断）

    note_type="speech"：口播稿，markdown 符号/括号是残留，要拦。
    note_type="note"：画面实录/整理稿，表格与加粗本身就是内容结构，
                     不可朗读性检查不适用（这类稿子本来就没口播）。
    """
    issues = []
    if r["frontmatter"]["缺失必填"]:
        issues.append(f"frontmatter 缺必填字段: {r['frontmatter']['缺失必填']}")
    if r["书面语"]["每百字密度"] > 1.5:
        issues.append(f"书面语密度 {r['书面语']['每百字密度']}/百字偏高，像 AI 写的稿子")
    if r["句长"]["长句占比%"] > 25:
        issues.append(f"长句占比 {r['句长']['长句占比%']}%，朗读会喘不过气")
    if note_type == "speech":
        if r["朗读干扰"]["括号插入语"] > 3:
            issues.append(f"括号插入语 {r['朗读干扰']['括号插入语']} 处，TTS 会读出括号或吞掉内容")
        if r["格式残留"]["markdown符号"]:
            issues.append(f"正文残留 markdown 符号 {r['格式残留']['markdown符号']} 处")
    if note_type == "speech" and tts.get("可用") and tts.get("语速(字/秒)"):
        v = tts["语速(字/秒)"]
        if v > 7:
            issues.append(f"语速 {v} 字/秒 偏快（正常口播 4.5-6），听感会赶")
        elif v < 3.2:
            issues.append(f"语速 {v} 字/秒 偏慢")
    if not issues:
        issues.append("✅ 无阻断项，可直接发")
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default="/tmp/qg_out")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--type", choices=["speech", "note"], default="speech",
                    help="speech=口播稿（可朗读性检查生效）；note=画面实录/整理稿"
                         "（表格加粗即内容，跳过朗读类检查）")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_r = []
    for f in args.files:
        p = Path(f)
        r = analyze(p)
        tts = tts_probe(r.pop("_body"), out_dir, args.voice)
        r["TTS"] = tts
        r["结论"] = judge(r, tts, note_type=args.type)
        r.pop("_sents", None)
        all_r.append(r)

    if args.json:
        print(json.dumps(all_r, ensure_ascii=False, indent=2))
        return

    for r in all_r:
        print("=" * 66)
        print(f"📄 {Path(r['文件']).name}")
        print("=" * 66)
        print(f"  正文 {r['正文字数']} 字 / {r['句数']} 句"
              f"  [{args.type} 稿]")
        w = r["书面语"]
        print(f"  书面语密度 : {w['每百字密度']}/百字  (命中 {w['总命中']} 次)")
        if w["明细"]:
            print("    " + "  ".join(f"{h['词']}×{h['次数']}" for h in w["明细"][:6]))
        s = r["句长"]
        print(f"  句长       : 最长 {s['最长句字数']} 字 | "
              f"长句 {s['长句(>40字)数']}({s['长句占比%']}%) | "
              f"碎句 {s['碎句(<8字)数']}({s['碎句占比%']}%)")
        print(f"    最长句   : {s['最长句样本']}")
        p = r["朗读干扰"]
        print(f"  朗读干扰   : 括号 {p['括号插入语']} | 破折号 {p['破折号']} | 英文词 {p['英文词']}")
        if p["英文词样本"]:
            print(f"    英文词   : {', '.join(p['英文词样本'])}")
        print(f"  格式残留   : markdown {r['格式残留']['markdown符号']} 处")
        fm = r["frontmatter"]
        print(f"  frontmatter: {'✅ 合规' if fm['合规'] else '❌ 缺 ' + str(fm['缺失必填'])}")
        t = r["TTS"]
        if t.get("可用"):
            print(f"  TTS 实测   : {t['音色']} | {t['时长秒']}s | "
                  f"{t['语速(字/秒)']} 字/秒")
            print(f"    试听文件 : {t['音频']}")
        else:
            print(f"  TTS 实测   : 不可用（{t.get('原因')}）")
        print("  ── 判定 ──")
        for c in r["结论"]:
            print(f"    · {c}")
        print()


if __name__ == "__main__":
    main()
