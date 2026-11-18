package com.example.resumebot

/**
 * 公共转发层（v2 · 轮询版）：把「发件人 / 验证码 / 正文」POST 到 PC 端 resume-bot 后端。
 *
 * v2 相比 v1 的变化：
 *  - 配置项从「PC IP」升级为「完整服务器地址」（默认 http://192.168.1.100:8000/sms），
 *    旧键 pc_ip 会在首次读取时自动迁移。
 *  - 新增**待发队列**：短信先落 SharedPreferences 再发送，失败不丢；网络恢复后回补。
 *    队列按短信 _id 去重，因此「轮询路径」与「通知路径」同时命中同一条短信也不会重复上报。
 *  - 新增同步上报 [postSync]，返回 null=成功 / 字符串=错误，便于上层做决策。
 *  - 新增无盘化轮询心跳（[lastTickAt]/[lastTickNote]），UI 每秒读取而不必落盘。
 */
import android.content.Context
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

object Forwarder {
    const val TAG = "ResumeBot"

    const val PREFS = "resumebot"

    /** 旧键（v1 只存 IP）；保留常量以免 [SmsReceiver] 引用处断裂。 */
    const val KEY_PC_IP = "pc_ip"
    const val PORT = 8000

    // ---- 配置 ----
    const val KEY_URL = "server_url"
    const val KEY_INTERVAL = "poll_seconds"
    const val KEY_WANT_RUN = "poll_enabled"

    // ---- 状态 ----
    const val KEY_LAST_ID = "last_sms_id"
    const val KEY_PENDING = "pending_queue"
    const val KEY_LOG = "ui_log"
    const val KEY_STAT_OK = "stat_ok"
    const val KEY_STAT_FAIL = "stat_fail"
    const val KEY_STAT_DROP = "stat_drop"

    const val DEFAULT_URL = "http://192.168.1.100:8000/sms"
    const val DEFAULT_INTERVAL = 5
    const val MAX_PENDING = 300
    const val MAX_LOG = 60

    /** 内存态轮询心跳（同一进程内供 UI 读取，不落盘）。 */
    @Volatile var lastTickAt = 0L
    @Volatile var lastTickNote = "尚未开始轮询"

    private val LOCK = Any()

    fun prefs(c: Context) = c.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    // ------------------------------------------------------------------
    // 配置读写
    // ------------------------------------------------------------------

    /** 服务器地址：优先新键；v1 只写了 IP 则自动迁移为完整 URL。 */
    fun serverUrl(c: Context): String {
        val p = prefs(c)
        val u = p.getString(KEY_URL, null)
        if (!u.isNullOrBlank()) return u.trim()
        val ip = p.getString(KEY_PC_IP, null)
        val migrated = if (ip.isNullOrBlank()) DEFAULT_URL else "http://${ip.trim()}:$PORT/sms"
        p.edit().putString(KEY_URL, migrated).commit()
        return migrated
    }

    fun setServerUrl(c: Context, url: String) {
        prefs(c).edit().putString(KEY_URL, url.trim()).commit()
    }

    fun intervalSeconds(c: Context): Int {
        val n = prefs(c).getInt(KEY_INTERVAL, DEFAULT_INTERVAL)
        return when {
            n < 1 -> DEFAULT_INTERVAL
            n > 3600 -> 3600
            else -> n
        }
    }

    fun setIntervalSeconds(c: Context, n: Int) {
        prefs(c).edit().putInt(KEY_INTERVAL, if (n < 1) DEFAULT_INTERVAL else n).commit()
    }

    /** 用户是否希望轮询常驻（决定开机是否自动拉起）。 */
    fun wantRun(c: Context) = prefs(c).getBoolean(KEY_WANT_RUN, false)
    fun setWantRun(c: Context, v: Boolean) {
        prefs(c).edit().putBoolean(KEY_WANT_RUN, v).commit()
    }

    // ------------------------------------------------------------------
    // 验证码提取
    // ------------------------------------------------------------------

    /** 粗提取 4~8 位验证码：优先取「验证码」附近的数字。 */
    fun extractCode(text: String): String {
        val near = Regex("""(?:验证码|校验码|动态码|安全码|验证码为|登录码)[^0-9]{0,8}(\d{4,8})""")
            .find(text)
        if (near != null) return near.groupValues[1]
        return Regex("""\b(\d{4,8})\b""").find(text)?.value ?: ""
    }

