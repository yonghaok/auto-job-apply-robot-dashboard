package com.example.resumebot

/**
 * 开机 / 应用更新后自动拉起轮询服务。
 *
 * 只在用户**曾经启用**过轮询（KEY_WANT_RUN=true）时才自启，避免骚扰。
 *
 * 说明：BOOT_COMPLETED 在用户解锁后才投递（非 directBootAware），
 * 因此此时可以安全读写凭据加密的 SharedPreferences。
 * MY_PACKAGE_REPLACED 让「覆盖安装新包」后无需再手动点一次启动。
 */
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
            "android.intent.action.QUICKBOOT_POWERON" -> {
                if (!Forwarder.wantRun(context)) return
                Forwarder.log(context, "开机/更新后自动拉起轮询服务（action=${intent.action}）")
                SmsPollService.start(context)
            }
        }
    }
}
