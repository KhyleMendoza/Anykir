package com.lnk.anykir_mobile

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.util.concurrent.Executors

class MainActivity : FlutterActivity() {
    private var updates: MethodChannel? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        val messenger = flutterEngine.dartExecutor.binaryMessenger
        MethodChannel(messenger, "anykir/tailscale").setMethodCallHandler { call, result ->
            if (call.method != "open") {
                result.notImplemented()
                return@setMethodCallHandler
            }
            val app = packageManager.getLaunchIntentForPackage(TAILSCALE)
            val intent = app ?: Intent(Intent.ACTION_VIEW, Uri.parse("market://details?id=$TAILSCALE"))
            try {
                startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            } catch (e: Exception) {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("https://play.google.com/store/apps/details?id=$TAILSCALE"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            }
            result.success(app != null)
        }
        updates = MethodChannel(messenger, "anykir/update").apply {
            setMethodCallHandler { call, result ->
                when (call.method) {
                    "dir" -> {
                        val dir = File(cacheDir, "updates")
                        dir.mkdirs()
                        result.success(dir.absolutePath)
                    }
                    "install" -> install(File(call.argument<String>("path") ?: ""), result)
                    "canReopen" -> result.success(Reopen.canReopen(this@MainActivity))
                    "meteredNetwork" -> {
                        val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as android.net.ConnectivityManager
                        result.success(try { cm.isActiveNetworkMetered } catch (e: Exception) { false })
                    }
                    "allowReopen" -> {
                        startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName"))
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                        result.success(true)
                    }
                    else -> result.notImplemented()
                }
            }
        }
        MethodChannel(messenger, "anykir/clipboard").setMethodCallHandler { call, result ->
            if (call.method != "stamp") {
                result.notImplemented()
                return@setMethodCallHandler
            }
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
                result.success(-2L)
                return@setMethodCallHandler
            }
            val clips = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            val about = clips.primaryClipDescription
            result.success(if (about != null && about.hasMimeType("text/*")) about.timestamp else 0L)
        }
        shareChannel(messenger)
        inputChannel(messenger)
        current = this
    }

    override fun onResume() {
        super.onResume()
        Reopen.opened(this)
        askNotificationsOnce()
    }

    private fun askNotificationsOnce() {
        if (Build.VERSION.SDK_INT < 33) return
        if (checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED) return
        val prefs = getSharedPreferences("anykir_native", Context.MODE_PRIVATE)
        if (prefs.getBoolean("asked_notifications", false)) return
        prefs.edit().putBoolean("asked_notifications", true).apply()
        requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 4102)
    }

    private var pendingShare: MethodChannel.Result? = null
    private val frames = Executors.newSingleThreadExecutor()

    private fun shareChannel(messenger: io.flutter.plugin.common.BinaryMessenger) {
        MethodChannel(messenger, "anykir/share").setMethodCallHandler { call, result ->
            when (call.method) {
                "start" -> {
                    if (CaptureService.running != null) {
                        result.success(true)
                        return@setMethodCallHandler
                    }
                    pendingShare = result
                    val manager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
                    val ask = if (Build.VERSION.SDK_INT >= 34)
                        manager.createScreenCaptureIntent(android.media.projection.MediaProjectionConfig.createConfigForDefaultDisplay())
                    else manager.createScreenCaptureIntent()
                    @Suppress("DEPRECATION") startActivityForResult(ask, SHARE_REQUEST)
                }
                "stop" -> {
                    stopService(Intent(this, CaptureService::class.java))
                    result.success(true)
                }
                "capturing" -> result.success(CaptureService.running != null)
                "deviceName" -> {
                    val name = Settings.Global.getString(contentResolver, "device_name")
                    result.success(if (!name.isNullOrBlank()) name else "${Build.MANUFACTURER} ${Build.MODEL}".trim())
                }
                "frame" -> {
                    val quality = call.argument<Int>("quality") ?: 60
                    val force = call.argument<Boolean>("force") ?: false
                    val main = Handler(Looper.getMainLooper())
                    frames.execute {
                        val frame = try { CaptureService.running?.frame(quality, force) } catch (e: Exception) { null }
                        main.post { result.success(frame) }
                    }
                }
                else -> result.notImplemented()
            }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION") super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != SHARE_REQUEST) return
        val answer = pendingShare
        pendingShare = null
        if (resultCode != RESULT_OK || data == null) {
            answer?.success(false)
            return
        }
        val start = Intent(this, CaptureService::class.java)
            .putExtra(CaptureService.EXTRA_CODE, resultCode)
            .putExtra(CaptureService.EXTRA_DATA, data)
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(start) else startService(start)
        val main = Handler(Looper.getMainLooper())
        var tries = 0
        lateinit var check: Runnable
        check = Runnable {
            if (CaptureService.running != null || tries++ > 30) answer?.success(CaptureService.running != null)
            else main.postDelayed(check, 100)
        }
        main.post(check)
    }

    private fun inputChannel(messenger: io.flutter.plugin.common.BinaryMessenger) {
        MethodChannel(messenger, "anykir/input").setMethodCallHandler { call, result ->
            val control = ControlService.running
            when (call.method) {
                "enabled" -> result.success(control != null)
                "settings" -> {
                    startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    result.success(true)
                }
                "gesture" -> {
                    val points = call.argument<List<Double>>("points") ?: emptyList()
                    val ms = (call.argument<Int>("ms") ?: 50).toLong()
                    result.success(control?.gesture(points, ms) ?: false)
                }
                "global" -> result.success(control?.global(call.argument<String>("name") ?: "") ?: false)
                "type" -> result.success(control?.type(call.argument<String>("text") ?: "") ?: false)
                "key" -> result.success(when (call.argument<String>("name")) {
                    "enter" -> control?.enter()
                    "backspace" -> control?.backspace()
                    "copy" -> control?.clipboardAction("copy")
                    "cut" -> control?.clipboardAction("cut")
                    "paste" -> control?.clipboardAction("paste")
                    "selectall" -> control?.selectAll()
                    else -> false
                } ?: false)
                else -> result.notImplemented()
            }
        }
    }

    override fun onDestroy() {
        if (current === this) current = null
        super.onDestroy()
    }

    private fun install(file: File, result: MethodChannel.Result) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !packageManager.canRequestPackageInstalls()) {
            startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            result.success("permission")
            return
        }
        val main = Handler(Looper.getMainLooper())
        Thread {
            try {
                val installer = packageManager.packageInstaller
                val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
                params.setAppPackageName(packageName)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                    params.setRequestUpdateOwnership(true)
                }
                val id = installer.createSession(params)
                installer.openSession(id).use { session ->
                    file.inputStream().use { input ->
                        session.openWrite("anykir.apk", 0, file.length()).use { out ->
                            input.copyTo(out)
                            session.fsync(out)
                        }
                    }
                    var flags = PendingIntent.FLAG_UPDATE_CURRENT
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) flags = flags or PendingIntent.FLAG_MUTABLE
                    val done = PendingIntent.getBroadcast(this, id, Intent(this, InstallReceiver::class.java), flags)
                    Reopen.arm(this)
                    session.commit(done.intentSender)
                }
                main.post { result.success("started") }
            } catch (e: Exception) {
                main.post { result.error("install", e.message ?: e.toString(), null) }
            }
        }.start()
    }

    fun report(text: String) {
        Handler(Looper.getMainLooper()).post { updates?.invokeMethod("status", text) }
    }

    companion object {
        const val TAILSCALE = "com.tailscale.ipn"
        const val SHARE_REQUEST = 4101
        var current: MainActivity? = null
    }
}

class InstallReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                val confirm: Intent? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                } else {
                    @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_INTENT)
                }
                MainActivity.current?.report("Tap Update on the Android screen")
                confirm?.let { context.startActivity(it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
            }
            PackageInstaller.STATUS_SUCCESS -> {}
            else -> {
                Reopen.disarm(context)
                MainActivity.current?.report(
                "Update failed: " + (intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: "cancelled"))
            }
        }
    }
}
