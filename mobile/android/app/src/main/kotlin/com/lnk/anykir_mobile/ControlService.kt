package com.lnk.anykir_mobile

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Context
import android.graphics.Path
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

class ControlService : AccessibilityService() {
    override fun onServiceConnected() {
        running = this
        Reopen.fromService(this)
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        running = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        running = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}

    override fun onInterrupt() {}

    private fun screen(): Pair<Float, Float> {
        val wm = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        return if (Build.VERSION.SDK_INT >= 30) {
            val b = wm.currentWindowMetrics.bounds
            b.width().toFloat() to b.height().toFloat()
        } else {
            val m = android.util.DisplayMetrics()
            @Suppress("DEPRECATION") wm.defaultDisplay.getRealMetrics(m)
            m.widthPixels.toFloat() to m.heightPixels.toFloat()
        }
    }

    fun gesture(points: List<Double>, durationMs: Long): Boolean {
        if (points.size < 2) return false
        val (w, h) = screen()
        val path = Path()
        path.moveTo((points[0] * w).toFloat().coerceIn(0f, w - 1), (points[1] * h).toFloat().coerceIn(0f, h - 1))
        var i = 2
        while (i + 1 < points.size) {
            path.lineTo((points[i] * w).toFloat().coerceIn(0f, w - 1), (points[i + 1] * h).toFloat().coerceIn(0f, h - 1))
            i += 2
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs.coerceIn(1, 10000))
        return dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
    }

    fun global(name: String): Boolean {
        val action = when (name) {
            "back" -> GLOBAL_ACTION_BACK
            "home" -> GLOBAL_ACTION_HOME
            "recents" -> GLOBAL_ACTION_RECENTS
            "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
            "lock" -> if (Build.VERSION.SDK_INT >= 28) GLOBAL_ACTION_LOCK_SCREEN else return false
            else -> return false
        }
        return performGlobalAction(action)
    }

    private fun focused(): AccessibilityNodeInfo? =
        rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)

    fun type(text: String): Boolean {
        val node = focused() ?: return false
        val now = node.text?.toString() ?: ""
        val shown = if (Build.VERSION.SDK_INT >= 26 && node.isShowingHintText) "" else now
        val start = node.textSelectionStart
        val end = node.textSelectionEnd
        val next = if (start in 0..shown.length && end in start..shown.length) shown.substring(0, start) + text + shown.substring(end)
        else shown + text
        val args = Bundle()
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, next)
        val ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
        val caret = if (start in 0..shown.length) start + text.length else next.length
        val sel = Bundle()
        sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, caret)
        sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, caret)
        node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, sel)
        return ok
    }

    fun backspace(): Boolean {
        val node = focused() ?: return false
        val now = if (Build.VERSION.SDK_INT >= 26 && node.isShowingHintText) "" else node.text?.toString() ?: ""
        if (now.isEmpty()) return true
        val start = node.textSelectionStart
        val end = node.textSelectionEnd
        val (next, caret) = when {
            start in 0..now.length && end in start..now.length && end > start -> (now.substring(0, start) + now.substring(end)) to start
            start in 1..now.length -> (now.substring(0, start - 1) + now.substring(start)) to (start - 1)
            else -> now.dropLast(1) to (now.length - 1)
        }
        val args = Bundle()
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, next)
        val ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
        val sel = Bundle()
        sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, caret)
        sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, caret)
        node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, sel)
        return ok
    }

    fun enter(): Boolean {
        val node = focused() ?: return false
        if (Build.VERSION.SDK_INT >= 30 &&
            node.actionList.any { it.id == AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id }) {
            return node.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)
        }
        return type("\n")
    }

    fun clipboardAction(name: String): Boolean {
        val node = focused() ?: return false
        return node.performAction(when (name) {
            "copy" -> AccessibilityNodeInfo.ACTION_COPY
            "cut" -> AccessibilityNodeInfo.ACTION_CUT
            "paste" -> AccessibilityNodeInfo.ACTION_PASTE
            else -> return false
        })
    }

    fun selectAll(): Boolean {
        val node = focused() ?: return false
        val len = node.text?.length ?: 0
        val sel = Bundle()
        sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, 0)
        sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, len)
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, sel)
    }

    companion object {
        @Volatile var running: ControlService? = null
    }
}
