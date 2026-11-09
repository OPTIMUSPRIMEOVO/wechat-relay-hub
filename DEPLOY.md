# WeChat Relay Hub 部署与使用完整指南 (局域网 / Tailscale 直连精简版)

本文档针对 **局域网 / Tailscale 直连模式（无需 Cloudflare 域名与穿透配置）** 编写。
所有设备只需处于 **同一局域网 Wi-Fi** 或 **连接同一个 Tailscale 网络**，即可完成通信。

---

## 一、 获取老旧 VAIO 笔记本的 IP 地址

在开始之前，我们需要先知道中枢服务端（老旧 VAIO 笔记本）的 IP 地址，供手机和主力 PC 电脑连接。

1. 在 **VAIO 笔记本** 上按下键盘上的快捷键：`Win + R`，输入 `cmd` 并按回车。
2. 在黑框窗口中输入以下命令并回车：
   ```cmd
   ipconfig
   ```
3. 查看输出结果：
   - **方式 A（家里 Wi-Fi 局域网直连，推荐）**：
     找到 `无线局域网适配器 WLAN` 下面的 **`IPv4 地址`**，通常形式为：`192.168.x.xxx`（例如 `192.168.1.50`）。
   - **方式 B（Tailscale 内网直连）**：
     找到名为 `Tailscale` 的网卡下面的 **`IPv4 地址`**，通常形式为：`100.x.y.z`。
4. **记下这个 IP**：假设为 `192.168.1.50`，那么你的中枢服务地址就是：
   👉 **`http://192.168.1.50:8000`**

---

## 二、 在 VAIO 老旧笔记本上运行中枢服务端

由于服务端已重构成单个 Go 原生程序（内存仅约 10MB，零依赖、免装虚拟机）：

1. 将编译好的服务端压缩包（或项目中的 `server_go/` 文件夹）：
   - `wechat-relay-hub.exe`
   - `config.json`（已默认配置为监听 `0.0.0.0:8000`）
   - `windows_vaio_deploy/`
   拷贝到 VAIO 笔记本的任意目录（例如 `D:\WeChatRelay\`）。
2. **启动运行（二选一）**：
   - **测试调试运行**：直接鼠标双击 `wechat-relay-hub.exe`，弹出一个小黑框显示 `listening on http://0.0.0.0:8000` 即可。
   - **开机静默无窗口运行（推荐日常使用）**：
     进入 `windows_vaio_deploy/` 文件夹，右键以管理员身份运行 `install_vaio_services.bat`，即可实现开机自动静默后台运行（完全隐藏黑框，任务管理器中占用约 10MB 内存）。

---

## 三、 在 Sony XZ1 Compact 手机端配置

1. 在 Sony XZ1 Compact 手机上安装好 `app-debug.apk`。
2. 打开 **WeChat Relay** App：
   - 首次弹出权限请求时，勾选允许「通知权限」和「忽略电池优化」（防止系统息屏杀后台）。
   - 在弹出的「中枢服务器连接设置」弹窗中输入：
     - **中枢服务器地址**：填入前面记下的 VAIO 地址，例如：
       `http://192.168.1.50:8000`
     - **访问密码**：保持默认密码 `xz1c-secret-2026`（如在 config.json 修改过则填你的新密码）。
   - 点击 **「保存并连接」**。
3. 连接成功后：
   - App 顶部圆点会立刻变绿。
   - 顶部状态胶囊显示 `🟡 手机备用`（因为此时你的主力 PC 微信节点还没开机）。

---

## 四、 在主力 Windows PC 电脑上运行微信节点（详细指南）

主力 PC 电脑运行 `client_pc/pc_agent.py` 脚本，负责维持与中枢的 10 秒心跳、将微信捕获的消息上报、并执行下行发送指令。

你可以选择以下 **3 种方式之一** 进行运行：

---

### 方式 1：双击一键批处理脚本（最省心、推荐小白）

