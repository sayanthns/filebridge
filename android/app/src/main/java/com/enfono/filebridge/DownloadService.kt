package com.enfono.filebridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Environment
import android.os.IBinder
import android.os.PowerManager
import android.provider.MediaStore
import androidx.core.app.NotificationCompat
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Downloads files from the Mac, in the foreground, holding the radio awake.
 *
 * Android's own DownloadManager was used until 1.9.0 and could not do this job.
 * It runs transfers as JobScheduler work and holds no wifi lock, so when the
 * screen went off the radio slept, the socket died, and the whole download was
 * dropped as "network lost" rather than retried. Measured on the Mac: three
 * attempts at a 707 MB file, killed at 15.8 s, 15.9 s and 16.2 s — three
 * different byte counts, the same clock, i.e. the phone's 15 s display timeout.
 * DownloadManager never once asked for a byte range afterwards.
 *
 * So the app does it itself: a foreground service, a WifiLock and a partial
 * WakeLock for as long as bytes are moving, and a resume loop that asks for
 * `Range: bytes=N-` after every drop. Partial files survive a give-up, so a
 * later retry of the same URL continues instead of restarting.
 */
class DownloadService : Service() {

    companion object {
        const val EXTRA_URL = "url"
        const val EXTRA_NAME = "name"
        const val EXTRA_LABEL = "label"
        const val EXTRA_MIME = "mime"
        const val ACTION_CANCEL = "com.enfono.filebridge.CANCEL"
        /** Sent when a download settles, so a live Activity can react at once. */
        const val ACTION_SETTLED = "com.enfono.filebridge.SETTLED"

        private const val CHANNEL = "transfers"
        private const val ONGOING_ID = 1
        private const val PREFS = "fb"
        private const val KEY_RESULTS = "download_results"
        private const val KEY_RESUME = "download_resume"

        /** Attempts with no progress at all before giving up. Progress resets it. */
        private const val STALLED_LIMIT = 8

        fun start(context: Context, url: String, name: String, label: String, mime: String) {
            val intent = Intent(context, DownloadService::class.java)
                .putExtra(EXTRA_URL, url)
                .putExtra(EXTRA_NAME, name)
                .putExtra(EXTRA_LABEL, label)
                .putExtra(EXTRA_MIME, mime)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        /** Results the app has not shown yet. Reading them clears the list. */
        fun drainResults(context: Context): List<Result> {
            val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val raw = prefs.getString(KEY_RESULTS, "[]") ?: "[]"
            prefs.edit().remove(KEY_RESULTS).apply()
            val out = mutableListOf<Result>()
            try {
                val array = JSONArray(raw)
                for (i in 0 until array.length()) {
                    val item = array.getJSONObject(i)
                    out.add(Result(item.optString("label"), item.optBoolean("ok"),
                        item.optString("detail")))
                }
            } catch (e: Exception) {
                // A corrupt list is not worth crashing over; it has been cleared.
            }
            return out
        }

        fun human(bytes: Long): String {
            if (bytes < 1024) return bytes.toString() + " B"
            val units = listOf("KB", "MB", "GB")
            var value = bytes.toDouble() / 1024
            var unit = 0
            while (value >= 1024 && unit < units.size - 1) { value /= 1024; unit++ }
            val tenths = Math.round(value * 10)
            return (tenths / 10).toString() + "." + (tenths % 10) + " " + units[unit]
        }
    }

    data class Result(val label: String, val ok: Boolean, val detail: String)

    private class Job(
        val url: String, val name: String, val label: String, val mime: String)

    private val queue = LinkedBlockingQueue<Job>()
    private val cancelled = AtomicBoolean(false)
    private var worker: Thread? = null
    @Volatile private var running = true

    private var wifiLock: WifiManager.WifiLock? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_CANCEL) {
            cancelled.set(true)
            queue.clear()
            return START_NOT_STICKY
        }

        val url = intent?.getStringExtra(EXTRA_URL)
        val name = intent?.getStringExtra(EXTRA_NAME)
        if (url.isNullOrEmpty() || name.isNullOrEmpty()) {
            if (queue.isEmpty() && worker == null) stopSelf()
            return START_NOT_STICKY
        }

        val label = intent.getStringExtra(EXTRA_LABEL) ?: name
        val mime = intent.getStringExtra(EXTRA_MIME) ?: "application/octet-stream"

