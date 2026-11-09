# 项目任务：构建轻量化、主备高可用的微信消息中转系统 (WeChat Relay Hub v2.0 重构版)

## 1. 背景与硬件/网络约束 (v2.0 架构重构)
我需要构建一套自用的微信消息中转系统，以便在一台不安装微信、也不开启内网 VPN (Tailscale) 的小屏备用手机（**Sony XZ1 Compact**，4.6英寸屏幕，Android 8/9）上，直接收发微信消息并获得**原生锁屏推送通知与通知栏直接回复体验**。

请严格遵循以下真实硬件与网络环境约束：
- **核心中枢服务器（VAIO Server）**：部署在异地的一台**老旧 Sony VAIO Windows 笔记本**上。
  - **关键约束**：老笔记本跑 VMware 虚拟机或 Docker 开销过大，**严禁使用 VMware / Docker / Redis / MySQL**！
  - **重构方案**：服务端采用 **Go 语言编译为单个独立的 Windows 原生 `.exe` 可执行文件 (`wechat-relay-hub.exe`)**，配合 Cloudflare 官方原生 Windows 客户端 **`cloudflared.exe`** 直接在 VAIO 的 Windows 宿主机后台静默运行（合计内存占用 **< 30MB**，CPU 占用接近 0%，无需安装任何虚拟机或运行环境）。
- **主节点（PC Node）**：我的主力 Windows 电脑，平时运行微信 PC 端，但不会 24 小时开机。
- **备用节点（Mobile Node）**：已登录微信的 Android 主力机（小米/三星），24 小时在线，通过通知栏监听与快捷回复（Tasker + AutoNotification）与中枢通信。
- **客户端（XZ1 Compact Native App）**：
  - **痛点解决**：移动端网页退到后台或锁屏后无法可靠收到新消息通知，因此改为**纯原生 Kotlin 极简 Android App (`XZ1WeChatRelay.apk`)**。
  - **特性**：内置 Android `ForegroundService` 常驻保活 WebSocket，支持锁屏响铃震动、高优先级横幅通知、以及 **Android 原生通知栏直接快捷回复 (`RemoteInput`)**，且完全兼容 XZ1 常驻开启代理软件（科学上网 VPN）通过 Cloudflare Tunnel 公网 HTTPS/WSS 访问中枢。

---

## 2. 核心模块与架构设计 (v2.0)

### 模块一：Go 单文件 Windows 原生中枢服务端 (`server_go/`)
1. **极致轻量**：纯 Go 编写（零 CGO 依赖），编译后为单个 ~7MB 的 `.exe` 文件，运行时内存 `< 12MB`，使用内存结构 + 原子 JSON 快照 (`relay_store.json`) 持久化最近会话。
2. **安全鉴权**：恒定时间校验客户端 `CLIENT_PASSWORD` 与节点侧静态 `NODE_API_KEY`。
3. **心跳与主备路由状态机 (Failover Routing)**：
   - `/api/heartbeat` 与 `/ws/node` 接收 PC 节点每 10 秒心跳，超过 15 秒未收到心跳则立即判定 `PC_OFFLINE` 并实时通知 XZ1 App 切换状态徽标。
   - 下行回复优先路由给 PC 主节点；若 `PC_OFFLINE` 或投递瞬间断开，**0 延迟自动降级**路由给 Android 备用节点。
4. **5 秒滑动窗口内存去重 (`MessageDeduplicator`)**：
   - 当 PC 与主力手机同时在线并上报同一条微信消息时，根据 `is_group|room_name|sender|content` 的 MD5 指纹在 5 秒窗口内精确去重，确保 XZ1 App 只收到一次推送与通知。

### 模块二：Sony XZ1 Compact 纯原生 Kotlin 客户端 App (`android_xz1_app/`)
1. **前台常驻保活服务 (`RelayForegroundService.kt`)**：
   - 使用 `START_STICKY` + 低优先级状态栏常驻通知（实时显示 `🟢 PC 主节点在线` 或 `🟡 手机备用节点代发`），内置 `OkHttp` WebSocket 自动重连，并申请加入 Android 电池优化白名单，防止息屏断连。
2. **原生系统通知 + 通知栏直接回复 (`NotificationHelper.kt` & `DirectReplyReceiver.kt`)**：
   - 锁屏或后台收到新消息时立即触发 `IMPORTANCE_HIGH` 横幅通知、震动与提示音。
   - 通知直接挂载 Android 原生 `RemoteInput` 输入框，无需点进 App 即可在下拉通知栏/锁屏直接打字回复微信。
3. **4.6 英寸紧凑原生界面 (`MainActivity.kt`)**：
   - 纯代码构建原生视图（零 XML 膨胀开销），包含 40dp 极简顶栏、36dp 单手大拇指快切会话胶囊栏、`RecyclerView` 微信风格气泡流及底部自适应软键盘输入栏。

### 模块三：PC 节点脚本 (`client_pc/pc_agent.py`) 与 Android 备用节点指南 (`client_mobile/tasker_guide.md`)
- PC 节点维持 `/ws/node` 与 10s 心跳，支持对接 `wxauto` / `WeChatFerry` / `GeweChat`。
- Android 主力机通过 Tasker + AutoNotification 实现通知监听上报与长轮询静默回复。
