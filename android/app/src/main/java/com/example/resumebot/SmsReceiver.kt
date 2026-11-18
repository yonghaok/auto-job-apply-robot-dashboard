package com.example.resumebot

/**
 * 断点1 **旧版**接收器（仅 Android 9 / API 28 及以下有效）。
 *
 * ⚠️ 自 Android 10（API 29）起，SMS_RECEIVED 广播只投递给**默认短信应用**，
 *    普通 App 的 BroadcastReceiver 收不到。因此 Android 16 上本接收器**不生效**，
 *    现代设备请以 [SmsNotificationListener] 为准（NotificationListenerService 路径）。
 *
 * 本类保留是为了兼容老设备 / 已 Root 测试机，并继续持有 SharedPreferences 常量。
 * 转发逻辑已统一收敛到 [Forwarder]，两条来源共用同一条 POST /sms 链路。
 */
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.telephony.SmsMessage
import android.util.Log

class SmsReceiver : BroadcastReceiver() {
    companion object {
        // 保留旧常量名，避免其它引用点改动（值统一来自 Forwarder）
        const val PREFS = Forwarder.PREFS
        const val KEY_PC_IP = Forwarder.KEY_PC_IP
        const val PORT = Forwarder.PORT
        const val TAG = "SmsReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != "android.provider.Telephony.SMS_RECEIVED") return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            Log.w(TAG, "Android 10+ 不再投递 SMS_RECEIVED 给非默认短信应用，已忽略")
            return
        }
        val extras = intent.extras ?: return
        val pdus = extras["pdus"] as? Array<*> ?: return
        val fmt = extras["format"] as? String
        val body = StringBuilder()
        var sender = ""
        for (pdu in pdus) {
            if (pdu !is ByteArray) continue
            val msg = SmsMessage.createFromPdu(pdu, fmt)
            if (sender.isBlank()) sender = msg.displayOriginatingAddress
            body.append(msg.messageBody)
        }
        val text = body.toString()
        Forwarder.send(context, sender, Forwarder.extractCode(text), text)
    }
}
