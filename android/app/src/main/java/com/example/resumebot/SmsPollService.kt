package com.example.resumebot

/**
 * 主路径：**持续轮询** content://sms/inbox 的前台服务。
 *
 * 为什么不用短信广播（SMS_RECEIVED）：
 *   自 Android 10（API 29）起，该广播只投递给**默认短信应用**，
 *   普通 App 收不到 —— 这也是 v1 版「装了却从不上报」的根因之一。
 *
 * 为什么不用通知监听（NotificationListenerService）：
 *   需要用户在「通知使用权」里单独授权，且部分 ROM 会限流/延迟通知；
 *   通知被折叠或 App 未发出常驻通知时都会漏。作为**第二路径**保留。
 *
 * 轮询方案只依赖 READ_SMS 一个运行时权限，语义上直接读收件箱，最可靠。
 *
 * 增量策略：
 *   - 首次运行：把 last_id 播种为当前收件箱最大 _id，**只转发此后到达的新短信**，
 *     避免把历史短信一次性灌进后端（可用界面上的「补发最近 5 条」做人工验证）。
 *   - 之后每轮：`_id > last_id`，按 _id 升序取，逐条入待发队列并推进 last_id。
 *
 * 断网不丢：短信先入 SharedPreferences 待发队列，再冲刷；失败下轮重试。
 *
 * 前台服务类型用 specialUse 而非 dataSync：dataSync 在 Android 15+ 有
 * 「每 6 小时强制 onTimeout、当日不可再启动」的限制，会导致长时间停止轮询。
 */
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Build
import android.os.IBinder
import android.provider.Telephony
import android.util.Log
import androidx.core.content.ContextCompat

class SmsPollService : Service() {

    @Volatile private var running = false
    private var worker: Thread? = null

    /** 上一轮写入通知的内容，避免每轮都 notify 造成闪烁。 */
    private var lastNotifText = ""

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        ensureChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startAsForeground()
        Forwarder.setWantRun(this, true)

        val alive = worker?.isAlive == true
        if (!alive) {
            running = true
            worker = Thread({ loop() }, "sms-poll").also {
                it.isDaemon = false
                it.start()
            }
            Forwarder.log(
                this,
                "轮询服务已启动（间隔 ${Forwarder.intervalSeconds(this)} 秒，目标 ${Forwarder.serverUrl(this)}）"
            )
        } else {
            Forwarder.log(this, "轮询服务已在运行，忽略重复启动")
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        Forwarder.log(this, "轮询服务已停止")
        worker?.interrupt()
        worker = null
        super.onDestroy()
    }

    // ------------------------------------------------------------------
    // 前台通知
    // ------------------------------------------------------------------

    private fun ensureChannel() {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        if (nm.getNotificationChannel(CHANNEL) == null) {
            val ch = NotificationChannel(CHANNEL, "短信转发状态", NotificationManager.IMPORTANCE_LOW)
            ch.description = "常驻轮询服务状态"
            ch.setShowBadge(false)
            nm.createNotificationChannel(ch)
        }
    }

    private fun buildNotification(text: String): Notification {
        val pi = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("简历机器人 · 短信转发")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            .setContentIntent(pi)
            .build()
    }