在 `client_pc/` 文件夹内，我们已经准备好了一键启动脚本：
1. 打开 `d:\Wechat App Cra\client_pc\`。
2. 鼠标双击运行 **`start_pc_agent.bat`**。
3. 窗口会提示输入 VAIO 地址：
   - 输入你前面记下的地址（例如 `http://192.168.1.50:8000`，如果在同台电脑测试直接按回车）。
4. 脚本会自动安装所需依赖并启动节点！

---

### 方式 2：使用 PyCharm 运行与调试（推荐开发者）

如果你习惯使用 PyCharm，按以下步骤配置即可一键点击绿色三角运行：

#### 步骤 1：在 PyCharm 中打开项目
1. 启动 PyCharm -> 点击 **File** -> **Open**。
2. 选中并打开目录：`d:\Wechat App Cra`。

#### 步骤 2：配置 Python 解释器
1. 在 PyCharm 右下角查看当前 Python 解释器（建议 Python 3.10 或 3.11）。
2. 打开 PyCharm 内置的终端（**Terminal**），执行一行命令安装依赖：
   ```powershell
   pip install -r client_pc/requirements.txt
   ```

#### 步骤 3：配置运行参数（Run Configuration）
1. 在左侧项目文件树展开 `client_pc` 文件夹，找到 `pc_agent.py`。
2. 右键点击 `pc_agent.py` -> 选择 **Modify Run Configuration...**（或 Edit Configuration）。
3. 在弹出的配置窗口中：
   - **Script path**：确保为 `.../client_pc/pc_agent.py`
   - **Parameters（运行参数）**：填入中枢地址与密钥，例如：
     ```text
     --url http://192.168.1.50:8000 --key node-static-key-9527 --simulate
     ```
     *(注：`--simulate` 参数会在启动 2 秒后自动模拟发送一条测试消息，方便验证；日常稳定运行后可去掉该参数)*
   - **Working directory**：设置为 `d:\Wechat App Cra\client_pc`
4. 点击 **Apply** 并确定。

#### 步骤 4：点击运行
- 点击右上角的 **绿色三角 ▶（Run）** 或 **小甲虫 🪲（Debug）**。
- 下方控制台输出如下信息即代表连接成功：
  ```text
  Connected to Relay Hub as PC Primary Node (🟢 PC_ONLINE)
  ```
- 此时观察你的 Sony XZ1 Compact 手机 App，顶栏徽标会瞬间从 `🟡 手机备用` 变为 `🟢 PC主节点`！

---

### 方式 3：使用系统命令行（CMD / PowerShell）手动运行

如果你喜欢手敲命令：
1. 打开 PowerShell 或 CMD，进入 PC 客户端目录：
   ```powershell
   cd "d:\Wechat App Cra\client_pc"
   ```
2. 安装依赖（首次运行需执行）：
   ```powershell
   pip install -r requirements.txt
   ```
3. 运行脚本：
   ```powershell
   python pc_agent.py --url http://192.168.1.50:8000 --key node-static-key-9527 --simulate
   ```

---

## 五、 全链路主备自动切换联调验证

按照以上步骤启动后，你可以亲手验证这套系统的“高可用主备切换”机制：

1. **PC 在线状态**：
   - PC 节点运行后，XZ1 手机 App 显示 `🟢 PC主节点`，并收到一条来自 PC 的模拟测试消息。
   - 此时在手机端回复消息，该消息会直接下发给 PC 端的 `pc_agent.py` 处理。
2. **PC 关机自动降级（Failover）**：
   - 在 PyCharm 或命令行中按 `Ctrl + C`（或点击红色方块停止运行）关闭 `pc_agent.py`。
   - 15 秒后，中枢检测不到 PC 心跳，XZ1 手机 App 顶栏状态会自动切回 `🟡 手机备用`。
   - 此时在手机端回复消息，中枢会自动将发送指令路由至备用手机（Tasker）队列中代发。
3. **PC 重新开机**：
   - 重新启动 `pc_agent.py`，手机端又会自动切回 `🟢 PC主节点`，全过程全自动感应！
