#!/usr/bin/env python3
"""
douyin-transcribe-text 单元测试。

覆盖：专有名词校正 / 疑词检测 / 错误分类 / mp4 校验 / 密钥解析 / 结构化输出。
全部离线，不触网、不调用转写 API。

运行：
    python3 -m pytest ~/.agents/skills/douyin-transcribe-text/tests/ -v
"""

import importlib.util
import os
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
        """不得再有跨目录 import —— 这是本次合并的核心目标。"""
        src = SCRIPT.read_text(encoding="utf-8")
        assert "sys.path.insert" not in src
        assert "from ingest" not in src
        assert "import ingest" not in src

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

FIXTURE_AUDIO = Path("/tmp/test_audio_only.m4a")
FIXTURE_VIDEO = Path("/tmp/test_video_only.mp4")
FIXTURE_COMBO = Path("/tmp/test_combined.mp4")


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
        import shutil
        shutil.copy(FIXTURE_AUDIO, dest)
    elif "video_fixture" in url:
        import shutil
        shutil.copy(FIXTURE_VIDEO, dest)
    elif "combo_fixture" in url:
        import shutil
        shutil.copy(FIXTURE_COMBO, dest)
    else:
        raise t.DouyinError(f"未知 URL: {url}")
    return dest


class TestFetchStream:
    """_fetch_stream(candidates) 按体积降序试流、probe 真实构成。"""

    @mock.patch.object(t, "download", side_effect=_mock_download)
    def test_合一流直接返回(self, _mock):
        # 候选只有合一流 → 第一条命中，返回 (path, None, True)
        cands = [("http://example.com/combo_fixture", 1000)]
        main, extra, has_v = t._fetch_stream(cands, Path("/tmp"), "http://ref")
        assert has_v is True
        assert extra is None
        # main 应该是合并后的文件（就是 fixture）
        assert main.exists()

    @mock.patch.object(t, "download", side_effect=_mock_download)
    def test_纯视频加纯音频_合并返回(self, _mock):
        # 候选：视频(大) + 音频(小) → 先拿视频，再拿音频合并
        # 为简化：按标记返回对应 fixture；_probe_streams 读取真实文件
        cands = [
            ("http://example.com/video_fixture", 9000),
            ("http://example.com/audio_fixture", 1000),
        ]
        main, extra, has_v = t._fetch_stream(cands, Path("/tmp"), "http://ref")
        assert has_v is True
        assert extra is not None  # 有额外音频待合并
        assert main.exists()

    @mock.patch.object(t, "download", side_effect=_mock_download)
    def test_只有纯音频_退化为音频流(self, _mock):
        cands = [("http://example.com/audio_fixture", 5000)]
        main, extra, has_v = t._fetch_stream(cands, Path("/tmp"), "http://ref")
        assert has_v is False
        assert extra is None
        assert main.exists()

    @mock.patch.object(t, "download", side_effect=_mock_download)
    def test_所有流下载失败_抛异常(self, _mock):
        def _fail(url, dest, ref):
            raise t.DouyinError("download fail")
        with mock.patch.object(t, "download", side_effect=_fail):
            cands = [("http://example.com/fail_fixture", 1000)]
            with pytest.raises(t.DouyinError, match="所有 CDN 候选流均下载或解析失败"):
                t._fetch_stream(cands, Path("/tmp"), "http://ref")


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
