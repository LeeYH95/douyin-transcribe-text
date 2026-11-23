#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书发文件（视频/大文件）——绕开 OpenClaw MEDIA: 指令对 .mp4 的缺陷。

背景（2026-10-30 实测，影视飓风 AI 工作流视频 7696090124497556474）：
  * OpenClaw 的 `MEDIA:` 指令把 .mp4 交给飞书通道时，通道发出的是飞书不接受的
    消息类型 → 会话里落成 msg_type=nonsupport / "This message type is currently
    not supported"，或直接静默丢弃（本次 4 段一个都没生成）。
    历史样本：北海千川 1.8MB .mp4 同样被拒（所以那条视频用户当时也没收到）。
  * `im/v1/media` 端点对本应用返回 404，原生视频气泡走不通。
  * 可用路线：`im/v1/files`（file_type=stream）上传拿 file_key → `im/v1/messages`
    以 msg_type=file 发送 → 飞书客户端可下载/播放。实测 23~24MB 正常送达。
  * 飞书单附件硬上限 30MB → 超过必须分段（用 ffmpeg -f segment 切）。

用法：
  python3 feishu_send_file.py --chat-id oc_xxx --file a.mp4 --file b.mp4 [--caption "说明"]
  python3 feishu_send_file.py ... --dry-run
"""
import argparse, json, os, urllib.request, urllib.error, uuid

BASE = "https://open.feishu.cn/open-apis"


def pj(url, data=None, headers=None, method="POST"):
    h = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        h.update(headers)
    r = urllib.request.Request(url, json.dumps(data).encode() if data is not None else None, h, method)
    return json.loads(urllib.request.urlopen(r, timeout=120).read().decode())


def multipart(fields, path, ctype):
    bnd = "----" + uuid.uuid4().hex
    out = b""
    for k, v in fields.items():
        out += (f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n').encode() + str(v).encode() + b"\r\n"
    fn = os.path.basename(path)
    out += (f'--{bnd}\r\nContent-Disposition: form-data; name="file"; filename="{fn}"\r\n'
            f'Content-Type: {ctype}\r\n\r\n').encode()
    with open(path, "rb") as f:
        out += f.read()
    return out + f"\r\n--{bnd}--\r\n".encode(), f"multipart/form-data; boundary={bnd}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", required=True)
    ap.add_argument("--file", action="append", required=True, help="可重复")
    ap.add_argument("--account", default="knowledge-master")
    ap.add_argument("--caption", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cfg = json.load(open(os.path.expanduser("~/.openclaw/openclaw.json")))
    acc = cfg["channels"]["feishu"]["accounts"][a.account]
    tk = pj(f"{BASE}/auth/v3/tenant_access_token/internal",
            {"app_id": acc["appId"], "app_secret": acc["appSecret"]})["tenant_access_token"]
    H = {"Authorization": f"Bearer {tk}"}

    if a.caption:
        r = pj(f"{BASE}/im/v1/messages?receive_id_type=chat_id",
               {"receive_id": a.chat_id, "msg_type": "text",
                "content": json.dumps({"text": a.caption}, ensure_ascii=False)}, H)
        print(f"caption → code={r.get('code')}")

    for p in a.file:
        ap_path = os.path.abspath(os.path.expanduser(p))
        sz = os.path.getsize(ap_path)
        if sz > 30 * 1024 * 1024:
            raise SystemExit(f"✗ {ap_path} {sz/1048576:.1f}MB > 飞书 30MB 上限，先用 ffmpeg 分段")
        body, ct = multipart({"file_type": "stream", "file_name": os.path.basename(ap_path)},
                              ap_path, "application/octet-stream")
        if a.dry_run:
            print(f"DRY-RUN upload {ap_path} ({sz/1048576:.1f}MB) → /im/v1/files, 然后 msg_type=file")
            continue
        req = urllib.request.Request(BASE + "/im/v1/files", body, {**H, "Content-Type": ct}, method="POST")
        up = json.loads(urllib.request.urlopen(req, timeout=300).read().decode())
        fk = (up.get("data") or {}).get("file_key")
        if up.get("code") != 0 or not fk:
            raise SystemExit(f"✗ upload 失败 code={up.get('code')} msg={up.get('msg')}")
        r = pj(f"{BASE}/im/v1/messages?receive_id_type=chat_id",
               {"receive_id": a.chat_id, "msg_type": "file",
                "content": json.dumps({"file_key": fk}, ensure_ascii=False)}, H)
        print(f"{os.path.basename(ap_path)} {sz/1048576:.1f}MB → file_key={fk[:20]}… send code={r.get('code')} msg={str(r.get('msg'))[:40]}")


if __name__ == "__main__":
    main()
