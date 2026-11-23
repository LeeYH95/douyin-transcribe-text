#!/usr/bin/env python3
"""
抖音视频 → 逐字稿 → 结构化 Markdown 全流程（单文件自包含）。

本脚本由原 douyin-ingest + douyin-transcribe-text 合并而来，不依赖任何其他 Skill。

Pipeline（2026-09 实测验证）：
  短链解析 → Playwright 渲染 + 触发播放
  → 网络拦截捕获 CDN 直链 → 下载
  → ffmpeg 合成 mp4 → 音频提取
  → 硅基流动 SenseVoice 转写（失败降级本地 Whisper）
  → 专有名词校正 → 结构化输出

用法：
    python3 transcribe.py "<抖音链接>"
    python3 transcribe.py "<链接>" --check-link
    python3 transcribe.py "<链接>" --headful
    python3 transcribe.py --video /path/to/local.mp4
    python3 transcribe.py "<链接>" --output /tmp/dy/
    python3 transcribe.py --video in.mp4 --frames 68,231,253,285

关于匿名访问的准确说明（不要误读）：
  抖音的媒体接口需要 a_bogus / msToken 等动态签名参数，纯 HTTP 下载器
  （yt-dlp 等）没有 JS 引擎算不出来，因此匿名下载会 403。
  本脚本用真实 Chromium 渲染让抖音自己的 JS 算出签名，所以能下载。
  这不等于使用登录凭据 —— 详见 SKILL.md「安全边界」一节。
"""

import argparse
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

# 目录命名模块（<主题>_<作者>_<日期>）—— 与本脚本同目录
sys.path.insert(0, str(Path(__file__).parent))
import naming  # noqa: E402

# =============================================================================
# 错误分类
# =============================================================================

class DouyinError(Exception):
    """抖音处理错误的基类"""


class DouyinLinkInvalidError(DouyinError):
    """链接失效（视频被删除 / 私密 / 不存在）"""


class DouyinParseError(DouyinError):
    """解析失败（页面结构变更）"""


class DouyinNetworkError(DouyinError):
    """网络错误（超时 / DNS / 连接失败）"""


class DouyinRateLimitError(DouyinError):
    """反爬或上游 API 限流（403/429）"""


# =============================================================================
# 配置
# =============================================================================

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/140.0.0.0 Safari/537.36',
    'Referer': 'https://www.douyin.com/',
}

UA = HEADERS['User-Agent']

# ---- 落盘位置：环境变量指定的外接盘优先，未指定/不可写时回退内置盘 ----
# 约定：所有转写下载的视频/图片优先放外接盘，不占内置盘。
# 外接盘路径不写死 —— 用环境变量 OPENCLAW_MEDIA_ROOT 指定，
# 例如 OPENCLAW_MEDIA_ROOT="/Volumes/YourSSD/06_媒体转写"。
MEDIA_ROOT_ENV = "OPENCLAW_MEDIA_ROOT"
INTERNAL_MEDIA_ROOT = Path.home() / "Downloads"


def _resolve_media_root(subdir: str) -> Path:
    """返回 <媒体根>/<subdir>；外接盘未挂载或不可写时回退内置盘 Downloads。

    用环境变量 OPENCLAW_MEDIA_ROOT 指定外接盘媒体根目录；
    未设置或不可写时回退 ~/Downloads。
    """
    override = os.environ.get(MEDIA_ROOT_ENV)
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(INTERNAL_MEDIA_ROOT)

    for base in candidates:
        target = base / subdir
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".write_test"
            probe.touch()
            probe.unlink()
            return target
        except OSError:
            continue
    # 全部不可写时仍返回一个路径，让调用方报错而不是崩在 import 阶段
    return candidates[0] / subdir


DEFAULT_OUTPUT = _resolve_media_root("抖音")
# Chromium profile 留在内置盘：外接卷上的文件锁/沙箱容易出问题，
# 且它属于工具产物而非下载的媒体，不在「不占内置盘」的范围内。
DEFAULT_PROFILE = Path.home() / ".cache/openclaw/chrome_profiles/douyin"

CDN_HOSTS = (
    "douyinvod.com", "bytecdn.cn", "zjcdn.com", "ixigua.com",
    "pstatp.com", "snssdk.com", "amemv.com", "bytedance.com",
)


def _load_api_key() -> str:
    """取硅基流动 key：环境变量优先，其次 ~/.agents/.siliconflow_key。"""
    for name in ('API_KEY', 'DOUYIN_API_KEY', 'SILICONFLOW_API_KEY'):
        value = os.environ.get(name)
        if value:
            return value.strip()
    key_file = Path.home() / '.agents' / '.siliconflow_key'
    if key_file.exists():
        return key_file.read_text(encoding='utf-8').strip()
    return ''


API_KEY = _load_api_key()


# =============================================================================
# 工具
# =============================================================================

