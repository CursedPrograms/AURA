package com.synthwomb.aura

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.graphics.Color
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiManager
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

private const val NAME = "AURA"
private const val PORT = 5017
private val BG = Color.parseColor("#070B14")
private val PANEL = Color.parseColor("#0E1626")
private val EDGE = Color.parseColor("#1E2C45")
private val TEXT = Color.parseColor("#EAF1F8")
private val ACCENT = Color.parseColor("#5EC8F2")

/**
 * AURA on the phone: her page from the PC she runs on (aura.py), full screen. The weather outside,
 * every room's temperature and humidity, and the graph of inside against outside. FIND asks RIFT's
 * fleet list, like the robots' apps; or type her PC's address. It reconnects there next time.
 */
class MainActivity : Activity() {
    private lateinit var web: WebView
    private lateinit var address: EditText
    private lateinit var state: TextView
    private val prefs by lazy { getPreferences(Context.MODE_PRIVATE) }
    private var wifi: ConnectivityManager.NetworkCallback? = null

    private fun button(text: String, action: () -> Unit) = Button(this).apply {
        this.text = text
        setTextColor(TEXT)
        setBackgroundColor(PANEL)
        setOnClickListener { action() }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        web = WebView(this).apply {
            setBackgroundColor(BG)
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            webViewClient = object : WebViewClient() {
                override fun onReceivedError(view: WebView, request: WebResourceRequest, error: android.webkit.WebResourceError) {
                    if (request.isForMainFrame) backToPanel("Can't reach AURA. Is she running?")
                }
            }
            visibility = View.GONE
        }
        state = TextView(this).apply { setTextColor(TEXT); gravity = Gravity.CENTER; setPadding(0, 24, 0, 24) }
        address = EditText(this).apply {
            hint = "her PC's address"
            setText(prefs.getString("address", "") ?: "")
            setTextColor(TEXT)
            setHintTextColor(EDGE)
            setSingleLine()
        }
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(BG)
            setPadding(48, 48, 48, 48)
            addView(TextView(context).apply { text = NAME; textSize = 32f; setTextColor(ACCENT); gravity = Gravity.CENTER })
            addView(TextView(context).apply {
                text = "The fleet's weather station: outside, every room, and the graph."
                setTextColor(TEXT); gravity = Gravity.CENTER; setPadding(0, 16, 0, 24)
            })
            addView(address, LinearLayout.LayoutParams(-1, -2))
            addView(LinearLayout(context).apply {
                addView(button("FIND") { find() }, LinearLayout.LayoutParams(0, -2, 1f))
                addView(button("CONNECT") { connect(address.text.toString()) }, LinearLayout.LayoutParams(0, -2, 1f))
            }, LinearLayout.LayoutParams(-1, -2))
            addView(state)
        }
        setContentView(FrameLayout(this).apply {
            setBackgroundColor(BG)
            addView(panel, FrameLayout.LayoutParams(-1, -1))
            addView(web, FrameLayout.LayoutParams(-1, -1))
        })
        showSplash()
        val last = prefs.getString("address", "") ?: ""
        if (last.isNotBlank()) connect(last) else state.text = "Press FIND, or type her PC's address"
    }

    private fun say(text: String) = runOnUiThread { state.text = text }

    /** "192.168.4.2" or "192.168.4.2:5017" */
    private fun connect(typed: String) {
        val text = typed.trim().removePrefix("http://").removePrefix("https://").substringBefore('/')
        if (text.isEmpty()) return say("Type her PC's address, or press FIND")
        val host = text.substringBefore(':')
        val port = text.substringAfter(':', "").toIntOrNull() ?: PORT
        prefs.edit().putString("address", text).apply()
        runOnUiThread {
            address.setText(text)
            state.text = "Connecting to $host..."
            web.visibility = View.VISIBLE
            web.loadUrl("http://$host:$port/")
        }
    }

    private fun backToPanel(why: String) = runOnUiThread {
        web.loadUrl("about:blank")
        web.visibility = View.GONE
        state.text = why
    }

    /** RIFT's fleet list on whoever hosts this network, the way the robots' apps look. */
    private fun find() = thread(isDaemon = true) {
        say("Looking for AURA...")
        val wm = applicationContext.getSystemService(WifiManager::class.java)
        @Suppress("DEPRECATION") val g = wm.dhcpInfo.gateway
        val gateway = "${g and 255}.${g shr 8 and 255}.${g shr 16 and 255}.${g shr 24 and 255}"
        val body = listOf("/fleet", "/robots").firstNotNullOfOrNull { get("http://$gateway:5000$it") }
            ?: return@thread say("No fleet list at $gateway. Type her PC's address.")
        val robots = try { JSONObject(body).getJSONArray("robots") } catch (_: Exception) { return@thread say("Odd fleet list") }
        for (i in 0 until robots.length()) {
            val r = robots.getJSONObject(i)
            if (r.optString("name").equals(NAME, ignoreCase = true) && r.optString("ip").isNotBlank()) {
                say("Found her at ${r.optString("ip")}")
                return@thread connect(r.optString("ip"))
            }
        }
        say("AURA isn't on this network. Is she running?")
    }

    private fun get(url: String): String? = try {
        val c = (URL(url).openConnection() as HttpURLConnection).apply { connectTimeout = 1500; readTimeout = 2000 }
        try { c.inputStream.bufferedReader().readText() } finally { c.disconnect() }
    } catch (_: Exception) {
        null
    }

    override fun onResume() {
        super.onResume()
        web.onResume()
        // The fleet's WiFi may have no internet: keep talking over WiFi, not mobile data
        val cm = getSystemService(ConnectivityManager::class.java)
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) { cm.bindProcessToNetwork(network) }
        }
        wifi = cb
        cm.requestNetwork(NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI).build(), cb)
    }

    override fun onPause() {
        web.onPause()
        wifi?.let { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(it) }
        wifi = null
        super.onPause()
    }

    @Deprecated("Back from her page shows the connect screen; back again closes the app")
    override fun onBackPressed() {
        if (web.visibility == View.VISIBLE) backToPanel("Disconnected") else super.onBackPressed()
    }
}
