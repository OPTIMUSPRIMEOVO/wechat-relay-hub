package com.xz1c.wechatrelay

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

data class NodeStatusInfo(
    val pcOnline: Boolean = false,
    val activeNode: String = "mobile",
    val activeNodeLabel: String = "手机备用节点"
) {
    companion object {
        fun fromJson(obj: JSONObject?): NodeStatusInfo {
            if (obj == null) return NodeStatusInfo()
            val pcOn = obj.optBoolean("pc_online", false)
            return NodeStatusInfo(
                pcOnline = pcOn,
                activeNode = obj.optString("active_node", if (pcOn) "pc" else "mobile"),
                activeNodeLabel = obj.optString("active_node_label", if (pcOn) "PC 主节点" else "手机备用")
            )
        }
    }
}

data class ConversationItem(
    val convId: String,
    var title: String,
    val isGroup: Boolean,
    var lastSender: String,
    var lastContent: String,
    var lastTimestamp: Long,
    var unreadCount: Int
) {
    companion object {
        fun fromJson(obj: JSONObject): ConversationItem {
            return ConversationItem(
                convId = obj.optString("conv_id", ""),
                title = obj.optString("title", "未知"),
                isGroup = obj.optBoolean("is_group", false),
                lastSender = obj.optString("last_sender", ""),
                lastContent = obj.optString("last_content", ""),
                lastTimestamp = obj.optLong("last_timestamp", 0L),
                unreadCount = obj.optInt("unread_count", 0)
            )
        }
    }
}

data class ChatMessageItem(
    val id: Long,
    val convId: String,
    val title: String,
    val sender: String,
    val content: String,
    val timestamp: Long,
    val sourceNode: String,
    val isGroup: Boolean,
    val roomName: String,
    val direction: String, // "in" or "out"
    val status: String
) {
    companion object {
        fun fromJson(obj: JSONObject): ChatMessageItem {
            val sender = obj.optString("sender", "")
            val room = obj.optString("room_name", "")
            val isGroup = obj.optBoolean("is_group", false)
            return ChatMessageItem(
                id = obj.optLong("id", System.currentTimeMillis()),
                convId = obj.optString("conv_id", ""),
                title = obj.optString("title", if (isGroup && room.isNotEmpty()) room else sender),
                sender = sender,
                content = obj.optString("content", ""),
                timestamp = obj.optLong("timestamp", System.currentTimeMillis() / 1000),
                sourceNode = obj.optString("source_node", "pc"),
                isGroup = isGroup,
                roomName = room,
                direction = obj.optString("direction", "in"),
                status = obj.optString("status", "delivered")
            )
        }
    }
}

object RelayHttpClient {
    private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()

    val okHttp: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .pingInterval(15, TimeUnit.SECONDS)
        .build()

    fun fetchConversations(baseUrl: String, token: String): Pair<List<ConversationItem>, NodeStatusInfo?> {
        val req = Request.Builder()
            .url("$baseUrl/api/conversations")
            .header("X-Client-Token", token)
            .get()
            .build()
        okHttp.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw RuntimeException("HTTP ${resp.code}")
            val root = JSONObject(resp.body?.string() ?: "{}")
            val arr = root.optJSONArray("conversations") ?: JSONArray()
            val list = ArrayList<ConversationItem>(arr.length())
            for (i in 0 until arr.length()) {
                list.add(ConversationItem.fromJson(arr.getJSONObject(i)))
            }
            val status = NodeStatusInfo.fromJson(root.optJSONObject("node_status"))
            return list to status
        }
    }

    fun fetchMessages(baseUrl: String, token: String, convId: String): List<ChatMessageItem> {
        val encoded = java.net.URLEncoder.encode(convId, "UTF-8")
        val req = Request.Builder()
            .url("$baseUrl/api/messages?conv_id=$encoded&limit=80")
            .header("X-Client-Token", token)
            .get()
            .build()
        okHttp.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw RuntimeException("HTTP ${resp.code}")
            val root = JSONObject(resp.body?.string() ?: "{}")
            val arr = root.optJSONArray("messages") ?: JSONArray()
            val list = ArrayList<ChatMessageItem>(arr.length())
            for (i in 0 until arr.length()) {
                list.add(ChatMessageItem.fromJson(arr.getJSONObject(i)))
            }
            return list
        }
    }

    fun sendReply(
        baseUrl: String,
        token: String,
        target: String,
        content: String,
        isGroup: Boolean,
        roomName: String,
        convId: String
    ): JSONObject {
        val bodyJson = JSONObject().apply {
            put("target", target)
            put("content", content)
            put("is_group", isGroup)
            put("room_name", roomName)
            put("conv_id", convId)
        }
        val req = Request.Builder()
            .url("$baseUrl/api/reply")
            .header("X-Client-Token", token)
            .post(bodyJson.toString().toRequestBody(JSON_MEDIA))
            .build()
        okHttp.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw RuntimeException("HTTP ${resp.code}")
            return JSONObject(resp.body?.string() ?: "{}")
        }
    }
}
