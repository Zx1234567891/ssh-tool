# SSH 隧道助手

![SSH 隧道助手 Logo](assets/logo.png)

一个面向 Windows 的多主机 SSH 反向代理管理器。界面使用 PyQt6，采用简洁的双栏布局；每台主机的代理隧道独立于 VSCode Remote SSH 连接运行，避免 `RemoteForward` 跟随 VSCode 重连而反复抢占端口。

## 主要功能

- 自动导入 `%USERPROFILE%\.ssh\config` 中的主机
- 每台主机单独设置远程代理端口和自动重连
- 独立运行 `ssh -NT -R`；终端和检测连接会自动清除额外转发，避免重复抢占端口
- 一键打开 SSH 终端、VSCode Remote SSH 和远程 Codex
- 自动寻找 `Code.exe`；也可在设置中手动选择 VSCode 安装路径
- 检测本地代理、SSH、远程代理以及 Codex
- 写入远程代理环境变量前自动备份 `~/.bashrc`
- 关闭窗口后驻留系统托盘

如果某个主机在 SSH config 中仍有 `RemoteForward`，专用隧道会同时应用它。建议把旧转发迁移到软件后从 SSH config 删除，以免建立不需要的额外端口。

## 日常使用

1. 启动软件，首次会自动导入 SSH 配置。
2. 选择主机，确认远程代理端口；不同主机可以使用同一个端口。
3. 点击“启动隧道”。状态变为“已连接”后再打开 VSCode 或 Codex。
4. 首次使用某台主机时，可点击“配置远程环境”，让远程命令行自动使用该隧道。

软件只保存主机别名和端口等设置，不保存 SSH 密码或私钥。

## 开发与打包

```powershell
python -m unittest discover -s tests -v
python app.py --self-test
.\build.ps1
```

生成的程序位于 `dist\SshTunnelManager.exe`。

## Release

推送形如 `v1.0.0` 的标签后，GitHub Actions 会在 Windows 环境运行测试、重新构建，并把 EXE 发布到对应的 GitHub Release。
