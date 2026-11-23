#!/usr/bin/env python3
"""
douyin-transcribe-text 单元测试。

覆盖：专有名词校正 / 疑词检测 / 错误分类 / mp4 校验 / 密钥解析 / 结构化输出。
全部离线，不触网、不调用转写 API。

运行：
    python3 -m pytest <douyin-transcribe-text-skill-dir>/tests/ -v
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "transcribe.py"
spec = importlib.util.spec_from_file_location("transcribe", SCRIPT)
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)


# =============================================================================
# 专有名词校正
# =============================================================================

class TestProperNounCorrection:
    """校正层必须纯本地、可离线运行。"""

    def test_截帧验证的四组词(self):
        """2026-09-28 视频烧录字幕验证过的四条。"""
        cases = [
            ("experad", "Expert Advisor"),
            ("劣制角色", "内置角色"),
            ("outtra模式", "Ultra 模式"),
            ("febo", "Fable"),
        ]
        for wrong, right in cases:
            assert t.correct_proper_nouns(wrong) == right, f"{wrong} 未正确校正"

    def test_高频基础词(self):
        assert t.correct_proper_nouns("codeex") == "Codex"
        assert t.correct_proper_nouns("GBT5.6") == "GPT-5.6"
        assert t.correct_proper_nouns("GBT6") == "GPT-6"
        assert t.correct_proper_nouns("子H") == "子 agent"
        assert t.correct_proper_nouns("scul") == "skill"
        assert t.correct_proper_nouns("cloud") == "Claude"

    def test_长词优先于短词(self):
        """GPT5.6terara 必须整体替换，不能先被 GPT5.6 截胡。"""
        assert t.correct_proper_nouns("GPT5.6terara") == "gpt-5.6-terra"

    def test_不误伤未收录文本(self):
        original = "这段话里没有任何听错词，应该原样返回。"
        assert t.correct_proper_nouns(original) == original

    def test_校正表非空且键唯一(self):
        assert len(t.PROPER_NOUN_MAP) > 40
        assert all(k and v for k, v in t.PROPER_NOUN_MAP.items())

    def test_幂等性(self):
        """再校正一次不应改变结果（已校正的不应被二次破坏）。"""
        once = t.correct_proper_nouns("experad 和 outtra模式 和 febo")
        twice = t.correct_proper_nouns(once)
        assert once == twice

    def test_无级联替换(self):
        """回归测试：曾因逐条 replace 导致 scul→skill→sskillll。

        规则 "kill"→"skill" 和 "ski"→"skill" 会命中上一条规则的替换结果。
        单次扫描后此问题消失。
        """
        for src in ("scul", "scu", "ski", "kill"):
            out = t.correct_proper_nouns(src)
            assert out == "skill", f"{src} → {out!r}，期望 'skill'"

    def test_长词不被短词截胡(self):
        assert t.correct_proper_nouns("expllo") == "Explore"
        assert t.correct_proper_nouns("exQ") == "Explore"

    def test_混合文本无残留错误词(self):
        text = "这个scul里面有scu和kill还有ski"
        out = t.correct_proper_nouns(text)
        assert "sskill" not in out
        assert "killll" not in out
        assert out == "这个skill里面有skill和skill还有skill"

    def test_已正确文本不被改坏(self):
        """回归测试：规则 "ski"/"kill" 是 "skill" 的子串。

        ASR 经常已把 skill 听对；若缺少 "skill" 恒等条目，
        正则会切开该词并重写为 "skillll"/"killll"。
        """
        # 真实稿中这 8 处 ASR 本就输出正确的 skill
        assert t.correct_proper_nouns("skill") == "skill"
        assert t.correct_proper_nouns("这个 skill 里面") == "这个 skill 里面"
        assert t.correct_proper_nouns("skill里面有规范") == "skill里面有规范"
        # 独立出现的误听词仍要正常校正
        assert t.correct_proper_nouns("一个scul") == "一个skill"
        assert t.correct_proper_nouns("一个kill") == "一个skill"
        assert t.correct_proper_nouns("一个ski") == "一个skill"

    def test_校正后不含损坏形态(self):
        """任何输入都不应产出 skillll / killll / sskillll 等损坏形态。"""
        bad_patterns = ("skillll", "killll", "sskillll", "sskill", "killll")
        for probe in ("skill", "scul", "scu", "ski", "kill",
                      "skill 里面有 scu 和 kill",
                      "先说skill再scul然后ski最后kill"):
            out = t.correct_proper_nouns(probe)
            for bad in bad_patterns:
                assert bad not in out, \
                    f"{probe!r} 产出损坏形态 {bad!r}: {out!r}"

    def test_恒等条目存在(self):
        """防止有人清理校正表时删掉保护条目。"""
        assert t.PROPER_NOUN_MAP.get("skill") == "skill"


# =============================================================================
# 疑词检测
# =============================================================================

class TestSuspectDetection:
    def test_返回列表(self):
        result = t.find_suspect_terms("这里提到了 GPT5.6luna 和 supertoolname")
        assert isinstance(result, list)
        assert all("term" in r and "type" in r for r in result)

    def test_已校正词不应被标为疑词(self):
        """校正后仍是疑词，说明检测规则误伤了已知词。"""
        corrected = t.correct_proper_nouns("experad 这个角色")
        # experad 已校正为 Expert Advisor，不应再出现为疑词
        terms = [r["term"] for r in t.find_suspect_terms(corrected)]
        assert "experad" not in terms

    def test_空文本不崩(self):
        assert t.find_suspect_terms("") == []


# =============================================================================
# 错误分类
# =============================================================================

class TestErrorClassification:
    def test_分类层次正确(self):
        assert issubclass(t.DouyinLinkInvalidError, t.DouyinError)
        assert issubclass(t.DouyinNetworkError, t.DouyinError)
        assert issubclass(t.DouyinParseError, t.DouyinError)
        assert issubclass(t.DouyinRateLimitError, t.DouyinError)

    def test_超时归为网络错误(self):
        import requests
        e = t.classify_request_error(requests.exceptions.Timeout(), "下载")
        assert isinstance(e, t.DouyinNetworkError)

    def test_连接失败归为网络错误(self):
        import requests
        e = t.classify_request_error(requests.exceptions.ConnectionError(), "下载")
        assert isinstance(e, t.DouyinNetworkError)

    def test_403归为限流(self):
        import requests
        resp = type("R", (), {"status_code": 403})()
        exc = requests.exceptions.HTTPError(response=resp)
        e = t.classify_request_error(exc, "CDN")
        assert isinstance(e, t.DouyinRateLimitError)

    def test_404归为链接失效(self):
        import requests
        resp = type("R", (), {"status_code": 404})()
        exc = requests.exceptions.HTTPError(response=resp)
        e = t.classify_request_error(exc, "解析")
        assert isinstance(e, t.DouyinLinkInvalidError)

    def test_未知异常被包装(self):
        e = t.classify_request_error(ValueError("boom"), "阶段")
        assert isinstance(e, t.DouyinError)


# =============================================================================
# mp4 校验
# =============================================================================

class TestMp4Validation:
    def test_有效mp4(self, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2000)
        assert t._validate_mp4(f) is True

    def test_非法magic(self, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"<html>error page</html>" + b"\x00" * 2000)
        assert t._validate_mp4(f) is False

    def test_文件太小(self, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"\x00\x00\x00\x18ftyp")
        assert t._validate_mp4(f) is False

    def test_文件不存在(self, tmp_path):
        assert t._validate_mp4(tmp_path / "nope.mp4") is False


# =============================================================================
# 密钥解析
# =============================================================================

class TestApiKeyResolution:
    def test_环境变量优先(self, monkeypatch, tmp_path):
        monkeypatch.setenv("API_KEY", "sk-test-from-env")
        monkeypatch.setattr(t.Path, "home", classmethod(lambda cls: tmp_path))
        assert t._load_api_key() == "sk-test-from-env"

    def test_回落到key文件(self, monkeypatch, tmp_path):
        for n in ("API_KEY", "DOUYIN_API_KEY", "SILICONFLOW_API_KEY"):
            monkeypatch.delenv(n, raising=False)
        key_dir = tmp_path / ".agents"
        key_dir.mkdir()
        (key_dir / ".siliconflow_key").write_text("sk-from-file\n", encoding="utf-8")
        monkeypatch.setattr(t.Path, "home", classmethod(lambda cls: tmp_path))
        assert t._load_api_key() == "sk-from-file"

    def test_都没有时返回空串(self, monkeypatch, tmp_path):
        for n in ("API_KEY", "DOUYIN_API_KEY", "SILICONFLOW_API_KEY"):
            monkeypatch.delenv(n, raising=False)
        monkeypatch.setattr(t.Path, "home", classmethod(lambda cls: tmp_path))
        assert t._load_api_key() == ""

    def test_脚本内未硬编码key(self):
        """防止回归：源码里不得出现字面量 key。"""
        src = SCRIPT.read_text(encoding="utf-8")
        assert "sk-" not in src, "源码中出现疑似硬编码的 API key"


# =============================================================================
# 结构化输出
# =============================================================================

class TestFormatOutput:
    def test_包含必需字段(self):
        out = t.format_output("正文内容", title="测试标题", source="https://v.douyin.com/x/",
                               description="这是一个测试描述")
        for field in ("title:", "description:", "tags:", "created:", "type:", "confidence:"):
            assert field in out, f"缺少 {field}"

    def test_frontmatter在开头(self):
        out = t.format_output("正文", title="标题")
        assert out.startswith("---\n")
        assert "type: transcript" in out

    def test_一级标题(self):
        out = t.format_output("正文", title="标题")
        assert "\n# 标题\n" in out

    def test_无来源时标注未提供(self):
        out = t.format_output("正文", title="标题", source="")
        assert "原始转发文本未提供" in out

    def test_有转发文本时附在末尾(self):
        out = t.format_output("正文", title="标题", share_text="5.30 分享文案 https://v.douyin.com/x/")
        assert "5.30 分享文案" in out
        assert "未提供" not in out

    def test_可选字段缺省时不写空行(self):
        """speaker 等视情况字段无值时应整行省略，不留空占位。"""
        out = t.format_output("正文", title="标题", speaker="")
        assert "speaker:" not in out

    def test_description缺省时整行省略(self):
        """description 缺省时不写空行，由 Agent 确认时填入。"""
        out = t.format_output("正文内容", title="标题", description="")
        assert "description:" not in out
        assert "description: " not in out


# =============================================================================
# 链接解析（纯正则部分，不触网）
# =============================================================================

class TestLinkParsing:
    def test_直接含video_id(self):
        assert t.resolve_video_id("https://www.douyin.com/video/7689421220315548971") == "7689421220315548971"

    def test_note链接也支持(self):
        assert t.resolve_video_id("https://www.douyin.com/note/7689421220315548971") == "7689421220315548971"

    def test_构造页面URL(self):
        assert t.page_url("123") == "https://www.douyin.com/video/123"

    def test_无ID时抛错(self):
        with pytest.raises(t.DouyinError):
            t.resolve_video_id("https://example.com/not-a-douyin-page")


# =============================================================================
# 架构约束
# =============================================================================

class TestArchitecture:
    def test_单文件自包含(self):
        """不得跨 Skill 依赖 —— 本 skill 必须能独立跑。

        同目录兄弟模块（naming.py）不算跨 Skill 依赖：它就在本 skill 内。
        """
        src = SCRIPT.read_text(encoding="utf-8")
        assert "from ingest" not in src
        assert "import ingest" not in src
        # sys.path 只能指向本脚本所在目录（兄弟模块），不能指向别的 Skill
        for line in src.splitlines():
            if "sys.path.insert" in line:
                assert "Path(__file__).parent" in line, \
                    f"sys.path.insert 指向了非本目录：{line.strip()}"

    def test_无yt_dlp调用(self):
        """yt-dlp 路径已死，代码中不得残留实际调用。"""
        src = SCRIPT.read_text(encoding="utf-8")
        assert "'yt-dlp'" not in src
        assert '"yt-dlp"' not in src

    def test_关键函数存在(self):
        for fn in ("resolve_video_id", "capture_stream_urls", "download", "mux",
                   "extract_audio", "transcribe", "correct_proper_nouns",
                   "format_output", "transcribe_douyin", "transcribe_local"):
            assert callable(getattr(t, fn)), f"缺少函数 {fn}"


# =============================================================================
# 幻觉重复检测
# =============================================================================

class TestHallucinationDetection:
    """铁律「原文一字不改」—— 检测器只标注，不修改文字。"""

    def test_find_repeats_长句英文x3_应命中(self):
        # 实测："I should keep on down" x 3 是短剧真实幻觉（2026-09-28）
        text = "look downI should keep on downI should keep on downI should keep on down"
        hits = t.find_repeats(text)
        assert len(hits) == 1
        assert hits[0]["times"] == 3

    def test_find_repeats_长句中文x4_应命中(self):
        text = "保护好贾世敏的保护好贾世敏的保护好贾世敏的保护好贾世敏的"
        hits = t.find_repeats(text)
        assert len(hits) == 1
        assert hits[0]["times"] == 4

    def test_find_repeats_单字x16_应命中(self):
        text = "走" * 16
        hits = t.find_repeats(text)
        assert len(hits) == 1
        assert hits[0]["times"] == 16
        assert hits[0]["unit"] == "走"

    def test_find_repeats_含标点短语_不应误报(self):
        # 单元含句末标点时被排除，不得误报
        text = "好的。走好的。走好的。走好的。走好的。走"
        hits = t.find_repeats(text)
        assert len(hits) == 0

    def test_find_repeats_叠字x3_不应误报(self):
        # 短单元需 ≥5 次才报，3 次「哈哈哈」是正常语气词
        assert len(t.find_repeats("哈哈哈哈")) == 0
        assert len(t.find_repeats("他他他来了")) == 0

    def test_find_repeats_正常转写_不应误报(self):
        text = ("我操，你们看看这是谁，76人选择了他们新的内线状元。"
                "恩彼得瘦成了闪电，勒布朗詹姆斯首次亮相。")
        assert len(t.find_repeats(text)) == 0

    def test_annotate_repeats_文字不变_只插标注(self):
        text = "出发去北京走走走走走走走走走走走走走走"
        hits = [{"unit": "走", "times": 16,
                 "start": 6, "end": 22}]
        annotated = t.annotate_repeats(text, hits)
        # 原文一字不动
        assert annotated.replace("〔⚠️ 疑似 ASR 幻觉重复 16 次，待确认〕", "") == text
        # 标注存在
        assert "疑似 ASR 幻觉重复" in annotated


# =============================================================================
# 流构成探测（ffprobe 封装）
# =============================================================================

FIXTURE_AUDIO: Path
FIXTURE_VIDEO: Path
FIXTURE_COMBO: Path


@pytest.fixture(scope="session", autouse=True)
def _media_fixtures(tmp_path_factory):
    """现场生成三个媒体样本（纯音频 / 纯视频 / 音视频合一）。

    以前靠 /tmp 里预先存在的副本，换台机器就整组测试失效。
    这里用 ffmpeg 现场生成：离线、可复现。
    """
    d = tmp_path_factory.mktemp("dy_fixtures")
    audio, video, combo = (d / "audio_only.m4a", d / "video_only.mp4",
                           d / "combined.mp4")

    def ff(*args):
        r = subprocess.run(["ffmpeg", "-v", "error", *args],
                           capture_output=True)
        if r.returncode != 0:
            pytest.skip(f"ffmpeg 不可用：{r.stderr.decode()[:120]}")

    ff("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1",
       "-c:a", "aac", "-f", "mp4", "-y", str(audio))
    ff("-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_range", "pc",
       "-an", "-y", str(video))
    ff("-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
       "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_range", "pc",
       "-c:a", "aac", "-shortest", "-y", str(combo))

    global FIXTURE_AUDIO, FIXTURE_VIDEO, FIXTURE_COMBO
    FIXTURE_AUDIO, FIXTURE_VIDEO, FIXTURE_COMBO = audio, video, combo
    yield
    shutil.rmtree(d, ignore_errors=True)


class TestProbeStreams:
    """_probe_streams(path) → (has_video, has_audio)。"""

    def test_纯音频文件(self):
        has_v, has_a = t._probe_streams(FIXTURE_AUDIO)
        assert has_v is False
        assert has_a is True

    def test_纯视频文件无音轨(self):
        has_v, has_a = t._probe_streams(FIXTURE_VIDEO)
        assert has_v is True
        assert has_a is False

    def test_合一文件(self):
        has_v, has_a = t._probe_streams(FIXTURE_COMBO)
        assert has_v is True
        assert has_a is True


# =============================================================================
# 选流逻辑（_fetch_stream）
# =============================================================================

from unittest import mock


def _mock_download(url: str, dest: Path, referer: str) -> Path:
    """根据 URL 里含的标记返回对应的 fixture 文件（不实际下载）。"""
    if "audio_fixture" in url:
        shutil.copy(FIXTURE_AUDIO, dest)
    elif "video_fixture" in url:
        shutil.copy(FIXTURE_VIDEO, dest)
    elif "combo_fixture" in url:
        shutil.copy(FIXTURE_COMBO, dest)
    else:
        raise t.DouyinError(f"未知 URL: {url}")
    return dest


class TestFetchStream:
    """_fetch_stream(candidates) 按体积降序试流、probe 真实构成。"""

    @mock.patch.object(t, "download", side_effect=_mock_download)
    def test_合一流直接返回(self, _mock, tmp_path):
        # 候选只有合一流 → 第一条命中，返回 (path, None, True)
        cands = [("http://example.com/combo_fixture", 1000)]
        main, extra, has_v = t._fetch_stream(cands, tmp_path, "http://ref")
        assert has_v is True
        assert extra is None
        # main 应该是合并后的文件（就是 fixture）
        assert main.exists()

    @mock.patch.object(t, "download", side_effect=_mock_download)
    def test_纯视频加纯音频_合并返回(self, _mock, tmp_path):
        # 候选：视频(大) + 音频(小) → 先拿视频，再拿音频合并
        # 为简化：按标记返回对应 fixture；_probe_streams 读取真实文件
        cands = [
            ("http://example.com/video_fixture", 9000),
            ("http://example.com/audio_fixture", 1000),
        ]
        main, extra, has_v = t._fetch_stream(cands, tmp_path, "http://ref")
        assert has_v is True
        assert extra is not None  # 有额外音频待合并
        assert main.exists()

    @mock.patch.object(t, "download", side_effect=_mock_download)
    def test_只有纯音频_退化为音频流(self, _mock, tmp_path):
        cands = [("http://example.com/audio_fixture", 5000)]
        main, extra, has_v = t._fetch_stream(cands, tmp_path, "http://ref")
        assert has_v is False
        assert extra is None
        assert main.exists()

    @mock.patch.object(t, "download", side_effect=_mock_download)
    def test_所有流下载失败_抛异常(self, _mock, tmp_path):
        def _fail(url, dest, ref):
            raise t.DouyinError("download fail")
        with mock.patch.object(t, "download", side_effect=_fail):
            cands = [("http://example.com/fail_fixture", 1000)]
            with pytest.raises(t.DouyinError, match="所有 CDN 候选流均下载或解析失败"):
                t._fetch_stream(cands, tmp_path, "http://ref")

    @mock.patch.object(t, "download", side_effect=_mock_download)
    def test_时长不符的合流不会被重新选中(self, _mock, tmp_path):
        with pytest.raises(t.DouyinError, match="符合目标时长"):
            t._fetch_stream(
                [("https://example.com/combo_fixture", 1000)], tmp_path,
                "https://example.com", target_duration=10.0,
            )

    @mock.patch.object(t, "download", side_effect=_mock_download)
    def test_排除错误合流后仍然接受有效分流(self, _mock, tmp_path):
        candidates = [(f"https://example.com/{kind}_fixture", 1000)
                      for kind in ("combo", "video", "audio")]
        with mock.patch.object(t, "_probe_media", side_effect=[
            (True, True, 107.8), (True, False, 114.2), (False, True, 114.2),
        ]) as probe:
            video, audio, has_video = t._fetch_stream(
                candidates, tmp_path, "https://example.com", target_duration=114.218,
            )
        assert (video.name, audio.name, has_video) == ("cand2.mp4", "cand3.mp4", True)
        assert probe.call_count == 3


# =============================================================================
# 疑词截帧验证（verify_terms.py）
# =============================================================================

import importlib.util as _ilu

_VT = Path(__file__).parent.parent / "scripts" / "verify_terms.py"
_vspec = _ilu.spec_from_file_location("verify_terms", _VT)
vt = _ilu.module_from_spec(_vspec)
_vspec.loader.exec_module(vt)


class TestVerifyTermsMapping:
    """时间轴映射 + 密集采样 + 命名。"""

    TEXT = "前面的铺垫内容" * 20 + "目标疑词" + "后面的收尾内容" * 20

    def test_按字符位置估算时间(self):
        plan = vt.estimate_times(["目标疑词"], self.TEXT, 100.0, window=0, step=999)
        ts = plan["目标疑词"]
        assert len(ts) == 1
        # 目标词在正中间 → 约 50s
        assert 45 < ts[0] < 55

    def test_密集采样覆盖窗口(self):
        """实测单点抽帧会落空 7s，必须 ±window 密集采。"""
        plan = vt.estimate_times(["目标疑词"], self.TEXT, 100.0, window=12, step=2)
        ts = plan["目标疑词"]
        assert len(ts) > 10, "采样点太少，仍会落空"
        assert min(ts) < 50 < max(ts), "采样窗口未覆盖估算点"

    def test_采样不越界(self):
        plan = vt.estimate_times(["目标疑词"], "目标疑词", 10.0, window=12, step=2)
        assert min(plan["目标疑词"]) >= 0
        assert max(plan["目标疑词"]) <= 10.0

    def test_词不在文本中_仍给采样点(self):
        """查不到的词也要给候选帧，否则直接漏检。"""
        plan = vt.estimate_times(["不存在的词"], self.TEXT, 100.0, window=2, step=2)
        assert len(plan["不存在的词"]) >= 1

    def test_分段时间戳覆盖疑词的多次出现(self):
        segments = [{"start": 8.2, "end": 9.1, "text": "第一个目标疑词"},
                    {"start": 30.6, "end": 32.0, "text": "再次目标疑词"}]
        assert vt.segment_times("目标疑词", segments, 45, 1) == [
            7, 8, 9, 10, 11, 29, 30, 31, 32, 33,
        ]

    def test_slugify_保留中文(self):
        assert vt.slugify("申屠电网") == "申屠电网"

    def test_slugify_处理空格与符号(self):
        assert vt.slugify("n twogaM") == "n_twogaM"
        assert vt.slugify("work-body!") == "work_body"

    def test_slugify_空词兜底(self):
        assert vt.slugify("!!!") == "term"


class TestVerifyTermsFFmpeg:
    """真跑 ffmpeg，验产物结构。"""

    @pytest.fixture
    def video(self, tmp_path):
        v = tmp_path / "v.mp4"
        # -color_range pc 必加：ffmpeg 8.x 的 mjpeg 编码器拒绝
        # non-full-range YUV（"Non full-range YUV is non-standard"），
        # 缺这行则连不加工具滤镜都存不出 jpg。已实测验证。
        r = __import__("subprocess").run(
            ["ffmpeg", "-v", "error", "-f", "lavfi",
             "-i", "testsrc=duration=3:size=320x240:rate=5",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-color_range", "pc", "-y", str(v)],
            capture_output=True)
        if r.returncode != 0:
            pytest.skip("ffmpeg 不可用")
        return v

    def test_产出全帧与字幕条(self, video, tmp_path):
        out = tmp_path / "frames"
        got = vt.grab(video, 1.0, out, "测试", 320, 240)
        assert len(got) == 2
        full, sub = got
        assert full.exists() and sub.exists()
        assert full.name == "测试_1.jpg"
        assert sub.name == "测试_1_sub.jpg"
        assert full.stat().st_size > 0 and sub.stat().st_size > 0

    def test_两个疑词共享同一时间点的帧(self, video, tmp_path, monkeypatch):
        raw = tmp_path / "transcript_raw.md"
        raw.write_text("同一段 NotebookLM 和 Codex", encoding="utf-8")
        raw.with_name("transcript_segments.json").write_text(
            '{"segments":[{"start":0.5,"end":1.5,"text":"同一段 NotebookLM 和 Codex"}]}',
            encoding="utf-8",
        )
        out = tmp_path / "frames"
        monkeypatch.setattr(sys, "argv", [
            "verify_terms.py", "--video", str(video), "--text", str(raw),
            "--terms", "NotebookLM,Codex", "--out", str(out),
        ])
        assert vt.main() == 0
        terms = json.loads((out / "manifest.json").read_text(encoding="utf-8"))["terms"]
        assert terms["NotebookLM"]["method"] == "asr_segments"
        assert terms["NotebookLM"]["frames"] == terms["Codex"]["frames"]
        assert len(list(out.glob("*.jpg"))) == len(terms["NotebookLM"]["frames"])


    def test_字幕条为底部裁剪(self, video, tmp_path):
        """字幕条是「宽而扁」的横带，不是全帧。

        注意：裁剪后又被 scale 拉到 720 宽，高度会按比例放大
        （320x52 → 720x117），所以不能断言绝对高度，
        只能断言宽高比显著小于原视频。
        """
        out = tmp_path / "frames"
        _, sub = vt.grab(video, 1.0, out, "t", 320, 240)
        w, h = vt.probe_size(sub)
        assert w == 720, "字幕条未缩放到 720 宽"
        assert h / w < 0.3, f"字幕条不是横带（{w}x{h}）"
        # 原视频 240/320 = 0.75，裁剪后应降到 1/3 以下
        assert h / w < 240 / 320 / 2

    def test_探测时长与尺寸(self, video):
        dur = vt.probe_duration(video)
        w, h = vt.probe_size(video)
        assert 2.5 < dur < 3.5
        assert (w, h) == (320, 240)


def test_正常Whisper只启动一次且原文保留正确术语(tmp_path, monkeypatch):
    video = tmp_path / "source.mp4"
    video.write_bytes(FIXTURE_COMBO.read_bytes())
    monkeypatch.setattr(t, "API_KEY", "offline-test-key")
    monkeypatch.setattr(t, "extract_audio", lambda _, audio: audio.write_bytes(b"test"))
    calls = []
    monkeypatch.setattr(t, "_probe_media", lambda _: (True, True, 12.0))
    monkeypatch.setattr(t, "_whisper_bin", lambda: "whisper")

    def run(cmd, **kwargs):
        calls.append(cmd)
        output_dir = Path(cmd[cmd.index("--output_dir") + 1])
        (output_dir / "audio.json").write_text(
            '{"text":"IPS面板", "segments":[{"start":0.0,"end":1.0,"text":"IPS面板"}]}',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(t.subprocess, "run", run)
    result = t.transcribe_local(video, output_dir=tmp_path, engine="whisper")
    assert len(calls) == 1
    assert "--help" not in calls[0]
    assert result["asr_engine"] == "whisper"
    assert result["segments"] == [{"start": 0.0, "end": 1.0, "text": "IPS面板"}]
    assert "IPS面板" in Path(result["markdown_path"]).read_text(encoding="utf-8")
    assert result["correction_candidates"][0]["suggestion"] == "FPS"


def test_云端超限跳过上传并转本地(monkeypatch):
    audio = mock.Mock(spec=Path)
    audio.stat.return_value.st_size = t.SILICONFLOW_MAX_AUDIO_BYTES + 1
    monkeypatch.setattr(t, "API_KEY", "offline-test-key")
    monkeypatch.setattr(t, "_transcribe_whisper_result",
                        lambda _: t.ASRResult("录音稿", "whisper", []))
    timings = {}
    with mock.patch.object(t.requests, "post") as post:
        assert t.transcribe_result(audio, "auto", timings).engine == "whisper"
        post.assert_not_called()
    assert timings.keys() == {"sensevoice", "whisper"}


# =============================================================================
# 字幕带几何（2026-10-30 修复：横向不再裁切）
# =============================================================================

class TestSubtitleBandGeometry:
    """字幕带必须覆盖**整幅画面宽度**。

    中文烧录字幕水平居中，横向裁左边会把字幕右半截切掉：
    实测 1280 宽视频上「就是把资料一股脑丢给AI」被裁成「就是把资料一股月」。
    纵向安全边必须按画面高度取比例 —— 固定 150px 在矮视频上会吃掉整帧。
    """

    def test_安全边按比例而非固定像素(self):
        assert not hasattr(vt, "SUB_SAFE"), "固定像素安全边已废弃"
        assert vt.SUB_SAFE_RATIO > 0

    @pytest.mark.parametrize("w,h", [(1080, 1920), (1280, 720), (320, 240)])
    def test_字幕带占画高约三成且不超整帧(self, w, h):
        band = int(h * vt.SUB_RATIO) + int(h * vt.SUB_SAFE_RATIO)
        assert band < h, "字幕带不能等于整帧"
        assert 0.25 <= band / h <= 0.35, f"字幕带占比异常：{band / h:.2f}"

    def test_横向不裁切(self, tmp_path):
        """裁剪参数里不能有横向 crop 起点 —— 必须整幅缩放。"""
        video = tmp_path / "wide.mp4"
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi",
             "-i", "testsrc=duration=1:size=1280x720:rate=5",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_range", "pc",
             "-y", str(video)], capture_output=True)
        if r.returncode != 0:
            pytest.skip("ffmpeg 不可用")
        _, sub = vt.grab(video, 0.5, tmp_path / "f", "g", 1280, 720)
        sw, sh = vt.probe_size(sub)
        # 整幅 1280 宽缩到 720 → 高度按比例，不做横向裁剪
        assert sw == 720
        expected_h = round((int(720 * vt.SUB_RATIO)
                            + int(720 * vt.SUB_SAFE_RATIO)) * 720 / 1280)
        assert abs(sh - expected_h) <= 2, f"字幕带高度异常 {sh} != {expected_h}"


# =============================================================================
# 无口播判定（纯 BGM 视频的 ASR 输出是幻觉碎片）
# =============================================================================

class TestSpeechRate:
    """speech_rate() 用「有效字符/秒」区分真人口播和纯 BGM。

    实测 11 个真实抖音视频：无人声 0.03–0.47 字/秒，有人声 4.55–8.11 字/秒。
    """

    def test_真人说话不算无口播(self):
        # 用实测口播样本的比例（约 5 字/秒）：33 字 ≈ 6.6s
        _, rate, no_speech = t.speech_rate(
            "兄弟们，贾维斯2.0已经正式上线了，我已经融入了我的生活当中。", 6.6)
        assert rate > t.NO_SPEECH_RATE
        assert no_speech is False

    def test_真人说话按实测全文比例(self):
        """实测样本（真实转写全文，336 有效字 / 73.7s = 4.56 字/秒 → 有人声）。

        用完整转写而非片段：片段长度配全长时长会把语速算低，
        让人声样本误判成无口播。
        """
        text = (
            "兄弟们，贾维斯2.0已经正式上线，他已经融入到我的生活当中了。"
            "我明天想去一下大唐不夜城，帮我导个航吧，我想看看堵不堵车。"
            "From Park QR performance about 38 minutes by car, roughly 20.6 km, "
            "arriving at 21432 looks reasonable just now, The route and the QR "
            "code for fun navigation are on your screen Sir我.我家有多少个你能"
            "控制的设备，请帮我打开阳台晾架的灯光。真的现在已经融入到我的生活"
            "当中了。我知道多智能。贾维不断定制的东西。虽然我现在用的是投影仪，"
            "但是我相信。全击特影来的那一天，他就是真正的家。宝马晾衣架灯光关了"
            "吧。The drawing rack light is off now, Sir.Goodod nightイズベス."
        )
        eff, rate, no_speech = t.speech_rate(text, 73.7)
        assert eff > 300, f"样本应接近真实全文 336 字，实际 {eff}"
        assert rate > t.NO_SPEECH_RATE
        assert no_speech is False

    def test_纯BGM判为无口播(self):
        _, rate, no_speech = t.speech_rate("Yeah.失实的。.", 15.0)
        assert rate < t.NO_SPEECH_RATE
        assert no_speech is True

    def test_音乐标记不计入有效字符(self):
        a = t.speech_rate("🎼。 🎼。.🎼。。", 210.0)
        b = t.speech_rate("。 。.。。", 210.0)
        assert a[0] == b[0], "🎼 不应计入有效字符"

    def test_过短片段不判定(self):
        """几秒的片段字数天然少，不能据此报「无口播」。"""
        _, _, no_speech = t.speech_rate("大家好", 3.0)
        assert no_speech is False

    def test_阈值有余量(self):
        """阈值要落在两组实测数据的中间，两侧都有余量。"""
        assert t.NO_SPEECH_RATE > 0.5      # 高于无人声组最大值 0.47
        assert t.NO_SPEECH_RATE < 4.0      # 低于有人声组最小值 4.55


# =============================================================================
# 标题取值规则（2026-10-30 修复：页面身份校验失败时不得采信 page_title）
# =============================================================================

class TestResolveTitle:
    """页面身份校验失败时，page_title 属于**另一个**视频，不能用。

    实测：抖音渲染成「jingxuan」页时，page_title 是
    「天地之大 困住你的是什么」，而实际下载的是
    「给自己的网站下了一场大雨」—— 标题对不上比标题难看更糟。
    """

    def test_显式标题最优先(self):
        assert t.resolve_title("我传的标题", auto_title="推导标题",
                               page_title="页面标题", video_id="123") == "我传的标题"

    def test_页面推导标题次之(self):
        assert t.resolve_title(auto_title="推导标题", page_title="页面标题",
                               video_id="123") == "推导标题"

    def test_身份正常时用页面标题并去掉话题标签(self):
        got = t.resolve_title(page_title="用html做PPT #vibecoding #AI教程",
                              page_identity_ok=True, video_id="123")
        assert got == "用html做PPT"

    def test_身份校验失败时不用页面标题(self):
        got = t.resolve_title(page_title="天地之大 困住你的是什么",
                              page_identity_ok=False, video_id="7700647620745735759")
        assert got == "7700647620745735759", "身份校验失败时只能退回 video_id"
        assert "天地之大" not in got

    def test_身份失败时显式标题仍然优先(self):
        got = t.resolve_title(explicit="我传的", page_title="别人的标题",
                              page_identity_ok=False, video_id="123")
        assert got == "我传的"