def retry_with_backoff(max_retries=3, base_delay=1.0, max_delay=10.0,
                       exceptions=(requests.exceptions.RequestException,)):
    """指数退避重试装饰器"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        print(f"⚠️  第 {attempt+1}/{max_retries} 次失败: "
                              f"{type(e).__name__}，{delay:.1f}s 后重试",
                              file=sys.stderr)
                        time.sleep(delay)
            raise last_exception
        return wrapper
    return decorator


def classify_request_error(e: Exception, context: str = "") -> DouyinError:
    """把 requests 异常分类成具体的 DouyinError"""
    if isinstance(e, requests.exceptions.Timeout):
        return DouyinNetworkError(f"{context}超时: {e}")
    if isinstance(e, requests.exceptions.ConnectionError):
        return DouyinNetworkError(f"{context}连接失败: {e}")
    if isinstance(e, requests.exceptions.HTTPError):
        status = e.response.status_code if e.response is not None else "unknown"
        if status == 404:
            return DouyinLinkInvalidError(f"{context}链接不存在 (404)，视频可能已被删除")
        if status == 403:
            return DouyinRateLimitError(f"{context}访问被拒 (403)，可能触发反爬")
        if status == 429:
            return DouyinRateLimitError(f"{context}请求过快 (429)，被限流")
        return DouyinError(f"{context}HTTP {status}: {e}")
    return DouyinError(f"{context}未知错误: {type(e).__name__}: {e}")


def _validate_mp4(filepath: Path) -> bool:
    """校验 mp4 文件完整性（magic bytes + 最小尺寸）"""
    if not filepath.exists():
        return False
    if filepath.stat().st_size < 1024:
        return False
    try:
        with open(filepath, 'rb') as f:
            header = f.read(12)
            return header[4:8] == b'ftyp'
    except Exception:
        return False


# =============================================================================
# 1. 链接解析
# =============================================================================

@retry_with_backoff(max_retries=3, base_delay=2.0)
def resolve_video_id(link: str) -> str:
    link = link.strip()
    m = re.search(r"/(?:video|note)/(\d+)", link)
    if m:
        return m.group(1)
    try:
        r = requests.get(link, headers=HEADERS, timeout=(10, 30),
                         allow_redirects=True)
    except requests.exceptions.RequestException as e:
        raise classify_request_error(e, "短链跳转")
    m = re.search(r"/(?:video|note)/(\d+)", r.url)
    if m:
        return m.group(1)
    raise DouyinLinkInvalidError(f"无法从链接提取 video_id: {r.url}")


def page_url(vid: str) -> str:
    return f"https://www.douyin.com/video/{vid}"


# =============================================================================
# 2. Playwright 渲染 + 网络拦截
# =============================================================================

def capture_stream_urls(video_id: str, profile_dir: Path,
                        headful: bool = False) -> dict:
    """渲染抖音页面并拦截 CDN 媒体请求。

    profile_dir 是专用独立 profile，不接触用户日常浏览器的任何数据。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise DouyinError("未安装 playwright：pip install playwright "
                          "&& python3 -m playwright install chromium")

    url = page_url(video_id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    found = {"video": [], "audio": [], "other": [], "sizes": {}}

    with sync_playwright() as p:
        # Playwright 自带的 chromium 可能没装（playwright install chromium 未跑过）。
        # 回退到系统 Chrome —— 抖音要真实 Chromium 算 a_bogus 签名，
        # 系统 Chrome 与内置 chromium 等价，且省掉几百 MB 下载。
        launch_kw = dict(
            user_data_dir=str(profile_dir),
            headless=not headful,
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled",
                  "--autoplay-policy=no-user-gesture-required"],
        )
        try:
            ctx = p.chromium.launch_persistent_context(**launch_kw)
        except Exception as e:
            if "Executable doesn't exist" not in str(e):
                raise
            print("⚠️  内置 chromium 缺失，回退系统 Chrome", file=sys.stderr)
            ctx = p.chromium.launch_persistent_context(channel="chrome", **launch_kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_response(resp):
            u = resp.url
            if not any(h in u for h in CDN_HOSTS):
                return
            ctype = (resp.headers or {}).get("content-type", "").lower()
            # ⚠️ 抖音的 audio / video / 合一流，content-type 全部报 video/mp4
            # （实测 2026-09-29，三条流 ct 全同）。靠 content-type 区分音视频无效，
            # 这里只做粗筛；真正的音视频判定交给 download_video_stream() 的 ffprobe。
            if "video" in ctype or "audio" in ctype or "mp4" in ctype:
                found["video"].append(u)
                try:
                    found["sizes"][u] = int((resp.headers or {}).get("content-length") or 0)
                except (TypeError, ValueError):
                    found["sizes"][u] = 0
            else:
                found["other"].append(u)

        # ── 校验：拦流之前先验明正身 ──────────────────────────────────
        # 关键修复（2026-10-01）：删掉了「mouse.wheel 向下滚 3 次」。
        # 抖音单视频页向下滚动会预载推荐流，滚动期间发出的 CDN 媒体请求
        # 属于推荐视频而非目标视频 → 静默下错文件（实测抓成天安门升旗，
        # 目标却是福布斯专访）。不滚动 + 只触发目标视频播放即可。
        #
        # 同时保留一道廉价的跳转校验：页面 URL 里的 id 必须仍是目标 id。
        #
        # ⚠️ 不要校验 video 元素的 src 是否是 CDN 域名：抖音用 MSE 分片
        # 推流，currentSrc 正常形态就是 blob:https://www.douyin.com/...，
        # 拿它当错误条件会把正常链路误杀（2026-10-01 已踩）。
        # 真实媒体分片本来就是靠拦截 CDN 响应拿的，与 video.src 无关。
        page.on("response", on_response)
        print(f"🌐  打开页面: {url}", file=sys.stderr)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)

        # 1. 页面 URL 里的 aweme_id 必须匹配目标 video_id
        current_url = page.url or ""
        import re
        page_vid_match = re.search(r'/video/(\d+)', current_url)
        page_vid = page_vid_match.group(1) if page_vid_match else ""
        if page_vid and page_vid != video_id:
            raise DouyinError(
                f"[校验失败] 页面跳转了：目标 id={video_id}，实际 id={page_vid}\n"
                f"  可能是视频已下架、被封禁、或页面发生了重定向。\n"
                f"  当前 URL: {current_url}"
            )

        print(f"✅ 视频 id 校验通过 (id={video_id})", file=sys.stderr)

        # ── 触发播放（仅在目标视频上操作，不会碰到推荐视频） ──────────────
        print("▶️  触发播放", file=sys.stderr)
        for sel in ("video", "div[class*='player']", "span[class*='play']"):
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=1500)
                    break
            except Exception:
                continue
        try:
            page.evaluate(
                "() => { document.querySelectorAll('video')"
                ".forEach(v => { v.muted = true; v.play().catch(()=>{}); }); }")
        except Exception:
            pass
        try:
            page.keyboard.press("Space")
        except Exception:
            pass

        for _ in range(6):
            page.wait_for_timeout(1500)
            if found["video"]:
                break

        title = ""
        try:
            title = page.title() or ""
        except Exception:
            pass

        # ── 页面 meta ────────────────────────────────────────────────
        # 抖音的 og:title 常为空；真正的信息在 description 里：
        #   「<标题> - <作者>于20261025发布在抖音，已经收获了4008个喜欢…」
        # 目录命名要用的主题/作者/日期全靠它（约定 2026-10-28）。
        og_title = ""
        desc = ""
        canonical = ""
        location = ""
        try:
            metas = page.evaluate(
                "() => { const o = {}; document.querySelectorAll('meta')"
                ".forEach(m => { const k = m.getAttribute('property')"
                " || m.getAttribute('name');"
                " if (k) o[k] = m.getAttribute('content') || ''; });"
                " const c = document.querySelector('link[rel=\"canonical\"]');"
                " o.__canonical = c ? c.href : '';"
                " o.__location = location.href; return o; }")
            og_title = metas.get("og:title", "") or ""
            desc = metas.get("description", "") or ""
            canonical = metas.get("__canonical", "") or ""
            location = metas.get("__location", "") or ""
        except Exception:
            pass

        # ── 采集目标视频时长（判别目标视频的唯一硬指标）────────────────
        # 页面 DOM 里 <video>.duration 就是这条视频的真实时长。
        # 抖音单视频页会预载并自动播放**其他**视频（推荐流 / 相邻作品），
        # 那些流同样是「音视频合一」且体积最大——不拿时长校验就会静默下错文件
        # （实测 2026-10-27：目标 114.218s，却下到另一条 PvZ 配音的 107.8s）。
        # ⚠️ 不要用 <video> 的 src 做校验：抖音走 MSE 推流，currentSrc 正常
        # 形态就是 blob:https://www.douyin.com/...（2026-10-01 已踩）。
        target_duration = 0.0
        try:
            durs = page.evaluate(
                "() => [...document.querySelectorAll('video')]"
                ".map(v => v.duration || 0).filter(d => d > 0)")
            if durs:
                target_duration = float(max(durs))
        except Exception:
            pass
        if target_duration:
            print(f"🎯 目标时长 {target_duration:.3f}s（用于校验候选流）",
                  file=sys.stderr)

        needs_login = False
        try:
            body = page.inner_text("body")[:500]
            needs_login = ("登录" in body and "验证" in body)
        except Exception:
            pass

        ctx.close()

    return {
        "video": found["video"],
        "audio": found["audio"],
        "other": found["other"],
        "sizes": found["sizes"],
        "title": title,
        "og_title": og_title,
        "desc": desc,
        "canonical": canonical,
        "location": location,
        "needs_login": needs_login,
        "target_duration": target_duration,
    }


# =============================================================================
# 3. 下载 + ffmpeg 合成
# =============================================================================

