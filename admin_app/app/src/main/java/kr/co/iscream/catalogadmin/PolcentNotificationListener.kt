package kr.co.iscream.catalogadmin

import android.app.Notification
import android.content.Context
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Base64
import android.util.Log
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * 폰에 뜬 "폴센트" 앱 알림의 제목/본문을 서버(/api/admin/polcent/ingest)로 보낸다.
 * 서버가 알림의 현재가와 맞는 쿠팡 상품을 찾아 핫딜 안내에 바로 올린다.
 * 인증은 관리자 앱이 이미 저장해둔 서버 비밀번호(HTTP Basic)를 그대로 쓴다.
 */
class PolcentNotificationListener : NotificationListenerService() {

    companion object {
        private const val TAG = "PolcentListener"
        private const val INGEST_URL = "https://www.is-cream.co.kr/api/admin/polcent/ingest"
        private const val PREFS = "admin_prefs"
        private const val KEY_PW = "server_password"
        private val RETRY_DELAYS_MS = longArrayOf(0, 5_000, 20_000)
    }

    private val isPolcentByPackage = HashMap<String, Boolean>()
    private val lastSentByKey = HashMap<String, String>()

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val pkg = sbn.packageName ?: return
        if (pkg == packageName || !isPolcent(pkg)) return
        val notification = sbn.notification ?: return
        if (notification.flags and Notification.FLAG_GROUP_SUMMARY != 0) return

        val extras = notification.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = (extras.getCharSequence(Notification.EXTRA_BIG_TEXT)
            ?: extras.getCharSequence(Notification.EXTRA_TEXT))?.toString().orEmpty()
        if (title.isBlank() && text.isBlank()) return

        // 같은 알림이 갱신(update)돼 다시 오는 경우 중복 전송을 막는다.
        val signature = "$title|$text"
        synchronized(lastSentByKey) {
            if (lastSentByKey[sbn.key] == signature) return
            lastSentByKey[sbn.key] = signature
            if (lastSentByKey.size > 200) lastSentByKey.clear()
        }

        thread { send(title, text) }
    }

    private fun isPolcent(pkg: String): Boolean {
        isPolcentByPackage[pkg]?.let { return it }
        val result = try {
            val label = packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
            label.contains("폴센트") || pkg.contains("polcent", ignoreCase = true)
        } catch (e: Exception) {
            pkg.contains("polcent", ignoreCase = true)
        }
        isPolcentByPackage[pkg] = result
        return result
    }

    private fun send(title: String, text: String) {
        val password = getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_PW, null)
        if (password.isNullOrEmpty()) {
            Log.w(TAG, "서버 비밀번호가 저장돼 있지 않아 전송하지 못했습니다.")
            return
        }
        val auth = "Basic " + Base64.encodeToString("admin:$password".toByteArray(), Base64.NO_WRAP)
        val body = JSONObject().put("title", title).put("text", text).put("source", "notification").toString()

        for (delay in RETRY_DELAYS_MS) {
            if (delay > 0) Thread.sleep(delay)
            try {
                val conn = URL(INGEST_URL).openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.connectTimeout = 15_000
                conn.readTimeout = 60_000
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                conn.setRequestProperty("Authorization", auth)
                conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
                val code = conn.responseCode
                conn.disconnect()
                if (code in 200..299) return
                Log.w(TAG, "서버 응답 코드 $code")
                if (code == 401 || code == 403) return  // 비밀번호 문제는 재시도해도 소용없다
            } catch (e: Exception) {
                Log.w(TAG, "전송 실패: ${e.message}")
            }
        }
    }
}
