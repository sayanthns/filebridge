package com.enfono.filebridge

import android.app.DownloadManager
import android.content.Intent
import android.content.Context
import android.database.Cursor
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.OpenableColumns
import android.view.View
import android.view.ViewGroup
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import org.json.JSONObject
import java.io.DataOutputStream
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.concurrent.Executors

/**
 * FileBridge — the phone half.
 *
 * Connects to the FileBridge server running on the Mac, lists what is in the
 * shared folder, downloads with the system DownloadManager and uploads back.
 *
 * Downloads deliberately go through DownloadManager rather than being read in
 * this process: it survives the app being backgrounded, shows a progress
 * notification, and resumes over Range if the wifi drops — which matters when
 * the files are hundreds of megabytes.
 */
class MainActivity : AppCompatActivity() {

    private data class Row(
        val name: String,
        val pretty: String,
        val path: String,
        val meta: String,
        val isDir: Boolean,
        val done: Boolean,
    )

    private val io = Executors.newFixedThreadPool(3)
    private val ui = Handler(Looper.getMainLooper())

    private var base = ""      // http://ip:port
    private var token = ""
    private var cwd = ""
    private var parent: String? = null
    private val rows = mutableListOf<Row>()

    private lateinit var prefs: android.content.SharedPreferences
    private lateinit var connectBar: View
    private lateinit var browser: View
    private lateinit var emptyState: View
    private lateinit var spinner: View
    private lateinit var connectError: TextView
    private lateinit var urlInput: EditText
    private lateinit var statusText: TextView
    private lateinit var pathText: TextView
    private lateinit var listView: ListView

    private val picker = registerForActivityResult(
        ActivityResultContracts.OpenMultipleDocuments()
    ) { uris -> if (!uris.isNullOrEmpty()) upload(uris) }

    // The scanner returns the same filebridge:// payload the deep link carries,
    // so both routes end up in one place: connectFromPayload().
    private val scanner = registerForActivityResult(ScanContract()) { result ->
        val text = result?.contents
        if (text.isNullOrBlank()) {
            toast("Nothing scanned")
        } else if (!connectFromPayload(text)) {
            toast("That QR is not a FileBridge code")
        }
    }

    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        setContentView(R.layout.activity_main)

        connectBar = findViewById(R.id.connectBar)
        browser = findViewById(R.id.browser)
        emptyState = findViewById(R.id.emptyState)
        spinner = findViewById(R.id.spinner)
        connectError = findViewById(R.id.connectError)
        urlInput = findViewById(R.id.urlInput)
        statusText = findViewById(R.id.statusText)
        pathText = findViewById(R.id.pathText)
        listView = findViewById(R.id.fileList)
        listView.adapter = Adapter()

        // Remember the link so this is a one-time setup, not every launch.
        prefs = getSharedPreferences("fb", Context.MODE_PRIVATE)
        urlInput.setText(prefs.getString("url", ""))

        findViewById<Button>(R.id.connectBtn).setOnClickListener {
            val raw = urlInput.text.toString().trim()
            if (!parse(raw)) {
                showConnectError("That does not look like a File Bridge link. " +
                    "It should look like http://10.0.0.5:8001/?t=key")
                return@setOnClickListener
            }
            connectError.visibility = View.GONE
            prefs.edit().putString("url", raw).apply()
            load("")
        }

        findViewById<Button>(R.id.scanBtn).setOnClickListener {
            val options = ScanOptions()
                .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                .setPrompt("Point at the QR on your Mac")
                .setBeepEnabled(false)
                .setOrientationLocked(true)
            scanner.launch(options)
        }

        findViewById<Button>(R.id.refreshBtn).setOnClickListener { load(cwd) }

        findViewById<Button>(R.id.disconnectBtn).setOnClickListener { disconnect() }
        findViewById<Button>(R.id.uploadBtn).setOnClickListener {
            picker.launch(arrayOf("*/*"))
        }

        listView.setOnItemClickListener { _, _, position, _ ->
            val row = rows[position]
            when {
                row.isDir -> load(row.path)
                else -> download(row)
            }
        }

        installBackBehaviour()