def download(url: str, dest: Path, referer: str) -> Path:
    """下载 CDN 直链。签名有时效（10-30 分钟），捕获后应立即下载。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = dict(HEADERS)
    h["Referer"] = referer
    try:
        r = requests.get(url, headers=h, stream=True, timeout=(10, 120))
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
    except requests.exceptions.RequestException as e:
        dest.unlink(missing_ok=True)
        raise classify_request_error(e, "CDN 下载")
    if dest.stat().st_size < 1024:
        dest.unlink(missing_ok=True)
        raise DouyinError("CDN 返回内容过小，签名可能已过期")
    return dest


def mux(video: Path, audio: Path | None, out: Path) -> Path:
    """合成 mp4。注意：抖音给的就是含音轨的合并单条 mp4，通常直接 copy。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    if audio and audio.exists() and audio.stat().st_size > 1024:
        cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(audio),
               "-c", "copy", "-map", "0:v:0", "-map", "1:a:0", str(out)]
    else:
        cmd = ["ffmpeg", "-y", "-i", str(video), "-c", "copy", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not _validate_mp4(out):
        out.unlink(missing_ok=True)
        raise DouyinError(f"ffmpeg 合成失败: {r.stderr[-400:]}")
    return out


# =============================================================================
# 4. 音频提取
# =============================================================================

def extract_audio(video_path: Path, audio_path: Path) -> Path:
    """用 ffmpeg 从视频提取音频（16kHz mono WAV），ffmpeg-python 缺失时降级 CLI"""
    audio_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import ffmpeg  # type: ignore
        (
            ffmpeg
            .input(str(video_path))
            .output(str(audio_path), acodec='pcm_s16le', ar=16000, ac=1)
            .run(capture_stdout=True, capture_stderr=True, overwrite_output=True)
        )
        return audio_path
    except ImportError:
        print("⚠️  ffmpeg-python 未安装，降级到 subprocess 调用 ffmpeg CLI",
              file=sys.stderr)
    except Exception as e:
        print(f"⚠️  ffmpeg-python 失败（{type(e).__name__}），降级到 subprocess",
              file=sys.stderr)

    cmd = [
        'ffmpeg', '-y', '-i', str(video_path),
        '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        str(audio_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise DouyinError(f"ffmpeg 提取音频失败（exit {result.returncode}），"
                          "请确认输入视频包含音轨")
    return audio_path


# =============================================================================
# 5. 转写（硅基流动优先，失败降级本地 Whisper）
# =============================================================================

def transcribe_siliconflow(audio_path: Path) -> str:
    """调用硅基流动 SenseVoice API 转写"""
    if not API_KEY:
        raise DouyinError("未设置 API_KEY 环境变量，"
                          "也未找到 ~/.agents/.siliconflow_key")

    with open(audio_path, 'rb') as f:
        files = {'file': ('audio.wav', f, 'audio/wav')}
        data = {
            'model': 'FunAudioLLM/SenseVoiceSmall',
            'language_hint': 'zh',
            'task': 'transcribe',
        }
        headers = {'Authorization': f'Bearer {API_KEY}'}
        resp = requests.post(
            'https://api.siliconflow.cn/v1/audio/transcriptions',
            files=files, data=data, headers=headers,
            timeout=300,
        )

    if resp.status_code != 200:
        if resp.status_code == 429:
            raise DouyinRateLimitError("硅基流动转写限流 (429)")
        if resp.status_code == 401:
            raise DouyinError("硅基流动鉴权失败 (401)，请更新 ~/.agents/.siliconflow_key")
        raise DouyinError(f"硅基流动转写失败（HTTP {resp.status_code}）")

    return resp.json().get('text', '')


WHISPER_VENV = Path.home() / ".agents/venvs/whisper/bin/whisper"


def _whisper_bin() -> str:
    """定位 whisper 可执行文件。

    优先用自建 venv（~/.agents/venvs/whisper）：Homebrew 装的 openai-whisper
    依赖 libtorch，而 libtorch 又硬链到某个特定版本的 abseil，Homebrew 一升级
    abseil 就会 dlopen 失败。venv 里装的是 PyPI 的 torch wheel，不受此影响。
    """
    if WHISPER_VENV.exists():
        return str(WHISPER_VENV)
    found = shutil.which('whisper')
    if found:
        return found
    raise DouyinError(
        "本地 Whisper 不可用。已尝试 venv 和 PATH 均未找到 whisper，"
        f"请先安装：python3 -m venv {WHISPER_VENV.parent} "
        f"&& {WHISPER_VENV.parent}/bin/pip install openai-whisper"
    )


def _whisper_environment() -> dict:
    """确认 whisper 能启动；顺带尝试绕过 Homebrew 的 abseil 版本冲突。"""
    environment = os.environ.copy()
    try:
        probe = subprocess.run(
            [_whisper_bin(), '--help'], capture_output=True, text=True, timeout=60,
            env=environment,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise DouyinError("本地 Whisper 无法启动，请检查安装") from error
    if probe.returncode == 0:
        return environment

    missing = re.search(
        r'Library not loaded: /opt/homebrew/opt/abseil/lib/(libabsl_[^\s/]+\.dylib)',
        probe.stderr,
    )
    if missing and sys.platform == 'darwin':
        for library in Path('/opt/homebrew/Cellar/abseil').glob(f'*/lib/{missing.group(1)}'):
            compatible = environment.copy()
            previous = compatible.get('DYLD_LIBRARY_PATH')
            compatible['DYLD_LIBRARY_PATH'] = (
                str(library.parent) + (f':{previous}' if previous else ''))
            check = subprocess.run(
                ['whisper', '--help'], capture_output=True, text=True, timeout=20,
                env=compatible,
            )
            if check.returncode == 0:
                return compatible
    raise DouyinError("本地 Whisper 依赖损坏，请修复 PyTorch 与 Abseil 的版本匹配")


def transcribe_whisper(audio_path: Path) -> str:
    """降级方案：本地 Whisper 转写（M4 实测 60s 音频 ~35s）。

    默认 large-v3-turbo：比 small 快且专有名词更准（Codex / skill / agent
    识别率明显高于 small）。模型已缓存在 ~/.cache/whisper/。
    """
    output_dir = audio_path.parent / "whisper_out"
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        _whisper_bin(), str(audio_path),
        '--language', os.environ.get('DOUYIN_WHISPER_LANGUAGE', 'Chinese'),
        '--model', os.environ.get('DOUYIN_WHISPER_MODEL', 'large-v3-turbo'),
        '--output_dir', str(output_dir), '--output_format', 'txt',
        '--fp16', 'False',
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800,
            env=_whisper_environment(),
        )
    except subprocess.TimeoutExpired as error:
        raise DouyinError("本地 Whisper 转写超时") from error
    if result.returncode != 0:
        raise DouyinError(f"本地 Whisper 转写失败（exit {result.returncode}）")

    txt_file = output_dir / f"{audio_path.stem}.txt"
    if not txt_file.exists():
        raise DouyinError(f"Whisper 输出文件不存在: {txt_file}")

    return txt_file.read_text(encoding='utf-8').strip()


def transcribe(audio_path: Path) -> str:
    """转写：先硅基流动，失败/限流自动降级到本地 Whisper"""
    if API_KEY:
        try:
            print("🎙️  尝试硅基流动 API...", file=sys.stderr)
            text = transcribe_siliconflow(audio_path)
            if text.strip():
                print("✅ 硅基流动转写成功", file=sys.stderr)
                return text
        except (DouyinError, requests.exceptions.RequestException, ValueError) as error:
            print(f"⚠️  云端转写不可用（{type(error).__name__}：{error}），"
                  "改用本地 Whisper", file=sys.stderr)

    print("🔄 降级到本地 Whisper...", file=sys.stderr)
    text = transcribe_whisper(audio_path)
    print("✅ Whisper 转写成功", file=sys.stderr)
    return text


# =============================================================================
# 6. 专有名词校正
# 校正依据：用量表帧 / Explore 规范帧 / 视频烧录字幕
# 格式："听错词": "正确写法"
# =============================================================================

PROPER_NOUN_MAP = {
    # --- 中文听错（小红书视频路径，2026-09-28 补）---
    # 只收录"非真实词"的听错形式；真实词（如「开箱」）不加，靠人工复核，
    # 避免全局单次扫描把正确用法改坏。
    "首针":          "首帧",
    "尾针":          "尾帧",
    "声图":          "生图",
    # 恒等护栏：「声图」是「声图像素」的子串，不加这条会把正确用法改坏。
    # 与 douyin 侧 "skill"→"skill" 恒等条目同一机制。
    "声图像素":      "声图像素",

    "codeex":          "Codex",
    "GBT5.6":          "GPT-5.6",
    "GPT5.6":          "GPT-5.6",
    "GPT5.6luna":      "GPT-5.6 Luna",
    "GPT5.6Luna":      "GPT-5.6 Luna",
    "gpt5.6luna":      "gpt-5.6-luna",
    "GPT5.6terara":    "gpt-5.6-terra",
    "GPT5.6sol":       "gpt-5.6-sol",
    "GPT5.6-sol":      "gpt-5.6-sol",
    "GPT5.6艘":        "gpt-6-sol",
    "GPT5.6-so":       "gpt-5.6-sol",
    "GPT5.6-so-high":  "gpt-5.6-sol high",
    "GPT6色":          "gpt-6-sol",
    "GPT6-Sol":        "gpt-6-sol",
    "GPT6ustra":       "gpt-6-astra",
    "GPT6Atra":        "gpt-6-astra",
    "GPT6-astra":      "gpt-6-astra",
    "GBT6luar":        "gpt-6-luna",
    "GPT6lu娜":        "gpt-6-luna",
    "GPT6Luna":        "gpt-6-luna",
    "GPT6luna":        "gpt-6-luna",
    "gpt6-luna":       "gpt-6-luna",
    "fat 模式":        "fast 模式",
    "子H":             "子 agent",
    "AIH":             "agent",
    "主ag":            "主 agent",
    "主A卷":           "主 agent",
    "主A":             "主 agent",
    "子ag":            "子 agent",
    "scul":            "skill",
    "sckill":          "skill",
    "scu":             "skill",
    "ski":             "skill",
    "kill":            "skill",
    "sQ":              "skill",
    # ⚠️ 恒等条目（必须保留）：ASR 经常已经把 skill 听对了。
    #    而规则里的 "ski" / "kill" 都是 "skill" 的子串，若无此条，
    #    正则会从已正确的 skill 前缀切开并重写，产生 "skillll" / "killll"。
    #    加入恒等条目后，最长优先会一次吃掉整个 skill，指针跳过内部。
    #    修复前实测：8 个已正确的 skill 全部被损坏。
    "skill":           "skill",
    "expllo":          "Explore",
    "expl":            "Explore",
    "exQ":             "Explore",
    "simplified":      "Simplify",
    "simplify":        "Simplify",
    "cloud":           "Claude",
    "snet":            "Sonnet",
    "oppos":           "Opus",
    "ops模型":         "Opus 模型",
    # ✅ 2026-09-28 截帧验证（CodeX Team-Mode 视频 7689421220315548971）
    "experad":         "Expert Advisor",   # 231s 烧录字幕角色列表
    "劣制角色":        "内置角色",          # 253s 烧录字幕：我把它作为了一个内置角色
    "outtra模式":      "Ultra 模式",        # 68s 烧录字幕：使用它的 Ultra 模式
    "outtra":          "Ultra",
    "febo":            "Fable",            # 285s 烧录字幕：请教这个 Fable 模型
    "APII":            "API",
    "emoage":          "image",
    "9十几亿":         "928.5 亿",
    "GBT6":            "GPT-6",
    "GBT5":            "GPT-5",

    # ── 2026-10-30 影视飓风 AI 工作流视频（7696090124497556474）──
    # 依据：抽帧 88 处 OCR + 烧录字幕 / 终端截图原文
    # 关键帧：586s 终端 for C in 'clawhub','openclaw','hermes'；
    #         617s sundial-org/awesome-openclaw-skills@ffmpeg-video-editor（安装量 993 installs）；
    #         2086s 幻灯片 PATH·CODEX APP FIRST / 安装 OpenClaw 和 Hermes Agent；
    #         2087s 字幕「都还是Hermes」；2088s 字幕「或者是ClaudeCode」；
    #         2104s 幻灯片 THE STABLE STACK：AGENT / CODEX APP / FEISHU IM
    # 子串重叠由「按长度降序」自动消解（scuki>scu、skillki>ski>skill）。
    "open car":        "OpenClaw",
    "fiers":           "Hermes",
    "howco":           "Claude Code",
    "scuki":           "skill",
    "skillki":       "skill",
    "scukill":         "skill",
    "cuki":            "skill",
    "A整":              "agent",
    "aggen":           "agent",
    "gian":            "agent",
    "FFMEPG":          "FFmpeg",
    "再asone":        "awesome",
    "coded S app":     "Codex App",
    "影视剧风":            "影视飓风",
    "9就3个":            "993",
    "再asome":          "awesome",
    "agian":           "agent",
    # scull / cukill 是上面那批变体里还没收录的两个（同样按长度降序自动消解重叠）
    "scull":           "skill",
    "cukill":          "skill",
    "生树":           "生数",          # 2026-10-01 视频7688978777174527241，27处
    "生诉":           "生数",          # 同上，4处
    "微度":           "Vidu",         # 同上，3处；产品线真名
    "vi度":           "Vidu",         # 同上，2处
    "vidow":          "Vidu",         # 同上，3处
    "vido":           "Vidu",         # 同上，2处
    "viow":           "Vidu",         # 同上，2处
    "V度":            "Vidu",         # 同上，含"们V度S团队"、"V度S221挺"等复合形式
    "阿瓦塔":          "Avatar",       # 同上，3处
    "阿瓦坦":          "Avatar",       # 同上，1处
    "阿瓦t":          "Avatar",       # 同上，1处
    "阿瓦":            "Avatar",       # 同上，1处（接在更长的变体之后才安全）
    "艾it挺":          "Edit",         # 同上，1处；Vidu S2 Edit
    "ID挺":           "Edit",         # 同上，1处
    "fo认":           "Diffusion",    # 同上，1处；帧证据：章节条"Diffusion是渲染器，数据是关键"
    "deerion":        "Diffusion",    # 同上，1处
    "diifferion":     "Diffusion",    # 同上，1处
    "dieferion":      "Diffusion",    # 同上，1处
    "defendion":      "Diffusion",    # 同上，1处
    "deeferion":      "Diffusion",    # 同上，1处
    "dfiion":         "Diffusion",    # 同上，1处
    "defier":         "Diffusion",    # 同上，1处
    "底ffer":          "Diffusion",    # ⚠️ 2026-10-01 初判buffer错；官方全文证实应为Diffusion
    "底fi":            "TurboDiffusion",# ⚠️ 同上，官方全文实锤为TurboDiffusion
    "tble Diffusion":  "TurboDiffusion",# 同上，官方全文
    "特步理学":         "TurboDiffusion",# 同上
    "table理学":        "TurboDiffusion",# 同上
    "prefiling":       "prefill",      # 2026-10-01，2处；LLM推理术语
    "prefiing":        "prefill",     # 同上
    "deding":          "decoding",     # 同上，1处
    "decocoding":      "decoding",     # 同上，1处
    "computer棒":      "compute bound", # 同上，1处
    "memorable":       "memory bound", # 同上，1处
    "memory bond":     "memory bound", # 同上，1处
    "decarte":         "Decart",       # 同上，1处；海外实时世界模型公司
    "decar":           "Decart",       # 同上，3处
    "迪卡尔利":         "Decart",       # 同上，1处
    "迪car":           "Decart",       # 同上，1处
    "in法":            "infra",        # 同上，4处；推理/训练infra
    "infa":            "infra",        # 同上，1处
    "infrar":          "infra",        # 同上，1处
    "序求量":           "生成需求",     # ⚠️ 初判"需求量"错；官方全文实为"生成需求"
    "挑价":             "挑战",         # 同上，1处；音近
    "全战":             "全栈",         # ⚠️ 初判全栈对；官方全文为"完整"
    "大圆模型":         "大语言模型",   # 同上，2处
    "诸多演验":         "诸多验证",     # 同上，1处
    "720PG":           "720P",         # 同上，1处
    "baselineline":    "baseline",     # 同上，1处
    "waow":            "Vidu",         # 官方全文："我们Vidu S团队的使命"；waow=Vidu
    "selfre forcing":  "Self-Replay Forcing", # 官方全文
    "VOM":             "视觉语言模型",   # 官方全文："基于视觉语言模型的Agent系统"
    "IPS":             "FPS",          # 官方全文："每秒25—40帧"
    "z者ten":          "vLLM 本身",    # 官方全文："适配了vLLM本身在decoding上"
    "s者tent def":     "SageAttention 和 TurboDiffusion", # 官方全文
    "我们Vidu S团队":  "我们 Vidu S 团队", # 官方全文
    "3月IC":           "3月份 S 系列",  # 官方全文："三月份正式启动S系列"
    "人类交互动作派娱乐":"人类极致的多模态交互体验", # 官方全文
    "3月份S系列正式启动":"3月份 S 系列正式启动",  # 官方全文（已含在上条复合形式中，此条防漏检）
    "很全栈的能力":     "很完整的能力",  # ⚠️ 初判全栈对；官方全文为"很完整的能力"


}

# 按长度降序，确保同一起始位置优先匹配更长的听错词
PROPER_NOUN_SORTED = sorted(PROPER_NOUN_MAP.items(), key=lambda x: -len(x[0]))

# 单次扫描的正则：交替匹配所有听错词，长词优先。
#
# ⚠️ 不要改回逐条 text.replace()：那会产生级联替换。
#    真实案例：scul → "skill" 之后，规则 "kill"→"skill" 又命中了这个新生成的
#    skill（"ski" 同理），最终产出 "sskillll"。单次扫描后替换文本不再被回扫，
#    此问题消失。
#
#    但单次扫描本身不充分：规则 "ski" / "kill" 是 "skill" 的子串，会切开
#    本已正确的 skill。必须配合 PROPER_NOUN_MAP 里的 "skill" 恒等条目。
_PROPER_NOUN_RE = re.compile(
    "|".join(re.escape(wrong) for wrong, _ in PROPER_NOUN_SORTED))


def correct_proper_nouns(text: str) -> str:
    """单次扫描替换所有已知听错词。纯本地，无需网络，不产生级联替换。"""
    if not text:
        return text
    return _PROPER_NOUN_RE.sub(lambda m: PROPER_NOUN_MAP[m.group(0)], text)


def find_suspect_terms(text: str) -> list:
    """返回文本中仍可能存疑的专有名词候选（人工/多模态复核用）。"""
    suspects = []
    patterns = [
        (r'[A-Za-z]*[一-鿿][A-Za-z]+', '中英混排'),      # 中英混杂
        (r'\b[a-z]+\d+[a-z0-9]*\b', '小写+数字'),        # 疑似型号
        (r'[A-Za-z]{2,}(?=\s*模式)', 'X 模式'),           # X 模式
    ]
    for pattern, label in patterns:
        for m in re.finditer(pattern, text):
            word = m.group(0)
            if word in PROPER_NOUN_MAP.values():
                continue
            suspects.append((word, label))
    seen, unique = set(), []
    for word, label in suspects:
        if word not in seen:
            seen.add(word)
            unique.append({"term": word, "type": label})
    return unique


def resolve_title(explicit: str = "", auto_title: str = "",
                  page_title: str = "", page_identity_ok: bool = True,
                  video_id: str = "") -> str:
    """定标题：显式 > 页面推导 > 页面标题 > video_id。

    页面身份校验失败时**不得**用 page_title —— 那时页面渲染的是另一个视频，
    它的标题属于别的作品。实测：身份校验失败后 meta.json 里写进了
    「天地之大 困住你的是什么」，而实际下载的是「给自己的网站下了一场大雨」。
    标题对不上比标题难看更糟：会让后续按标题检索的人找错文件。
    """
    if explicit:
        return explicit
    if auto_title:
        return auto_title
    if page_identity_ok:
        stripped = re.sub(r"\s*#\S+", "", page_title or "").strip()
        if stripped:
            return stripped
    return video_id


def has_audio_track(path: Path) -> bool:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30)
        return "audio" in r.stdout
    except Exception:
        return False


