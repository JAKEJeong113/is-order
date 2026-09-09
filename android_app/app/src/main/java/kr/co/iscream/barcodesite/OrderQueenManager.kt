package kr.co.iscream.barcodesite

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID

/**
 * 오더퀸 자동등록 기능의 로컬 상태(기기 식별자/사용 여부)와 서버 API 호출을
 * 담당한다. 본체 사이트(is-cream.co.kr) 로그인과는 완전히 별개로 동작한다
 * (사용자 요청) - 이 기기가 스스로 만든 임의의 device_id 하나로,
 * main.py의 /api/oq-app 이하 엔드포인트에 저장된 자신의 오더퀸 계정을
 * 식별한다. 여기 있는 함수는 전부 네트워크 호출을 포함해 블로킹되므로,
 * 반드시 백그라운드 스레드에서 불러야 한다(UI 스레드에서 부르면 안 됨).
 */
object OrderQueenManager {
    private const val PREFS_NAME = "oq_prefs"
    private const val KEY_DEVICE_ID = "device_id"
    private const val KEY_ENABLED = "enabled"

    // main.py(is-order 본체 백엔드)가 이 API들을 갖고 있다 - barcode_site는
    // Playwright가 없는 가벼운 배포라 이 기능을 실행할 수 없다.
    private const val API_BASE_URL = "https://www.is-cream.co.kr"

    // 서버에서 최신 목록을 받아오기 전까지 쓰는 기본값 - orderqueen_bot.py의
    // CLASS_CODES와 반드시 같은 값으로 유지해야 한다.
    val DEFAULT_CLASS_CODES: Map<String, String> = linkedMapOf(
        "아이스크림" to "003",
        "음료수" to "004",
        "간식" to "006",
        "완구,문구" to "007",
        "과자" to "015",
    )

    private fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun getDeviceId(context: Context): String {
        val p = prefs(context)
        val existing = p.getString(KEY_DEVICE_ID, null)
        if (existing != null) return existing
        val newId = UUID.randomUUID().toString()
        p.edit().putString(KEY_DEVICE_ID, newId).apply()
        return newId
    }

    fun isEnabledLocally(context: Context): Boolean = prefs(context).getBoolean(KEY_ENABLED, false)

    fun setEnabledLocally(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean(KEY_ENABLED, enabled).apply()
    }

    private fun readBody(conn: HttpURLConnection): JSONObject {
        val code = conn.responseCode
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        val text = stream?.bufferedReader()?.use { it.readText() } ?: "{}"
        return try {
            JSONObject(text)
        } catch (e: Exception) {
            JSONObject().put("ok", false).put("message", "서버 응답을 해석할 수 없습니다.")
        }
    }

    private fun request(path: String, method: String, body: JSONObject? = null, readTimeoutMs: Int = 20000): JSONObject {
        val conn = URL("$API_BASE_URL$path").openConnection() as HttpURLConnection
        conn.requestMethod = method
        conn.connectTimeout = 15000
        conn.readTimeout = readTimeoutMs
        if (body != null) {
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
        }
        return readBody(conn)
    }

    fun fetchRegisteredStatus(context: Context): Boolean {
        val deviceId = getDeviceId(context)
        return try {
            request("/api/oq-app/credentials/status?device_id=$deviceId", "GET").optBoolean("registered", false)
        } catch (e: Exception) {
            false
        }
    }

    fun saveCredentials(context: Context, loginId: String, loginPwd: String): Result<Unit> {
        val body = JSONObject()
            .put("device_id", getDeviceId(context))
            .put("login_id", loginId)
            .put("login_pwd", loginPwd)
        return try {
            val res = request("/api/oq-app/credentials", "POST", body)
            if (res.optBoolean("ok", false)) Result.success(Unit)
            else Result.failure(Exception(res.optString("message", "저장에 실패했습니다.")))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    fun deleteCredentials(context: Context) {
        try {
            request("/api/oq-app/credentials?device_id=${getDeviceId(context)}", "DELETE")
        } catch (e: Exception) {
            // 서버 삭제가 실패해도 로컬에서는 어차피 기능을 끄므로 조용히 무시한다.
        }
    }

    fun fetchClassCodes(): Map<String, String> {
        return try {
            val res = request("/api/oq-app/class-codes", "GET")
            val classes = res.optJSONObject("classes") ?: return DEFAULT_CLASS_CODES
            val map = LinkedHashMap<String, String>()
            classes.keys().forEach { key -> map[key] = classes.getString(key) }
            if (map.isEmpty()) DEFAULT_CLASS_CODES else map
        } catch (e: Exception) {
            DEFAULT_CLASS_CODES
        }
    }

    fun registerItem(context: Context, barcode: String, menuName: String, salePrice: Int, classCd: String): Result<String> {
        val body = JSONObject()
            .put("device_id", getDeviceId(context))
            .put("barcode", barcode)
            .put("menu_name", menuName)
            .put("sale_price", salePrice)
            .put("class_cd", classCd)
        return try {
            // 실제 로그인+폼입력+저장까지 하는 동작이라 시간이 좀 걸린다(실측
            // 20~30초) - 읽기 타임아웃을 넉넉히 둔다.
            val res = request("/api/oq-app/register-item", "POST", body, readTimeoutMs = 60000)
            if (res.optBoolean("ok", false)) Result.success(res.optString("message", "등록 완료"))
            else Result.failure(Exception(res.optString("message", "등록에 실패했습니다.")))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
}
