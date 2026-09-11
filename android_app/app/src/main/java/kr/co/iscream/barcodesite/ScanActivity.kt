package kr.co.iscream.barcodesite

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * 안드로이드 시스템 카메라(CameraX + ML Kit)로 직접 바코드를 스캔한다.
 *
 * 왜 필요한가: 웹페이지(barcode_site)의 스캔 기능은 브라우저 표준 카메라
 * API(getUserMedia)를 쓰는 html5-qrcode를 쓰는데, 일부 기기는 이 API가
 * 노출하는 초점 제어 능력치 자체가 고장나 있다(실측: 카메라
 * capabilities.focusMode가 "continuous"를 지원 안 한다면서 현재 settings는
 * "continuous"라고 답하는 모순된 값) - 웹 표준 API 레벨의 기기/브라우저
 * 버그라 JS 쪽에서 재요청을 아무리 해도 초점이 안 맞는다. CameraX(+Camera2)는
 * 브라우저의 이 제한된 추상화를 거치지 않고 시스템 카메라를 직접 제어하므로
 * 이 문제를 우회한다.
 *
 * MainActivity가 웹페이지에 심어주는 JS 인터페이스(AndroidScanner)를 통해서만
 * 여기로 들어오고, 인식된 바코드는 RESULT_BARCODE extra로 돌려준다.
 */
class ScanActivity : AppCompatActivity() {

    companion object {
        const val RESULT_BARCODE = "barcode"

        // 카메라 블러로 인한 순간적 오인식을 걸러내기 위해, 같은 값이 이만큼
        // 연속으로 잡혀야 확정한다 - 웹 버전(index.html)의 matchStreak와
        // 동일한 정책.
        private const val REQUIRED_MATCH_STREAK = 2
    }

    private lateinit var previewView: PreviewView
    private lateinit var hintText: TextView
    private lateinit var cameraExecutor: ExecutorService

    private var lastDecoded: String? = null
    private var matchStreak = 0
    private var resultSent = false

