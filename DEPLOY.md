# 部署与联调步骤清单 (v2.0 重构版：VAIO 原生 .exe + XZ1 原生 Android App)

本指南针对 **老旧 Sony VAIO Windows 笔记本（彻底停用 VMware/Docker）** 与 **Sony XZ1 Compact 原生 Android App（带后台保活与通知栏直接回复）** 进行了全链路重构。

---

## 一、 编译或获取可执行文件（支持本地编译 或 GitHub 云端一键编译）

### 方式 A：使用项目内置的 GitHub Actions 免环境云编译（最省心）
无需在本机安装 Go 或庞大的 Android Studio SDK：
1. 将本项目上传到你的 GitHub 私有仓库（推送到 `main` 或 `master` 分支）。
2. 打开 GitHub 仓库的 **Actions** 页面，等待 `Build VAIO Server (.exe) & XZ1 Android App (.apk)` 任务执行完毕（约 2 分钟）。
3. 直接下载构建产物：
   - `vaio-windows-server-bundle.zip`（内含 `wechat-relay-hub.exe`、`wechat-relay-hub-x86.exe`、`config.json` 及一键自启脚本）
   - `sony-xz1-compact-apk.zip`（内含 `app-debug.apk`，直接发到 Sony XZ1 Compact 安装）

### 方式 B：本地直接编译
- **编译 Go 服务端 `.exe`**：
  ```powershell
  cd server_go
  go mod tidy
  go build -ldflags="-s -w" -o wechat-relay-hub.exe .
  ```
- **编译 Sony XZ1 Compact `.apk`**：
  使用 Android Studio 打开 `android_xz1_app` 目录，点击 `Build -> Build Bundle(s) / APK(s) -> Build APK(s)`。

---

## 二、 老旧 VAIO Windows 笔记本原生部署（零虚拟机、总内存 < 30MB）

1. **彻底关闭 VAIO 上的 VMware**，释放数 GB 内存与 CPU。
2. 在 VAIO 上新建文件夹（例如 `C:\WeChatRelay\`），放入以下文件：
   - `wechat-relay-hub.exe`（来自 `server_go/`）
   - `config.json`（修改其中的 `client_password` 和 `node_api_key`）
   - `windows_vaio_deploy/` 文件夹
   - 从 Cloudflare 官网下载 Windows 原生单文件 [`cloudflared-windows-amd64.exe`](https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe) 并重命名为 `cloudflared.exe`。
3. **配置 Cloudflare Tunnel 原生 Windows 服务**：
   - 在 Cloudflare Zero Trust 控制台创建 Tunnel，将 Public Hostname（如 `https://wx.example.com`）指向 `http://127.0.0.1:8000`。
   - 在 VAIO 上以管理员身份打开 CMD/PowerShell，执行一行命令将 `cloudflared.exe` 安装为系统服务：
     ```powershell
     C:\WeChatRelay\cloudflared.exe service install 你的_TUNNEL_TOKEN
     net start cloudflared
     ```
4. **一键注册 `wechat-relay-hub.exe` 开机无黑框后台自启**：
   - 右键以「管理员身份」运行 `windows_vaio_deploy\install_vaio_services.bat`。
   - 脚本会自动通过 `start_background_hidden.vbs` 在后台静默启动 `wechat-relay-hub.exe` 并注册 Windows 计划任务。
   - 打开 VAIO 的任务管理器可以看到：`wechat-relay-hub.exe` 仅占用 **~10MB 内存**，无任何命令行黑框！

---

## 三、 Sony XZ1 Compact 安装原生 App 与锁屏通知测试

1. 将编译好的 `app-debug.apk` 安装到 Sony XZ1 Compact。
2. 首次打开 **WeChat Relay** App：
   - 系统会提示「忽略电池优化」，请点击**允许**（防止索尼 STAMINA 模式杀后台）。
   - 在弹出的「中枢服务器连接设置」中输入：
     - 地址：`https://wx.example.com`（即使 XZ1 开着科学上网 VPN 也能直接连）
     - 密码：与 VAIO `config.json` 中一致的 `client_password`。
3. **验证常驻保活与原生通知栏直接回复**：
   - 状态栏会常驻一条静默通知：`WeChat Relay · 🟡 手机备用节点代发 (实时通道已连接)`。
   - **锁屏或退回桌面**，当有微信新消息到达时，XZ1 Compact 会立刻**震动、响铃并弹出原生通知横幅**。
   - 直接下拉通知栏点击 **「直接回复」**，输入文字点发送，即可在不打开 App 的情况下秒回微信！

---

## 四、 启动主力 Windows PC 节点与 Android 备用节点

1. **主力 Windows PC 节点**：
   ```powershell
   pip install -r client_pc/requirements.txt
   python client_pc/pc_agent.py --url https://wx.example.com --key node-static-key-9527 --simulate
   ```
   - 启动瞬间，Sony XZ1 Compact App 顶栏与通知栏同步变为 `🟢 PC 主节点在线`。
   - 关闭 PC 脚本或关机后，15 秒内自动切回 `🟡 手机备用`。
2. **Android 主力机备用节点**：
   - 按照 [`client_mobile/tasker_guide.md`](file:///d:/Wechat%20App%20Cra/client_mobile/tasker_guide.md) 配置 Tasker + AutoNotification 的通知上报与长轮询回复任务即可。
