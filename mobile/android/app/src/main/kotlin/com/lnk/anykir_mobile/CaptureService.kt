package com.lnk.anykir_mobile

import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.view.WindowManager
import java.io.ByteArrayOutputStream

class CaptureService : Service() {
    private var projection: MediaProjection? = null
    private var display: VirtualDisplay? = null
    private var reader: ImageReader? = null
    private var thread: HandlerThread? = null
    private var width = 0
    private var height = 0
    private var lastHash = 0L
    private val lock = Object()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForegroundNotice()
        val code = intent?.getIntExtra(EXTRA_CODE, Activity.RESULT_CANCELED) ?: Activity.RESULT_CANCELED
        @Suppress("DEPRECATION")
        val data: Intent? = if (Build.VERSION.SDK_INT >= 33) intent?.getParcelableExtra(EXTRA_DATA, Intent::class.java)
        else intent?.getParcelableExtra(EXTRA_DATA)
        if (code != Activity.RESULT_OK || data == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        val manager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        val proj = manager.getMediaProjection(code, data)
        if (proj == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        proj.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                release()
                running = null
                stopSelf()
            }
        }, Handler(mainLooper))
        projection = proj
        thread = HandlerThread("anykir-capture").apply { start() }
        createDisplay()
        running = this
        return START_NOT_STICKY
    }

    private fun startForegroundNotice() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Sharing this phone", NotificationManager.IMPORTANCE_LOW))
        }
        val open = packageManager.getLaunchIntentForPackage(packageName)
        val pending = android.app.PendingIntent.getActivity(this, 0, open,
            android.app.PendingIntent.FLAG_IMMUTABLE or android.app.PendingIntent.FLAG_UPDATE_CURRENT)
        val builder = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL) else @Suppress("DEPRECATION") Notification.Builder(this)
        val notice = builder.setContentTitle("AnyKir is sharing this phone")
            .setContentText("Tap to open AnyKir and stop sharing")
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentIntent(pending)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTICE_ID, notice, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
        } else {
            startForeground(NOTICE_ID, notice)
        }
    }

    private fun targetSize(): Pair<Int, Int> {
        val wm = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val (w, h) = if (Build.VERSION.SDK_INT >= 30) {
            val b = wm.currentWindowMetrics.bounds
            b.width() to b.height()
        } else {
            val m = android.util.DisplayMetrics()
            @Suppress("DEPRECATION") wm.defaultDisplay.getRealMetrics(m)
            m.widthPixels to m.heightPixels
        }
        val k = minOf(1.0, MAX_SIDE.toDouble() / maxOf(w, h))
        return ((w * k).toInt() / 2 * 2) to ((h * k).toInt() / 2 * 2)
    }

    private fun createDisplay() {
        val (w, h) = targetSize()
        synchronized(lock) {
            width = w
            height = h
            val old = reader
            reader = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
            val dpi = resources.displayMetrics.densityDpi
            if (display == null) {
                display = projection?.createVirtualDisplay("AnyKir", w, h, dpi,
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, reader!!.surface, null, Handler(thread!!.looper))
            } else {
                display?.resize(w, h, dpi)
                display?.surface = reader!!.surface
            }
            old?.close()
            lastHash = 0
        }
    }

    fun frame(quality: Int, force: Boolean): Map<String, Any>? {
        val (tw, th) = targetSize()
        if (tw != width || th != height) createDisplay()
        synchronized(lock) {
            val image = reader?.acquireLatestImage() ?: return if (force) lastFrame else null
            try {
                val plane = image.planes[0]
                val rowPixels = plane.rowStride / plane.pixelStride
                val bmp = Bitmap.createBitmap(rowPixels, height, Bitmap.Config.ARGB_8888)
                bmp.copyPixelsFromBuffer(plane.buffer)
                val pic = if (rowPixels != width) Bitmap.createBitmap(bmp, 0, 0, width, height) else bmp
                val hash = sample(pic)
                if (!force && hash == lastHash) return null
                lastHash = hash
                val out = ByteArrayOutputStream()
                pic.compress(Bitmap.CompressFormat.JPEG, quality.coerceIn(20, 95), out)
                if (pic !== bmp) pic.recycle()
                bmp.recycle()
                lastFrame = mapOf("w" to width, "h" to height, "jpeg" to out.toByteArray())
                return lastFrame
            } finally {
                image.close()
            }
        }
    }

    private var lastFrame: Map<String, Any>? = null

    private fun sample(b: Bitmap): Long {
        var h = 1125899906842597L
        val stepX = maxOf(1, b.width / 48)
        val stepY = maxOf(1, b.height / 96)
        var y = 0
        while (y < b.height) {
            var x = 0
            while (x < b.width) {
                h = 31 * h + b.getPixel(x, y)
                x += stepX
            }
            y += stepY
        }
        return h
    }

    private fun release() {
        synchronized(lock) {
            display?.release()
            display = null
            reader?.close()
            reader = null
        }
        thread?.quitSafely()
        thread = null
    }

    override fun onDestroy() {
        running = null
        release()
        projection?.stop()
        projection = null
        super.onDestroy()
    }

    companion object {
        const val EXTRA_CODE = "code"
        const val EXTRA_DATA = "data"
        const val CHANNEL = "anykir-share"
        const val NOTICE_ID = 7
        const val MAX_SIDE = 1600
        @Volatile var running: CaptureService? = null
    }
}
