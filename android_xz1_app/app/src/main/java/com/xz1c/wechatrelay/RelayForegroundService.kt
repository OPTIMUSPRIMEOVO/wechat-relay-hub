package com.xz1c.wechatrelay

import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.CopyOnWriteArraySet

/**
 * 常驻前台服务：
 * 1. 维持到中枢 /ws/client 的 OkHttp WebSocket 长连接
 * 2. 当 App 处于后台、锁屏或正在浏览其他会话时，一旦收到微信新消息，立即弹出 Android 原生横幅通知 + 震动 + 锁屏提醒 + 通知栏直接回复
 * 3. 当 MainActivity 在前台时，通过内存监听器零延迟刷新 UI
 */
class RelayForegroundService : Service() {

    interface RelayEventListener {
        fun onWsConnectionChanged(connected: Boolean)
        fun onNodeStatusUpdated(status: NodeStatusInfo)
        fun onNewMessageArrived(msg: ChatMessageItem)
        fun onOutboundMessageSynced(msg: ChatMessageItem)
    }

    companion object {
        val listeners = CopyOnWriteArraySet<RelayEventListener>()

        @Volatile
        var isWsConnected: Boolean = false
            private set

        @Volatile
        var latestNodeStatus: NodeStatusInfo = NodeStatusInfo()
            private set

        fun startOrReload(ctx: Context) {
            val intent = Intent(ctx, RelayForegroundService::class.java).apply {
                action = "ACTION_RECONNECT"
            }
            ctx.startForegroundService(intent)
        }
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private var webSocket: WebSocket? = null
    private var isDestroyed = false
    private var reconnectDelayMs = 2000L

    private val reconnectRunnable = Runnable {
        if (!isDestroyed) {
            connectWebSocket()
        }
    }

    override fun onCreate() {
        super.onCreate()
        NotificationHelper.ensureChannels(this)
        startForeground(
            NotificationHelper.DAEMON_NOTIFICATION_ID,
            NotificationHelper.buildDaemonNotification(this, false, latestNodeStatus)
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(
            NotificationHelper.DAEMON_NOTIFICATION_ID,
            NotificationHelper.buildDaemonNotification(this, isWsConnected, latestNodeStatus)
        )
        if (intent?.action == "ACTION_RECONNECT" || webSocket == null) {
            connectWebSocket()
        }
        return START_STICKY
    }

    private fun connectWebSocket() {
        mainHandler.removeCallbacks(reconnectRunnable)
        try {
            webSocket?.cancel()
        } catch (_: Exception) {
        }
        webSocket = null

        if (!AppPrefs.isConfigured(this)) {
            updateConnectionState(false)
            return
        }

        val baseUrl = AppPrefs.getServerUrl(this)
        val token = AppPrefs.getClientToken(this)
        val wsBase = baseUrl
            .replaceFirst("^https://".toRegex(), "wss://")
            .replaceFirst("^http://".toRegex(), "ws://")
        val encodedToken = URLEncoder.encode(token, "UTF-8")
        val wsUrl = "$wsBase/ws/client?token=$encodedToken"

        val req = Request.Builder()
            .url(wsUrl)
            .header("X-Client-Token", token)
            .build()

        webSocket = RelayHttpClient.okHttp.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                reconnectDelayMs = 2000L
                updateConnectionState(true)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleWsPacket(text)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
                updateConnectionState(false)
                scheduleReconnect()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                updateConnectionState(false)
                scheduleReconnect()
            }
        })
    }

    private fun handleWsPacket(raw: String) {
        try {
            val packet = JSONObject(raw)
            val type = packet.optString("type", "")

            packet.optJSONObject("node_status")?.let { nsObj ->
                val ns = NodeStatusInfo.fromJson(nsObj)
                dispatchNodeStatus(ns)
            }

            when (type) {
                "node_status", "pong" -> {
                    val dataObj = packet.optJSONObject("data")
                    if (dataObj != null) {
                        dispatchNodeStatus(NodeStatusInfo.fromJson(dataObj))
                    }
                }
                "new_message" -> {
                    val dataObj = packet.optJSONObject("data") ?: return
                    val msg = ChatMessageItem.fromJson(dataObj)

                    // 判断是否需要弹出 Android 原生通知（当 App 在后台/锁屏，或当前浏览的不是该会话时）
                    val shouldNotify = !AppPrefs.isMainActivityResumed ||
                        AppPrefs.activeViewingConvId != msg.convId

                    if (shouldNotify) {
                        NotificationHelper.showIncomingMessageNotification(applicationContext, msg)
                    }

                    mainHandler.post {
                        for (l in listeners) {
                            l.onNewMessageArrived(msg)
                        }
                    }
                }
                "outbound_message" -> {
                    val dataObj = packet.optJSONObject("data") ?: return
                    val msg = ChatMessageItem.fromJson(dataObj)
                    mainHandler.post {
                        for (l in listeners) {
                            l.onOutboundMessageSynced(msg)
                        }
                    }
                }
            }
        } catch (_: Exception) {
        }
    }

    private fun updateConnectionState(connected: Boolean) {
        isWsConnected = connected
        refreshDaemonNotification()
        mainHandler.post {
            for (l in listeners) {
                l.onWsConnectionChanged(connected)
            }
        }
    }

    private fun dispatchNodeStatus(status: NodeStatusInfo) {
        latestNodeStatus = status
        refreshDaemonNotification()
        mainHandler.post {
            for (l in listeners) {
                l.onNodeStatusUpdated(status)
            }
        }
    }

    private fun refreshDaemonNotification() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.notify(
            NotificationHelper.DAEMON_NOTIFICATION_ID,
            NotificationHelper.buildDaemonNotification(this, isWsConnected, latestNodeStatus)
        )
    }

    private fun scheduleReconnect() {
        if (isDestroyed) return
        mainHandler.removeCallbacks(reconnectRunnable)
        mainHandler.postDelayed(reconnectRunnable, reconnectDelayMs)
        reconnectDelayMs = (reconnectDelayMs * 2).coerceAtMost(15000L)
    }

    override fun onDestroy() {
        isDestroyed = true
        mainHandler.removeCallbacks(reconnectRunnable)
        try {
            webSocket?.close(1000, "Service destroyed")
        } catch (_: Exception) {
        }
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