# ── 无口播判定 ────────────────────────────────────────────────────
# 有效发音字符 = 中文 + 字母数字（排除 🎼 音乐标记与空白/标点）。
# 纯 BGM 视频的 ASR 输出是幻觉碎片（实测「Yeah.🎼失实的」这类），
# 真人说话则稳定在 4~8 字/秒。实测 11 个真实抖音视频：
#   无人声：0.03 / 0.33 / 0.47 字每秒
#   有人声：4.55 / 4.56 / 5.27 / 5.89 / 6.21 / 7.48 / 8.11 字每秒
# 阈值取 1.5，两侧都有 3 倍以上余量。
EFFECTIVE_CHARS = re.compile(r"[0-9A-Za-z一-鿿]")
NO_SPEECH_RATE = 1.5
# 短于此时长不判定：几秒钟的片段天然字数少，容易误报。
NO_SPEECH_MIN_DURATION = 10.0


def speech_rate(text: str, duration: float) -> tuple[int, float, bool]:
    """返回 (有效字符数, 每秒有效字符数, 是否疑似无口播)。

    text 应是已清掉 🎼 音乐标记的转写文本；duration 是音频秒数。
    """
    eff = len(EFFECTIVE_CHARS.findall(text or ""))
    rate = eff / duration if duration and duration > 0 else 0.0
    no_speech = duration >= NO_SPEECH_MIN_DURATION and rate < NO_SPEECH_RATE
    return eff, round(rate, 2), no_speech


