package kr.co.iscream.barcodesite

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

/**
 * "무인매장 바코드 조회" 웹사이트를 그대로 감싸는 WebView 앱.
 *
 * 이 웹사이트 자체에 카메라 바코드 스캔 기능(html5-qrcode, getUserMedia)이
 * 들어있어서 원래는 앱 쪽에 따로 스캔 로직을 안 만들어도 됐다. 그런데 일부
 * 기기는 브라우저 표준 카메라 API가 노출하는 초점 제어 능력치 자체가 고장나
 * 있어(실측: capabilities.focusMode가 "continuous"를 지원 안 한다면서 현재
 * settings는 "continuous"라고 답하는 모순된 값 - 웹 표준 API 레벨의
 * 기기/브라우저 버그라 JS로는 손쓸 수 없음) 초점이 전혀 안 맞는 문제가
 * 있었다. 이걸 우회하기 위해 앱에서만 네이티브 카메라(ScanActivity,
 * CameraX+ML Kit)를 대신 쓰도록 JS 인터페이스(AndroidScanner)를 심어준다 -
 * 웹페이지는 이 인터페이스가 있으면 자동으로 그쪽을 쓰고, 없는(=일반
 * 브라우저) 환경에서는 기존 html5-qrcode 방식 그대로 동작한다.
 */
class MainActivity : AppCompatActivity() {

    companion object {
        // 정식 도메인(barcode.is-cream.co.kr) 연결 완료 - Render의 임시
        // onrender.com 주소(barcod-site.onrender.com, "barcode"에서 e가
        // 빠진 오타성 이름이었음) 대신 이 도메인을 쓴다.
        private const val BASE_URL = "https://barcode.is-cream.co.kr/"
    }

    private lateinit var webView: WebView
    private lateinit var swipeRefresh: SwipeRefreshLayout
    private lateinit var progressBar: android.widget.ProgressBar

    // WebView가 카메라 사용 허락을 요청해오면(getUserMedia), 실제 안드로이드
    // CAMERA 런타임 권한이 있는지부터 확인해야 한다 - 권한이 없으면 여기서
    // 사용자에게 물어보고, 결과가 오면 보류해둔 PermissionRequest를
    // 그때 그랜트/거부한다.
    private var pendingPermissionRequest: PermissionRequest? = null

