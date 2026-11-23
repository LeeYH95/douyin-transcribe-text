import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import review
import transcribe
import verify_terms


@pytest.fixture
def draft(tmp_path):
    raw = tmp_path / "transcript_raw.md"
    text = "这台用了IPS面板。\n演示里说的是IPS。"
    raw.write_text(text, encoding="utf-8")
    video = tmp_path / "media.mp4"
    video.write_bytes(b"synthetic media identity")
    segments = [
        {"start": 3.0, "end": 4.0, "text": "这台用了IPS面板。"},
        {"start": 30.0, "end": 31.0, "text": "演示里说的是IPS。"},
    ]
    source = {"value": str(video), "video_id": "local", "verification": "local_file"}
    record = review.build_review(raw, video, source, segments,
                                 transcribe.propose_corrections(text), 35.0, False)
    record_path = tmp_path / review.REVIEW_FILE
    record_path.write_text(json.dumps(record), encoding="utf-8")
    md = tmp_path / "transcript.md"
    md.write_text(transcribe.format_output(text, title="面板: 对照",
                                            source=source["value"], description="术语校验"),
                  encoding="utf-8")
    return raw, video, md, record


def write_decisions(folder, record, verdicts, confirmation=None):
    payload = {key: record[key] for key in ("media_sha256", "raw_sha256")}
    payload.update(occurrences=verdicts, source_confirmation=confirmation)
    (folder / review.DECISIONS_FILE).write_text(json.dumps(payload), encoding="utf-8")


def verdict(action, seconds, replacement=None):
    return {"action": action, "replacement": replacement,
            "evidence": {"kind": "audio", "seconds": seconds, "reason": "回听定位原词"}}


def gate(path, *flags):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "quality_gate.py"), str(path), *flags],
        capture_output=True, encoding="utf-8", check=False,
    )


def test_same_word_is_reviewed_per_occurrence_and_gate_checks_edits(draft):
    _, _, md, record = draft
    first, second = record["occurrences"]
    assert first["interval_seconds"] == [3.0, 4.0]
    assert second["interval_seconds"] == [30.0, 31.0]
    decided = {second["id"]: verdict("replace", 30.5, "FPS")}
    write_decisions(md.parent, record, decided)

    pending = gate(md, "--json")
    assert pending.returncode == 2
    item = json.loads(pending.stdout)[0]
    assert item["status"] == "needs_review"
    assert any(first["id"] in reason for reason in item["blockers"])

    decided[first["id"]] = verdict("keep", 3.5)
    write_decisions(md.parent, record, decided)
    rendered = gate(md, "--render-reviewed")
    assert rendered.returncode == 0
    assert "这台用了IPS面板。\n演示里说的是FPS。" == rendered.stdout.strip()

    wrong = md.read_text().replace("IPS", "FPS")
    md.write_text(wrong)
    rejected = gate(md, "--json")
    assert rejected.returncode == 2
    assert "未核验的修改" in json.loads(rejected.stdout)[0]["blockers"][0]

    md.write_text(wrong.replace("这台用了FPS", "这台用了IPS"))
    accepted = gate(md, "--json")
    assert accepted.returncode == 0
    assert json.loads(accepted.stdout)[0]["status"] == "ready"


def test_unbound_source_needs_media_confirmation_and_blocks_substitution(draft):
    _, media, md, record = draft
    record["source"]["verification"] = "needs_confirmation"
    (md.parent / review.REVIEW_FILE).write_text(json.dumps(record))
    first, second = record["occurrences"]
    decided = {first["id"]: verdict("keep", 3.5), second["id"]: verdict("keep", 30.5)}
    write_decisions(md.parent, record, decided)
    assert json.loads(gate(md, "--json").stdout)[0]["status"] == "needs_source_confirmation"
    confirmation = {"source": str(media), "reason": "核对页面和媒体可见内容"}
    write_decisions(md.parent, record, decided, confirmation)
    assert gate(md, "--json").returncode == 0
    media.write_bytes(b"another recording")
    assert json.loads(gate(md, "--json").stdout)[0]["status"] == "needs_media"


