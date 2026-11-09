package com.xz1c.wechatrelay

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.RemoteInput

object NotificationHelper {
    const val CHANNEL_DAEMON_ID = "relay_daemon_channel"
    const val CHANNEL_MESSAGE_ID = "wechat_incoming_msg_channel"
    const val DAEMON_NOTIFICATION_ID = 1001
    const val KEY_DIRECT_REPLY_TEXT = "key_direct_reply_text"

    fun ensureChannels(ctx: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

            // 1. 后台常驻保活静默通知栏通道 (低优先级，不弹窗不响铃)
            val daemonCh = NotificationChannel(
                CHANNEL_DAEMON_ID,
                "中枢常驻连接状态",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "保持 Sony XZ1 Compact 在锁屏下实时接收微信消息推送"
                setShowBadge(false)
            }

            // 2. 微信新消息高优先级通道 (响铃 + 震动 + 锁屏横幅 + 通知栏直接回复)
            val msgCh = NotificationChannel(
                CHANNEL_MESSAGE_ID,
                "微信新消息通知",
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "收到微信新消息时弹出横幅提醒并支持通知栏直接回复"
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 180, 80, 180)
                setShowBadge(true)
            }

            nm.createNotificationChannel(daemonCh)
            nm.createNotificationChannel(msgCh)
        }
    }

    fun buildDaemonNotification(ctx: Context, wsConnected: Boolean, nodeStatus: NodeStatusInfo): Notification {
        ensureChannels(ctx)
        val openIntent = Intent(ctx, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pi = PendingIntent.getActivity(
            ctx,
            0,
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val connLabel = if (wsConnected) "实时通道已连接" else "正在重连中枢..."
        val routeLabel = if (nodeStatus.pcOnline) "🟢 PC 主节点在线" else "🟡 手机备用节点代发"

        return NotificationCompat.Builder(ctx, CHANNEL_DAEMON_ID)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle("WeChat Relay · $routeLabel")
            .setContentText(connLabel)
            .setContentIntent(pi)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    /**
     * 弹出原生微信新消息横幅通知，并附带 Android 原生 RemoteInput「直接回复」按钮。
     * 用户在 Sony XZ1 Compact 锁屏或下拉通知栏即可直接输入文字回复，无需打开 App！
     */
    fun showIncomingMessageNotification(ctx: Context, msg: ChatMessageItem) {
        ensureChannels(ctx)
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

        val notificationId = msg.convId.hashCode()
        val displayTitle = if (msg.isGroup && msg.roomName.isNotEmpty()) {
            "[群] ${msg.roomName}"
        } else {
            msg.sender
        }
        val displayBody = if (msg.isGroup) "${msg.sender}: ${msg.content}" else msg.content

        // 点击通知进入对应会话
        val openIntent = Intent(ctx, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra("conv_id", msg.convId)
            putExtra("title", msg.title)
            putExtra("is_group", msg.isGroup)
        }
        val contentPi = PendingIntent.getActivity(
            ctx,
            notificationId,
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        // 构建通知栏直接回复 Action (RemoteInput)
        val remoteInput = RemoteInput.Builder(KEY_DIRECT_REPLY_TEXT)
            .setLabel("直接回复 $displayTitle...")
            .build()

        val replyIntent = Intent(ctx, DirectReplyReceiver::class.java).apply {
            putExtra("conv_id", msg.convId)
            putExtra("target", if (msg.isGroup && msg.roomName.isNotEmpty()) msg.roomName else msg.sender)
            putExtra("is_group", msg.isGroup)
            putExtra("room_name", msg.roomName)
            putExtra("notification_id", notificationId)
        }

        val replyFlags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
        } else {
            PendingIntent.FLAG_UPDATE_CURRENT
        }
        val replyPi = PendingIntent.getBroadcast(ctx, notificationId, replyIntent, replyFlags)

        val replyAction = NotificationCompat.Action.Builder(
            android.R.drawable.ic_menu_send,
            "直接回复",
            replyPi
        ).addRemoteInput(remoteInput)
            .setAllowGeneratedReplies(true)
            .build()

        val notification = NotificationCompat.Builder(ctx, CHANNEL_MESSAGE_ID)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle(displayTitle)
            .setContentText(displayBody)
            .setStyle(NotificationCompat.BigTextStyle().bigText(displayBody))
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setDefaults(NotificationCompat.DEFAULT_ALL)
            .setAutoCancel(true)
            .setContentIntent(contentPi)
            .addAction(replyAction)
            .build()

        nm.notify(notificationId, notification)
    }
}
