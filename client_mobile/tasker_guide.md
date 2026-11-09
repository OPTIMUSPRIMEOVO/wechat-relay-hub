# Android 备用节点对接指南 (Tasker + AutoNotification)

本指南指导如何在 24 小时在线的 Android 主力机（小米 / 三星）上，利用 **Tasker + AutoNotification** プラグイン实现：
1. **上行捕获**：监听微信通知栏新消息并实时 POST 到中枢服务器 `/api/webhook/message`（由中枢负责与 PC 端 5 秒窗口自动去重）。
2. **下行回复**：当 PC 关机处于 `PC_OFFLINE` 状态时，接收中枢下发的回复指令，直接调用 Android 原生 `Notification Reply Action` 静默回复微信消息（无需亮屏或打开微信界面）。

---

## 一、 前置准备与权限设置

1. **微信通知设置**：
   - 打开主力机微信 -> `我` -> `设置` -> `新消息通知` -> 开启**接收新消息通知**与**通知显示消息详情**（必须显示发信人和内容摘要）。
2. **安装软件**：
   - 安装 **Tasker** 与 **AutoNotification**。
   - 授予 AutoNotification「通知使用权 / Notification Access」，并在 MIUI / OneUI 中将 Tasker 与 AutoNotification 设为**「无限制后台运行 / 锁定后台 / 允许自启动」**。
3. **在 Tasker「VARS (变量)」标签页创建 2 个全局变量**：
   - `%RELAY_URL`：`https://你的域名.example.com`（末尾不要加斜杠）
   - `%NODE_API_KEY`：`node-static-key-9527`（与服务端 `.env` 中一致）

---

## 二、 配置文件 1：监听微信通知并上报中枢 (上行链路)

### 1. 创建触发器 (Profile)
- 点击 `+` -> **Event (事件)** -> **Plugin (插件)** -> **AutoNotification** -> **Intercept**。
- 配置项：
  - **Action Type**：`Created` (仅在新通知产生时触发)
  - **Persistency Type**：`Non-Persistent Only`
  - **Apps**：勾选 `微信 (com.tencent.mm)`
  - **Has Reply Action**：勾选（过滤掉“微信正在后台运行”或语音通话提示等不可回复的系统通知，只捕获真正的聊天消息！）

### 2. 绑定执行任务 (Task: `WeChat_Uplink_To_Hub`)
AutoNotification 触发后会自动注入以下局部变量：
- `%antitle`：通知标题（私聊为**好友昵称/备注**；群聊为**群聊名称**）
- `%antext`：通知正文（私聊为`消息内容` 或 `[2条] 消息内容`；群聊通常为`群成员昵称: 消息内容`）
- `%anreplyaction`：该会话对应的原生通知快捷回复 Action ID

在任务中按顺序添加以下 Action：

#### Step 1：添加 **JavaScriptlet**（清洗通知文本并识别群聊/私聊）
在 Tasker 中添加 `Code -> JavaScriptlet`，粘贴以下代码：

```javascript
// 1. 去除微信通知前可能附带的未读条数前缀，例如 "[3条] "
var rawTitle = (local("antitle") || "").trim();
var rawText = (local("antext") || "").replace(/^\[?\d+条\]?\s*/, "").trim();

var isGroup = false;
var roomName = "";
var sender = rawTitle;
var content = rawText;

// 2. 判断是否为群聊格式 ("群成员: 消息内容")
// 如果通知正文包含 ": " 且冒号前长度合理，可判定为群消息（也可根据自己常用的群名称列表进一步精确匹配）
var colonIdx = rawText.indexOf(": ");
if (colonIdx > 0 && colonIdx <= 24) {
    var possibleSender = rawText.substring(0, colonIdx).trim();
    var possibleBody = rawText.substring(colonIdx + 2).trim();
    // 当标题不是发送者本人时，判定为群聊
    if (possibleSender && possibleBody && possibleSender !== rawTitle) {
        isGroup = true;
        roomName = rawTitle;
        sender = possibleSender;
        content = possibleBody;
    }
}

var payloadObj = {
    sender: sender,
    content: content,
    timestamp: Math.floor(Date.now() / 1000),
    source_node: "mobile",
    is_group: isGroup,
    room_name: roomName
};

setLocal("json_body", JSON.stringify(payloadObj));
```

#### Step 2：添加 **HTTP Request (发送 Webhook)**
- **Method**：`POST`
- **URL**：`%RELAY_URL/api/webhook/message`
- **Headers**：
  ```text
  Content-Type:application/json
  X-API-Key:%NODE_API_KEY
  ```
