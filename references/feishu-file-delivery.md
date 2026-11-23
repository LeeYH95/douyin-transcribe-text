# 飞书交付：视频/大文件

> **读这里的条件**：要把 **.mp4 视频或大文件**发给用户（不是发 .md 逐字稿）。
> 只发逐字稿 markdown → 见 `references/output-format.md` 的「发送方式」。

---

## 1. 不要用 `MEDIA:` 指令发 .mp4

**现象**：OpenClaw 的 `MEDIA:` 指令把 `.mp4` 交给飞书通道时，飞书侧消息类型不被接受，
会话里落成 `msg_type=nonsupport` /「This message type is currently not supported」，
或**直接静默丢弃**（2026-10-30 实测：4 段一个都没生成，用户收到的是纯文字 + 一个英文报错气泡）。
历史样本：北海千川那条 1.8MB `.mp4` 同样被拒 —— 该条视频用户当时**也没收到**。

**已排除的原因**（别再往这些方向查）：

- 不是大小：23.6MB 走 `im/v1/files` 上传 → `code=0`
- 不是路径/Unicode：1.8MB 那条从 `/Volumes/EXTERNAL_SSD/...` 发，一样被拒

---

## 2. 可用路线（已实测 4/4 送达）

```
python3 scripts/feishu_send_file.py --chat-id oc_xxx --file a.mp4 --file b.mp4
```

= `im/v1/files`（`file_type=stream`）上传拿 `file_key` → `im/v1/messages` 以
`msg_type=file` 发送。飞书客户端可下载/播放。

可用 `--caption "说明"` 先发一条文字说明，用 `--account` 选择已配置账号。
发送前需要用户授权。当前脚本的 `--dry-run` 仍会获取令牌，带 `--caption` 时还会发送文字。
本地预览可核对文件及说明内容，避免执行发送脚本。

**不要走** `im/v1/media`（原生视频气泡）：该端点对本应用返回 **404**，走不通。

---

## 3. 硬上限 30MB

飞书单附件 30MB。超过必须分段：

```bash
ffmpeg -i in.mp4 -c:v libx264 -preset veryfast -crf 26 -maxrate 360k -bufsize 720k \
       -c:a aac -b:a 64k -ac 1 -f segment -segment_time 530 in.mp4 out/part%02d.mp4
```

分段后校验 `Σ时长 == 原片时长`，否则会静默丢片：

```bash
for f in out/part*.mp4; do
  ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$f"
done
```

`scripts/feishu_send_file.py` 内置 30MB 检查，超过会直接报错并提示分段，
不会发出一个必然失败的大附件。
