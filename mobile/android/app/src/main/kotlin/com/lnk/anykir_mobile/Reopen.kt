package com.lnk.anykir_mobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import java.io.File

object Reopen {
    private const val CHANNEL = "anykir-updated"
    private const val NOTICE_ID = 4202
    private const val FRESH_MS = 15 * 60 * 1000L

    private fun flag(context: Context) = File(context.filesDir, "reopen-after-update")

    fun arm(context: Context) {
        try { flag(context).writeText(System.currentTimeMillis().toString()) } catch (_: Exception) {}
    }

    private fun take(context: Context): Boolean {
        val f = flag(context)
        if (!f.exists()) return false
        val at = try { f.readText().trim().toLong() } catch (_: Exception) { 0L }
        f.delete()
        return System.currentTimeMillis() - at in 0..FRESH_MS
    }

    private fun launchIntent(context: Context): Intent? =
        context.packageManager.getLaunchIntentForPackage(context.packageName)
            ?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)

    fun afterUpdate(context: Context, finished: () -> Unit = {}) {
        if (!take(context)) return finished()
        val open = launchIntent(context) ?: return finished()
        notice(context, open)
        val overlay = showOverlay(context)
        try { context.startActivity(open) } catch (_: Exception) {}
        val main = Handler(Looper.getMainLooper())
        if (overlay != null) {
            main.postDelayed({ try { context.startActivity(open) } catch (_: Exception) {} }, 400)
        }
        main.postDelayed({
            overlay?.let { removeOverlay(context, it) }
            finished()
        }, 3000)
        pending = true
        ControlService.running?.let { fromService(it) }
    }

    fun canReopen(context: Context) =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.Q || Settings.canDrawOverlays(context)

    private fun showOverlay(context: Context): View? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M || !Settings.canDrawOverlays(context)) return null
        return try {
            val wm = context.applicationContext.getSystemService(Context.WINDOW_SERVICE) as WindowManager
            val view = View(context.applicationContext)
            @Suppress("DEPRECATION")
            val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY else WindowManager.LayoutParams.TYPE_PHONE
            val params = WindowManager.LayoutParams(1, 1, type,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE,
                PixelFormat.TRANSPARENT)
            params.gravity = Gravity.TOP or Gravity.START
            wm.addView(view, params)
            view
        } catch (_: Exception) { null }
    }

    private fun removeOverlay(context: Context, view: View) {
        try {
            (context.applicationContext.getSystemService(Context.WINDOW_SERVICE) as WindowManager).removeView(view)
        } catch (_: Exception) {}
    }

    fun fromService(context: Context) {
        if (!pending) return
        pending = false
        launchIntent(context)?.let { try { context.startActivity(it) } catch (_: Exception) {} }
    }

    fun opened(context: Context) {
        pending = false
        try { (context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).cancel(NOTICE_ID) } catch (_: Exception) {}
    }

    fun disarm(context: Context) {
        flag(context).delete()
    }

    @Volatile private var pending = false

    private fun notice(context: Context, open: Intent) {
        try {
            val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (Build.VERSION.SDK_INT >= 26) {
                nm.createNotificationChannel(NotificationChannel(CHANNEL, "Updates", NotificationManager.IMPORTANCE_HIGH))
            }
            val tap = PendingIntent.getActivity(context, NOTICE_ID, open,
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            val builder = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(context, CHANNEL)
            else @Suppress("DEPRECATION") Notification.Builder(context).setPriority(Notification.PRIORITY_HIGH)
            val version = try {
                context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: ""
            } catch (_: Exception) { "" }
            nm.notify(NOTICE_ID, builder
                .setContentTitle("AnyKir updated" + if (version.isNotEmpty()) " to $version" else "")
                .setContentText("Tap to open AnyKir")
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentIntent(tap)
                .setAutoCancel(true)
                .build())
        } catch (_: Exception) {}
    }
}

class UpdatedReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        val result = goAsync()
        Reopen.afterUpdate(context) { result.finish() }
    }
}