    fun now(): String = SimpleDateFormat("HH:mm:ss", Locale.CHINA).format(Date())

    // ------------------------------------------------------------------
    // 上报
    // ------------------------------------------------------------------

    /**
     * 同步 POST JSON 到 /sms。
     * @return null 表示成功（HTTP 2xx）；否则返回错误描述，如 "HTTP 400" / "ConnectException: ..."。
     */
    fun postSync(url: String, phone: String, code: String, text: String, timeoutMs: Int = 6000): String? {
        if (url.isBlank()) return "服务器地址为空"
        var conn: HttpURLConnection? = null
        return try {
            conn = URL(url).openConnection() as HttpURLConnection
            conn.requestMethod = "POST"
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            // 后端是 ThreadingHTTPServer，主动关连接可避免 keep-alive 复用踩坑
            conn.setRequestProperty("Connection", "close")
            conn.doOutput = true
            conn.connectTimeout = timeoutMs
            conn.readTimeout = timeoutMs
            val payload = JSONObject().apply {
                put("phone", phone)
                put("code", code)
                put("text", text)
                // 只带 ts，**不要**带 "src" 之类的短名：后端 SMS_FIELD_ALIASES 把 "src"
                // 也算作 phone 的别名，发件人为空时会被误当成发件人写进库里。
                put("ts", System.currentTimeMillis())
            }.toString()
            conn.outputStream.use { it.write(payload.toByteArray(Charsets.UTF_8)) }
            val rc = conn.responseCode
            // 必须读掉响应体，否则部分实现会在 disconnect 时抛异常
            try {
                val s = if (rc in 200..299) conn.inputStream else conn.errorStream
                s?.use { it.readBytes() }
            } catch (_: Exception) {
            }
            if (rc in 200..299) null else "HTTP $rc"
        } catch (e: Exception) {
            "${e.javaClass.simpleName}: ${e.message}"
        } finally {
            try {
                conn?.disconnect()
            } catch (_: Exception) {
            }
        }
    }

    /** 立即发送一条（同步）。供 UI「测试通道」与通知路径复用。 */
    fun postNow(c: Context, phone: String, code: String, text: String): String? {
        val err = postSync(serverUrl(c), phone, code, text)
        if (err == null) addStats(c, ok = 1) else addStats(c, fail = 1)
        return err
    }

    // ------------------------------------------------------------------
    // 待发队列（断网不丢）
    // ------------------------------------------------------------------

    fun pending(c: Context): JSONArray {
        val s = prefs(c).getString(KEY_PENDING, null) ?: return JSONArray()
        return try {
            JSONArray(s)
        } catch (_: Exception) {
            JSONArray()
        }
    }

    fun pendingCount(c: Context) = pending(c).length()

    /**
     * 入队一条短信。按 _id 去重 —— 同一条短信无论被轮询还是通知路径命中都只入队一次。
     * @return true 表示本次确实新增。
     */
    fun enqueue(c: Context, id: Long, phone: String, code: String, text: String, ts: Long): Boolean {
        synchronized(LOCK) {
            val arr = pending(c)
            for (i in 0 until arr.length()) {
                if (arr.optJSONObject(i)?.optLong("id", -1L) == id) return false
            }
            arr.put(JSONObject().apply {
                put("id", id)
                put("phone", phone)
                put("code", code)
                put("text", text)
                put("ts", ts)
            })
            while (arr.length() > MAX_PENDING) arr.remove(0) // 溢出丢最旧
            prefs(c).edit().putString(KEY_PENDING, arr.toString()).commit()
            return true
        }
    }

    /**
     * 冲刷待发队列。
     *
     * 策略：
     *  - 顺序发送，**首次网络类失败即中止本轮**（判定链路断，避免 300 条 × 6 秒超时）；
     *  - HTTP 4xx 视为「该条服务端拒收」，单条丢弃并计数，不阻塞后续；
     *  - 每轮最多实际尝试 [maxTry] 条，其余留到下轮。
     *
     * @return 本轮成功上报的条数；[lastFlushError] 记录中止原因。
     */
    @Volatile var lastFlushError: String = ""

