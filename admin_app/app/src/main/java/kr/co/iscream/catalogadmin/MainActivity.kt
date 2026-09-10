package kr.co.iscream.catalogadmin

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.webkit.HttpAuthHandler
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity

/**
 * "카탈로그 관리자" - is-order 본체의 /admin/quick-catalog 페이지를 감싸는
 * 개인용(사이드로드) WebView 앱. 바코드를 스캔/입력하고 상품명·분류·
 * 추천판매가를 넣어 저장하면 catalog_items에 반영된다.
 *
 * /admin 이하는 HTTP Basic 인증(ADMIN_PASSWORD)으로 보호돼 있어서,
 * 처음 실행 시 서버 비밀번호를 한 번 입력받아 저장해두고 이후 자동으로
 * 넣는다(우측 위 톱니 버튼으로 다시 바꿀 수 있음).
 */
class MainActivity : AppCompatActivity() {

    companion object {
        private const val BASE_URL = "https://www.is-cream.co.kr/admin/quick-catalog"
        private const val PREFS = "admin_prefs"
        private const val KEY_PW = "server_password"
    }

    private lateinit var webView: WebView
    private lateinit var progressBar: ProgressBar

    // 방금 저장된 비밀번호로 Basic 인증을 시도했는데 서버가 또 물어보면
    // (= 비번이 틀림) 무한 재시도 대신 사용자에게 다시 입력받기 위한 플래그.
    private var triedSavedPassword = false

    private val scanLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode != RESULT_OK) return@registerForActivityResult
        val barcode = result.data?.getStringExtra(ScanActivity.RESULT_BARCODE) ?: return@registerForActivityResult
        val escaped = barcode.replace("\\", "\\\\").replace("'", "\\'")
        webView.evaluateJavascript(
            "window.receiveNativeScanResult && window.receiveNativeScanResult('$escaped');", null,
        )
    }

    private inner class ScannerBridge {
        @JavascriptInterface
        fun openNativeScanner() {
            runOnUiThread {
                scanLauncher.launch(Intent(this@MainActivity, ScanActivity::class.java))
            }
        }
    }

    private fun prefs() = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    private fun savedPassword(): String? = prefs().getString(KEY_PW, null)
    private fun setPassword(pw: String) = prefs().edit().putString(KEY_PW, pw).apply()

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webView)
        progressBar = findViewById(R.id.progressBar)

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
        }
        webView.addJavascriptInterface(ScannerBridge(), "AndroidScanner")

        webView.webViewClient = object : WebViewClient() {
            override fun onReceivedHttpAuthRequest(
                view: WebView, handler: HttpAuthHandler, host: String, realm: String,
            ) {
                val pw = savedPassword()
                if (pw != null && !triedSavedPassword) {
                    triedSavedPassword = true
                    // 사용자 이름은 서버가 검사하지 않으므로 아무 값이나 보낸다.
                    handler.proceed("admin", pw)
                } else {
                    // 저장된 비번이 없거나, 방금 그 비번으로도 인증이 안 됐다 -> 다시 입력.
                    promptPassword(handler)
                }
            }

            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                progressBar.visibility = View.VISIBLE
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                progressBar.visibility = View.GONE
            }
        }

        webView.webChromeClient = object : android.webkit.WebChromeClient() {
            override fun onProgressChanged(view: WebView, newProgress: Int) {
                progressBar.progress = newProgress
            }
        }

        findViewById<View>(R.id.settingsBtn).setOnClickListener { promptPassword(null) }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack() else finish()
            }
        })

        loadPage()
    }

    private fun loadPage() {
        triedSavedPassword = false
        webView.loadUrl(BASE_URL)
    }

    /** handler != null 이면 지금 대기 중인 Basic 인증 요청에 바로 응답하고,
     *  null 이면(톱니 버튼) 저장만 하고 페이지를 새로 로드한다. */
    private fun promptPassword(handler: HttpAuthHandler?) {
        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            hint = "서버 관리자 비밀번호 (ADMIN_PASSWORD)"
            setText(savedPassword() ?: "")
        }
        AlertDialog.Builder(this)
            .setTitle("서버 접속 비밀번호")
            .setMessage("is-order 관리자 비밀번호를 입력하세요. 한 번 저장하면 다음부터 자동으로 접속합니다.")
            .setView(input)
            .setPositiveButton("저장") { _, _ ->
                val pw = input.text.toString()
                if (pw.isEmpty()) {
                    Toast.makeText(this, "비밀번호를 입력해주세요.", Toast.LENGTH_SHORT).show()
                    handler?.cancel()
                    return@setPositiveButton
                }
                setPassword(pw)
                if (handler != null) {
                    triedSavedPassword = true
                    handler.proceed("admin", pw)
                } else {
                    loadPage()
                }
            }
            .setNegativeButton("취소") { _, _ -> handler?.cancel() }
            .setCancelable(false)
            .show()
    }
}