        // A QR scan arrives as a filebridge:// intent and wins over the saved
        // link, since scanning is the user asking to connect to that machine.
        if (!handleLink(intent) &&
            urlInput.text.isNotBlank() && parse(urlInput.text.toString().trim())) {
            load("")   // straight in on relaunch
        }
    }

    /** Back: up one folder, then to the connect screen, then out of the app. */
    private fun installBackBehaviour() {
        onBackPressedDispatcher.addCallback(this,
            object : androidx.activity.OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    when {
                        browser.visibility != View.VISIBLE -> {
                            isEnabled = false
                            onBackPressedDispatcher.onBackPressed()
                        }
                        cwd.isNotBlank() -> load(parent ?: "")
                        else -> {
                            // Leave the browser but keep the connection, so
                            // coming back does not need another scan.
                            browser.visibility = View.GONE
                            connectBar.visibility = View.VISIBLE
                        }
                    }
                }
            })
    }

    override fun onNewIntent(incoming: Intent?) {
        super.onNewIntent(incoming)
        handleLink(incoming)
    }

    /** A filebridge:// intent, i.e. the QR opened by the phone's camera app. */
    private fun handleLink(incoming: Intent?): Boolean {
        val data = incoming?.data ?: return false
        return connectFromPayload(data.toString())
    }

    /** filebridge://c?u=<url-encoded base>&t=<key>, from a scan or a deep link. */
    private fun connectFromPayload(payload: String): Boolean {
        val text = payload.trim()
        if (!text.startsWith("filebridge://", ignoreCase = true)) return false

        val data = try { Uri.parse(text) } catch (e: Exception) { return false }
        val u = data.getQueryParameter("u")?.trim()?.trimEnd('/') ?: return false
        val key = data.getQueryParameter("t")?.trim() ?: return false
        if (u.isEmpty() || key.isEmpty()) return false

        base = u
        token = key
        val full = u + "/?t=" + key
        urlInput.setText(full)
        prefs.edit().putString("url", full).apply()
        toast("Connecting to " + u)
        load("")
        return true
    }

    /** Split "http://host:port/?t=key" into base + token. */
    private fun parse(raw: String): Boolean {
        return try {
            val url = URL(if (raw.startsWith("http")) raw else "http://$raw")
            val port = if (url.port > 0) ":" + url.port else ""
            base = url.protocol + "://" + url.host + port
            token = (url.query ?: "").split("&")
                .firstOrNull { it.startsWith("t=") }?.removePrefix("t=") ?: ""
            base.isNotEmpty() && token.isNotEmpty()
        } catch (e: Exception) {
            false
        }
    }

    private fun api(path: String, extra: String = "") =
        base + path + "?t=" + enc(token) + extra

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")

    // ------------------------------------------------------------ listing

    /** Leave the Mac: tell it, forget the link, and go back to the start. */
    private fun disconnect() {
        val target = if (base.isNotEmpty()) api("/api/bye") else ""
        if (target.isNotEmpty()) {
            io.execute { try { post(target, "") } catch (e: Exception) { } }
        }
        prefs.edit().remove("url").apply()
        base = ""; token = ""; cwd = ""; parent = null
        rows.clear()
        (listView.adapter as Adapter).notifyDataSetChanged()
        browser.visibility = View.GONE
        connectBar.visibility = View.VISIBLE
        connectError.visibility = View.GONE
        urlInput.setText("")
        toast("Disconnected")
    }

    private fun showConnectError(message: String) {
        connectError.text = message
        connectError.visibility = View.VISIBLE
    }

    private fun load(path: String) {
        status("Loading…")
        if (rows.isEmpty()) spinner.visibility = View.VISIBLE
        io.execute {
            try {
                val text = get(api("/api/list", "&path=" + enc(path)))
                val json = JSONObject(text)
                if (json.has("error")) {
                    ui.post { status(json.getString("error")) }
                    return@execute
                }

                val parsed = mutableListOf<Row>()
                val dirs = json.getJSONArray("dirs")
                for (i in 0 until dirs.length()) {
                    val d = dirs.getJSONObject(i)
                    parsed.add(Row(d.getString("name"), d.getString("name"),
                        d.getString("path"), "folder", true, false))
                }
                val files = json.getJSONArray("files")
                for (i in 0 until files.length()) {
                    val f = files.getJSONObject(i)
                    val bits = listOf(f.optString("size_h"), f.optString("duration_h"))
                        .filter { it.isNotBlank() }
                    parsed.add(Row(f.getString("name"), f.optString("pretty"),
                        f.getString("path"), bits.joinToString(" · "),
                        false, f.optBoolean("done")))
                }

                val cwdNow = json.optString("cwd")
                val parentNow = if (json.isNull("parent")) null else json.optString("parent")
                val fileCount = json.optInt("count")
                val summary = if (fileCount == 0)
                    "Pick a folder"
                else
                    json.optInt("done_count").toString() + " / " + fileCount +
                        " taken · " + json.optString("total_h")

                ui.post {
                    spinner.visibility = View.GONE
                    cwd = cwdNow
                    parent = parentNow
                    rows.clear()
                    if (parent != null) {
                        rows.add(Row("..", "Up a folder", if (parent == ".") "" else parent!!,
                            "", true, false))
                    }
                    rows.addAll(parsed)
                    (listView.adapter as Adapter).notifyDataSetChanged()
                    connectBar.visibility = View.GONE
                    browser.visibility = View.VISIBLE
                    emptyState.visibility =
                        if (rows.isEmpty()) View.VISIBLE else View.GONE
                    // Root holds both folders, so calling it "To Phone" was wrong.
                    pathText.text = if (cwd.isBlank()) "File Bridge  /" else "/$cwd"
                    status(summary)
                }
            } catch (e: Exception) {
                val paused = e is PausedException
                ui.post {
                    spinner.visibility = View.GONE
                    if (paused) {
                        // Stay on the list: sharing will come back with one tap
                        // on the Mac, so throwing the user out would be rude.
                        status("Paused on the Mac")
                        toast("Sharing is paused on the Mac. Press Start sharing there.")
                    } else {
                        status("Not connected")
                        browser.visibility = View.GONE
                        connectBar.visibility = View.VISIBLE
                        showConnectError(
                            "Could not reach the Mac. Check both are on the same wifi " +
                            "and that File Bridge is open with Start sharing pressed.")
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------ download

    private fun download(row: Row) {
        val url = api("/file", "&path=" + enc(row.path))
        try {
            val request = DownloadManager.Request(Uri.parse(url))
                .setTitle(row.pretty)
                .setDescription("FileBridge")
                .setNotificationVisibility(
                    DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalPublicDir(
                    Environment.DIRECTORY_DOWNLOADS, "FileBridge/" + row.name)
                .setAllowedOverRoaming(false)
            request.setMimeType(guessMime(row.name))

            val manager = getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
            manager.enqueue(request)
            toast("Downloading " + row.pretty)

            // Tell the Mac it has been taken so the tick shows on both ends.
            io.execute {
                try {
                    post(api("/api/mark"),
                        JSONObject().put("path", row.path).put("done", true).toString())
                } catch (ignored: Exception) {
                }
            }
        } catch (e: Exception) {
            toast("Could not start: " + e.message)
        }
    }

    private fun guessMime(name: String): String {
        val n = name.lowercase()
        return when {
            n.endsWith(".mp4") || n.endsWith(".m4v") -> "video/mp4"
            n.endsWith(".mkv") -> "video/x-matroska"
            n.endsWith(".mp3") -> "audio/mpeg"
            n.endsWith(".jpg") || n.endsWith(".jpeg") -> "image/jpeg"
            n.endsWith(".png") -> "image/png"
            n.endsWith(".pdf") -> "application/pdf"
            else -> "application/octet-stream"
        }
    }

    // ------------------------------------------------------------ upload

    private fun upload(uris: List<Uri>) {
        status("sending " + uris.size + " file(s)...")
        io.execute {
            var ok = 0
            for (uri in uris) {
                try {
                    sendOne(uri)
                    ok++
                    val done = ok
                    ui.post { status("sent " + done + " / " + uris.size) }
                } catch (e: Exception) {
                    ui.post { toast("Failed: " + e.message) }
                }
            }
            ui.post {
                toast("Sent " + ok + " of " + uris.size)
                load(cwd)
            }
        }
    }

    private fun sendOne(uri: Uri) {
        val name = displayName(uri)
        val size = sizeOf(uri)
        val boundary = "----fb" + System.currentTimeMillis()

        val head = ("--" + boundary + "\r\n" +
            "Content-Disposition: form-data; name=\"f\"; filename=\"" + name + "\"\r\n" +
            "Content-Type: application/octet-stream\r\n\r\n").toByteArray()
        val tail = ("\r\n--" + boundary + "--\r\n").toByteArray()

        val conn = URL(api("/api/upload")).openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.doOutput = true
        // Fixed length, not chunked: declaring the size keeps the body framed
        // in a way the server can read, and still streams from disk so a large
        // video never lands in this process's memory.
        if (size > 0) {
            conn.setFixedLengthStreamingMode(head.size + size + tail.size)
        } else {
            conn.setChunkedStreamingMode(256 * 1024)
        }
        conn.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
        conn.connectTimeout = 15000
        conn.readTimeout = 120000

        DataOutputStream(conn.outputStream).use { out ->
            out.write(head)
            contentResolver.openInputStream(uri)?.use { input -> copy(input, out) }
            out.write(tail)
            out.flush()
        }

        val code = conn.responseCode
        val detail = if (code !in 200..299) {
            try { conn.errorStream?.bufferedReader()?.readText() ?: "" } catch (e: Exception) { "" }
        } else ""
        conn.disconnect()
        if (code !in 200..299) {
            throw RuntimeException("server said " + code +
                (if (detail.isNotBlank()) ": " + detail.take(120) else ""))
        }
    }

    private fun copy(input: InputStream, out: DataOutputStream) {
        val buffer = ByteArray(256 * 1024)
        while (true) {
            val read = input.read(buffer)
            if (read <= 0) break
            out.write(buffer, 0, read)
        }
    }

    /** Byte size from the content provider, or -1 when it will not say. */
    private fun sizeOf(uri: Uri): Long {
        var size = -1L
        val cursor: Cursor? = contentResolver.query(uri, null, null, null, null)
        cursor?.use {
            val index = it.getColumnIndex(OpenableColumns.SIZE)
            if (index >= 0 && it.moveToFirst() && !it.isNull(index)) size = it.getLong(index)
        }
        return size
    }

    private fun displayName(uri: Uri): String {
        var name = "upload.bin"
        val cursor: Cursor? = contentResolver.query(uri, null, null, null, null)
        cursor?.use {
            val index = it.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (index >= 0 && it.moveToFirst()) name = it.getString(index) ?: name
        }
        return name
    }

    // ------------------------------------------------------------ http

    private fun get(url: String): String {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 8000
        conn.readTimeout = 120000
        try {
            if (conn.responseCode == 503) {
                // The Mac pressed Stop Sharing; that is not a network fault.
                throw PausedException()
            }
            return conn.inputStream.bufferedReader().readText()
        } finally {
            conn.disconnect()
        }
    }

    private class PausedException : RuntimeException("paused")

    private fun post(url: String, body: String) {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json")
        conn.connectTimeout = 8000
        conn.outputStream.use { it.write(body.toByteArray()) }
        conn.responseCode
        conn.disconnect()
    }

    // ------------------------------------------------------------ ui bits

    private fun status(text: String) { statusText.text = text }

    private fun toast(text: String) =
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show()

    private inner class Adapter : BaseAdapter() {
        override fun getCount() = rows.size
        override fun getItem(position: Int) = rows[position]
        override fun getItemId(position: Int) = position.toLong()

        override fun getView(position: Int, convert: View?, parentView: ViewGroup?): View {
            val view = convert ?: layoutInflater.inflate(R.layout.row_file, parentView, false)
            val row = rows[position]
            view.findViewById<TextView>(R.id.icon).text = when {
                row.name == ".." -> "\u2191"
                row.isDir -> "\u25B8"
                row.done -> "\u2713"
                else -> "\u2b07"
            }
            view.findViewById<TextView>(R.id.name).text = row.pretty
            val meta = view.findViewById<TextView>(R.id.meta)
            meta.text = if (row.done) row.meta + "  ·  on this phone" else row.meta
            view.findViewById<TextView>(R.id.chevron).text =
                if (row.isDir) "\u203A" else ""
            // Dim, never hide: a taken file still has to be findable.
            view.alpha = if (row.done && !row.isDir) 0.55f else 1f
            return view
        }
    }
}
