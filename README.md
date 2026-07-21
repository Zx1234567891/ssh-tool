# SSH 隧道助手

![SSH 隧道助手 Logo](assets/logo.png)

一个面向 Windows 的多主机 SSH 反向代理管理器。界面使用 PyQt6，采用简洁的双栏布局；每台主机的代理隧道独立于 VSCode Remote SSH 连接运行，避免 `RemoteForward` 跟随 VSCode 重连而反复抢占端口。

## 主要功能

- 新安装保持空列表，可按需导入 `%USERPROFILE%\.ssh\config` 中的主机
- 可在软件内新建 SSH 连接；写入配置前自动备份原 `~/.ssh/config`
- 每台主机单独设置远程代理端口和自动重连
- 左侧服务器列表由软件独立维护；删除不会修改 SSH config，重新导入时可再次添加
- 服务器支持拖拽自定义顺序；展开后可单击默认或历史工作区直接打开 VSCode
- 升级或重复启动时自动识别已有隧道，避免状态误报和重复抢占端口
- 独立运行 `ssh -NT -R`；终端和检测连接会自动清除额外转发，避免重复抢占端口
- 一键打开 SSH 终端、VSCode Remote SSH 和远程 Codex
- 自动寻找 `Code.exe`；也可在设置中手动选择 VSCode 安装路径
- 打开 VSCode 前可选择远程工作区，并按主机保存最近使用的文件夹
- 复用日常 VSCode 的设置、字体、主题和扩展；软件启动与 VSCode 直接连接界面一致
- 每台 Windows 电脑通过本机 SSH 配置保存各主机端口；新终端自动使用该电脑选择的远程代理端口
- 检测本地代理、SSH、远程代理以及 Codex
- 分层检测远程 Codex、远程端口、SSH 隧道、本机 Clash、可选 Clash 节点和 OpenAI
- 用绿、黄、红、灰节点展示链路健康状态，并提供深度诊断详情
- 保存滚动文本日志和结构化 JSONL 事件日志，实时记录 SSH stderr
- 可查看远程 Codex、远程 VSCode Server 日志，并打开 VSCode 本机日志目录
- 一键导出脱敏诊断包，不进行 HTTPS 截流
- 安装不含固定端口的远程代理切换器，避免同一用户从多台电脑登录时互相覆盖
- 关闭窗口后驻留系统托盘
- 清晰的齿轮“设置”按钮，可快速打开全局配置
- 支持 GitHub Release 更新检查、SHA-256 校验和更新后恢复隧道

如果某个主机在 SSH config 中仍有 `RemoteForward`，专用隧道会同时应用它。建议把旧转发迁移到软件后从 SSH config 删除，以免建立不需要的额外端口。

## 日常使用

1. 启动软件；新安装默认保持空列表，可选择“从 SSH 导入”或“添加”。
2. 选择主机，确认远程代理端口；不同主机可以使用同一个端口。
3. 点击“启动隧道”。状态变为“已连接”后再打开 VSCode 或 Codex。
4. 使用“深度诊断”检查完整代理链路；绿色表示最近成功，红色表示明确失败，灰色表示未检测。
5. 在左侧按住服务器上下拖动可保存自定义顺序；展开服务器即可点击最近工作区直接打开 VSCode。
6. 首次使用某台主机时，可点击“安装代理切换器”。软件会把本机端口传给远端；此后无论从软件还是普通 VSCode 连接，新终端都会自动使用该端口。

同一个远程用户可以从多台电脑同时登录。例如电脑 A 使用 `10099`、电脑 B 使用 `10098`；端口分别保存在两台电脑自己的 SSH 配置中，不会改写远端共享的固定端口。手动登录时仍可运行 `stm_proxy_use 10099` 或 `stm_proxy_off`。

软件只保存主机别名和端口等设置，不保存 SSH 密码或私钥。
新建连接时可引用已有私钥文件，但软件不会复制私钥内容。只有密码登录的主机可先从 SSH 终端连接，自动隧道建议使用密钥认证。

## 用户数据与日志

- 用户配置：`%APPDATA%\SshTunnelManager\config.json`
- 配置迁移备份：`%APPDATA%\SshTunnelManager\backups`
- 日志：`%LOCALAPPDATA%\SshTunnelManager\logs`
- 诊断包：`%LOCALAPPDATA%\SshTunnelManager\diagnostics`
- 更新缓存：`%LOCALAPPDATA%\SshTunnelManager\updates`

配置使用带版本号的 schema。升级旧配置前会自动备份；如果配置来自当前程序不支持的更高版本，程序会禁止覆盖保存。日志和诊断包会对常见密码、Token、Cookie 和 Authorization 信息进行脱敏。

## 开发与打包

```powershell
python -m unittest discover -s tests -v
python app.py --self-test
python app.py --ui-self-test
.\build.ps1
.\build.ps1 -BuildInstaller
```

普通构建生成便携版；安装器构建需要 Inno Setup 6。产物包括：

```text
dist\SshTunnelManager-Portable-<版本>.exe
dist\SshTunnelManager-Setup-<版本>.exe
dist\SHA256SUMS.txt
```

## Release

推送形如 `v1.7.0` 的标签后，GitHub Actions 会在 Windows 环境运行测试，构建便携版和 Inno Setup 安装器，并发布到对应的 GitHub Release。
