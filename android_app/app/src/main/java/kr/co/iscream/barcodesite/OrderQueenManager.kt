package kr.co.iscream.barcodesite

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID

/** 등록된 오더퀸 계정 하나(계정명만 - 아이디/비밀번호는 목록 조회에 안 실림). */
data class OqAccount(
    val id: Int, val nickname: String, val isDefault: Boolean,
    val salesDataConsent: Boolean = false,
)

/** "설정"(수정) 다이얼로그를 열 때만 받는 상세 정보 - 이때만 아이디를
 * 복호화해서 내려준다(비밀번호는 이때도 절대 안 내려줌). */
data class OqAccountDetail(val nickname: String, val loginId: String, val salesDataConsent: Boolean)

/** 계정 하나에 대한 등록 시도 결과 - "모든 계정에 추가"로 여러 계정을
 * 골랐을 때 계정별로 성공/실패가 다를 수 있어 따로 담는다. */
data class OqRegisterResult(val accountId: Int, val nickname: String?, val ok: Boolean, val message: String)

/** 오더퀸 등록 화면의 "분류" 하나(코드/이름) - 매장마다 구성이 달라서
 * 계정별로 따로 저장한다("분류 설정" 화면 참고). */
data class OqCategory(val classCd: String, val className: String)

/**
 * 오더퀸 자동등록 기능의 로컬 상태(기기 식별자/사용 여부)와 서버 API 호출을
 * 담당한다. 본체 사이트(is-cream.co.kr) 로그인과는 완전히 별개로 동작한다
 * (사용자 요청) - 이 기기가 스스로 만든 임의의 device_id 하나로,
 * main.py의 /api/oq-app 이하 엔드포인트에 저장된 자신의 오더퀸 계정(들)을
 * 식별한다. 다매장 점주는 매장마다 오더퀸 계정이 달라서 계정을 여러 개
 * 등록할 수 있다(각 계정은 "계정명"으로 구분). 여기 있는 함수는 전부
 * 네트워크 호출을 포함해 블로킹되므로, 반드시 백그라운드 스레드에서
 * 불러야 한다(UI 스레드에서 부르면 안 됨).
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

    /** 등록된 계정 목록을 가져오면서, 하나라도 있으면 로컬 사용 여부 플래그도
     * 같이 맞춰둔다(isAvailable()이 매번 네트워크를 타지 않고 이 캐시된
     * 값을 쓰기 때문에, 계정 목록이 바뀔 때마다 항상 최신으로 유지해야 함). */
    fun fetchAccounts(context: Context): List<OqAccount> {
        val accounts = try {
            val res = request("/api/oq-app/accounts?device_id=${getDeviceId(context)}", "GET")
            val arr = res.optJSONArray("accounts") ?: org.json.JSONArray()
            (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                OqAccount(
                    o.getInt("id"), o.getString("nickname"), o.optBoolean("is_default", false),
                    o.optBoolean("sales_data_consent", false),
                )
            }
        } catch (e: Exception) {
            emptyList()
        }
        setEnabledLocally(context, accounts.isNotEmpty())
        return accounts
    }

    fun saveAccount(
        context: Context, nickname: String, loginId: String, loginPwd: String,
        salesDataConsent: Boolean = false,
    ): Result<Int> {
        val body = JSONObject()
            .put("device_id", getDeviceId(context))
            .put("nickname", nickname)
            .put("login_id", loginId)
            .put("login_pwd", loginPwd)
            .put("sales_data_consent", salesDataConsent)
        return try {
            // 저장 전에 서버가 실제로 오더퀸 로그인을 시도해 아이디/비밀번호를
            // 확인한다(실측: 성공 약 5초, 틀린 비밀번호 등 실패는 재시도
            // 대기 때문에 최대 20초 가까이 걸림) - 기본 20초 타임아웃으로는
            // 실패 응답이 오기 직전에 클라이언트가 먼저 타임아웃날 수 있어
            // 여유를 둔다.
            val res = request("/api/oq-app/credentials", "POST", body, readTimeoutMs = 30000)
            if (res.optBoolean("ok", false)) {
                setEnabledLocally(context, true)
                Result.success(res.optInt("account_id"))
            } else {
                Result.failure(Exception(res.optString("message", "저장에 실패했습니다.")))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /** "설정"(수정) 다이얼로그를 열 때 계정명/아이디/동의 상태를 미리 채우기
     * 위해 부른다(비밀번호는 여기서도 안 내려줌). */
    fun fetchAccountDetail(context: Context, accountId: Int): Result<OqAccountDetail> {
        return try {
            val res = request("/api/oq-app/accounts/$accountId?device_id=${getDeviceId(context)}", "GET")
            if (res.optBoolean("ok", false)) {
                Result.success(
                    OqAccountDetail(
                        res.getString("nickname"), res.getString("login_id"),
                        res.optBoolean("sales_data_consent", false),
                    )
                )
            } else {
                Result.failure(Exception(res.optString("message", "조회에 실패했습니다.")))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /** "설정"(수정) 다이얼로그의 저장 - 계정명/아이디/동의는 항상 반영하고,
     * loginPwd가 null/빈 문자열이면 기존 비밀번호를 그대로 둔다. 아이디나
     * 비밀번호가 바뀐 경우 서버가 다시 로그인 검증을 하므로(성공 ~5초,
     * 실패 시 최대 20여 초) saveAccount와 같은 여유 있는 타임아웃을 쓴다. */
    fun updateAccount(
        context: Context, accountId: Int, nickname: String, loginId: String,
        loginPwd: String?, salesDataConsent: Boolean,
    ): Result<Unit> {
        val body = JSONObject()
            .put("device_id", getDeviceId(context))
            .put("account_id", accountId)
            .put("nickname", nickname)
            .put("login_id", loginId)
            .put("login_pwd", if (loginPwd.isNullOrBlank()) JSONObject.NULL else loginPwd)
            .put("sales_data_consent", salesDataConsent)
        return try {
            val res = request("/api/oq-app/credentials", "PUT", body, readTimeoutMs = 30000)
            if (res.optBoolean("ok", false)) {
                Result.success(Unit)
            } else {
                Result.failure(Exception(res.optString("message", "수정에 실패했습니다.")))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    private fun parseCategories(res: JSONObject): List<OqCategory> {
        val arr = res.optJSONArray("categories") ?: org.json.JSONArray()
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            OqCategory(o.getString("class_cd"), o.getString("class_name"))
        }
    }

    /** 이 계정에 저장된 분류 목록을 가져온다("분류 설정" 화면과 등록 화면의
     * 분류 드롭다운 둘 다 이걸 쓴다). 한 번도 동기화 안 했으면 서버가 예전
     * 기본값 5개를 대신 내려준다. */
    fun fetchCategories(context: Context, accountId: Int): Result<List<OqCategory>> {
        return try {
            val res = request(
                "/api/oq-app/categories?device_id=${getDeviceId(context)}&account_id=$accountId", "GET",
            )
            if (res.optBoolean("ok", false)) {
                Result.success(parseCategories(res))
            } else {
                Result.failure(Exception(res.optString("message", "분류를 불러오지 못했습니다.")))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /** "분류 설정" 화면의 "동기화" - 오더퀸에서 이 계정의 실제 분류를 다시
     * 긁어와 저장한다(로그인이 들어가는 만큼 시간이 좀 걸림). */
    fun syncCategories(context: Context, accountId: Int): Result<List<OqCategory>> {
        val body = JSONObject().put("device_id", getDeviceId(context)).put("account_id", accountId)
        return try {
            val res = request("/api/oq-app/categories/sync", "POST", body, readTimeoutMs = 45000)
            if (res.optBoolean("ok", false)) {
                Result.success(parseCategories(res))
            } else {
                Result.failure(Exception(res.optString("message", "동기화에 실패했습니다.")))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /** "분류 설정" 화면에서 사용자가 직접 추가/수정/삭제한 목록을 저장한다. */
    fun saveCategories(context: Context, accountId: Int, categories: List<OqCategory>): Result<Unit> {
        val arr = org.json.JSONArray()
        categories.forEach { c ->
            arr.put(JSONObject().put("class_cd", c.classCd).put("class_name", c.className))
        }
        val body = JSONObject()
            .put("device_id", getDeviceId(context))
            .put("account_id", accountId)
            .put("categories", arr)
        return try {
            val res = request("/api/oq-app/categories", "PUT", body, readTimeoutMs = 20000)
            if (res.optBoolean("ok", false)) {
                Result.success(Unit)
            } else {
                Result.failure(Exception(res.optString("message", "저장에 실패했습니다.")))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /** account_id를 주면 그 계정 하나만, 안 주면(기능을 완전히 끌 때) 등록된
     * 계정을 전부 지운다. 어느 쪽이든 서버 삭제가 실패해도 조용히 무시한다
     * (로컬 사용 여부는 호출부가 최신 목록으로 다시 맞춘다). */
    fun deleteAccount(context: Context, accountId: Int? = null) {
        try {
            val deviceId = getDeviceId(context)
            val path = if (accountId != null) {
                "/api/oq-app/credentials?device_id=$deviceId&account_id=$accountId"
            } else {
                "/api/oq-app/credentials?device_id=$deviceId"
            }
            request(path, "DELETE")
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

    /** accountIds에 담긴 계정 각각에 순서대로 로그인+등록을 시도한다(서버가
     * 순차 처리) - "모든 계정에 추가"를 고르면 여러 개가 담겨온다. 계정
     * 수만큼 시간이 늘어나므로(계정당 실측 20~30초) 읽기 타임아웃도 계정
     * 수에 비례해서 넉넉히 둔다. */
    fun registerItem(
        context: Context, barcode: String, menuName: String, salePrice: Int,
        classCd: String, className: String, accountIds: List<Int>,
    ): Result<List<OqRegisterResult>> {
        val body = JSONObject()
            .put("device_id", getDeviceId(context))
            .put("account_ids", org.json.JSONArray(accountIds))
            .put("barcode", barcode)
            .put("menu_name", menuName)
            .put("sale_price", salePrice)
            .put("class_cd", classCd)
            .put("class_name", className)
        return try {
            val timeoutMs = 60000 * accountIds.size.coerceAtLeast(1)
            val res = request("/api/oq-app/register-item", "POST", body, readTimeoutMs = timeoutMs)
            val arr = res.optJSONArray("results")
            if (arr == null) {
                return Result.failure(Exception(res.optString("message", "등록에 실패했습니다.")))
            }
            val results = (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                val nickname = if (o.isNull("nickname")) null else o.optString("nickname")
                OqRegisterResult(o.optInt("account_id"), nickname, o.optBoolean("ok", false), o.optString("message", ""))
            }
            Result.success(results)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
}
