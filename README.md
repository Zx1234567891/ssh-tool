# SSH 隧道助手

![SSH 隧道助手 Logo](assets/logo.png)

一个面向 Windows 的多主机 SSH 反向代理管理器。界面使用 PyQt6，采用简洁的双栏布局；每台主机的代理隧道独立于 VSCode Remote SSH 连接运行，避免 `RemoteForward` 跟随 VSCode 重连而反复抢占端口。

## 主要功能

- 自动导入 `%USERPROFILE%\.ssh\config` 中的主机
- 可在软件内新建 SSH 连接；写入配置前自动备份原 `~/.ssh/config`
- 每台主机单独设置远程代理端口和自动重连
- 已连接主机自动置顶，断开后恢复原有列表顺序
- 升级或重复启动时自动识别已有隧道，避免状态误报和重复抢占端口
- 独立运行 `ssh -NT -R`；终端和检测连接会自动清除额外转发，避免重复抢占端口
- 一键打开 SSH 终端、VSCode Remote SSH 和远程 Codex
- 自动寻找 `Code.exe`；也可在设置中手动选择 VSCode 安装路径
- 打开 VSCode 前可选择远程工作区，并按主机保存最近使用的文件夹
- 复用日常 VSCode 的设置、字体、主题和扩展；软件启动与 VSCode 直接连接界面一致
- 自动安装本机 VSCode 配套组件，按远程窗口为新终端注入该主机的代理端口
- 每台 Windows 电脑在本地保存各主机端口；多电脑、多主机之间不会互相覆盖
- 检测本地代理、SSH、远程代理以及 Codex
- VSCode 代理不再依赖修改远程 `.bashrc` 或重启共享的 VSCode Server
- 关闭窗口后驻留系统托盘
- 清晰的齿轮“设置”按钮，可快速打开全局配置

如果某个主机在 SSH config 中仍有 `RemoteForward`，专用隧道会同时应用它。建议把旧转发迁移到软件后从 SSH config 删除，以免建立不需要的额外端口。

## 日常使用

1. 启动软件，首次会自动导入 SSH 配置。
2. 选择主机，确认远程代理端口；不同主机可以使用同一个端口。
3. 点击“启动隧道”。状态变为“已连接”后再打开 VSCode 或 Codex。
4. 软件会自动安装本机 VSCode 配套组件；从软件打开远程文件夹后，新建终端会自动使用该主机端口。

同一个远程用户可以从多台电脑同时登录。例如电脑 A 使用 `10099`、电脑 B 使用 `10098`；端口映射和 VSCode 配套组件都位于各自 Windows 本机，不会修改远端共享配置。已经打开的终端不会被改变，新建终端自动生效。

软件只保存主机别名和端口等设置，不保存 SSH 密码或私钥。
新建连接时可引用已有私钥文件，但软件不会复制私钥内容。只有密码登录的主机可先从 SSH 终端连接，自动隧道建议使用密钥认证。

## 开发与打包

```powershell
python -m unittest discover -s tests -v
python app.py --self-test
.\build.ps1
```

生成的程序位于 `dist\SshTunnelManager.exe`。

## Release

推送形如 `v1.0.0` 的标签后，GitHub Actions 会在 Windows 环境运行测试、重新构建，并把 EXE 发布到对应的 GitHub Release。
