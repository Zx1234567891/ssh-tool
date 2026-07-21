# SSH 隧道助手功能概览

## 产品定位

SSH 隧道助手是一个面向 Windows 的多主机 SSH 反向代理管理器。它使用独立的 `ssh -NT -R` 进程，把远程服务器的代理端口连接到本机 Clash 或其他 HTTP 代理，并为远程终端、VSCode Remote SSH 和 Codex 提供统一入口。

```text
远程 Codex
  → 远程 127.0.0.1:<代理端口>
  → SSH 反向隧道
  → 本机 Clash 端口
  → Clash 节点
  → OpenAI
```

## 当前功能

- 从 SSH config 按需导入主机，也可以写入新 Host；写入前自动备份。
- 软件独立保存服务器清单和自定义顺序，支持拖拽排序及无损移除导入项。
- 每台主机独立设置远程代理端口、自动启动、自动重连和远程工作区。
- 独立监管 SSH 反向隧道，使用 keepalive、转发失败检测和退避重连。
- 识别由其他应用实例创建的相同隧道，避免重复占用端口。
- 检测本机代理、SSH、远程端口、远程代理、OpenAI 和 Codex。
- 用绿、黄、红、灰状态展示完整链路健康情况。
- 安装远程 `stm_proxy_use`/`stm_proxy_off` Shell 集成。
- 一键启动 SSH 终端、VSCode Remote SSH 和远程 Codex。
- 按主机记录最近使用的远程目录，在左侧服务器树和文件夹选择器中快速复用。
- 保存滚动文本日志和结构化 JSONL 事件日志。
- 实时采集 SSH stderr，支持诊断模式 `ssh -vv`。
- 为 Codex 设置 `RUST_LOG` 和独立远程日志目录。
- 查看 Codex、VSCode Server 日志，并导出脱敏诊断包。
- 可选连接 Clash External Controller，显示当前策略和节点。
- 使用 schema v2 保存配置，升级前备份并保护高版本配置。
- 新安装保持空主机列表，不再未经用户确认自动导入。
- 使用 Inno Setup 生成当前用户安装器，并保留用户数据覆盖升级。
- 检查 GitHub Release 更新、校验 SHA-256，并在更新后恢复原有隧道。

## 数据位置

```text
%APPDATA%\SshTunnelManager\config.json       用户配置
%APPDATA%\SshTunnelManager\backups\         配置迁移备份
%LOCALAPPDATA%\SshTunnelManager\state.json  本机运行和更新状态
%LOCALAPPDATA%\SshTunnelManager\logs\       应用及事件日志
%LOCALAPPDATA%\SshTunnelManager\diagnostics 诊断包
%USERPROFILE%\.ssh\config                    用户 SSH 配置
```

软件不保存 SSH 密码或私钥内容，也不进行 HTTPS 中间人截流。

## GitHub 同类项目搜索词

```text
SSH reverse tunnel manager Windows GUI
SSH RemoteForward manager
reverse SSH port forwarding GUI
PyQt SSH tunnel manager
SSH tunnel health monitor
SSH tunnel observability
VSCode Remote SSH proxy manager
Clash SSH reverse tunnel
```
