#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
目录命名模块 —— 让下载目录一眼能看出主题。

命名规则：<主题>_<作者>
可追溯性：目录内 meta.json 记录 video_id / title / author / source / created。

设计原则（2026-10-28 ）：
  - 目录名用**视频主题**，不用 video_id —— Finder 里要能认出内容是什么
  - video_id 保留在 meta.json，不丢可追溯性
  - 自动提取 + 人工覆盖（--title / --author），Agent 读稿后可以修正

⚠️ 孪生模块：`xhs-transcribe-text/scripts/naming_xhs.py` 是同一套命名规则的
另一份实现。两侧的 `sanitize` / `folder_base` / `unique_dir` 必须保持行为一致，
改一边就要同步另一边 —— 规则本身以 README《媒体落盘位置》一节为准。
"""
import json
import re
import unicodedata
from pathlib import Path

# ── 非法字符（macOS / exFAT 通用）────────────────────────────────
_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
# 抖音标题里常见的噪音
_TRAILING_PUNCT = "。，、；：！？!?,.;:　 \t"

MAX_TITLE = 28
MAX_AUTHOR = 16
MAX_TOTAL = 56


def sanitize(text: str, maxlen: int = MAX_TITLE) -> str:
    """清洗成安全的文件名片段。

    超长时在标点/空格边界截断 —— 宁可短一点，也不要切出半截词
    （实测 2026-10-28：硬切会把「工程落地踩坑点」截成「工程落」）。
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    t = _ILLEGAL.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.strip(_TRAILING_PUNCT).strip()
    if len(t) > maxlen:
        head = t[:maxlen]
        # 在标点边界回退（保留标点，目录名读起来更自然）
        for sep in ("，", ",", "。", "、", "；", ";", "：", ":", "｜", "丨", " "):
            idx = head.rfind(sep)
            if idx >= maxlen * 0.45:
                head = head[:idx + len(sep)]
                break
        t = head.strip(_TRAILING_PUNCT).strip()
    return t


def extract_author(desc: str) -> str:
    """从抖音 description 提取作者名。

    抖音 description 形如：
      「<标题> - <作者>于20261025发布在抖音，已经收获了4008个喜欢，来抖音，记录美好生活！」
    """
    if not desc:
        return ""
    m = re.search(r"-\s*([^-\n]{1,24}?)于\d{8}发布在抖音", desc)
    if m:
        return sanitize(m.group(1).strip(), MAX_AUTHOR)
    return ""


def extract_date(desc: str) -> str:
    """从 description 提取发布日期（YYYYMMDD）。

    抖音 description 形如：「… - <作者>于20261025发布在抖音，已经收获了…」
    抽不到时返回空串（调用方决定是否退回文件时间）。
    """
    if not desc:
        return ""
    m = re.search(r"于(\d{8})发布在抖音", desc)
    if m:
        d = m.group(1)
        # 校验是合理日期，防止抓到别的 8 位数字
        try:
            y, mo, dd = int(d[:4]), int(d[4:6]), int(d[6:])
            if not (2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= dd <= 31):
                return ""
        except ValueError:
            return ""
        return d
    return ""


def _strip_noise(text: str) -> str:
    """去掉话题标签、平台后缀、作者后缀。"""
    t = text or ""
    # 去掉「#话题」及其后可能的连续空格
    t = re.sub(r"#\S*", "", t)
    # 去掉「- 抖音」结尾
    t = re.sub(r"\s*-\s*抖音\s*$", "", t)
    # 去掉「- <作者>于YYYYMMDD发布在抖音…」
    t = re.sub(r"\s*-\s*[^-\n]{1,24}?于\d{8}发布在抖音[^\n]*$", "", t)
    return re.sub(r"\s+", " ", t).strip()


def derive_title(og_title: str, desc: str = "") -> str:
    """从页面元数据推导**简洁**的标题。

    抖音的 og:title 常常是「标题 + 正文」的整段caption，
    所以规则是：去掉噪音后取到第一个空白为止（那是标题与正文的分界）。
    """
    for cand in (og_title, desc):
        if not cand:
            continue
        t = _strip_noise(cand)
        if not t:
            continue
        # 优先按换行切（caption 常用换行分隔标题与正文）
        t = t.split("\n")[0].strip()
        # 标题与正文之间通常是空格分隔，取第一段
        segs = t.split()
        head = segs[0].strip() if segs else ""
        # 首段太短（如「10月份」）说明切早了 —— 往后合并到下一个分隔符
        if len(head) < 6 and len(segs) > 1:
            merged = t
            for sep in ("，", ",", "、", "。", "｜", "丨"):
                if sep in merged:
                    merged = merged.split(sep)[0]
                    break
            else:
                # 无标点则取到下一个空格之前的一段（含原首段）
                parts = t.split(" ", 2)
                merged = " ".join(parts[:2]) if len(parts) > 1 else t
            merged = merged.strip(_TRAILING_PUNCT).strip()
            if len(merged) >= 4:
                head = merged
        head = head.strip(_TRAILING_PUNCT).strip()
        if len(head) >= 4:
            return sanitize(head, MAX_TITLE)
        # 整段没有空格 → 取前若干字符
        if len(t) >= 4:
            return sanitize(t, MAX_TITLE)
    return ""


def folder_base(title: str, author: str = "", date: str = "",
                fallback: str = "") -> str:
    """拼出目录基名：<主题>_<作者>_<日期>。

    日期放最后 —— 主题才是扫目录时最先要找的信息。
    """
    t = sanitize(title, MAX_TITLE)
    a = sanitize(author, MAX_AUTHOR)
    d = sanitize(date, 8) if date else ""
    if not t:
        t = sanitize(fallback, MAX_TITLE) or "未命名"
    parts = [t]
    if a:
        parts.append(a)
    if d:
        parts.append(d)
    base = "_".join(parts)
    if len(base) > MAX_TOTAL:
        base = base[:MAX_TOTAL].strip(_TRAILING_PUNCT).strip()
    return base


def unique_dir(parent: Path, base: str) -> Path:
    """目标目录已存在时加 _2 / _3 后缀，避免覆盖。"""
    cand = parent / base
    if not cand.exists():
        return cand
    for i in range(2, 100):
        cand = parent / f"{base}_{i}"
        if not cand.exists():
            return cand
    return parent / f"{base}_{int(__import__('time').time())}"


def write_meta(dest_dir: Path, *, video_id: str, title: str, author: str = "",
                date: str = "", source: str = "", extra: dict | None = None) -> Path:
    """在目录内写 meta.json，记录 video_id 等可追溯信息。"""
    meta = {
        "video_id": video_id,
        "title": title,
        "author": author,
        "date": date,
        "source": source or (f"https://www.douyin.com/video/{video_id}"
                              if str(video_id).isdigit() else ""),
        "named_by": "douyin-transcribe-text/naming.py",
    }
    if extra:
        meta.update(extra)
    p = dest_dir / "meta.json"
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