def extract_frames(video_path: Path, timestamps: list, out_dir: Path) -> list:
    """按指定时间点抽帧，用于专有名词截帧验证。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for ts in timestamps:
        target = out_dir / f"frame_{ts}s.jpg"
        if not target.exists():
            r = subprocess.run(
                ["ffmpeg", "-y", "-ss", str(ts), "-i", str(video_path),
                 "-frames:v", "1", "-q:v", "2", str(target)],
                capture_output=True, text=True)
            if r.returncode != 0 or not target.exists():
                print(f"⚠️  {ts}s 抽帧失败", file=sys.stderr)
                continue
        frames.append(str(target))
    return frames


# =============================================================================
# 7. 结构化输出
# =============================================================================

def format_output(text: str, *, title: str, source: str = "",
                  tags: list | None = None, speaker: str = "",
                  description: str = "", created: str = "",
                  confidence: str = "medium", share_text: str = "",
                  segmented: str = "") -> str:
    """生成带 frontmatter 的 Markdown。

    segmented: 已人工/模型分段好的正文（带一级小标题）。
               若为空则原样输出全文，由调用方（Agent）按
               references/output-format.md 规则补充分段标题。
    """
    import datetime
    created = created or datetime.date.today().isoformat()
    tags = tags or ["抖音", "视频转写"]
    # description 是 schema 必填字段。
    # 不自动从正文推断——转写首句常是语气词/碎片，不是内容描述。
    # 留空，由 Agent 填入概括，或调用方传 --description 参数。
    if not description:
        description = ""

    # 🎼 U+1F3BC 是 SenseVoice 对非语音片段（背景音乐/BGM）的标记，不是口播内容，
    # 绝不能进知识库。这是最后一道闸：无论上游走哪条路径、经过几层后处理，
    # 只要汇到 format_output 就一定被清掉。
    # 实测 2026-10-01：只在 transcribe_local 里局部清理并不可靠，transcript.md
    # 仍漏进 39 个。故改为在出口兜底，而不是信任上游。
    text = text.replace("\U0001f3bc", "")
    if segmented:
        segmented = segmented.replace("\U0001f3bc", "")

    lines = ["---", f"title: {title}"]
    if description:
        lines.append(f"description: {description}")
    if source:
        lines.append(f"source: {source}")
    lines.append(f"tags: [{', '.join(tags)}]")
    lines.append(f"created: {created}")
    lines.append("type: transcript")
    lines.append(f"confidence: {confidence}")
    if speaker:
        lines.append(f"speaker: {speaker}")
    lines.extend(["---", "", f"# {title}", "", segmented or text, ""])

    if share_text:
        lines.extend(["---", "", "原始转发文本：", "", share_text, ""])
    else:
        lines.extend(["---", "", "原始转发文本未提供", ""])

    return "\n".join(lines)


# =============================================================================
# 8. 完整流程
# =============================================================================

def _probe_streams(path: Path) -> tuple[bool, bool]:
    """用 ffprobe 判定文件的真实流构成，返回 (有视频流, 有音频流)。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20)
        out = r.stdout or ""
        return ("video" in out, "audio" in out)
    except Exception:
        return (False, False)


