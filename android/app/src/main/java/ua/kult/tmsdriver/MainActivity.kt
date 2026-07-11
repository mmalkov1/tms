package ua.kult.tmsdriver

import android.Manifest
import android.annotation.SuppressLint
import android.content.*
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : AppCompatActivity() {

    companion object {
        const val PREFS = "tms"
        const val BASE_URL = "https://tms.rpa.com.ua"
        const val ACTION_GPS = "ua.kult.tmsdriver.GPS_ACC"
    }

    private var webView: WebView? = null

    private val gpsReceiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context?, i: Intent?) {
            val acc = i?.getFloatExtra("acc", -1f) ?: return
            if (acc >= 0) webView?.evaluateJavascript(
                "window.nativeGps && nativeGps(${acc})", null)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val token = prefs().getString("token", null)
        if (token.isNullOrBlank()) showTokenScreen() else showTrip(token)
    }

    private fun prefs() = getSharedPreferences(PREFS, MODE_PRIVATE)

    // ---------- екран введення токена ----------

    private fun showTokenScreen() {
        setContentView(R.layout.activity_token)
        val input = findViewById<EditText>(R.id.tokenInput)
        findViewById<Button>(R.id.tokenSave).setOnClickListener {
            var t = input.text.toString().trim()
            // приймаємо і повне посилання, і голий токен
            Uri.parse(t).getQueryParameter("token")?.let { t = it }
            if (t.isBlank()) {
                Toast.makeText(this, "Встав токен або посилання", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            prefs().edit().putString("token", t).apply()
            showTrip(t)
        }
    }

    // ---------- кабінет водія ----------

    @SuppressLint("SetJavaScriptEnabled")
    private fun showTrip(token: String) {
        setContentView(R.layout.activity_main)
        webView = findViewById<WebView>(R.id.web).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.cacheMode = android.webkit.WebSettings.LOAD_NO_CACHE  // v27: driver.html завжди свіжий
            settings.userAgentString = settings.userAgentString + " TMSKultApp/1.0"
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(
                    view: WebView?, req: WebResourceRequest?): Boolean {
                    val url = req?.url ?: return false
                    val sch = url.scheme ?: return false
                    // v28: google.navigation / waze / tel — прямо у застосунок навігатора
                    if (sch != "http" && sch != "https") {
                        try { startActivity(Intent(Intent.ACTION_VIEW, url)) }
                        catch (e: Exception) {
                            Toast.makeText(this@MainActivity,
                                "Застосунок не встановлено", Toast.LENGTH_SHORT).show()
                        }
                        return true
                    }
                    // свій домен — усередині, зовнішні http-посилання — назовні
                    return if (url.host == Uri.parse(BASE_URL).host) false
                    else { startActivity(Intent(Intent.ACTION_VIEW, url)); true }
                }
            }
            loadUrl("$BASE_URL/driver.html?token=$token")
        }
        askPermissionsAndStart()
        Updater.check(this, BASE_URL)          // v27: оновлення «по повітрю»
    }

    private fun askPermissionsAndStart() {
        val need = mutableListOf<String>()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
            != PackageManager.PERMISSION_GRANTED)
            need += Manifest.permission.ACCESS_FINE_LOCATION
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED)
            need += Manifest.permission.POST_NOTIFICATIONS
        if (need.isEmpty()) startGps()
        else ActivityCompat.requestPermissions(this, need.toTypedArray(), 1)
    }

    override fun onRequestPermissionsResult(
        code: Int, perms: Array<out String>, res: IntArray) {
        super.onRequestPermissionsResult(code, perms, res)
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
            == PackageManager.PERMISSION_GRANTED) startGps()
        else Toast.makeText(this,
            "Без дозволу на геолокацію трек не пишеться", Toast.LENGTH_LONG).show()
    }

    private fun startGps() {
        val token = prefs().getString("token", null) ?: return
        val i = Intent(this, LocationService::class.java)
            .putExtra("token", token).putExtra("base", BASE_URL)
        ContextCompat.startForegroundService(this, i)
    }

    override fun onResume() {
        super.onResume()
        ContextCompat.registerReceiver(this, gpsReceiver,
            IntentFilter(ACTION_GPS), ContextCompat.RECEIVER_NOT_EXPORTED)
    }

    override fun onPause() {
        super.onPause()
        runCatching { unregisterReceiver(gpsReceiver) }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (webView?.canGoBack() == true) webView?.goBack() else super.onBackPressed()
    }
}
