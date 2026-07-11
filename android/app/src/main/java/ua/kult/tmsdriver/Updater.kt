package ua.kult.tmsdriver

import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * v27: оновлення застосунку «по повітрю».
 * GET /api/app/version → якщо versionCode на сервері більший — качаємо
 * GET /api/app/apk у cache і віддаємо системному інсталятору.
 * Працює лише для підписаних релізів: debug-APK поверх release не встане.
 */
object Updater {

    private val io = Executors.newSingleThreadExecutor()
    private val ui = Handler(Looper.getMainLooper())

    fun check(act: AppCompatActivity, base: String, silent: Boolean = true) {
        io.execute {
            val info = runCatching {
                val con = URL("$base/api/app/version").openConnection() as HttpURLConnection
                con.connectTimeout = 8_000; con.readTimeout = 8_000
                val txt = con.inputStream.bufferedReader().readText()
                con.disconnect()
                JSONObject(txt)
            }.getOrNull() ?: return@execute

            val remote = info.optInt("versionCode", 0)
            val name = info.optString("versionName", "")
            val notes = info.optString("notes", "")
            val avail = info.optBoolean("available", false)
            val local = act.packageManager
                .getPackageInfo(act.packageName, 0).longVersionCode.toInt()

            ui.post {
                if (!avail || remote <= local) {
                    if (!silent) Toast.makeText(act,
                        "Встановлена остання версія", Toast.LENGTH_SHORT).show()
                    return@post
                }
                AlertDialog.Builder(act)
                    .setTitle("Є оновлення $name")
                    .setMessage(if (notes.isBlank()) "Оновити застосунок?" else notes)
                    .setPositiveButton("Оновити") { _, _ -> download(act, base) }
                    .setNegativeButton("Пізніше", null)
                    .show()
            }
        }
    }

    private fun download(act: AppCompatActivity, base: String) {
        Toast.makeText(act, "Завантаження…", Toast.LENGTH_SHORT).show()
        io.execute {
            val file = File(act.cacheDir, "update.apk")
            val ok = runCatching {
                val con = URL("$base/api/app/apk").openConnection() as HttpURLConnection
                con.connectTimeout = 10_000; con.readTimeout = 60_000
                con.inputStream.use { inp -> file.outputStream().use { inp.copyTo(it) } }
                con.disconnect()
                file.length() > 0
            }.getOrDefault(false)

            ui.post {
                if (!ok) {
                    Toast.makeText(act, "Не вдалося завантажити", Toast.LENGTH_LONG).show()
                    return@post
                }
                val uri: Uri = FileProvider.getUriForFile(
                    act, "${act.packageName}.fileprovider", file)
                act.startActivity(Intent(Intent.ACTION_VIEW).apply {
                    setDataAndType(uri, "application/vnd.android.package-archive")
                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                })
            }
        }
    }
}