    private val requestCameraPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            startCamera()
        } else {
            // 권한이 없으면 스캔 자체가 의미 없으니 바로 닫는다 - 메인화면의
            // WebView 쪽 카메라 권한 안내와 중복해서 여기서 또 설명할 필요는
            // 없다(같은 CAMERA 권한을 공유하므로 한쪽에서 허용하면 계속 재요청
            // 안 뜸).
            finish()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_scan)

        previewView = findViewById(R.id.previewView)
        hintText = findViewById(R.id.hintText)
        findViewById<TextView>(R.id.closeBtn).setOnClickListener { finish() }

        cameraExecutor = Executors.newSingleThreadExecutor()

        val hasPermission = ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.CAMERA,
        ) == PackageManager.PERMISSION_GRANTED

        if (hasPermission) {
            startCamera()
        } else {
            requestCameraPermission.launch(Manifest.permission.CAMERA)
        }
    }

    private fun startCamera() {
        val cameraProviderFuture = ProcessCameraProvider.getInstance(this)
        cameraProviderFuture.addListener(
            {
                val cameraProvider = cameraProviderFuture.get()

                val preview = Preview.Builder().build().also {
                    it.setSurfaceProvider(previewView.surfaceProvider)
                }

                // 실제 상품 바코드(EAN/UPC)와 QR코드를 우선순위로 - 웹
                // 버전(index.html)의 formatsToSupport와 동일한 목록.
                val scannerOptions = BarcodeScannerOptions.Builder()
                    .setBarcodeFormats(
                        Barcode.FORMAT_EAN_13,
                        Barcode.FORMAT_EAN_8,
                        Barcode.FORMAT_UPC_A,
                        Barcode.FORMAT_UPC_E,
                        Barcode.FORMAT_CODE_128,
                        Barcode.FORMAT_CODE_39,
                        Barcode.FORMAT_CODABAR,
                        Barcode.FORMAT_ITF,
                        Barcode.FORMAT_QR_CODE,
                    )
                    .build()
                val scanner = BarcodeScanning.getClient(scannerOptions)

                val imageAnalysis = ImageAnalysis.Builder()
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .build()
                imageAnalysis.setAnalyzer(cameraExecutor) { imageProxy ->
                    processImage(imageProxy, scanner)
                }

                try {
                    cameraProvider.unbindAll()
                    cameraProvider.bindToLifecycle(
                        this,
                        CameraSelector.DEFAULT_BACK_CAMERA,
                        preview,
                        imageAnalysis,
                    )
                } catch (e: Exception) {
                    hintText.text = "카메라를 시작할 수 없습니다."
                }
            },
            ContextCompat.getMainExecutor(this),
        )
    }

    @ExperimentalGetImage
    private fun processImage(
        imageProxy: ImageProxy,
        scanner: com.google.mlkit.vision.barcode.BarcodeScanner,
    ) {
        val mediaImage = imageProxy.image
        if (mediaImage == null) {
            imageProxy.close()
            return
        }
        val image = InputImage.fromMediaImage(mediaImage, imageProxy.imageInfo.rotationDegrees)
        scanner.process(image)
            .addOnSuccessListener { barcodes ->
                if (!resultSent) {
                    handleBarcodes(barcodes)
                }
            }
            .addOnCompleteListener {
                imageProxy.close()
            }
    }

    private fun handleBarcodes(barcodes: List<Barcode>) {
        val value = barcodes.firstOrNull { !it.rawValue.isNullOrBlank() }?.rawValue ?: return

        // 체크섬이 안 맞는 코드는 블러로 인한 확실한 오인식이므로 무시하고
        // 계속 스캔한다 - 웹 버전(index.html)의 isPlausibleBarcode와 동일한
        // GS1(EAN/UPC) 체크섬 검증.
        if (!isPlausibleBarcode(value)) return

        if (value == lastDecoded) {
            matchStreak += 1
        } else {
            lastDecoded = value
            matchStreak = 1
        }
        if (matchStreak < REQUIRED_MATCH_STREAK) return

        resultSent = true
        runOnUiThread {
            val data = Intent().putExtra(RESULT_BARCODE, value)
            setResult(RESULT_OK, data)
            finish()
        }
    }

    private fun isPlausibleBarcode(code: String): Boolean {
        if (!code.all { it.isDigit() }) return true
        if (code.length != 8 && code.length != 12 && code.length != 13 && code.length != 14) return true

        // 8자리는 EAN-8일 수도, UPC-E(작은 포장에 흔한 압축형 UPC)일 수도
        // 있다 - 아래 표준 체크섬(뒤에서부터 3,1 가중치)은 EAN-8엔 맞지만
        // UPC-E엔 안 맞는다(UPC-E는 12자리 UPC-A로 먼저 복원한 뒤 그 값으로
        // 체크섬을 계산해야 함). 초콜릿 등 작은 포장 상품의 UPC-E가 전부
        // "체크섬 불일치"로 걸러지는 문제가 있어, 8자리는 둘 다 시도해서
        // 하나라도 맞으면 통과시킨다.
        if (code.length == 8 && isValidUpcE(code)) return true

        val digits = code.map { it - '0' }
        val checkDigit = digits.last()
        val body = digits.dropLast(1).reversed()
        val total = body.foldIndexed(0) { index, acc, d ->
            acc + d * (if (index % 2 == 0) 3 else 1)
        }
        return (10 - total % 10) % 10 == checkDigit
    }

    // UPC-E(8자리: 시스템자릿수 1 + 압축된 6자리 + 체크숫자 1)를 표준 규칙으로
    // UPC-A(12자리)로 복원한 뒤 그 체크숫자가 맞는지 확인한다.
    private fun isValidUpcE(code: String): Boolean {
        val d = code.map { it - '0' }
        val n = d[0]; val x1 = d[1]; val x2 = d[2]; val x3 = d[3]
        val x4 = d[4]; val x5 = d[5]; val x6 = d[6]; val c = d[7]
        if (n != 0 && n != 1) return false // UPC-E는 항상 0 또는 1로 시작

        val upcA11 = when {
            x6 <= 2 -> listOf(n, x1, x2, x6, 0, 0, 0, 0, x3, x4, x5)
            x6 == 3 -> listOf(n, x1, x2, x3, 0, 0, 0, 0, 0, x4, x5)
            x6 == 4 -> listOf(n, x1, x2, x3, x4, 0, 0, 0, 0, 0, x5)
            else -> listOf(n, x1, x2, x3, x4, x5, 0, 0, 0, 0, x6)
        }
        val total = upcA11.foldIndexed(0) { index, acc, digit ->
            acc + digit * (if (index % 2 == 0) 3 else 1)
        }
        return (10 - total % 10) % 10 == c
    }

    override fun onDestroy() {
        super.onDestroy()
        cameraExecutor.shutdown()
    }
}