    private val requestCameraPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        val request = pendingPermissionRequest
        pendingPermissionRequest = null
        if (request == null) return@registerForActivityResult
        if (granted) {
            request.grant(request.resources)
        } else {
            request.deny()
        }
    }

    // ScanActivity(네이티브 스캐너)가 인식한 바코드를 결과로 받아서, 웹페이지의
    // window.receiveNativeScanResult(...)를 직접 호출해 검색창에 채워 넣는다 -
    // 웹의 startScan() 성공 콜백과 똑같은 경로를 타므로 UI 동작이 일관된다.
    private val scanActivityLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode != RESULT_OK) return@registerForActivityResult
        val barcode = result.data?.getStringExtra(ScanActivity.RESULT_BARCODE) ?: return@registerForActivityResult
        // JS 문자열 리터럴 안에 그대로 넣을 거라 백슬래시/작은따옴표만 이스케이프하면
        // 충분하다 - 바코드는 항상 숫자/영문 조합이라 다른 특수문자가 나올 일이 없다.
        val escaped = barcode.replace("\\", "\\\\").replace("'", "\\'")
        webView.evaluateJavascript(
            "window.receiveNativeScanResult && window.receiveNativeScanResult('$escaped');",
            null,
        )
    }

    // 웹페이지에서 "window.AndroidScanner.openNativeScanner()"로 호출하는
    // 다리 역할 - addJavascriptInterface로 노출된 메서드는 UI 스레드가 아닌
    // 별도 스레드에서 실행되므로 액티비티 실행은 runOnUiThread로 감싼다.
    private inner class WebAppInterface {
        @JavascriptInterface
        fun openNativeScanner() {
            runOnUiThread {
                scanActivityLauncher.launch(Intent(this@MainActivity, ScanActivity::class.java))
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webView)
        swipeRefresh = findViewById(R.id.swipeRefresh)
        progressBar = findViewById(R.id.progressBar)

        setupWebView()
        webView.loadUrl(BASE_URL)

        swipeRefresh.setOnRefreshListener { webView.reload() }

        onBackPressedDispatcher.addCallback(
            this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    // 카메라 스캔 오버레이는 페이지 이동이 아니라 JS로 화면 위에
                    // 띄우는 레이어라 webView.canGoBack()으로는 존재를 알 수
                    // 없다 - 그 상태에서 뒤로가기를 누르면 오버레이는 그대로 둔
                    // 채 앱이 최소화돼버리는 문제가 있었다(실사용 확인). 뒤로가기
                    // 시 먼저 JS에게 오버레이가 열려있는지 물어보고, 열려있으면
                    // 페이지의 stopScan()(카메라 정지까지 포함)을 호출해 오버레이만
                    // 닫고 끝낸다. 안 열려있을 때만 기존 동작(웹뷰 히스토리 뒤로가기
                    // -> 없으면 앱 종료)으로 넘어간다.
                    webView.evaluateJavascript(
                        """
                        (function() {
                          var overlay = document.getElementById('scanOverlay');
                          if (overlay && overlay.classList.contains('open') && typeof stopScan === 'function') {
                            stopScan();
                            return true;
                          }
                          return false;
                        })();
                        """.trimIndent(),
                    ) { result ->
                        val overlayWasOpen = result == "true"
                        if (!overlayWasOpen) {
                            if (webView.canGoBack()) {
                                webView.goBack()
                            } else {
                                isEnabled = false
                                onBackPressedDispatcher.onBackPressed()
                            }
                        }
                    }
                }
            },
        )
    }

    private fun setupWebView() {
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
        }

        // shouldOverrideUrlLoading이 우리 도메인(barcode.is-cream.co.kr) 외의
        // 모든 이동을 외부 브라우저로 돌려보내므로, 이 WebView 안에는 항상
        // 우리 자신이 만든 신뢰된 콘텐츠만 로드된다 - addJavascriptInterface로
        // 노출한 네이티브 브리지를 제3자 콘텐츠가 악용할 여지가 없다.
        webView.addJavascriptInterface(WebAppInterface(), "AndroidScanner")

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean {
                // 우리 사이트 안에서는 계속 앱(WebView) 안에서 이동하고,
                // 그 외 도메인(예: i's ORDER 로그인 페이지 링크)은 사용자의
                // 기본 브라우저로 넘긴다 - 로그인/회원가입처럼 민감한 화면은
                // 신뢰된 브라우저에서 진행하는 게 더 안전하고 자연스럽다.
                // 정확히 이 서브도메인일 때만 WebView 안에 머무른다 - "is-cream.co.kr"
                // 전체를 느슨하게 매칭하면 본체 사이트(www.is-cream.co.kr)의 로그인
                // 페이지 링크까지 WebView 안에 붙잡아버려서, 원래 의도(로그인처럼
                // 민감한 화면은 신뢰된 외부 브라우저로 보냄)가 깨진다.
                val uri = Uri.parse(url)
                return if (uri.host == "barcode.is-cream.co.kr") {
                    false
                } else {
                    startActivity(Intent(Intent.ACTION_VIEW, uri))
                    true
                }
            }

            override fun onPageFinished(view: WebView, url: String?) {
                swipeRefresh.isRefreshing = false
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView, newProgress: Int) {
                progressBar.progress = newProgress
                progressBar.visibility = if (newProgress >= 100) View.GONE else View.VISIBLE
            }

            // 웹페이지가 카메라(getUserMedia)를 요청할 때 호출됨 - 실제
            // 안드로이드 CAMERA 권한이 있는지 확인 후에만 승인한다.
            override fun onPermissionRequest(request: PermissionRequest) {
                val needsVideo = request.resources.contains(PermissionRequest.RESOURCE_VIDEO_CAPTURE)
                if (!needsVideo) {
                    request.deny()
                    return
                }

                val hasPermission = ContextCompat.checkSelfPermission(
                    this@MainActivity,
                    Manifest.permission.CAMERA,
                ) == PackageManager.PERMISSION_GRANTED

                if (hasPermission) {
                    request.grant(request.resources)
                } else {
                    pendingPermissionRequest = request
                    requestCameraPermission.launch(Manifest.permission.CAMERA)
                }
            }
        }
    }
}
