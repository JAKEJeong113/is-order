package kr.co.iscream.barcodesite

import android.content.Context
import android.os.Handler
import android.os.Looper

/**
 * OqRegisterService(다중선택 등록)가 끝났을 때 그 결과를 MainActivity에
 * 전달한다. 결과 알림만 믿고 두면 사용자가 무심코 스와이프해 지워버려
 * 등록이 실제로 됐는지 확인할 길이 없어지는 문제가 있었다(사용자 확인) -
 * 앱이 지금 화면에 떠 있으면(liveCallback이 등록돼 있으면) 바로 그
 * 콜백으로 팝업을 띄우고, 백그라운드/종료 상태라 받을 사람이 없으면
 * SharedPreferences에 남겨뒀다가 다음에 앱을 열 때(MainActivity.onResume)
 * 확인해서 보여준다.
 */
object OqRegisterResultBus {
    private const val PREFS_NAME = "oq_register_result"
    private const val KEY_PENDING_SUMMARY = "pending_summary"

    @Volatile
    var liveCallback: ((String) -> Unit)? = null

    private val mainHandler = Handler(Looper.getMainLooper())

    fun deliver(context: Context, summary: String) {
        val callback = liveCallback
        if (callback != null) {
            mainHandler.post { callback(summary) }
        } else {
            context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .putString(KEY_PENDING_SUMMARY, summary)
                .apply()
        }
    }

    /** 앱을 다시 열었을 때(MainActivity.onResume) 한 번 호출해서, 그 사이
     * 조용히 끝난 등록 결과가 있으면 꺼내온다(있었다면 다시 안 뜨게 지운다). */
    fun consumePending(context: Context): String? {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val summary = prefs.getString(KEY_PENDING_SUMMARY, null) ?: return null
        prefs.edit().remove(KEY_PENDING_SUMMARY).apply()
        return summary
    }
}
