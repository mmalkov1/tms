package ua.kult.tmsdriver

import android.annotation.SuppressLint
import android.app.*
import android.content.Context
import android.content.Intent
import android.os.*
import androidx.core.app.NotificationCompat
import com.google.android.gms.location.*
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * Фоновий трек GPS (фаза 2).
 * Черга точок переживає перезапуск (SharedPreferences), відправка пачкою
 * кожні 20 с на /api/driver/{token}/position — той самий контракт, що й web.
 */
class LocationService : Service() {

    private lateinit var fused: FusedLocationProviderClient
    private val queue = ArrayList<JSONObject>()
    private val io = Executors.newSingleThreadExecutor()
    private val handler = Handler(Looper.getMainLooper())
    private var token = ""
    private var base = MainActivity.BASE_URL

    private val callback = object : LocationCallback() {
        override fun onLocationResult(r: LocationResult) {
            val loc = r.lastLocation ?: return
            synchronized(queue) {
                queue.add(JSONObject().apply {
                    put("ts", loc.time)
                    put("lat", loc.latitude)
                    put("lon", loc.longitude)
                    put("speed_kmh", if (loc.hasSpeed()) loc.speed * 3.6 else JSONObject.NULL)
                    put("accuracy_m", if (loc.hasAccuracy()) loc.accuracy else JSONObject.NULL)
                })
                while (queue.size > 1000) queue.removeAt(0)
                persist()
            }
            if (loc.hasAccuracy())
                sendBroadcast(Intent(MainActivity.ACTION_GPS)
                    .setPackage(packageName).putExtra("acc", loc.accuracy))
        }
    }

    private val flusher = object : Runnable {
        override fun run() {
            flush()
            handler.postDelayed(this, 20_000)
        }
    }

    override fun onCreate() {
        super.onCreate()
        fused = LocationServices.getFusedLocationProviderClient(this)
        restore()
    }

    @SuppressLint("MissingPermission")
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        intent?.getStringExtra("token")?.let { token = it }
        intent?.getStringExtra("base")?.let { base = it }
        if (token.isBlank())
            token = getSharedPreferences(MainActivity.PREFS, MODE_PRIVATE)
                .getString("token", "") ?: ""

        startForeground(1, buildNotification())

        val req = LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, 15_000)
            .setMinUpdateIntervalMillis(10_000)
            .build()
        fused.requestLocationUpdates(req, callback, Looper.getMainLooper())

        handler.removeCallbacks(flusher)
        handler.postDelayed(flusher, 20_000)
        return START_STICKY
    }

    private fun buildNotification(): Notification {
        val chId = "gps"
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (nm.getNotificationChannel(chId) == null)
            nm.createNotificationChannel(NotificationChannel(
                chId, "GPS-трек", NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this, 0,
            Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, chId)
            .setSmallIcon(R.drawable.ic_stat_gps)
            .setContentTitle("ТМС Культтовари")
            .setContentText("Передача GPS у рейсі")
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    private fun flush() {
        val batch: List<JSONObject>
        synchronized(queue) {
            if (queue.isEmpty() || token.isBlank()) return
            batch = ArrayList(queue.take(500))
        }
        io.execute {
            val ok = runCatching {
                val body = JSONObject().put("points", JSONArray(batch)).toString()
                val con = URL("$base/api/driver/$token/position")
                    .openConnection() as HttpURLConnection
                con.requestMethod = "POST"
                con.setRequestProperty("Content-Type", "application/json")
                con.connectTimeout = 10_000
                con.readTimeout = 10_000
                con.doOutput = true
                con.outputStream.use { it.write(body.toByteArray()) }
                val code = con.responseCode
                con.disconnect()
                code in 200..299
            }.getOrDefault(false)
            if (ok) synchronized(queue) {
                repeat(minOf(batch.size, queue.size)) { queue.removeAt(0) }
                persist()
            }
            // не ok — точки лишаються в черзі, дошлемо наступним циклом
        }
    }

    private fun persist() {
        getSharedPreferences(MainActivity.PREFS, MODE_PRIVATE).edit()
            .putString("gpsq", JSONArray(queue).toString()).apply()
    }

    private fun restore() {
        runCatching {
            val raw = getSharedPreferences(MainActivity.PREFS, MODE_PRIVATE)
                .getString("gpsq", "[]") ?: "[]"
            val arr = JSONArray(raw)
            for (i in 0 until arr.length()) queue.add(arr.getJSONObject(i))
        }
    }

    override fun onDestroy() {
        fused.removeLocationUpdates(callback)
        handler.removeCallbacks(flusher)
        flush()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
