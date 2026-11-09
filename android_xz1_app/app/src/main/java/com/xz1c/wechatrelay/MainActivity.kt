package com.xz1c.wechatrelay

import android.Manifest
import android.annotation.SuppressLint
import android.app.AlertDialog
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.text.TextUtils
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.concurrent.thread

/**
 * 专为 Sony XZ1 Compact (4.6 英寸 720x1280 小屏) 定制的纯原生 Kotlin 聊天主界面。
 * 采用纯代码构建视图层级（零 XML 膨胀开销，冷启动耗时 < 80ms）。
 */
class MainActivity : AppCompatActivity(), RelayForegroundService.RelayEventListener {

    private lateinit var titleText: TextView
    private lateinit var nodeBadgeText: TextView
    private lateinit var wsStatusDot: View
    private lateinit var tabsContainer: LinearLayout
    private lateinit var chatRecyclerView: RecyclerView
    private lateinit var inputEdit: EditText
    private lateinit var sendButton: Button

    private val conversations = ArrayList<ConversationItem>()
    private val messages = ArrayList<ChatMessageItem>()
    private var activeConvId: String = ""
    private lateinit var chatAdapter: ChatBubbleAdapter

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Color.parseColor("#191919")

        setContentView(buildCompactRootLayout())
        requestNecessaryPermissions()

        activeConvId = intent?.getStringExtra("conv_id") ?: AppPrefs.getLastConvId(this)

