package com.example.resumebot

/**
 * 配置页（v2 · 持续轮询版）
 *
 * 三件事：
 *  1. 拿权限：READ_SMS（轮询收件箱的前提）+ POST_NOTIFICATIONS（前台服务通知可见）。
 *  2. 配地址：后端完整 URL（默认 http://192.168.1.100:8000/sms）+ 轮询间隔。
 *  3. 启停与自检：启动/停止轮询，并提供「发送测试短信」「补发最近 5 条」「立即冲刷队列」
 *     三个验证工具，用于把「手机→PC」这段链路单独验证出来。
 *
 * 界面每秒刷新一次状态与日志，因此不用猜服务是否活着。
 */
import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.provider.Telephony
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : AppCompatActivity() {

    private lateinit var tvPerm: TextView
    private lateinit var tvState: TextView
    private lateinit var tvLog: TextView
    private lateinit var etUrl: EditText
    private lateinit var etInterval: EditText

    private val ui = Handler(Looper.getMainLooper())
    private val hhmmss = SimpleDateFormat("HH:mm:ss", Locale.CHINA)

    private val ticker = object : Runnable {
        override fun run() {
            refresh()
            ui.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        tvPerm = findViewById(R.id.tv_perm)
        tvState = findViewById(R.id.tv_state)
        tvLog = findViewById(R.id.tv_log)
        etUrl = findViewById(R.id.et_url)
        etInterval = findViewById(R.id.et_interval)

        etUrl.setText(Forwarder.serverUrl(this))
        etInterval.setText(Forwarder.intervalSeconds(this).toString())

        findViewById<Button>(R.id.btn_perm).setOnClickListener { requestPerms() }
        findViewById<Button>(R.id.btn_battery).setOnClickListener { requestBatteryWhitelist() }
        findViewById<Button>(R.id.btn_save_start).setOnClickListener { saveAndStart() }
        findViewById<Button>(R.id.btn_stop).setOnClickListener { stopPoll() }
        findViewById<Button>(R.id.btn_test).setOnClickListener { sendTest() }
        findViewById<Button>(R.id.btn_backfill).setOnClickListener { backfillRecent() }
        findViewById<Button>(R.id.btn_flush).setOnClickListener { flushNow() }
        findViewById<Button>(R.id.btn_notify).setOnClickListener { openNotificationAccess() }
        findViewById<Button>(R.id.btn_clear).setOnClickListener {
            Forwarder.clearLogs(this)
            Forwarder.resetStats(this)
            toast("已清空日志与计数")
        }

        // 首次进入且已配好地址：若用户此前启用过，直接续跑
        if (Forwarder.wantRun(this) && hasReadSms()) {
            SmsPollService.start(this)
        } else if (!hasReadSms()) {
            requestPerms()
        }
    }

    override fun onResume() {
        super.onResume()
        ui.removeCallbacks(ticker)
        ui.post(ticker)
    }

    override fun onPause() {
        super.onPause()
        ui.removeCallbacks(ticker)
    }

    // ------------------------------------------------------------------
    // 权限
    // ------------------------------------------------------------------

    private fun missingPerms(): Array<String> {
        val out = ArrayList<String>()
        if (!hasReadSms()) out.add(Manifest.permission.READ_SMS)
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, "android.permission.POST_NOTIFICATIONS") !=
            PackageManager.PERMISSION_GRANTED
        ) {
            out.add("android.permission.POST_NOTIFICATIONS")
        }
        return out.toTypedArray()
    }

    private fun requestPerms() {
        val need = missingPerms()
        if (need.isEmpty()) {
            toast("短信权限已就绪")
            return
        }
        ActivityCompat.requestPermissions(this, need, REQ_PERMS)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQ_PERMS) return
        if (hasReadSms()) {
            Forwarder.log(this, "READ_SMS 已授予")
            toast("短信权限已授予")
            if (Forwarder.wantRun(this)) SmsPollService.start(this)
        } else {
            Forwarder.log(this, "READ_SMS 被拒绝 —— 轮询无法读取收件箱")
            toast("短信权限被拒绝，轮询无法工作；可到系统「应用信息」里手动开启")
        }
        refresh()
    }

    private fun hasReadSms() =
        ContextCompat.checkSelfPermission(this, Manifest.permission.READ_SMS) ==
            PackageManager.PERMISSION_GRANTED

    private fun requestBatteryWhitelist() {
        try {
            val i = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            i.data = Uri.parse("package:$packageName")
            startActivity(i)
            toast("请在弹出的系统对话框中选择「允许」")
        } catch (t: Throwable) {
            try {
                startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            } catch (t2: Throwable) {
                toast("该系统不支持直接跳转，请到「电池 → 应用省电策略」设为「无限制」")
            }
        }
    }

    // ------------------------------------------------------------------
    // 启停
    // ------------------------------------------------------------------

    private fun saveAndStart() {
        val url = etUrl.text.toString().trim()
        if (url.isBlank() || !(url.startsWith("http://") || url.startsWith("https://"))) {
            toast("请填写完整地址，例如 http://192.168.1.100:8000/sms")
            return
        }
        val iv = etInterval.text.toString().trim().toIntOrNull() ?: Forwarder.DEFAULT_INTERVAL
        Forwarder.setServerUrl(this, url)
        Forwarder.setIntervalSeconds(this, iv)
        Forwarder.setWantRun(this, true)

        if (!hasReadSms()) {
            Forwarder.log(this, "已保存配置，但缺少 READ_SMS，先授权")
            toast("已保存。请先授予短信权限")
            requestPerms()
            return
        }
        SmsPollService.start(this)
        toast("已启动轮询（每 $iv 秒）")
        refresh()
    }

    private fun stopPoll() {
        SmsPollService.stop(this)
        Forwarder.log(this, "用户手动停止轮询")
        toast("已停止轮询")
        refresh()
    }

    // ------------------------------------------------------------------
    // 验证工具
    // ------------------------------------------------------------------

    private fun sendTest() {
        val url = etUrl.text.toString().trim()
        if (url.isNotBlank()) Forwarder.setServerUrl(this, url)
        val code = (100000..999999).random().toString()
        toast("正在发送测试上报…")
        Thread {
            val err = Forwarder.postNow(this, "TEST", code, "【链路自检】简历机器人测试短信，验证码 $code")
            ui.post {
                if (err == null) {
                    Forwarder.log(this, "测试上报成功，验证码 $code")
                    toast("测试上报成功（后端应能看到验证码 $code）")
                } else {
                    Forwarder.log(this, "测试上报失败：$err")
                    toast("测试上报失败：$err")
                }
                refresh()
            }
        }.start()
    }

    /** 把收件箱最近 5 条重新入队，用于验证「有短信就能上报」。 */
    private fun backfillRecent() {
        if (!hasReadSms()) {
            toast("请先授予短信权限")
            requestPerms()
            return
        }
        var n = 0
        try {
            contentResolver.query(
                Uri.parse("content://sms/inbox"),
                arrayOf(Telephony.Sms._ID, Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.DATE),
                null,
                null,
                "${Telephony.Sms._ID} DESC"
            )?.use { c ->
                while (c.moveToNext() && n < 5) {
                    val id = c.getLong(0)
                    val addr = c.getString(1) ?: ""
                    val body = c.getString(2) ?: ""
                    val date = c.getLong(3)
                    if (body.isBlank()) continue
                    if (Forwarder.enqueue(this, id, addr, Forwarder.extractCode(body), body, date)) n++
                }
            }
        } catch (t: Throwable) {
            Forwarder.log(this, "补发读取收件箱失败：${t.message}")
            toast("读取收件箱失败：${t.message}")
            return
        }
        Forwarder.log(this, "补发：最近 5 条中新增入队 $n 条")
        toast("已入队 $n 条，正在冲刷…")
        flushNow()
    }

    private fun flushNow() {
        Thread {
            val ok = Forwarder.flush(this, maxTry = 10)
            val err = Forwarder.lastFlushError
            ui.post {
                tvLog.invalidate()
                toast(if (ok > 0) "已上报 $ok 条" else "本轮未上报（${err.ifBlank { "队列为空或无新数据" }}）")
                refresh()
            }
        }.start()
    }

    private fun openNotificationAccess() {
        try {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
            toast("在列表里勾选「简历机器人·短信转发」并确认")
        } catch (t: Throwable) {
            toast("无法打开通知使用权设置：${t.message}")
        }
    }

    // ------------------------------------------------------------------
    // 状态刷新
    // ------------------------------------------------------------------

    private fun refresh() {
        val readSms = hasReadSms()
        val notifPerm = if (Build.VERSION.SDK_INT < 33) true else
            ContextCompat.checkSelfPermission(this, "android.permission.POST_NOTIFICATIONS") ==
                PackageManager.PERMISSION_GRANTED
        val listener = isNotificationListenerEnabled()

        tvPerm.text = buildString {
            append("权限状态：")
            append(if (readSms) "短信读取 ✓" else "短信读取 ✗（必须）")
            append(" ｜ 通知 ").append(if (notifPerm) "✓" else "✗")
            append(" ｜ 通知使用权 ").append(if (listener) "✓" else "—")
        }

        val want = Forwarder.wantRun(this)
        val tickAt = Forwarder.lastTickAt
        val age = if (tickAt > 0) (System.currentTimeMillis() - tickAt) / 1000 else -1L
        val fresh = age in 0..(Forwarder.intervalSeconds(this) * 3 + 10).toLong()
        val s = Forwarder.stats(this)

        tvState.text = buildString {
            append("服务状态：")
            when {
                !want -> append("未启用（点下方按钮启动）")
                fresh -> append("运行中 ✓")
                else -> append("已启用但心跳陈旧 ⚠")
            }
            if (age >= 0) {
                append("\n最近心跳：").append(hhmmss.format(Date(tickAt)))
                append("（").append(age).append(" 秒前）")
            }
            append("\n").append(Forwarder.lastTickNote)
            append("\n目标：").append(Forwarder.serverUrl(this@MainActivity))
            append("\n统计：已上报 ").append(s[0])
            append(" · 待发 ").append(s[3])
            append(" · 失败 ").append(s[1])
            append(" · 拒收 ").append(s[2])
            append(" · 间隔 ").append(Forwarder.intervalSeconds(this@MainActivity)).append("s")
        }

        val logs = Forwarder.logs(this)
        tvLog.text = if (logs.isEmpty()) "（暂无日志）" else logs.joinToString("\n")
    }

    /** API 27+ 用官方 API；低版本退回读 Settings.Secure。 */
    private fun isNotificationListenerEnabled(): Boolean {
        if (Build.VERSION.SDK_INT >= 27) {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as android.app.NotificationManager
            return nm.isNotificationListenerAccessGranted(
                android.content.ComponentName(this, SmsNotificationListener::class.java)
            )
        }
        val flat = Settings.Secure.getString(contentResolver, "enabled_notification_listeners")
        return !flat.isNullOrBlank() && flat.contains(packageName)
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()

    companion object {
        private const val REQ_PERMS = 1001
    }
}
