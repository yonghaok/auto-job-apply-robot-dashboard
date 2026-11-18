package com.example.resumebot

/**
 * 断点1 现代主路径（Android 10 / API 29+，含 Android 16 均有效）：
 * 通过 NotificationListenerService 读取「短信 App 发出的通知」来转发验证码。
 *
 * 为什么不用 SMS_RECEIVED 广播：
 *   自 Android 10（API 29）起，SMS_RECEIVED 只投递给**默认短信应用**，
 *   普通 App 的 BroadcastReceiver 收不到 —— Android 16 上更是完全不可用。
 *   NotificationListenerService 只需用户在「通知使用权」里勾选本 App 即可，
 *   不需要成为默认短信应用，侵入性最低。
 *
 * 提取规则：
 *   title（android.title）         → 发件人 / 联系人名 / 号码
 *   text （android.text / bigText / textLines）→ 短信正文
 *   验证码：优先取「验证码/校验码/动态码」附近的 4~8 位数字
 */
import android.app.Notification
import android.os.Bundle
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log

class SmsNotificationListener : NotificationListenerService() {

    override fun onListenerConnected() {
        super.onListenerConnected()
        Log.i(Forwarder.TAG, "SmsNotificationListener 已连接（通知使用权已授予）")
    }

    override fun onListenerDisconnected() {
        super.onListenerDisconnected()
        Log.w(Forwarder.TAG, "SmsNotificationListener 已断开")
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val pkg = sbn.packageName
        if (pkg == packageName) return // 不处理自己

        val extras: Bundle = sbn.notification?.extras ?: return

        val title = firstText(
            extras.getCharSequence(Notification.EXTRA_TITLE),
            extras.getCharSequence(Notification.EXTRA_TITLE_BIG)
        )
        val text = firstText(
            extras.getCharSequence(Notification.EXTRA_TEXT),
            extras.getCharSequence(Notification.EXTRA_BIG_TEXT),
            extras.getCharSequence(Notification.EXTRA_SUMMARY_TEXT),
            joinLines(extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES))
        )
        if (text.isBlank()) return

        // 只转发短信类通知：已知短信包名，或正文含验证码特征
        if (!isSmsPackage(pkg) && !VERIFY_HINT.containsMatchIn(text)) return

        // 简单去重：同一条短信可能被通知更新重复投递
        if (!remember(text)) return

        val sender = normalizeSender(title)
        Forwarder.send(this, sender, Forwarder.extractCode(text), text)
        Log.i(Forwarder.TAG, "captured from $pkg sender=$sender")
    }

    /** 取第一个非空文本。 */
    private fun firstText(vararg cs: CharSequence?): String {
        for (c in cs) {
            val s = c?.toString()?.trim().orEmpty()
            if (s.isNotBlank()) return s
        }
        return ""
    }

    private fun joinLines(lines: Array<CharSequence>?): String {
        if (lines.isNullOrEmpty()) return ""
        return lines.joinToString("\n") { it.toString() }.trim()
    }

    /** 发件人：可能是「张三」「1069xxxx」或带前缀的标题，原样传给后端即可。 */
    private fun normalizeSender(title: String): String {
        val t = title.trim()
        if (t.isBlank()) return "unknown"
        // 形如「新短信 - 张三」的标题取后半段
        val idx = t.lastIndexOf('：')
        val idx2 = t.lastIndexOf(':')
        val cut = maxOf(idx, idx2)
        return if (cut >= 0 && cut + 1 < t.length) t.substring(cut + 1).trim() else t
    }

    private fun isSmsPackage(pkg: String): Boolean = SMS_PACKAGES.contains(pkg)

    /** 内存级去重（服务生命周期内），上限后清空，避免无限增长。 */
    private val seen = HashSet<String>()
    private fun remember(text: String): Boolean {
        val key = text.trim()
        synchronized(seen) {
            if (seen.contains(key)) return false
            if (seen.size > 200) seen.clear()
            seen.add(key)
            return true
        }
    }

    companion object {
        private val VERIFY_HINT = Regex("验证码|校验码|动态码|安全码|verification\\s*code|passcode")

        /** 常见短信 App 包名（命中即转发；未命中则靠 VERIFY_HINT 兜底）。 */
        private val SMS_PACKAGES = setOf(
            "com.google.android.apps.messaging", // Google Messages（Pixel / Android 16 常见）
            "com.android.mms",                   // AOSP 短信
            "com.samsung.android.messaging",     // 三星
            "com.miui.mms",                      // 小米
            "com.huawei.android.mms",            // 华为
            "com.huawei.message",
            "com.coloros.mms",                   // OPPO / 一加
            "com.vivo.mms",                      // vivo
            "com.android.messaging",
            "com.tencent.mobileqq",              // QQ（偶尔承接验证码）
            "com.eg.android.AlipayGphone"
        )
    }
}
