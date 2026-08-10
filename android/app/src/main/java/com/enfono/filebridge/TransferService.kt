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
import androidx.core.content.FileProvider
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
 * Moves files between the Mac and this phone, in the foreground, holding the
 * radio awake. Both directions live here.
 *
 * Android's own DownloadManager was used for downloads until 1.9.0 and could not
 * do the job. It runs transfers as JobScheduler work and holds no wifi lock, so
 * when the screen went off the radio slept, the socket died, and the whole
 * download was dropped as "network lost" rather than retried. Measured on the
 * Mac: three attempts at a 707 MB file, killed at 15.8 s, 15.9 s and 16.2 s —
 * three different byte counts, the same clock, i.e. the phone's 15 s display
 * timeout.
 *
 * Uploads had the same hole for longer: they ran on a pool inside the Activity
 * with no lock and no notification, so a big send died silently the moment the
 * screen slept, and nothing on screen ever said so. 1.11.0 moved them here.
 *
 * Downloads resume: `Range: bytes=N-` plus `If-Match` after every drop, and a
 * partial file survives a give-up so a later attempt continues. **Uploads do
 * not** — the server takes one multipart POST and has no offset endpoint, so an
 * interrupted send has to start over. That is the next thing worth building.
 */
class TransferService : Service() {

