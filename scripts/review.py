"""Bind review decisions to one source file and one raw transcript."""

import hashlib
import json
import math
import re
from pathlib import Path

REVIEW_FILE = "transcript_review.json"
DECISIONS_FILE = "review_decisions.json"
MUSIC_MARKER = "\U0001f3bc"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 必须是 JSON 对象")
    return value


def transcript_basis(raw_path: Path) -> str:
    return raw_path.read_text(encoding="utf-8").replace(MUSIC_MARKER, "")


def build_review(raw_path: Path, video: Path, source: dict, segments: list[dict],
                 candidates: list[dict], duration: float, no_speech: bool) -> dict:
    text = transcript_basis(raw_path)
    # The Whisper writer joins stripped segment text with one newline. Use
    # offsets only if that layout agrees with the original transcript.
    chunks = [(s["text"].strip().replace(MUSIC_MARKER, ""), s) for s in segments]
    spans = []
    reconstructed = "\n".join(c for c, _ in chunks)
    if reconstructed.strip() == text:
        offset = -(len(reconstructed) - len(reconstructed.lstrip()))
        for chunk, segment in chunks:
            spans.append((offset, offset + len(chunk), segment))
            offset += len(chunk) + 1

    occurrences = []
    for candidate in sorted(candidates, key=lambda c: (c["start"], c["end"])):
        start, end = candidate["start"], candidate["end"]
        interval = next(([s["start"], s["end"]] for lo, hi, s in spans
                         if lo <= start < end <= hi), None)
        occurrences.append({
            **candidate, "id": f"{start}:{end}", "interval_seconds": interval,
        })
    return {
        "schema_version": 1,
        "raw_sha256": file_sha256(raw_path),
        "media_sha256": file_sha256(video),
        "media_path": str(video.resolve()),
        "source": source,
        "duration_seconds": duration,
        "no_speech": no_speech,
        "occurrences": occurrences,
    }


def valid_seconds(value, duration: float) -> bool:
    return (type(value) in (int, float)
            and math.isfinite(value) and 0 <= value < duration)


def evidence_error(evidence, occurrence: dict, record: dict,
                   base: Path, frame_cache: dict) -> str | None:
    if not isinstance(evidence, dict) or not nonblank(evidence.get("reason")):
        return "缺少校验理由"
    time = evidence.get("seconds")
    if not valid_seconds(time, record["duration_seconds"]):
        return "证据时点超出视频范围"
    kind = evidence.get("kind")
    interval = occurrence["interval_seconds"]
    if interval and not interval[0] - 1 <= time <= interval[1] + 1:
        return "证据时点与本次出现不对应"
    if kind == "audio":
        return None
    if kind != "frame" or not nonblank(evidence.get("manifest")):
        return "证据必须指向原音或抽帧清单"

    manifest_path = (base / evidence["manifest"]).resolve()
    try:
        if manifest_path not in frame_cache:
            frame_cache[manifest_path] = json_object(manifest_path)
        manifest = frame_cache[manifest_path]
        if any(manifest.get(k) != record[k] for k in ("raw_sha256", "media_sha256")):
            return "抽帧清单对应的是其他原稿或媒体"
        capture = manifest["occurrences"][occurrence["id"]]["captures"]
        frame = next((item for item in capture if item.get("seconds") == time), None)
        if not frame or not frame["frames"] or len(frame["frames"]) != len(frame["sha256"]):
            return "本次出现缺少对应时点的实际帧文件"
        if any(file_sha256(Path(path)) != digest
               for path, digest in zip(frame["frames"], frame["sha256"])):
            return "核验帧与抽帧记录不一致"
    except (OSError, ValueError, KeyError, TypeError):
        return "无法读取本次出现的抽帧证据"
    return None


def nonblank(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def evaluate_review(folder: Path, decisions_path: Path | None = None) -> dict:
    result = {"status": "needs_review", "blockers": [], "body": "", "source": ""}
    blockers = result["blockers"]
    try:
        record = json_object(folder / REVIEW_FILE)
        if record.get("schema_version") != 1:
            raise ValueError("不支持的核验记录版本")
        raw = folder / "transcript_raw.md"
        if file_sha256(raw) != record["raw_sha256"]:
            raise ValueError("原稿已更改，请重新建立核验记录")
        if file_sha256(Path(record["media_path"])) != record["media_sha256"]:
            result["status"] = "needs_media"
            raise ValueError("媒体文件与核验记录不一致")
        result["source"] = record["source"]["value"]
        if record["no_speech"]:
            result["status"] = "no_speech"
            blockers.append("疑似无口播，需要先复听")
            return result
        if not math.isfinite(record["duration_seconds"]) or record["duration_seconds"] <= 0:
            result["status"] = "needs_media"
            raise ValueError("缺少可验证的媒体时长")

        decisions_path = decisions_path or folder / DECISIONS_FILE
        decisions = json_object(decisions_path) if decisions_path.exists() else {}
        if decisions and any(decisions.get(k) != record[k] for k in ("raw_sha256", "media_sha256")):
            raise ValueError("核验决定对应的是其他原稿或媒体")
        if record["source"].get("verification") not in {"linked_media", "local_file"}:
            confirmation = decisions.get("source_confirmation") or {}
            if (confirmation.get("source") != result["source"]
                    or not nonblank(confirmation.get("reason"))):
                result["status"] = "needs_source_confirmation"
                blockers.append("待确认下载媒体与来源相符")

        verdicts = decisions.get("occurrences", {})
        if not isinstance(verdicts, dict):
            raise ValueError("occurrences 必须按候选 ID 索引")
        occurrences = record["occurrences"]
        known = {item["id"] for item in occurrences}
        for unknown in sorted(verdicts.keys() - known):
            blockers.append(f"未知的疑词出现 ID {unknown}")

        text = transcript_basis(raw)
        edits = []
        frame_cache = {}
        for occurrence in occurrences:
            start, end = occurrence["start"], occurrence["end"]
            key = occurrence["id"]
            if (type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(text) or key != f"{start}:{end}"
                    or text[start:end] != occurrence["term"]):
                raise ValueError(f"疑词 {key} 与原稿位置不符")
            verdict = verdicts.get(key) or {}
            action = verdict.get("action")
            if action not in {"keep", "replace"}:
                blockers.append(f"疑词 {key} 待核验")
                continue
            error = evidence_error(verdict.get("evidence"), occurrence, record, folder, frame_cache)
            if error:
                blockers.append(f"疑词 {key} {error}")
                continue
            if action == "replace":
                replacement = verdict.get("replacement")
                if not nonblank(replacement) or "\n" in replacement or "\r" in replacement:
                    blockers.append(f"疑词 {key} 缺少有效替换文本")
                    continue
                edits.append((start, end, replacement))

        edits.sort()
        for previous, following in zip(edits, edits[1:]):
            if previous[1] > following[0]:
                blockers.append("已核验的替换范围重叠")
                break
        if not blockers:
            for start, end, replacement in reversed(edits):
                text = text[:start] + replacement + text[end:]
            result.update(status="ready", body=text)
    except FileNotFoundError as exc:
        name = Path(exc.filename).name if exc.filename else "未知文件"
        if name not in {REVIEW_FILE, "transcript_raw.md"}:
            result["status"] = "needs_media"
        blockers.append(f"缺少核验所需文件 {name}")
    except (OSError, ValueError, AttributeError, KeyError, TypeError) as exc:
        blockers.append(f"核验记录无效：{exc}")
    return result


def normalized_body(text: str) -> str:
    return re.sub(r"\s+", "", text)