def _probe_duration(path: Path) -> float:
    """用 ffprobe 取文件时长（秒）；失败返回 0.0。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20)
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _fetch_stream(candidates: list[tuple[str, int]], dest_dir: Path,
                  referer: str,
                  target_duration: float | None = None
                  ) -> tuple[Path, Path | None, bool]:
    """按体积降序试候选 CDN 流。

    抖音同一条视频会同时下发三类流，且 content-type 全是 video/mp4：
      - 纯音频（ve-15，体积小）
      - 纯视频（vd-00xx，中等）
      - 音视频合一（ve-15，体积最大）

    返回 (主文件, 需合并的音频文件或 None, 是否含视频轨)。
    合流最优（直接用）；否则视频+音频合并；最差只有音频流。

    ⚠️ target_duration = 目标视频的真实时长（来自页面 DOM <video>.duration）。
    抖音页面会预载并自动播放**其他**视频（如推荐流），这些流同样是
    "音视频合一"且体积最大——不校验时长就会被静默当成目标下载
    （实测 2026-10-27：目标 114.218s，却下到另一条 PvZ 配音的 107.8s）。
    时长是判别目标视频的唯一硬指标，必须校验。
    """
    TOLERANCE = 3.0  # 秒
    audio_fallback: Path | None = None
    video_only: Path | None = None
    mismatch: list[Path] = []   # 时长不符的流（页面预载的其他视频）
    # （候选, 已下载文件 or None, 实际体积, 声称体积）
    tried: list[tuple[tuple[str, int], Path | None, int, int]] = []

    def _probe_all() -> tuple[Path, Path | None, bool] | None:
        """按已下载文件重排再试一轮（应对 content-length 谎报）。"""
        have = sorted((t for t in tried if t[1] is not None),
                      key=lambda t: t[2], reverse=True)
        for cand, path, actual, _hinted in have:
            has_v, has_a = _probe_streams(path)
            if has_v and has_a:
                return path, None, True
        return None

    for i, (url, hinted) in enumerate(candidates, 1):
        ext = "m4a" if "/tos-cn-ve-" in url else "mp4"
        dest = dest_dir / f"cand{i}.{ext}"
        try:
            print(f"⬇️  候选流 {i}/{len(candidates)}"
                  f"（{hinted/1e6:.1f}MB）: {url[:110]}", file=sys.stderr)
            path = download(url, dest, referer)
        except Exception as e:
            print(f"   ⚠️  跳过：{e}", file=sys.stderr)
            dest.unlink(missing_ok=True)
            tried.append(((url, hinted), None, 0, hinted))
            continue

        actual = path.stat().st_size
        has_v, has_a = _probe_streams(path)
        dur = _probe_duration(path)
        print(f"   → 实收 {actual/1e6:.1f}MB"
              f"  视频轨={has_v}  音频轨={has_a}"
              f"  时长={dur:.3f}s", file=sys.stderr)
        tried.append(((url, hinted), path, actual, hinted))

        # ── 时长校验：挡掉页面预载的其他视频 ──────────────────────────
        if target_duration and dur and abs(dur - target_duration) > TOLERANCE:
            print(f"   ⚠️  时长不符：目标 {target_duration:.3f}s 实收 {dur:.3f}s"
                  f" → 判定为页面预载的其他视频，跳过", file=sys.stderr)
            mismatch.append(path)
            continue

        # 可观测性：header 声称与实收偏差超过 3 倍 → 警告
        # （实测 2026-09-29：抖音对部分流返回 0 或错误 content-length）
        if hinted > 0 and (actual > hinted * 3 or hinted > actual * 3):
            print(f"   ⚠️  content-length 不可信：声称 {hinted/1e6:.1f}MB "
                  f"实收 {actual/1e6:.1f}MB（偏差 {max(actual,hinted)/max(min(actual,hinted),1):.1f}×）",
                  file=sys.stderr)

        if has_v and has_a:
            return path, None, True          # 音视频合一，最理想
        if has_v and video_only is None:
            video_only = path
        elif has_a and audio_fallback is None:
            audio_fallback = path

    # 第一轮没找到合流 → 按实收体积重排再试一轮
    retry = _probe_all()
    if retry is not None:
        print("🔁 首轮未找到合流，按实收体积重试后命中合流", file=sys.stderr)
        return retry

    # 全部候选都时长不符 → 绝不能默默交出一个错视频
    if mismatch and not target_duration:
        print(f"   ⚠️  有 {len(mismatch)} 条流但无法核对时长"
              f"（页面未给出 duration），按原逻辑继续", file=sys.stderr)
    elif mismatch:
        raise DouyinError(
            f"所有 CDN 候选流时长均与目标不符（目标 {target_duration:.3f}s）。"
            f"抖音页面预载了其他视频，已拒绝交付错误内容。"
            f"请重试，或用 --headful 观察页面实际播放内容。")

    if video_only is not None:
        return video_only, audio_fallback, True
    if audio_fallback is not None:
        return audio_fallback, None, False
    raise DouyinError("所有 CDN 候选流均下载或解析失败")


def find_repeats(text: str, min_times: int | None = None) -> list[dict]:
    """检测疑似 ASR 幻觉重复（同一片段连续重复多次）。

    ⚠️ 只检测、不修改。铁律「原文一字不改」：调用方拿到结果后自行决定如何标注，
    transcript_raw.md 永远保存未标注的原始输出。

    阈值按单元长度自适应（min_times=None 时）：
      - 单元 ≤2 字符：需重复 5 次（否则「哈哈哈」「他他他」会误报）
      - 单元 ≥3 字符：需重复 3 次（长句复读 3 遍基本就是 ASR 幻觉，
        实测英文 "I should keep on down" × 3 就是这种情况）

    返回 [{'unit': 重复片段, 'times': 次数, 'start', 'end', 'snippet'}]。
    """
    # 排除换行与句末标点，避免跳句误判；允许空格（英文复读会出现）。
    # 上限 40：实测 "I should keep on down" 长 21 字符，设 20 会整条漏检。
    unit_cls = r"[^\n\r，。！？、,.!?…；;：:]{1,40}?"
    # 先宽松匹配（3 次），再按单元长度判定是否达到阈值
    pat = re.compile(rf"(?P<u>{unit_cls})(?P=u){{2,}}")
    out: list[dict] = []
    for m in pat.finditer(text):
        u = m.group("u")
        times = len(m.group(0)) // len(u)
        need = min_times if min_times is not None else (5 if len(u) <= 2 else 3)
        if times < need:
            continue
        lo = max(0, m.start() - 12)
        hi = min(len(text), m.end() + 12)
        out.append({
            "unit": u,
            "times": times,
            "start": m.start(),
            "end": m.end(),
            "snippet": text[lo:m.start()] + "〔…〕" + text[m.start():m.end()][:40] + text[m.end():hi],
        })
    return out


def annotate_repeats(text: str, hits: list[dict]) -> str:
    """在重复处插入标注，文字本身保持不变。"""
    if not hits:
        return text
    out, last = [], 0
    for h in sorted(hits, key=lambda x: x["start"]):
        if h["start"] < last:
            continue
        out.append(text[last:h["end"]])
        out.append(f"〔⚠️ 疑似 ASR 幻觉重复 {h['times']} 次，待确认〕")
        last = h["end"]
    out.append(text[last:])
    return "".join(out)


def transcribe_douyin(link: str,
                      output_dir: Path = DEFAULT_OUTPUT,
                      profile_dir: Path = DEFAULT_PROFILE,
                      headful: bool = False,
                      keep_streams: bool = False,
                      title: str = "",
                      keep_audio: bool = False,
                      description: str = "",
                      author: str = "",
                      pub_date: str = "",
                      download_only: bool = False) -> dict:
    """主流程（链接入口）：解析 → 下载 → 合成 → 转写 → 校正 → 目录命名。

    download_only=True 时只下载视频、不转写 —— 提示词提取等场景只要画面，
    不需要音频，省掉一次 ASR 调用。
    """
    video_id = resolve_video_id(link)

    print(f"🔖 video_id: {video_id}", file=sys.stderr)

    cap = capture_stream_urls(video_id, profile_dir, headful=headful)

    if cap.get("needs_login"):
        raise DouyinError("页面要求登录验证。请用 --headful 在可见窗口手动登录一次，"
                          "会话存进独立 profile 目录复用。")

    if not cap["video"]:
        raise DouyinError(
            f"未捕获到 CDN 媒体流。video={len(cap['video'])} "
            f"audio={len(cap['audio'])} other={len(cap['other'])}")

    # ── 目录命名（约定 2026-10-28）───────────────────────────────
    # 目录名用 <主题>_<作者>_<日期>，让人一眼看出内容是什么；
    # video_id 记进 meta.json，不丢可追溯性。
    #
    # ⚠️ 采信前必须校验页面身份：抖音页面在并发/风控下可能渲染出**别的视频**
    # （实测 2026-10-28：两个进程共用同一 profile 时，页面被渲染成另一条视频，
    #  导致目录名与下载内容完全对不上）。
    # 判据：canonical / location 必须仍指向目标 video_id。
    canonical = cap.get("canonical", "") or ""
    location = cap.get("location", "") or ""
    page_identity_ok = (video_id in canonical) or (video_id in location)

    desc = cap.get("desc", "") or ""
    og_title = cap.get("og_title", "") or ""
    if page_identity_ok:
        auto_title = naming.derive_title(og_title, desc)
        auto_author = naming.extract_author(desc)
        auto_date = naming.extract_date(desc)
    else:
        # 页面身份对不上 → 不采信任何 meta，目录退回 video_id 并告警
        print(f"⚠️  页面身份校验失败（canonical/location 不含 {video_id}），"
              f"不使用页面元数据命名目录", file=sys.stderr)
        print(f"   canonical: {canonical[-60:]}", file=sys.stderr)
        print(f"   location : {location[-60:]}", file=sys.stderr)
        auto_title = auto_author = auto_date = ""

    final_title = title or auto_title or video_id
    folder = naming.folder_base(final_title, author or auto_author,
                                 pub_date or auto_date, fallback=video_id)
    dest_dir = naming.unique_dir(output_dir, folder)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final_mp4 = dest_dir / f"{video_id}.mp4"
    print(f"📁 输出目录: {dest_dir.name}", file=sys.stderr)

    # 抖音会同时下发多条流（纯音频 / 纯视频 / 音视频合一），且它们的
    # content-type 一律是 video/mp4（实测 2026-09-29），无法据此区分。
    # 策略：按 content-length 降序遍历候选，下载后用 ffprobe 验证真实流构成。
    sizes = cap.get("sizes", {})
    ordered, seen = [], set()
    for u in cap["video"]:
        if u in seen:
            continue
        seen.add(u)
        ordered.append((u, sizes.get(u, 0)))
    ordered.sort(key=lambda x: x[1], reverse=True)

    main_path, extra_audio, video_only = _fetch_stream(
        ordered, dest_dir, page_url(video_id),
        target_duration=cap.get("target_duration") or None)

    print("🔧 合成 mp4", file=sys.stderr)
    mux(main_path, extra_audio, final_mp4)
    if not keep_streams:
        main_path.unlink(missing_ok=True)
        if extra_audio:
            extra_audio.unlink(missing_ok=True)
    if not video_only:
        print("⚠️  本次未取到视频轨（抖音未下发），截帧验证不可用", file=sys.stderr)

    # 页面标题常带一串话题标签（#ai教程 #vibecoding…），只作参考；
    # 显式传入的 --title 优先。页面身份校验失败时不能用 page_title（见 resolve_title）。
    page_title = cap.get("title", "") or ""
    final_title = resolve_title(explicit=title, auto_title=auto_title,
                                page_title=page_title,
                                page_identity_ok=page_identity_ok,
                                video_id=video_id)

    # ── 只要视频：跳过后续转写（提示词提取等场景只要画面，不需要音频）──
    if download_only:
        try:
            naming.write_meta(dest_dir, video_id=video_id, title=final_title,
                              author=author or auto_author,
                              date=pub_date or auto_date,
                              source=f"https://www.douyin.com/video/{video_id}")
        except Exception as e:
            print(f"⚠️  写 meta.json 失败（非致命）: {e}", file=sys.stderr)
        result = {
            "ok": True,
            "video_id": video_id,
            "title": final_title,
            "video_path": str(final_mp4),
            "output_dir": str(dest_dir),
            "folder": dest_dir.name,
            "download_only": True,
        }
        result["output_dir"] = str(dest_dir)
        result["folder"] = dest_dir.name
        return result

    result = transcribe_local(final_mp4, output_dir=output_dir,
                              video_id=video_id, title=final_title,
                              keep_audio=keep_audio,
                              description=description,
                              dest_dir=dest_dir)

    # 目录内写 meta.json：记 video_id / 主题 / 作者 / 日期 / 来源
    try:
        naming.write_meta(dest_dir, video_id=video_id, title=final_title,
                          author=author or auto_author,
                          date=pub_date or auto_date,
                          source=f"https://www.douyin.com/video/{video_id}")
    except Exception as e:
        print(f"⚠️  写 meta.json 失败（非致命）: {e}", file=sys.stderr)
    result["output_dir"] = str(dest_dir)
    result["folder"] = dest_dir.name
    return result


def transcribe_local(video_path: Path,
                     output_dir: Path = DEFAULT_OUTPUT,
                     video_id: str | None = None,
                     title: str = "",
                     keep_audio: bool = False,
                     description: str = "",
                     source: str = "",
                     dest_dir: Path | None = None) -> dict:
    """转写本地 MP4（不经过下载）。链接流程和 --video 入口共用此后半段。

    dest_dir: 显式指定输出目录（下载流程已按主题命名好时传入）。
    """
    video_path = Path(video_path).resolve()
    if not _validate_mp4(video_path):
        raise DouyinError("本地文件不是有效 MP4，或文件尚未下载完整")

    # _validate_mp4 只查 ftyp magic + 体积，纯音频 m4a 也能通过。
    # 这里补一次真实流校验：--video 入口直接给音频文件时会被拦下。
    has_video, has_audio = _probe_streams(video_path)
    if not has_audio:
        raise DouyinError("输入文件不含音频轨，无法转写")
    if not has_video:
        print("⚠️  输入文件无视频轨（纯音频），--frames 截帧验证不可用",
              file=sys.stderr)

    if video_id is None:
        import hashlib
        stem = re.sub(r'[^A-Za-z0-9_-]+', '-', video_path.stem).strip('-')[:48] or 'video'
        identity = f'{video_path}:{video_path.stat().st_size}:{video_path.stat().st_mtime_ns}'
        video_id = f'local-{stem}-{hashlib.sha256(identity.encode()).hexdigest()[:8]}'
    title = title or video_path.stem

    dest_dir = dest_dir or (output_dir / video_id)
    dest_dir.mkdir(parents=True, exist_ok=True)

    print("🎙️  提取音频 + 转写", file=sys.stderr)
    audio_path = dest_dir / "audio.wav"
    if not audio_path.exists():
        extract_audio(video_path, audio_path)
    raw_text = transcribe(audio_path)
    if not raw_text.strip():
        raise DouyinError("转写结果为空，请检查视频是否包含清晰口播")
    (dest_dir / "transcript_raw.md").write_text(raw_text, encoding="utf-8")

    # SenseVoice 非语音音频标记（背景音乐/BGM）→ 直接删，不进知识库
    # transcript_raw.md 保留纯净原文；清理只发生在 corrected 及之后的链路
    raw_text = raw_text.replace("\U0001f3bc", "")  # 🎼 U+1F3BC

    # ── 无口播判定：纯 BGM 视频的 ASR 输出是幻觉碎片，不是口播稿 ──────
    audio_seconds = _probe_duration(video_path)
    eff_chars, rate, no_speech = speech_rate(raw_text, audio_seconds)
    if no_speech:
        print(f"⚠️  本片疑似无口播：有效字符 {eff_chars} 个 / {audio_seconds:.0f}s "
              f"= {rate:.2f} 字每秒（阈值 {NO_SPEECH_RATE}）。"
              "大概率只有 BGM/画面，没有可转写的口播 —— "
              "不要把这份幻觉碎片当逐字稿交付。",
              file=sys.stderr)

    corrected = correct_proper_nouns(raw_text)
    suspects = find_suspect_terms(corrected)
    repeats = find_repeats(corrected)
    if repeats:
        print(f"⚠️  检测到 {len(repeats)} 处疑似 ASR 幻觉重复（已标注，文字未改）:",
              file=sys.stderr)
        for r in repeats:
            print(f"   · {r['times']}×「{r['unit']}」", file=sys.stderr)
    display = annotate_repeats(corrected, repeats)

    if not description:
        print("⚠️  description 未提供：schema 标为必填字段，本次输出将缺失该字段。"
              "请由调用方概括后通过 --description 传入", file=sys.stderr)

    markdown = format_output(
        display, title=title,
        # 优先用显式传入的 source；否则本地文件路径猜不出抖音 id，
        # 只能对数字 id（真实 aweme_id）反推页面链接。
        source=source or (page_url(video_id) if str(video_id).isdigit() else ""),
        description=description,
        confidence="medium",
    )
    md_path = dest_dir / "transcript.md"
    md_path.write_text(markdown, encoding="utf-8")

    # audio.wav 是纯中间产物：16kHz 单声道 WAV 无压缩，体积比 mp4 还大，
    # 转写完就没用了，需要时从 mp4 重抽只要 ~1 秒。默认转写成功后删除。
    if not keep_audio and audio_path.exists():
        try:
            audio_path.unlink()
            print("🧹 已清理中间音频 audio.wav（需保留加 --keep-audio）",
                  file=sys.stderr)
        except OSError as e:
            print(f"⚠️  清理 audio.wav 失败（非致命）: {e}", file=sys.stderr)

    return {
        "ok": True,
        "video_id": video_id,
        "title": title,
        "raw_text": raw_text,  # 已清 🎼；transcript_raw.md 写在此之前，保留原样
        "corrected_text": corrected,
        "display_text": display,  # 已清除 🎼
        "suspect_terms": suspects,
        "repeats": repeats,
        "no_speech": no_speech,       # True = 疑似无口播，别当逐字稿交付
        "speech_rate": rate,          # 有效字符/秒
        "effective_chars": eff_chars,
        "video_path": str(video_path),
        "audio_path": str(audio_path),
        "markdown_path": str(md_path),
        "output_dir": str(dest_dir),
        "size_mb": round(video_path.stat().st_size / 1_000_000, 2),
        "has_audio": has_audio,
        "has_video": has_video,
    }


# =============================================================================
# CLI
# =============================================================================

def main():
    ap = argparse.ArgumentParser(
        description="抖音视频 → 逐字稿 → 结构化 Markdown（单文件自包含）")
    ap.add_argument("link", nargs="?", help="抖音分享链接或 video_id")
    ap.add_argument("--video", type=Path,
                    help="已有本地 MP4 路径，跳过网络下载直接转写")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT),
                    help=f"输出根目录（默认 {DEFAULT_OUTPUT}）")
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE),
                    help=f"Chromium 独立 profile 目录（默认 {DEFAULT_PROFILE}）")
    ap.add_argument("--headful", action="store_true",
                    help="有头模式（首次登录验证用）")
    ap.add_argument("--keep-streams", action="store_true",
                    help="保留中间视频流文件")
    ap.add_argument("--keep-audio", action="store_true",
                    help="保留中间音频 audio.wav（默认转写后自动删除）")
    ap.add_argument("--check-link", action="store_true",
                    help="只解析 video_id，不下载不转写")
    ap.add_argument("--download-only", action="store_true",
                    help="只下载视频，不转写（提示词提取等只要画面的场景）")
    ap.add_argument("--frames", type=str, default="",
                    help="对本地视频抽帧，如 68,231,253,285（配合 --video）")
    ap.add_argument("--title", default="", help="输出文档标题")
    ap.add_argument("--speaker", default="", help="演讲者/作者")
    ap.add_argument("--source", default="", help="原始来源链接")
    ap.add_argument("--description", default="", help="description（不填则留空，由 Agent 确认时填入）")
    ap.add_argument("--author", default="", help="作者名（不填则自动从页面 description 提取）")
    ap.add_argument("--date", default="", help="发布日期 YYYYMMDD（不填则自动从页面提取）")
    args = ap.parse_args()

    if not args.link and not args.video:
        ap.error("请提供抖音链接，或用 --video 指定本地 MP4")

    try:
        if args.frames:
            if not args.video:
                ap.error("--frames 需要配合 --video 使用")
            stamps = [int(s) for s in args.frames.split(",") if s.strip()]
            frames = extract_frames(args.video, stamps,
                                    args.video.parent / "frames")
            print(json.dumps({"ok": True, "frames": frames},
                             ensure_ascii=False, indent=2))
            return

        if args.check_link:
            if not args.link:
                ap.error("--check-link 需要抖音链接")
            vid = resolve_video_id(args.link)
            result = {"ok": True, "video_id": vid, "page_url": page_url(vid)}
        elif args.video:
            result = transcribe_local(
                args.video, output_dir=Path(args.output), title=args.title,
                keep_audio=args.keep_audio,
                description=args.description,
                source=args.source)
        else:
            result = transcribe_douyin(
                args.link,
                output_dir=Path(args.output),
                profile_dir=Path(args.profile),
                headful=args.headful,
                keep_streams=args.keep_streams,
                title=args.title,
                keep_audio=args.keep_audio,
                description=args.description,
                author=args.author,
                pub_date=args.date,
                download_only=args.download_only,
            )

        print(json.dumps(result, ensure_ascii=False, indent=2))

    except DouyinError as e:
        print(f"❌ {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