        // Foreground before anything slow: Android gives a service seconds, not
        // minutes, to put its notification up, and kills it otherwise.
        startForeground(ONGOING_ID, ongoing(label, 0, 0, 0.0))
        queue.add(Job(url, name, label, mime))
        startWorker()
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        running = false
        cancelled.set(true)
        worker?.interrupt()
        releaseLocks()
        super.onDestroy()
    }

    // ------------------------------------------------------------ the worker

    private fun startWorker() {
        if (worker?.isAlive == true) return
        cancelled.set(false)
        worker = Thread {
            acquireLocks()
            try {
                while (running) {
                    val job = queue.poll() ?: break
                    val outcome = try {
                        runJob(job)
                    } catch (e: InterruptedException) {
                        "Cancelled."
                    } catch (e: Exception) {
                        "Unexpected error: " + (e.message ?: e.javaClass.simpleName)
                    }
                    record(job.label, outcome == null, outcome ?: "Saved to Downloads/FileBridge")
                    finished(job.label, outcome)
                }
            } finally {
                releaseLocks()
                stopForeground(true)
                stopSelf()
            }
        }
        worker!!.start()
    }

    /** Returns null on success, or a sentence explaining the failure. */
    private fun runJob(job: Job): String? {
        val target = openTarget(job) ?: return "Could not create the file in Downloads."
        var have = target.size()
        var stalled = 0
        var total = -1L

        while (true) {
            if (cancelled.get()) return "Cancelled."
            val before = have

            try {
                val connection = URL(job.url).openConnection() as HttpURLConnection
                connection.connectTimeout = 15000
                connection.readTimeout = 30000
                connection.setRequestProperty("Accept-Encoding", "identity")
                if (have > 0) {
                    connection.setRequestProperty("Range", "bytes=" + have + "-")
                    target.etag?.let { connection.setRequestProperty("If-Match", it) }
                }

                val code = connection.responseCode
                if (code == 412) {
                    // The file on the Mac changed under us. Start again rather
                    // than splicing bytes from two different files together.
                    target.truncate()
                    have = 0
                    target.etag = null
                    connection.disconnect()
                    continue
                }
                if (have > 0 && code == HttpURLConnection.HTTP_OK) {
                    // Asked to resume, got the whole file: the server ignored the
                    // range, so what we hold is not a prefix we can trust.
                    target.truncate()
                    have = 0
                }
                if (code != HttpURLConnection.HTTP_OK && code != HttpURLConnection.HTTP_PARTIAL) {
                    connection.disconnect()
                    return "The Mac answered HTTP " + code + ". Is sharing still on?"
                }

                connection.getHeaderField("ETag")?.let { target.etag = it }
                if (total <= 0) total = totalFrom(connection, have)
                target.rememberResume(job.url)

                var lastTick = System.currentTimeMillis()
                var tickBytes = have
                connection.inputStream.use { input ->
                    target.append().use { output ->
                        val buffer = ByteArray(64 * 1024)
                        while (true) {
                            if (cancelled.get()) throw InterruptedException()
                            val read = input.read(buffer)
                            if (read < 0) break
                            output.write(buffer, 0, read)
                            have += read

                            val now = System.currentTimeMillis()
                            if (now - lastTick >= 1000) {
                                val rate = (have - tickBytes) * 1000.0 / (now - lastTick)
                                notifyOngoing(job.label, have, total, rate)
                                lastTick = now
                                tickBytes = have
                            }
                        }
                        output.flush()
                    }
                }
                connection.disconnect()

                have = target.size()
                if (total > 0 && have >= total) {
                    target.finish()
                    return null
                }
                // Ended early without an exception: the connection closed
                // mid-body. Loop round and resume from where we got to.
            } catch (e: InterruptedException) {
                return "Cancelled."
            } catch (e: IOException) {
                have = target.size()
                if (total > 0 && have >= total) {
                    target.finish()
                    return null
                }
            }

            if (have > before) {
                stalled = 0  // it moved; a dropped connection is not a failure
            } else if (++stalled >= STALLED_LIMIT) {
                return "The connection kept dropping without moving forward. " +
                    "The Mac may have stopped sharing, or the phone left the wifi. " +
                    "Downloading again resumes from " + human(have) + "."
            }

            notifyOngoing(job.label, have, total, 0.0)
            try {
                Thread.sleep(minOf(2000L * (stalled + 1), 15000L))
            } catch (e: InterruptedException) {
                return "Cancelled."
            }
        }
    }

    /** Size of the whole file, from Content-Range or Content-Length. */
    private fun totalFrom(connection: HttpURLConnection, offset: Long): Long {
        val range = connection.getHeaderField("Content-Range")
        if (range != null && range.contains("/")) {
            val tail = range.substringAfterLast("/").trim()
            tail.toLongOrNull()?.let { return it }
        }
        val length = connection.getHeaderField("Content-Length")?.toLongOrNull() ?: -1L
        return if (length < 0) -1L else length + offset
    }

    // ------------------------------------------------------------ the file
    //
    // Two worlds. On Android 10+ the public Downloads folder is only reachable
    // through MediaStore, which can append ("wa") and hide a half-written file
    // behind IS_PENDING. Below that it is a plain File.

    private inner class Target(val uri: Uri?, val file: File?) {
        var etag: String? = null

        fun size(): Long {
            if (file != null) return if (file.exists()) file.length() else 0L
            return try {
                contentResolver.openFileDescriptor(uri!!, "r")?.use { it.statSize } ?: 0L
            } catch (e: Exception) {
                0L
            }
        }

        fun append(): OutputStream =
            if (file != null) FileOutputStream(file, true)
            else contentResolver.openOutputStream(uri!!, "wa")
                ?: throw IOException("cannot open " + uri)

        fun truncate() {
            if (file != null) {
                FileOutputStream(file, false).close()
                return
            }
            contentResolver.openOutputStream(uri!!, "wt")?.close()
        }

        fun finish() {
            if (uri != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val values = ContentValues()
                values.put(MediaStore.Downloads.IS_PENDING, 0)
                contentResolver.update(uri, values, null, null)
            }
            forgetResume()
        }

        fun rememberResume(url: String) {
            val entry = JSONObject()
                .put("target", uri?.toString() ?: file!!.absolutePath)
                .put("isFile", file != null)
                .put("etag", etag ?: "")
            val map = resumeMap().put(url, entry)
            prefs().edit().putString(KEY_RESUME, map.toString()).apply()
        }

        fun forgetResume() {
            val map = resumeMap()
            val mine = uri?.toString() ?: file!!.absolutePath
            for (key in map.keys().asSequence().toList()) {
                if (map.optJSONObject(key)?.optString("target") == mine) map.remove(key)
            }
            prefs().edit().putString(KEY_RESUME, map.toString()).apply()
        }
    }

    /**
     * Reopens the partial file from a previous attempt at the same URL, or makes
     * a new one. Reopening is the whole point: a give-up leaves bytes on disk,
     * and tapping download again should continue rather than start over.
     */
    private fun openTarget(job: Job): Target? {
        resumeMap().optJSONObject(job.url)?.let { saved ->
            val where = saved.optString("target")
            try {
                val target = if (saved.optBoolean("isFile")) {
                    val file = File(where)
                    if (file.exists()) Target(null, file) else null
                } else {
                    val uri = Uri.parse(where)
                    // Throws if the row is gone (deleted from Downloads by hand).
                    contentResolver.openFileDescriptor(uri, "r")?.close()
                    Target(uri, null)
                }
                if (target != null) {
                    val savedEtag = saved.optString("etag")
                    if (savedEtag.isNotEmpty()) target.etag = savedEtag
                    return target
                }
            } catch (e: Exception) {
                // Fall through and make a fresh one.
            }
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val values = ContentValues()
            values.put(MediaStore.Downloads.DISPLAY_NAME, job.name)
            values.put(MediaStore.Downloads.MIME_TYPE, job.mime)
            values.put(MediaStore.Downloads.RELATIVE_PATH,
                Environment.DIRECTORY_DOWNLOADS + "/FileBridge")
            values.put(MediaStore.Downloads.IS_PENDING, 1)
            val uri = try {
                contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            } catch (e: Exception) {
                null
            } ?: return null
            return Target(uri, null)
        }

        val dir = File(
            Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
            "FileBridge")
        if (!dir.exists() && !dir.mkdirs()) return null
        // A finished file of the same name must not be appended to.
        var candidate = File(dir, job.name)
        val stem = job.name.substringBeforeLast('.', job.name)
        val ext = job.name.substringAfterLast('.', "")
        var counter = 2
        while (candidate.exists() && counter < 100) {
            val suffix = if (ext.isEmpty()) "" else "." + ext
            candidate = File(dir, stem + "-" + counter + suffix)
            counter++
        }
        return Target(null, candidate)
    }

    // ------------------------------------------------------------ bookkeeping

    private fun prefs() = getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun resumeMap(): JSONObject =
        try { JSONObject(prefs().getString(KEY_RESUME, "{}") ?: "{}") }
        catch (e: Exception) { JSONObject() }

    private fun record(label: String, ok: Boolean, detail: String) {
        val raw = prefs().getString(KEY_RESULTS, "[]") ?: "[]"
        val array = try { JSONArray(raw) } catch (e: Exception) { JSONArray() }
        array.put(JSONObject().put("label", label).put("ok", ok).put("detail", detail))
        prefs().edit().putString(KEY_RESULTS, array.toString()).apply()
        sendBroadcast(Intent(ACTION_SETTLED).setPackage(packageName))
    }

    // ------------------------------------------------------------ notifications

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(CHANNEL, getString(R.string.channel_transfers),
            NotificationManager.IMPORTANCE_LOW)
        channel.description = getString(R.string.channel_transfers_desc)
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
            .createNotificationChannel(channel)
    }

    private fun pendingFlags(): Int =
        PendingIntent.FLAG_UPDATE_CURRENT or
            (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
                PendingIntent.FLAG_IMMUTABLE else 0)

    private fun openAppIntent(): PendingIntent =
        PendingIntent.getActivity(this, 0,
            Intent(this, MainActivity::class.java), pendingFlags())

    private fun ongoing(label: String, have: Long, total: Long, rate: Double): Notification {
        val cancel = PendingIntent.getService(this, 1,
            Intent(this, DownloadService::class.java).setAction(ACTION_CANCEL), pendingFlags())

        val line = if (total > 0)
            human(have) + " of " + human(total) +
                (if (rate > 0) " · " + human(rate.toLong()) + "/s" else "")
        else human(have)

        val builder = NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle(label)
            .setContentText(line)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(openAppIntent())
            .addAction(0, getString(R.string.cancel), cancel)
        if (total > 0) {
            builder.setProgress(1000, ((have * 1000) / total).toInt(), false)
        } else {
            builder.setProgress(0, 0, true)
        }
        return builder.build()
    }

    private fun notifyOngoing(label: String, have: Long, total: Long, rate: Double) {
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
            .notify(ONGOING_ID, ongoing(label, have, total, rate))
    }

    /** A separate, dismissible notification so the result survives the service. */
    private fun finished(label: String, failure: String?) {
        val title = if (failure == null) getString(R.string.saved) + " " + label
                    else getString(R.string.download_failed)
        val body = if (failure == null) getString(R.string.saved_where)
                   else label + "\n\n" + failure
        val builder = NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle(title)
            .setContentText(if (failure == null) getString(R.string.saved_where) else failure)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setSmallIcon(if (failure == null) android.R.drawable.stat_sys_download_done
                          else android.R.drawable.stat_notify_error)
            .setAutoCancel(true)
            .setContentIntent(openAppIntent())
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
            .notify(label.hashCode() and 0xffff, builder.build())
    }

    // ------------------------------------------------------------ locks

    /**
     * The wifi lock is the reason this class exists. Without it the radio powers
     * down seconds after the screen does, and a transfer dies mid-file.
     */
    private fun acquireLocks() {
        try {
            val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
                WifiManager.WIFI_MODE_FULL_LOW_LATENCY else WifiManager.WIFI_MODE_FULL_HIGH_PERF
            wifiLock = wifi.createWifiLock(mode, "filebridge:transfer").also {
                it.setReferenceCounted(false)
                it.acquire()
            }
        } catch (e: Exception) {
            wifiLock = null
        }
        try {
            val power = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "filebridge:download")
                .also {
                    it.setReferenceCounted(false)
                    it.acquire(3 * 60 * 60 * 1000L)  // ceiling; released in finally
                }
        } catch (e: Exception) {
            wakeLock = null
        }
    }

    private fun releaseLocks() {
        try { wifiLock?.let { if (it.isHeld) it.release() } } catch (e: Exception) { }
        try { wakeLock?.let { if (it.isHeld) it.release() } } catch (e: Exception) { }
        wifiLock = null
        wakeLock = null
    }
}