    fun flush(c: Context, maxTry: Int = 30): Int {
        val snapshot = synchronized(LOCK) { pending(c) }
        if (snapshot.length() == 0) return 0
        val url = serverUrl(c)
        val remain = JSONArray()
        var ok = 0
        var drop = 0
        var tried = 0
        var broken = false
        var firstErr = ""
        lastFlushError = ""

        for (i in 0 until snapshot.length()) {
            val o = snapshot.optJSONObject(i) ?: continue
            if (broken || tried >= maxTry) {
                remain.put(o)
                continue
            }
            tried++
            val err = postSync(url, o.optString("phone"), o.optString("code"), o.optString("text"))
            if (err == null) {
                ok++
            } else if (err.startsWith("HTTP 4")) {
                // 服务端明确拒收：丢弃这一条，别让它卡住整个队列
                drop++
                log(c, "丢弃服务端拒收短信 #${o.optLong("id")}（$err）")
            } else {
                broken = true
                firstErr = err
                remain.put(o)
            }
        }

        synchronized(LOCK) {
            prefs(c).edit().putString(KEY_PENDING, remain.toString()).commit()
        }
        if (ok > 0 || drop > 0) addStats(c, ok = ok, drop = drop)
        if (broken && ok == 0) {
            addStats(c, fail = 1)
            lastFlushError = firstErr
        }
        return ok
    }

    // ------------------------------------------------------------------
    // 统计与日志
    // ------------------------------------------------------------------

    fun addStats(c: Context, ok: Int = 0, fail: Int = 0, drop: Int = 0) {
        synchronized(LOCK) {
            val p = prefs(c)
            val e = p.edit()
            if (ok != 0) e.putInt(KEY_STAT_OK, p.getInt(KEY_STAT_OK, 0) + ok)
            if (fail != 0) e.putInt(KEY_STAT_FAIL, p.getInt(KEY_STAT_FAIL, 0) + fail)
            if (drop != 0) e.putInt(KEY_STAT_DROP, p.getInt(KEY_STAT_DROP, 0) + drop)
            e.commit()
        }
    }

    /** @return IntArray[已成功上报, 失败次数, 服务端拒收, 待发队列长度] */
    fun stats(c: Context): IntArray {
        val p = prefs(c)
        return intArrayOf(
            p.getInt(KEY_STAT_OK, 0),
            p.getInt(KEY_STAT_FAIL, 0),
            p.getInt(KEY_STAT_DROP, 0),
            pendingCount(c)
        )
    }

    fun resetStats(c: Context) {
        prefs(c).edit()
            .putInt(KEY_STAT_OK, 0)
            .putInt(KEY_STAT_FAIL, 0)
            .putInt(KEY_STAT_DROP, 0)
            .commit()
    }

    fun log(c: Context, line: String) {
        Log.i(TAG, line)
        synchronized(LOCK) {
            val arr = logsJson(c)
            arr.put("${now()}  $line")
            while (arr.length() > MAX_LOG) arr.remove(0)
            prefs(c).edit().putString(KEY_LOG, arr.toString()).commit()
        }
    }

    private fun logsJson(c: Context): JSONArray {
        val s = prefs(c).getString(KEY_LOG, null) ?: return JSONArray()
        return try {
            JSONArray(s)
        } catch (_: Exception) {
            JSONArray()
        }
    }

    /** 最新在前，便于界面直接展示（不必滚动）。 */
    fun logs(c: Context): List<String> {
        val arr = logsJson(c)
        val out = ArrayList<String>(arr.length())
        for (i in arr.length() - 1 downTo 0) out.add(arr.optString(i))
        return out
    }

    fun clearLogs(c: Context) {
        prefs(c).edit().putString(KEY_LOG, "[]").commit()
    }

    // ------------------------------------------------------------------
    // v1 兼容：通知路径直接调用
    // ------------------------------------------------------------------

    /** 通知路径专用：先入队再冲刷，保证与轮询路径共用同一套去重与重试逻辑。 */
    fun send(context: Context, phone: String, code: String, text: String) {
        if (phone.isBlank() && text.isBlank()) return
        val id = -(System.currentTimeMillis() % 1_000_000_000L) - 1L // 负数 id 避免与真实短信 _id 冲突
        enqueue(context, id, phone, code, text, System.currentTimeMillis())
        Thread {
            val n = flush(context, maxTry = 5)
            log(context, if (n > 0) "通知路径上报成功（$phone）" else "通知路径上报失败：$lastFlushError")
        }.start()
    }
}