def test_frames_are_attached_to_occurrence_not_word(draft, monkeypatch):
    raw, video, md, record = draft
    first, second = record["occurrences"]
    out = md.parent / "frames"
    monkeypatch.setattr(verify_terms, "probe_duration", lambda _: 35.0)
    monkeypatch.setattr(verify_terms, "probe_size", lambda _: (320, 240))

    def grab(_, seconds, folder, __, ___, ____):
        frame = folder / f"{seconds}.jpg"
        frame.write_bytes(b"synthetic")
        return [frame]

    monkeypatch.setattr(verify_terms, "grab", grab)
    monkeypatch.setattr(sys, "argv", [
        "verify_terms.py", "--video", str(video), "--text", str(raw),
        "--occurrences", f"{first['id']},{second['id']}", "--out", str(out),
    ])
    assert verify_terms.main() == 0
    manifest = review.json_object(out / "manifest.json")
    assert manifest["raw_sha256"] == record["raw_sha256"]
    frames = manifest["occurrences"]
    first_times = frames[first["id"]]["captured_times"]
    second_times = frames[second["id"]]["captured_times"]
    assert max(first_times) < min(second_times)

    second_evidence = {"action": "keep", "evidence": {
        "kind": "frame", "seconds": 31, "manifest": "frames/manifest.json",
        "reason": "画面显示的拼写与此处口播相符",
    }}
    write_decisions(md.parent, record, {
        first["id"]: verdict("keep", 30.5), second["id"]: second_evidence,
    })
    assert any("本次出现不对应" in item for item in
               json.loads(gate(md, "--json").stdout)[0]["blockers"])
    write_decisions(md.parent, record, {
        first["id"]: verdict("keep", 3.5), second["id"]: second_evidence,
    })
    assert gate(md, "--json").returncode == 0
    capture = next(item for item in frames[second["id"]]["captures"] if item["seconds"] == 31)
    Path(capture["frames"][0]).write_bytes(b"replaced frame")
    assert any("核验帧与抽帧记录不一致" in item for item in
               json.loads(gate(md, "--json").stdout)[0]["blockers"])


def test_missing_properties_and_no_speech_have_explicit_status(draft):
    _, _, md, record = draft
    content = md.read_text()
    md.write_text(content.replace("description:", "# description:"))
    blocked = gate(md, "--json")
    assert blocked.returncode == 2
    assert "description" in json.loads(blocked.stdout)[0]["frontmatter"]["缺失必填"]
    md.write_text(content.replace('description: "术语校验"', "description:"))
    blocked = gate(md, "--json")
    assert blocked.returncode == 2
    assert "description" in json.loads(blocked.stdout)[0]["frontmatter"]["无效字段"]
    record["no_speech"] = True
    (md.parent / review.REVIEW_FILE).write_text(json.dumps(record))
    assert json.loads(gate(md, "--json").stdout)[0]["status"] == "no_speech"


def test_raw_revision_change_invalidates_approval(draft):
    raw, _, md, record = draft
    first, second = record["occurrences"]
    write_decisions(md.parent, record, {
        first["id"]: verdict("keep", 3.5), second["id"]: verdict("keep", 30.5),
    })
    assert gate(md, "--json").returncode == 0
    raw.write_text(raw.read_text() + "另一版原稿")
    blocked = gate(md, "--json")
    assert blocked.returncode == 2
    assert any("原稿已更改" in item for item in json.loads(blocked.stdout)[0]["blockers"])


def test_page_identity_rejects_prefix_query_and_conflicting_canonical():
    assert transcribe.matches_page_identity("123", "https://www.douyin.com/video/123")
    assert not transcribe.matches_page_identity("123", "https://www.douyin.com/video/1234")
    assert not transcribe.matches_page_identity("123", "https://www.douyin.com/?q=/video/123")
    assert not transcribe.matches_page_identity("123", "https://example.com/video/123")
    assert not transcribe.matches_page_identity("123", "https://www.douyin.com/video/123",
                                                "https://www.douyin.com/video/456")
