# 服务器上线部署说明

四个常驻服务，仓库里都带好了配置，开箱即用：

| 服务 | 端口 | 启动脚本 | systemd 单元 |
|---|---|---|---|
| 控制面（Java Spring） | 8080 | `scripts/run-control-plane.sh` | `deploy/systemd/ai-pr-control-plane.service` |
| worker | — | `scripts/run-worker.sh` | `deploy/systemd/ai-pr-worker.service` |
| Jira 监控 | 8098 | `scripts/run-jira-monitor.sh` | `deploy/systemd/ai-pr-jira-monitor.service` |
| 日志监控 | 8099 | `scripts/run-log-monitor.sh` | `deploy/systemd/ai-pr-log-monitor.service` |

## Linux 服务器（上线测试）

```bash
# 1. 克隆仓库、配好 .env、建好 .venv（pip install -r requirements.txt）
# 2. 装服务（自动把仓库实际路径填进单元文件）
sudo bash scripts/install-systemd.sh

# 3. 常用运维
systemctl status ai-pr-jira-monitor     # 看状态
journalctl -u ai-pr-log-monitor -f      # 看日志
systemctl restart ai-pr-worker          # 重启
```

systemd 单元带 `Restart=always`（崩溃 30 秒后自动拉起）+ 开机自启，
不依赖任何本机路径——安装脚本按仓库实际位置渲染。

## macOS 本地（开发机）

macOS 隐私保护禁止 launchd 后台服务访问桌面目录（已实测），
本地用登录项 applet 方案：`~/Applications/ai-pr-monitors-startup.app`
（已注册进登录项，隐藏运行；脚本幂等，已在运行就不会重复启动）。
重启后首次运行若弹「想访问桌面上的文件」，点允许后永久生效。

也可以手动跑：`nohup scripts/run-jira-monitor.sh > /tmp/jira-monitor-8098.log 2>&1 &`

## worker 的 GitHub token

worker 需要 `GITHUB_ISSUE_TOKEN`：服务器上写进 `.env`；
本地没写时自动退回 `gh auth token`（本机 gh 登录态）。