    companion object {
        const val EXTRA_KIND = "kind"
        const val EXTRA_URL = "url"
        const val EXTRA_NAME = "name"
        const val EXTRA_LABEL = "label"
        const val EXTRA_MIME = "mime"
        const val EXTRA_PATH = "path"      // the file's path on the Mac
        const val EXTRA_SOURCE = "source"  // content:// uri being uploaded
        const val KIND_DOWNLOAD = "download"
        const val KIND_UPLOAD = "upload"

        const val ACTION_CANCEL = "com.enfono.filebridge.CANCEL"
        /** Sent when a transfer settles, so a live Activity can react at once. */
        const val ACTION_SETTLED = "com.enfono.filebridge.SETTLED"

        private const val CHANNEL = "transfers"
        private const val ONGOING_ID = 1
        private const val PREFS = "fb"
        private const val KEY_RESULTS = "download_results"
        private const val KEY_RESUME = "download_resume"
        private const val KEY_SAVED = "download_saved"

        /** Attempts with no progress at all before giving up. Progress resets it. */
        private const val STALLED_LIMIT = 8

        fun startDownload(context: Context, url: String, name: String, label: String,
                          mime: String, path: String) {
            launch(context, Intent(context, TransferService::class.java)
                .putExtra(EXTRA_KIND, KIND_DOWNLOAD)
                .putExtra(EXTRA_URL, url)
                .putExtra(EXTRA_NAME, name)
                .putExtra(EXTRA_LABEL, label)
                .putExtra(EXTRA_MIME, mime)
                .putExtra(EXTRA_PATH, path))
        }

        fun startUpload(context: Context, url: String, source: Uri, name: String) {
            launch(context, Intent(context, TransferService::class.java)
                .putExtra(EXTRA_KIND, KIND_UPLOAD)
                .putExtra(EXTRA_URL, url)
                .putExtra(EXTRA_NAME, name)
                .putExtra(EXTRA_LABEL, name)
                .putExtra(EXTRA_SOURCE, source.toString()))
        }

        private fun launch(context: Context, intent: Intent) {
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
                        item.optString("detail"), item.optString("uri"),
                        item.optString("mime")))
                }
            } catch (e: Exception) {
                // A corrupt list is not worth crashing over; it has been cleared.
            }
            return out
        }

        /** Where a finished download of this Mac path landed, if we still know. */
        fun savedUri(context: Context, path: String): Pair<Uri, String>? {
            val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val map = try {
                JSONObject(prefs.getString(KEY_SAVED, "{}") ?: "{}")
            } catch (e: Exception) {
                return null
            }
            val entry = map.optJSONObject(path) ?: return null
            val uri = entry.optString("uri")
            if (uri.isEmpty()) return null
            return Pair(Uri.parse(uri), entry.optString("mime", "*/*"))
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

    data class Result(
        val label: String,
        val ok: Boolean,
        val detail: String,
        val uri: String = "",
        val mime: String = "")

    private class Job(
        val kind: String,
        val url: String,
        val name: String,
        val label: String,
        val mime: String,
        val path: String,
        val source: Uri?)

    private val queue = LinkedBlockingQueue<Job>()
    private val cancelled = AtomicBoolean(false)
    /** Guards the busy flag against the queue, so no job is ever left unclaimed. */
    private val gate = Object()
    private var busy = false
    @Volatile private var running = true
    @Volatile private var current: String = ""

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
            synchronized(gate) { queue.clear() }
            return START_NOT_STICKY
        }

        val url = intent?.getStringExtra(EXTRA_URL)
        val name = intent?.getStringExtra(EXTRA_NAME)
        if (url.isNullOrEmpty() || name.isNullOrEmpty()) {
            synchronized(gate) { if (queue.isEmpty() && !busy) stopSelf() }
            return START_NOT_STICKY
        }

        val job = Job(
            kind = intent.getStringExtra(EXTRA_KIND) ?: KIND_DOWNLOAD,
            url = url,
            name = name,
            label = intent.getStringExtra(EXTRA_LABEL) ?: name,
            mime = intent.getStringExtra(EXTRA_MIME) ?: "application/octet-stream",
            path = intent.getStringExtra(EXTRA_PATH) ?: "",
            source = intent.getStringExtra(EXTRA_SOURCE)?.let { Uri.parse(it) })

        // Foreground before anything slow: Android gives a service seconds, not
        // minutes, to put its notification up, and kills it otherwise. Keep the
        // label of whatever is actually transferring — a second tap must not
        // relabel the notification of the file already in flight.
        val shown = if (current.isEmpty()) job.label else current
        startForeground(ONGOING_ID, ongoing(shown, job.kind, 0, 0, 0.0))

        synchronized(gate) {
            queue.add(job)
            if (!busy) {
                busy = true
                cancelled.set(false)
                spawnWorker()
            }
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        running = false
        cancelled.set(true)
        releaseLocks()
        super.onDestroy()
    }

    // ------------------------------------------------------------ the worker

    /**
     * One job at a time, in order. Serial on purpose: two large transfers over
     * one wifi link finish later than the same two run back to back, and the
     * progress notification can only honestly describe one of them.
     *
     * The handshake with [busy] matters. Polling an empty queue and *then*
     * exiting would let a job arriving in that window sit unclaimed forever,
     * with the tap looking accepted — so the queue is only declared empty while
     * holding the lock that a new job must also take.
     */
    private fun spawnWorker() {
        Thread {
            acquireLocks()
            try {
                while (running) {
                    val job = synchronized(gate) {
                        val next = queue.poll()
                        if (next == null) busy = false
                        next
                    } ?: break

                    current = job.label
                    val outcome = try {
                        if (job.kind == KIND_UPLOAD) runUpload(job) else runDownload(job)
                    } catch (e: InterruptedException) {
                        "Cancelled."
                    } catch (e: Exception) {
                        "Unexpected error: " + (e.message ?: e.javaClass.simpleName)
                    }
                    current = ""

                    val saved = if (outcome == null && job.kind == KIND_DOWNLOAD)
                        savedUri(this, job.path) else null
                    record(job.label, outcome == null,
                        outcome ?: successLine(job),
                        saved?.first?.toString() ?: "", saved?.second ?: "")
                    finished(job.label, outcome, saved)
                }
            } finally {
                releaseLocks()
                stopForeground(true)
                stopSelf()
            }
        }.start()
    }

    private fun successLine(job: Job) =
        if (job.kind == KIND_UPLOAD) getString(R.string.sent_to_mac)
        else getString(R.string.saved_where)

    // ------------------------------------------------------------ download

    /** Returns null on success, or a sentence explaining the failure. */
    private fun runDownload(job: Job): String? {
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
                                notifyOngoing(job.label, job.kind, have, total, rate)
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
                    target.finish(job)
                    return null
                }
                // Ended early without an exception: the connection closed
                // mid-body. Loop round and resume from where we got to.
            } catch (e: InterruptedException) {
                return "Cancelled."
            } catch (e: IOException) {
                have = target.size()
                if (total > 0 && have >= total) {
                    target.finish(job)
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

            notifyOngoing(job.label, job.kind, have, total, 0.0)
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

    // ------------------------------------------------------------ upload

    /**
     * One multipart POST, streamed from the content provider so a large video
     * never lands in this process's memory. No resume: the server has a single
     * upload endpoint with no offset, so an interruption means starting over.
     * The wifi lock is what stops that being the normal case.
     */
    private fun runUpload(job: Job): String? {
        val source = job.source ?: return "Nothing to send."
        val total = sizeOf(source)
        val boundary = "----fb" + System.currentTimeMillis()
        val head = ("--" + boundary + "\r\n" +
            "Content-Disposition: form-data; name=\"f\"; filename=\"" + job.name + "\"\r\n" +
            "Content-Type: application/octet-stream\r\n\r\n").toByteArray()
        val tail = ("\r\n--" + boundary + "--\r\n").toByteArray()

        var sent = 0L
        val connection = URL(job.url).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = "POST"
            connection.doOutput = true
            // Declaring the length keeps the body framed in a way the server can
            // read; chunked was rejected as empty before server 1.6.0.
            if (total > 0) {
                connection.setFixedLengthStreamingMode(head.size + total + tail.size)
            } else {
                connection.setChunkedStreamingMode(256 * 1024)
            }
            connection.setRequestProperty("Content-Type",
                "multipart/form-data; boundary=" + boundary)
            connection.connectTimeout = 15000
            connection.readTimeout = 120000

            var lastTick = System.currentTimeMillis()
            var tickBytes = 0L
            connection.outputStream.use { out ->
                out.write(head)
                val input = contentResolver.openInputStream(source)
                    ?: return "The phone would not open that file."
                input.use {
                    val buffer = ByteArray(256 * 1024)
                    while (true) {
                        if (cancelled.get()) throw InterruptedException()
                        val read = it.read(buffer)
                        if (read <= 0) break
                        out.write(buffer, 0, read)
                        sent += read

                        val now = System.currentTimeMillis()
                        if (now - lastTick >= 1000) {
                            val rate = (sent - tickBytes) * 1000.0 / (now - lastTick)
                            notifyOngoing(job.label, job.kind, sent, total, rate)
                            lastTick = now
                            tickBytes = sent
                        }
                    }
                }
                out.write(tail)
                out.flush()
            }

            val code = connection.responseCode
            if (code !in 200..299) {
                val detail = try {
                    connection.errorStream?.bufferedReader()?.readText() ?: ""
                } catch (e: Exception) {
                    ""
                }
                return "The Mac refused it (HTTP " + code + ")" +
                    (if (detail.isNotBlank()) ": " + detail.take(120) else "") + "."
            }
            return null
        } catch (e: InterruptedException) {
            return "Cancelled."
        } catch (e: IOException) {
            return "The connection dropped after " + human(sent) +
                " of " + human(total) + ". Sends cannot resume yet, so this one " +
                "has to start over."
        } finally {
            try { connection.disconnect() } catch (e: Exception) { }
        }
    }

    private fun sizeOf(uri: Uri): Long {
        return try {
            contentResolver.openFileDescriptor(uri, "r")?.use { it.statSize } ?: -1L
        } catch (e: Exception) {
            -1L
        }
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

        fun finish(job: Job) {
            if (uri != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val values = ContentValues()
                values.put(MediaStore.Downloads.IS_PENDING, 0)
                contentResolver.update(uri, values, null, null)
            }
            forgetResume()
            rememberSaved(job)
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

        /** So tapping a finished file in the app can open it instead of re-fetching. */
        fun rememberSaved(job: Job) {
            if (job.path.isEmpty()) return
            val openable = uri ?: fileUri(file!!) ?: return
            val map = savedMap().put(job.path, JSONObject()
                .put("uri", openable.toString())
                .put("mime", job.mime))
            prefs().edit().putString(KEY_SAVED, map.toString()).apply()
        }
    }

    /** A uri another app may actually open, for the pre-MediaStore path. */
    private fun fileUri(file: File): Uri? = try {
        FileProvider.getUriForFile(this, packageName + ".files", file)
    } catch (e: Exception) {
        null
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

    private fun savedMap(): JSONObject =
        try { JSONObject(prefs().getString(KEY_SAVED, "{}") ?: "{}") }
        catch (e: Exception) { JSONObject() }

    private fun record(label: String, ok: Boolean, detail: String,
                       uri: String, mime: String) {
        val raw = prefs().getString(KEY_RESULTS, "[]") ?: "[]"
        val array = try { JSONArray(raw) } catch (e: Exception) { JSONArray() }
        array.put(JSONObject()
            .put("label", label).put("ok", ok).put("detail", detail)
            .put("uri", uri).put("mime", mime))
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

    /** Tapping a finished download plays it, rather than reopening this app. */
    private fun openFileIntent(saved: Pair<Uri, String>): PendingIntent {
        val view = Intent(Intent.ACTION_VIEW)
            .setDataAndType(saved.first, saved.second)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return PendingIntent.getActivity(this, saved.first.hashCode(), view, pendingFlags())
    }

    private fun ongoing(label: String, kind: String, have: Long, total: Long,
                        rate: Double): Notification {
        val cancel = PendingIntent.getService(this, 1,
            Intent(this, TransferService::class.java).setAction(ACTION_CANCEL), pendingFlags())

        val moved = if (total > 0)
            human(have) + " of " + human(total) +
                (if (rate > 0) " · " + human(rate.toLong()) + "/s" else "")
        else human(have)
        val waiting = queue.size
        val line = if (waiting > 0)
            moved + " · " + waiting + " " + getString(R.string.waiting) else moved

        val builder = NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle(
                (if (kind == KIND_UPLOAD) getString(R.string.sending) + " " else "") + label)
            .setContentText(line)
            .setSmallIcon(if (kind == KIND_UPLOAD) android.R.drawable.stat_sys_upload
                          else android.R.drawable.stat_sys_download)
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

    private fun notifyOngoing(label: String, kind: String, have: Long, total: Long,
                              rate: Double) {
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
            .notify(ONGOING_ID, ongoing(label, kind, have, total, rate))
    }

    /** A separate, dismissible notification so the result survives the service. */
    private fun finished(label: String, failure: String?, saved: Pair<Uri, String>?) {
        val title = if (failure == null) getString(R.string.saved) + " " + label
                    else getString(R.string.transfer_failed)
        val detail = failure ?: (
            if (saved != null) getString(R.string.tap_to_open)
            else getString(R.string.saved_where))
        val builder = NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle(title)
            .setContentText(detail)
            .setStyle(NotificationCompat.BigTextStyle()
                .bigText(if (failure == null) detail else label + "\n\n" + failure))
            .setSmallIcon(if (failure == null) android.R.drawable.stat_sys_download_done
                          else android.R.drawable.stat_notify_error)
            .setAutoCancel(true)
            .setContentIntent(if (saved != null) openFileIntent(saved) else openAppIntent())
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
            wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "filebridge:transfer")
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
