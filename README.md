# trump-speech-watch

这个项目用于追踪联合早报里与特朗普讲话、表态、下令、威胁相关的报道。我给它取的名字是 `trump-speech-watch`。

项目能力和今天另外两个项目保持一致：

- 每天固定时间执行一次，默认 19:00
- 用 Playwright 抓取联合早报搜索结果和文章正文
- 聚焦“特朗普讲话/表态”相关报道
- 调用 DashScope 兼容接口生成中文 Markdown 摘要
- 通过 picoclaw 推送到飞书或其他渠道
- 支持 `testsend` / `testsend-live`
- 输出最终发送给渠道的 Markdown 到 `output/`
- 支持 `deploy.py` 部署到远端
- 支持 PM2 托管主进程

## 目录

- [main.py](D:/dev/github/trump-speech-watch/main.py): 每日定时主进程
- [app_common.py](D:/dev/github/trump-speech-watch/app_common.py): `.env`、代理、通知、日志公共能力
- [scripts/zaobao_trump_report.py](D:/dev/github/trump-speech-watch/scripts/zaobao_trump_report.py): Playwright 抓取、正文提取、LLM 摘要
- [deploy.py](D:/dev/github/trump-speech-watch/deploy.py): 上传部署脚本
- [ecosystem.config.js](D:/dev/github/trump-speech-watch/ecosystem.config.js): PM2 配置

## 环境变量

复制 [`.env.example`](D:/dev/github/trump-speech-watch/.env.example) 为 `.env`，至少配置这些项：

```env
SCRAPE_TIME=19:00
SEARCH_QUERY=特朗普
SEARCH_URL=https://www.zaobao.com.sg/sitesearch?r=%E7%89%B9%E6%9C%97%E6%99%AE

DASHSCOPE_API_KEY=你的 DashScope Key
DASHSCOPE_BASE_URL=https://coding.dashscope.aliyuncs.com/v1
DASHSCOPE_MODEL=qwen3.5-plus

PICOCLAW_EXE=/home/nuonuo/picoclaw-linux-amd64
PICOCLAW_CHANNEL=feishu
```

可选：

```env
HTTP_PROXY=127.0.0.1:2334
HTTPS_PROXY=127.0.0.1:2334
```

## 安装

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

建议使用虚拟环境：

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

## 使用

主进程：

```bash
python main.py
```

复用最近一次报告发送测试消息：

```bash
python main.py testsend
```

实时抓取并立即发送：

```bash
python main.py testsend-live
```

## 输出

- `output/`: 搜索结果 JSON、文章详情 JSON，以及最终发送给渠道的 Markdown
- `output/latest_report.md`: 最近一次发送用的 Markdown
- `output/report-YYYYMMDD-HHMMSS.md`: 按时间归档的发送内容
- `logs/`: 主进程日志、PM2 日志
- `state/latest_report.json`: 最近一次完整报告
- `state/main_state.json`: 主进程每日执行状态

## 部署

`.env` 配好 `UPLOAD_HOST / UPLOAD_USER / UPLOAD_PASSWORD` 后执行：

```bash
python deploy.py
```

默认部署到：

```text
/home/nuonuo/app/trump-speech-watch
```

## PM2

```bash
cd /home/nuonuo/app/trump-speech-watch
pm2 start ecosystem.config.js
pm2 save
```