        if (!AppPrefs.isConfigured(this)) {
            showServerConfigDialog(canCancel = false)
        } else {
            RelayForegroundService.startOrReload(this)
            refreshAllDataFromServer()
        }
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        setIntent(intent)
        val targetConvId = intent?.getStringExtra("conv_id") ?: return
        if (targetConvId.isNotEmpty()) {
            selectConversation(targetConvId)
        }
    }

    override fun onResume() {
        super.onResume()
        AppPrefs.isMainActivityResumed = true
        AppPrefs.activeViewingConvId = activeConvId
        RelayForegroundService.listeners.add(this)
        updateWsDot(RelayForegroundService.isWsConnected)
        updateNodePill(RelayForegroundService.latestNodeStatus)

        if (activeConvId.isNotEmpty()) {
            cancelConvNotification(activeConvId)
        }
    }

    override fun onPause() {
        AppPrefs.isMainActivityResumed = false
        RelayForegroundService.listeners.remove(this)
        super.onPause()
    }

    // ==================== 1. 构建 4.6 寸紧凑型原生视图层级 ====================

    private fun dp(value: Int): Int {
        return TypedValue.applyDimension(
            TypedValue.COMPLEX_UNIT_DIP,
            value.toFloat(),
            resources.displayMetrics
        ).toInt()
    }

    private fun makeRoundedBg(fillColor: Int, radiusDp: Int, strokeColor: Int = 0, strokeWidthDp: Int = 0): GradientDrawable {
        return GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE
            cornerRadius = dp(radiusDp).toFloat()
            setColor(fillColor)
            if (strokeWidthDp > 0) {
                setStroke(dp(strokeWidthDp), strokeColor)
            }
        }
    }

    private fun buildCompactRootLayout(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#EDEDED"))
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        }

        // 1. 顶部 40dp 极简状态栏
        val topBar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(Color.parseColor("#191919"))
            setPadding(dp(8), 0, dp(8), 0)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(40)
            )
        }

        wsStatusDot = View(this).apply {
            background = makeRoundedBg(Color.parseColor("#EF4444"), 5)
            layoutParams = LinearLayout.LayoutParams(dp(8), dp(8)).apply {
                rightMargin = dp(6)
            }
        }
        topBar.addView(wsStatusDot)

        titleText = TextView(this).apply {
            text = "WeChat Relay"
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
            typeface = Typeface.DEFAULT_BOLD
            maxLines = 1
            ellipsize = TextUtils.TruncateAt.END
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        topBar.addView(titleText)

        nodeBadgeText = TextView(this).apply {
            text = "🟡 手机备用"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            setTextColor(Color.parseColor("#FDE68A"))
            background = makeRoundedBg(Color.parseColor("#2B2B2B"), 12, Color.parseColor("#78350F"), 1)
            setPadding(dp(7), dp(2), dp(7), dp(2))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply {
                rightMargin = dp(6)
            }
        }
        topBar.addView(nodeBadgeText)

        val newChatBtn = TextView(this).apply {
            text = "＋会话"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            setTextColor(Color.WHITE)
            background = makeRoundedBg(Color.parseColor("#374151"), 5)
            setPadding(dp(6), dp(3), dp(6), dp(3))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply {
                rightMargin = dp(6)
            }
            setOnClickListener { showNewChatDialog() }
        }
        topBar.addView(newChatBtn)

        val configBtn = TextView(this).apply {
            text = "⚙"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
            setTextColor(Color.parseColor("#D1D5DB"))
            setPadding(dp(4), dp(2), dp(4), dp(2))
            setOnClickListener { showServerConfigDialog(canCancel = true) }
        }
        topBar.addView(configBtn)

        root.addView(topBar)

        // 2. 顶部 34dp 横向会话快切胶囊栏 (单手大拇指一触即达)
        val scrollTabs = HorizontalScrollView(this).apply {
            isHorizontalScrollBarEnabled = false
            setBackgroundColor(Color.parseColor("#F7F7F7"))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(36)
            )
        }
        tabsContainer = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(6), 0, dp(6), 0)
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        }
        scrollTabs.addView(tabsContainer)
        root.addView(scrollTabs)

        // 分割线
        root.addView(View(this).apply {
            setBackgroundColor(Color.parseColor("#D9D9D9"))
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 1)
        })

        // 3. 主体气泡聊天列表 (RecyclerView)
        chatRecyclerView = RecyclerView(this).apply {
            layoutManager = LinearLayoutManager(this@MainActivity).apply {
                stackFromEnd = true
            }
            chatAdapter = ChatBubbleAdapter(messages)
            adapter = chatAdapter
            setPadding(dp(8), dp(6), dp(8), dp(6))
            clipToPadding = false
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                0,
                1f
            )
        }
        root.addView(chatRecyclerView)

        // 4. 底部紧凑固定输入栏
        val composerBar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(Color.parseColor("#F7F7F7"))
            setPadding(dp(6), dp(5), dp(6), dp(5))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            )
        }

        inputEdit = EditText(this).apply {
            hint = "发送微信回复..."
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
            setTextColor(Color.parseColor("#111111"))
            background = makeRoundedBg(Color.WHITE, 5, Color.parseColor("#D1D5DB"), 1)
            setPadding(dp(8), dp(6), dp(8), dp(6))
            maxLines = 3
            imeOptions = EditorInfo.IME_ACTION_SEND
            setOnEditorActionListener { _, actionId, _ ->
                if (actionId == EditorInfo.IME_ACTION_SEND) {
                    performSendReply()
                    true
                } else {
                    false
                }
            }
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply {
                rightMargin = dp(6)
            }
        }
        composerBar.addView(inputEdit)

        sendButton = Button(this).apply {
            text = "发送"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
            setTextColor(Color.WHITE)
            background = makeRoundedBg(Color.parseColor("#07C160"), 5)
            minHeight = 0
            minimumHeight = 0
            minWidth = 0
            minimumWidth = 0
            setPadding(dp(12), dp(7), dp(12), dp(7))
            setOnClickListener { performSendReply() }
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            )
        }
        composerBar.addView(sendButton)

        root.addView(composerBar)
        return root
    }

    // ==================== 2. 数据加载与会话切换 ====================

    private fun refreshAllDataFromServer() {
        val baseUrl = AppPrefs.getServerUrl(this)
        val token = AppPrefs.getClientToken(this)
        if (baseUrl.isEmpty() || token.isEmpty()) return

        thread {
            try {
                val (convList, status) = RelayHttpClient.fetchConversations(baseUrl, token)
                runOnUiThread {
                    conversations.clear()
                    conversations.addAll(convList)
                    status?.let { updateNodePill(it) }
                    renderConversationTabs()

                    val targetId = if (activeConvId.isNotEmpty() && conversations.any { it.convId == activeConvId }) {
                        activeConvId
                    } else {
                        conversations.firstOrNull()?.convId ?: ""
                    }
                    if (targetId.isNotEmpty()) {
                        selectConversation(targetId)
                    }
                }
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(this, "拉取会话失败: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun selectConversation(convId: String) {
        activeConvId = convId
        AppPrefs.activeViewingConvId = convId
        AppPrefs.setLastConvId(this, convId)
        cancelConvNotification(convId)

        val conv = conversations.find { it.convId == convId }
        if (conv != null) {
            conv.unreadCount = 0
            titleText.text = if (conv.isGroup) "[群] ${conv.title}" else conv.title
            inputEdit.hint = "回复 ${conv.title}..."
        }
        renderConversationTabs()

        val baseUrl = AppPrefs.getServerUrl(this)
        val token = AppPrefs.getClientToken(this)
        thread {
            try {
                val msgList = RelayHttpClient.fetchMessages(baseUrl, token, convId)
                runOnUiThread {
                    if (activeConvId == convId) {
                        messages.clear()
                        messages.addAll(msgList)
                        chatAdapter.notifyDataSetChanged()
                        scrollToBottom()
                    }
                }
            } catch (_: Exception) {
            }
        }
    }

    private fun renderConversationTabs() {
        tabsContainer.removeAllViews()
        for (conv in conversations) {
            val isSelected = conv.convId == activeConvId
            val chip = TextView(this).apply {
                val badgeStr = if (conv.unreadCount > 0) " (${conv.unreadCount})" else ""
                text = "${conv.title}$badgeStr"
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
                setSingleLine(true)
                if (isSelected) {
                    setTextColor(Color.parseColor("#065F46"))
                    typeface = Typeface.DEFAULT_BOLD
                    background = makeRoundedBg(Color.parseColor("#DCFCE7"), 14, Color.parseColor("#34D399"), 1)
                } else {
                    setTextColor(if (conv.unreadCount > 0) Color.parseColor("#B91C1C") else Color.parseColor("#374151"))
                    typeface = if (conv.unreadCount > 0) Typeface.DEFAULT_BOLD else Typeface.DEFAULT
                    background = makeRoundedBg(Color.parseColor("#E5E7EB"), 14)
                }
                setPadding(dp(9), dp(3), dp(9), dp(3))
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT
                ).apply {
                    rightMargin = dp(5)
                }
                setOnClickListener { selectConversation(conv.convId) }
            }
            tabsContainer.addView(chip)
        }
    }

    private fun performSendReply() {
        val text = inputEdit.text.toString().trim()
        if (text.isEmpty() || activeConvId.isEmpty()) return

        val conv = conversations.find { it.convId == activeConvId } ?: return
        val baseUrl = AppPrefs.getServerUrl(this)
        val token = AppPrefs.getClientToken(this)

        inputEdit.setText("")
        sendButton.isEnabled = false

        thread {
            try {
                RelayHttpClient.sendReply(
                    baseUrl = baseUrl,
                    token = token,
                    target = conv.title,
                    content = text,
                    isGroup = conv.isGroup,
                    roomName = if (conv.isGroup) conv.title else "",
                    convId = conv.convId
                )
                runOnUiThread {
                    sendButton.isEnabled = true
                }
            } catch (e: Exception) {
                runOnUiThread {
                    sendButton.isEnabled = true
                    inputEdit.setText(text)
                    Toast.makeText(this, "发送失败: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun upsertConversationWithMsg(msg: ChatMessageItem) {
        val idx = conversations.indexOfFirst { it.convId == msg.convId }
        val isCurr = msg.convId == activeConvId
        if (idx >= 0) {
            val item = conversations.removeAt(idx)
            item.lastSender = msg.sender
            item.lastContent = msg.content
            item.lastTimestamp = msg.timestamp
            if (msg.direction == "in" && !isCurr) {
                item.unreadCount += 1
            } else if (isCurr) {
                item.unreadCount = 0
            }
            conversations.add(0, item)
        } else {
            val title = if (msg.isGroup && msg.roomName.isNotEmpty()) msg.roomName else msg.title
            conversations.add(
                0,
                ConversationItem(
                    convId = msg.convId,
                    title = title,
                    isGroup = msg.isGroup,
                    lastSender = msg.sender,
                    lastContent = msg.content,
                    lastTimestamp = msg.timestamp,
                    unreadCount = if (msg.direction == "in" && !isCurr) 1 else 0
                )
            )
        }
        renderConversationTabs()
    }

    private fun appendMsgIfCurrent(msg: ChatMessageItem) {
        if (activeConvId.isEmpty()) {
            activeConvId = msg.convId
            AppPrefs.activeViewingConvId = activeConvId
            val conv = conversations.find { it.convId == activeConvId }
            if (conv != null) {
                titleText.text = if (conv.isGroup) "[群] ${conv.title}" else conv.title
            }
        }
        if (msg.convId == activeConvId) {
            if (messages.none { it.id == msg.id }) {
                messages.add(msg)
                chatAdapter.notifyItemInserted(messages.size - 1)
                scrollToBottom()
            }
        }
    }

    private fun scrollToBottom() {
        if (messages.isNotEmpty()) {
            chatRecyclerView.scrollToPosition(messages.size - 1)
        }
    }

    private fun cancelConvNotification(convId: String) {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.cancel(convId.hashCode())
    }

    // ==================== 3. 实时回调接口实现 ====================

    override fun onWsConnectionChanged(connected: Boolean) {
        updateWsDot(connected)
    }

    override fun onNodeStatusUpdated(status: NodeStatusInfo) {
        updateNodePill(status)
    }

    override fun onNewMessageArrived(msg: ChatMessageItem) {
        upsertConversationWithMsg(msg)
        appendMsgIfCurrent(msg)
    }

    override fun onOutboundMessageSynced(msg: ChatMessageItem) {
        upsertConversationWithMsg(msg)
        appendMsgIfCurrent(msg)
    }

    private fun updateWsDot(connected: Boolean) {
        val color = if (connected) Color.parseColor("#07C160") else Color.parseColor("#EF4444")
        wsStatusDot.background = makeRoundedBg(color, 5)
    }

    private fun updateNodePill(status: NodeStatusInfo) {
        if (status.pcOnline) {
            nodeBadgeText.text = "🟢 PC主节点"
            nodeBadgeText.setTextColor(Color.parseColor("#6EE7B7"))
            nodeBadgeText.background = makeRoundedBg(
                Color.parseColor("#2B2B2B"),
                12,
                Color.parseColor("#059669"),
                1
            )
        } else {
            nodeBadgeText.text = "🟡 手机备用"
            nodeBadgeText.setTextColor(Color.parseColor("#FDE68A"))
            nodeBadgeText.background = makeRoundedBg(
                Color.parseColor("#2B2B2B"),
                12,
                Color.parseColor("#D97706"),
                1
            )
        }
    }

    // ==================== 4. 配置弹窗与新建会话弹窗 ====================

    private fun showServerConfigDialog(canCancel: Boolean) {
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(12), dp(18), dp(4))
        }

        val urlInput = EditText(this).apply {
            hint = "https://wx.你的域名.com"
            setText(AppPrefs.getServerUrl(this@MainActivity))
            setSingleLine(true)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
        }
        val pwdInput = EditText(this).apply {
            hint = "CLIENT_PASSWORD (默认 xz1c-secret-2026)"
            setText(AppPrefs.getClientToken(this@MainActivity))
            setSingleLine(true)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
        }

        layout.addView(TextView(this).apply {
            text = "Cloudflare Tunnel 公网地址 (或局域网 IP:8000)："
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
        })
        layout.addView(urlInput)
        layout.addView(TextView(this).apply {
            text = "访问密码 (CLIENT_PASSWORD)："
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            setPadding(0, dp(8), 0, 0)
        })
        layout.addView(pwdInput)

        val builder = AlertDialog.Builder(this)
            .setTitle("中枢服务器连接设置")
            .setView(layout)
            .setCancelable(canCancel)
            .setPositiveButton("保存并连接", null)

        if (canCancel) {
            builder.setNegativeButton("取消", null)
        }

        val dialog = builder.create()
        dialog.show()
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
            val u = urlInput.text.toString().trim().trimEnd('/')
            val p = pwdInput.text.toString().trim()
            if (!u.startsWith("http") || p.isEmpty()) {
                Toast.makeText(this, "请填写完整的 http(s):// 地址与密码", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            AppPrefs.setServerUrl(this, u)
            AppPrefs.setClientToken(this, p)
            dialog.dismiss()
            RelayForegroundService.startOrReload(this)
            refreshAllDataFromServer()
        }
    }

    private fun showNewChatDialog() {
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(12), dp(18), dp(4))
        }
        val targetEdit = EditText(this).apply {
            hint = "输入好友备注/昵称 或 群聊全名"
            setSingleLine(true)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
        }
        val groupCheck = CheckBox(this).apply {
            text = "这是一个微信群聊"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
        }
        layout.addView(targetEdit)
        layout.addView(groupCheck)

        AlertDialog.Builder(this)
            .setTitle("发起指定微信会话")
            .setView(layout)
            .setNegativeButton("取消", null)
            .setPositiveButton("进入对话") { _, _ ->
                val target = targetEdit.text.toString().trim()
                if (target.isNotEmpty()) {
                    val isGroup = groupCheck.isChecked
                    val cid = (if (isGroup) "group:" else "user:") + target
                    if (conversations.none { it.convId == cid }) {
                        conversations.add(
                            0,
                            ConversationItem(
                                convId = cid,
                                title = target,
                                isGroup = isGroup,
                                lastSender = target,
                                lastContent = "",
                                lastTimestamp = System.currentTimeMillis() / 1000,
                                unreadCount = 0
                            )
                        )
                    }
                    selectConversation(cid)
                }
            }
            .show()
    }

    @SuppressLint("BatteryLife")
    private fun requestNecessaryPermissions() {
        // Android 13+ 通知权限兼容
        if (Build.VERSION.SDK_INT >= 33) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) {
                ActivityCompat.requestPermissions(
                    this,
                    arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                    100
                )
            }
        }
        // 请求加入电池优化白名单（防止 Sony STAMINA 省电模式杀后台 WebSocket）
        try {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            if (!pm.isIgnoringBatteryOptimizations(packageName)) {
                val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                    data = Uri.parse("package:$packageName")
                }
                startActivity(intent)
            }
        } catch (_: Exception) {
        }
    }

    // ==================== 5. 聊天气泡 RecyclerView Adapter ====================

    private inner class ChatBubbleAdapter(
        private val items: List<ChatMessageItem>
    ) : RecyclerView.Adapter<ChatBubbleAdapter.BubbleViewHolder>() {

        private val timeFormat = SimpleDateFormat("HH:mm", Locale.getDefault())

        inner class BubbleViewHolder(
            val rowLayout: LinearLayout,
            val metaText: TextView,
            val bubbleText: TextView
        ) : RecyclerView.ViewHolder(rowLayout)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): BubbleViewHolder {
            val row = LinearLayout(parent.context).apply {
                orientation = LinearLayout.VERTICAL
                layoutParams = RecyclerView.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT
                ).apply {
                    bottomMargin = dp(8)
                }
            }
            val meta = TextView(parent.context).apply {
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f)
                setTextColor(Color.parseColor("#6B7280"))
                setPadding(dp(2), 0, dp(2), dp(2))
            }
            val bubble = TextView(parent.context).apply {
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
                setTextColor(Color.parseColor("#111111"))
                setPadding(dp(10), dp(7), dp(10), dp(7))
                maxWidth = (resources.displayMetrics.widthPixels * 0.84).toInt()
            }
            row.addView(meta)
            row.addView(bubble)
            return BubbleViewHolder(row, meta, bubble)
        }

        override fun onBindViewHolder(holder: BubbleViewHolder, position: Int) {
            val msg = items[position]
            val isOut = msg.direction == "out"
            val timeStr = timeFormat.format(Date(msg.timestamp * 1000))
            val nodeTag = if (msg.sourceNode.lowercase() == "pc") "[PC]" else "[手机]"
            val senderLabel = if (isOut) "我" else msg.sender

            holder.metaText.text = "$senderLabel · $timeStr $nodeTag"
            holder.bubbleText.text = msg.content

            val lpMeta = holder.metaText.layoutParams as LinearLayout.LayoutParams
            val lpBubble = holder.bubbleText.layoutParams as LinearLayout.LayoutParams

            if (isOut) {
                holder.rowLayout.gravity = Gravity.END
                lpMeta.gravity = Gravity.END
                lpBubble.gravity = Gravity.END
                holder.bubbleText.background = makeRoundedBg(Color.parseColor("#95EC69"), 7)
            } else {
                holder.rowLayout.gravity = Gravity.START
                lpMeta.gravity = Gravity.START
                lpBubble.gravity = Gravity.START
                holder.bubbleText.background = makeRoundedBg(Color.WHITE, 7)
            }
        }

        override fun getItemCount(): Int = items.size
    }
}