- **Body**：`%json_body`
- **Timeout**：`10` 秒
- **Continue Task After Error**：勾选

---

## 三、 配置文件 2：接收中枢回复指令并静默发送 (下行链路)

中枢服务器专门为 Android Tasker 提供了**无需公网 IP、无需维持脆弱 WebSocket** 的长轮询拉取接口：`GET /api/node/commands?node_type=mobile&wait=20`。
> 当 PC 处于 `PC_ONLINE` 时，中枢会把回复直接发给 PC，手机端轮询返回空数组；当 PC 关机处于 `PC_OFFLINE` 时，用户在 Sony XZ1 Compact 上点击发送的瞬间，长轮询会立即唤醒并在 0.1 秒内返回回复指令！

### 1. 创建触发器 (Profile)
- 点击 `+` -> **Time (时间)** -> 取消起止时间限制，勾选 **Every (每隔) 2 分钟**（或搭配 Tick 事件循环触发）。

### 2. 绑定执行任务 (Task: `WeChat_Downlink_Worker`)
在任务属性（右上角齿轮）中将 **Collision Handling (冲突处理)** 设为：`Abort New Task (中止新任务)`，防止重复运行。

按顺序添加以下 Action：

#### Step 1：标签 `Loop_Start` (Action -> Task -> Anchor)
定义一个锚点标签 `Loop_Start` 以实现连续长轮询。

#### Step 2：添加 **HTTP Request**
- **Method**：`GET`
- **URL**：`%RELAY_URL/api/node/commands?node_type=mobile&wait=20`
- **Headers**：
  ```text
  X-API-Key:%NODE_API_KEY
  ```
- **Timeout**：`28` 秒
- **Continue Task After Error**：勾选

#### Step 3：添加 **JavaScriptlet**（解析待回复指令）
```javascript
var count = 0;
var targetTitle = "";
var replyText = "";

try {
    var res = JSON.parse(local("http_data") || "{}");
    if (res && res.commands && res.commands.length > 0) {
        count = res.commands.length;
        // 取出第一条指令执行（若有多条可在后续循环中依次消费）
        var cmd = res.commands[0];
        targetTitle = cmd.is_group && cmd.room_name ? cmd.room_name : cmd.target;
        replyText = cmd.content;
    }
} catch (e) {
    count = 0;
}

setLocal("cmd_count", String(count));
setLocal("target_title", targetTitle);
setLocal("reply_text", replyText);
```

#### Step 4：条件分支 `If %cmd_count > 0`
添加 **Plugin -> AutoNotification -> Reply**：
- **Intercept Action ID**：留空（使用下方通知标题精确匹配目标会话的最新通知）
- **Notification Apps**：勾选 `微信`
- **Notification Title**：`%target_title` (Exact Match 精确匹配好友备注或群名)
- **Reply Text**：`%reply_text`

添加 `End If`。

#### Step 5：循环跳回 `Goto -> Label: Loop_Start`
- 添加 `Task -> Wait`：`1` 秒（若上一轮 HTTP 报错可等待 5 秒）。
- 添加 `Task -> Goto` -> 跳转到 `Loop_Start`。

---

## 四、 接口 JSON 契约速查表

### 1. 上行推送接口：`POST /api/webhook/message`
- **Request Headers**:
  - `Content-Type: application/json`
  - `X-API-Key: node-static-key-9527`
- **Request Body 示例 (私聊)**:
  ```json
  {
    "sender": "张三",
    "content": "今晚几点到？",
    "timestamp": 1762672000,
    "source_node": "mobile",
    "is_group": false,
    "room_name": ""
  }
  ```
- **Request Body 示例 (群聊)**:
  ```json
  {
    "sender": "李四",
    "content": "@全体成员 周末聚餐",
    "timestamp": 1762672005,
    "source_node": "mobile",
    "is_group": true,
    "room_name": "相亲相爱一家人"
  }
  ```

### 2. 下行拉取接口：`GET /api/node/commands?node_type=mobile&wait=20`
- **Request Headers**:
  - `X-API-Key: node-static-key-9527`
- **Response Body 示例**:
  ```json
  {
    "ok": true,
    "node_type": "mobile",
    "count": 1,
    "commands": [
      {
        "command_id": "a1b2c3d4e5f6",
        "action": "send_wechat_msg",
        "target": "张三",
        "content": "我大概八点半到",
        "is_group": false,
        "room_name": "",
        "conv_id": "user:张三",
        "routed_node": "mobile",
        "timestamp": 1762672010
      }
    ]
  }
  ```
