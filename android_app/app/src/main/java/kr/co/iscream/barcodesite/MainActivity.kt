package kr.co.iscream.barcodesite

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.view.View
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
 * 이미 들어있어서, 앱 쪽에서 따로 스캔 로직을 만들 필요가 없다 - WebView가
 * 웹페이지의 카메라 요청을 실제 안드로이드 카메라 권한과 연결해주기만 하면
 * 웹과 완전히 같은 스캔 기능이 앱에서도 그대로 동작한다.
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
