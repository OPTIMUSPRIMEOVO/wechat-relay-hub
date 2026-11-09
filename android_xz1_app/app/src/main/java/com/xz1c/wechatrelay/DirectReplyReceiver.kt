package com.xz1c.wechatrelay

import android.app.NotificationManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import androidx.core.app.RemoteInput

/**
 * 处理用户在 Sony XZ1 Compact 锁屏或下拉通知栏中直接输入的微信回复内容。
 */
class DirectReplyReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val remoteInput = RemoteInput.getResultsFromIntent(intent) ?: return
        val replyText = remoteInput.getCharSequence(NotificationHelper.KEY_DIRECT_REPLY_TEXT)
            ?.toString()?.trim() ?: return
        if (replyText.isEmpty()) return

        val convId = intent.getStringExtra("conv_id") ?: ""
        val target = intent.getStringExtra("target") ?: ""
        val isGroup = intent.getBooleanExtra("is_group", false)
        val roomName = intent.getStringExtra("room_name") ?: ""
        val notificationId = intent.getIntExtra("notification_id", 0)

        val baseUrl = AppPrefs.getServerUrl(context)
        val token = AppPrefs.getClientToken(context)
        val pendingResult = goAsync()

        Thread {
            try {
                RelayHttpClient.sendReply(
                    baseUrl = baseUrl,
                    token = token,
                    target = target,
                    content = replyText,
                    isGroup = isGroup,
                    roomName = roomName,
                    convId = convId
                )
                val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
                val updated = NotificationCompat.Builder(context, NotificationHelper.CHANNEL_MESSAGE_ID)
                    .setSmallIcon(android.R.drawable.stat_notify_chat)
                    .setContentTitle(target)
                    .setContentText("已回复: $replyText")
                    .setPriority(NotificationCompat.PRIORITY_LOW)
                    .setAutoCancel(true)
                    .build()
                nm.notify(notificationId, updated)
            } catch (e: Exception) {
                // 发送失败时保留原通知
            } finally {
                pendingResult.finish()
            }
        }.start()
    }
}
