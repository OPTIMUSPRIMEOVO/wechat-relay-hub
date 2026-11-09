package com.xz1c.wechatrelay

import android.content.Context

object AppPrefs {
    private const val PREF_NAME = "xz1c_wechat_relay_prefs"
    private const val KEY_SERVER_URL = "server_url"
    private const val KEY_CLIENT_TOKEN = "client_token"
    private const val KEY_LAST_CONV_ID = "last_conv_id"

    // 追踪当前 Activity 是否在前台可见，以决定是否弹出横幅系统通知与震动
    @Volatile
    var isMainActivityResumed: Boolean = false

    @Volatile
    var activeViewingConvId: String = ""

    fun getServerUrl(ctx: Context): String {
        val sp = ctx.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)
        return (sp.getString(KEY_SERVER_URL, "") ?: "").trim().trimEnd('/')
    }

    fun setServerUrl(ctx: Context, url: String) {
        ctx.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_SERVER_URL, url.trim().trimEnd('/'))
            .apply()
    }

    fun getClientToken(ctx: Context): String {
        val sp = ctx.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)
        return (sp.getString(KEY_CLIENT_TOKEN, "xz1c-secret-2026") ?: "").trim()
    }

    fun setClientToken(ctx: Context, token: String) {
        ctx.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_CLIENT_TOKEN, token.trim())
            .apply()
    }

    fun getLastConvId(ctx: Context): String {
        val sp = ctx.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)
        return sp.getString(KEY_LAST_CONV_ID, "") ?: ""
    }

    fun setLastConvId(ctx: Context, convId: String) {
        ctx.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_LAST_CONV_ID, convId)
            .apply()
    }

    fun isConfigured(ctx: Context): Boolean {
        return getServerUrl(ctx).startsWith("http") && getClientToken(ctx).isNotEmpty()
    }
}