    private fun startAsForeground() {
        val n = buildNotification("正在轮询短信…")
        try {
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            } else {
                startForeground(NOTIF_ID, n)
            }
        } catch (t: Throwable) {
            // 部分 ROM 对 specialUse 更严格；退化为普通前台服务，能跑就继续跑
            Log.e(Forwarder.TAG, "startForeground(specialUse) 失败，退化重试: ${t.message}")
            try {
                startForeground(NOTIF_ID, n)
            } catch (t2: Throwable) {
                Log.e(Forwarder.TAG, "startForeground 彻底失败: ${t2.message}")
            }
        }
    }

    private fun updateNotification() {
        val s = Forwarder.stats(this)
        val txt = "已上报 ${s[0]} · 待发 ${s[3]}" + (if (s[1] > 0) " · 失败 ${s[1]}" else "")
        if (txt == lastNotifText) return
        lastNotifText = txt
        try {
            getSystemService(NotificationManager::class.java)?.notify(NOTIF_ID, buildNotification(txt))
        } catch (_: Throwable) {
        }
    }

    // ------------------------------------------------------------------
    // 轮询主循环
    // ------------------------------------------------------------------

    private fun loop() {
        var needSeed = true
        var tick = 0L
        while (running) {
            val t0 = System.currentTimeMillis()
            try {
                val found = pollOnce(needSeed)
                if (needSeed) needSeed = false
                val posted = Forwarder.flush(this)
                updateNotification()

                Forwarder.lastTickAt = System.currentTimeMillis()
                Forwarder.lastTickNote = when {
                    found > 0 && posted > 0 -> "本轮新短信 $found 条，已上报 $posted 条"
                    found > 0 -> "本轮新短信 $found 条，已入队待发（链路未通）"
                    posted > 0 -> "本轮补发成功 $posted 条"
                    else -> "本轮无新短信"
                }
            } catch (t: Throwable) {
                Forwarder.lastTickAt = System.currentTimeMillis()
                Forwarder.lastTickNote = "异常：${t.javaClass.simpleName}: ${t.message}"
                // 异常可能每轮都发生，降频记盘（每 6 轮记一次）
                if (tick % 6 == 0L) {
                    Forwarder.log(this, "轮询异常：${t.javaClass.simpleName}: ${t.message}")
                }
            }
            tick++

            val cost = System.currentTimeMillis() - t0
            val wait = Forwarder.intervalSeconds(this) * 1000L - cost
            if (wait > 0) {
                try {
                    Thread.sleep(wait)
                } catch (_: InterruptedException) {
                    break
                }
            }
        }
    }

    /** @return 本轮新捕获并入队的短信条数。 */
    private fun pollOnce(seed: Boolean): Int {
        if (!hasReadSms()) {
            Forwarder.lastTickNote = "缺少短信读取权限（READ_SMS），请在首页点「授予短信权限」"
            return 0
        }
        val p = Forwarder.prefs(this)
        var lastId = p.getLong(Forwarder.KEY_LAST_ID, -1L)

        if (seed || lastId < 0) {
            val maxId = maxInboxId()
            p.edit().putLong(Forwarder.KEY_LAST_ID, maxId).commit()
            Forwarder.log(this, "首次轮询：收件箱当前最大 _id=$maxId，仅转发此后到达的新短信")
            return 0
        }

        val cols = arrayOf(
            Telephony.Sms._ID,
            Telephony.Sms.ADDRESS,
            Telephony.Sms.BODY,
            Telephony.Sms.DATE
        )
        var max = lastId
        var found = 0
        contentResolver.query(
            Uri.parse("content://sms/inbox"),
            cols,
            "${Telephony.Sms._ID} > ?",
            arrayOf(lastId.toString()),
            "${Telephony.Sms._ID} ASC"
        )?.use { c ->
            while (c.moveToNext()) {
                val id = c.getLong(0)
                val addr = c.getString(1) ?: ""
                val body = c.getString(2) ?: ""
                val date = c.getLong(3)
                if (id > max) max = id
                if (body.isBlank()) continue
                val code = Forwarder.extractCode(body)
                if (Forwarder.enqueue(this, id, addr, code, body, date)) {
                    found++
                    Forwarder.log(
                        this,
                        "捕获新短信 #$id 来自 ${addr.ifBlank { "未知" }}，验证码=${code.ifBlank { "未识别" }}"
                    )
                }
            }
        }
        if (max != lastId) p.edit().putLong(Forwarder.KEY_LAST_ID, max).commit()
        return found
    }

    private fun maxInboxId(): Long = try {
        contentResolver.query(
            Uri.parse("content://sms/inbox"),
            arrayOf(Telephony.Sms._ID),
            null,
            null,
            "${Telephony.Sms._ID} DESC"
        )?.use { c -> if (c.moveToFirst()) c.getLong(0) else 0L } ?: 0L
    } catch (t: Throwable) {
        Log.e(Forwarder.TAG, "读取收件箱最大 _id 失败: ${t.message}")
        0L
    }

    private fun hasReadSms(): Boolean =
        ContextCompat.checkSelfPermission(this, android.Manifest.permission.READ_SMS) ==
            PackageManager.PERMISSION_GRANTED

    companion object {
        const val CHANNEL = "resumebot_poll"
        const val NOTIF_ID = 1001

        /**
         * 由界面/开机接收器调用。
         *
         * 先试 startForegroundService（Android 8+ 必须用这个启动前台服务），
         * 失败再退化为 startService（例如处于后台启动限制下）。
         */
        fun start(c: Context) {
            val i = Intent(c, SmsPollService::class.java)
            try {
                c.startForegroundService(i)
            } catch (t: Throwable) {
                Log.e(Forwarder.TAG, "startForegroundService 失败: ${t.message}")
                try {
                    c.startService(i)
                } catch (t2: Throwable) {
                    Log.e(Forwarder.TAG, "startService 也失败: ${t2.message}")
                }
            }
        }

        fun stop(c: Context) {
            Forwarder.setWantRun(c, false)
            try {
                c.stopService(Intent(c, SmsPollService::class.java))
            } catch (t: Throwable) {
                Log.e(Forwarder.TAG, "stopService 失败: ${t.message}")
            }
        }
    }
}
