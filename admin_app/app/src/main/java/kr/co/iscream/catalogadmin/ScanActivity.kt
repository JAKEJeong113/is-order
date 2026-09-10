package kr.co.iscream.catalogadmin

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
 * 시스템 카메라(CameraX + ML Kit)로 바코드를 스캔한다 - 무인 바코드 검색기
 * 앱의 ScanActivity와 동일. MainActivity가 웹페이지에 심어주는 JS
 * 인터페이스(AndroidScanner)로만 진입하고, 인식된 바코드는 RESULT_BARCODE
 * extra로 돌려준다.
 */
class ScanActivity : AppCompatActivity() {

    companion object {
        const val RESULT_BARCODE = "barcode"
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
        if (granted) startCamera() else finish()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_scan)

        previewView = findViewById(R.id.previewView)
        hintText = findViewById(R.id.hintText)
        findViewById<TextView>(R.id.closeBtn).setOnClickListener { finish() }

        cameraExecutor = Executors.newSingleThreadExecutor()

        val hasPermission = ContextCompat.checkSelfPermission(
            this, Manifest.permission.CAMERA,
        ) == PackageManager.PERMISSION_GRANTED

        if (hasPermission) startCamera() else requestCameraPermission.launch(Manifest.permission.CAMERA)
    }

    private fun startCamera() {
        val cameraProviderFuture = ProcessCameraProvider.getInstance(this)
        cameraProviderFuture.addListener(
            {
                val cameraProvider = cameraProviderFuture.get()

                val preview = Preview.Builder().build().also {
                    it.setSurfaceProvider(previewView.surfaceProvider)
                }

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
                        this, CameraSelector.DEFAULT_BACK_CAMERA, preview, imageAnalysis,
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
                if (!resultSent) handleBarcodes(barcodes)
            }
            .addOnCompleteListener { imageProxy.close() }
    }

    private fun handleBarcodes(barcodes: List<Barcode>) {
        val value = barcodes.firstOrNull { !it.rawValue.isNullOrBlank() }?.rawValue ?: return
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
            setResult(RESULT_OK, Intent().putExtra(RESULT_BARCODE, value))
            finish()
        }
    }

    private fun isPlausibleBarcode(code: String): Boolean {
        if (!code.all { it.isDigit() }) return true
        if (code.length != 8 && code.length != 12 && code.length != 13 && code.length != 14) return true
        val digits = code.map { it - '0' }
        val checkDigit = digits.last()
        val body = digits.dropLast(1).reversed()
        val total = body.foldIndexed(0) { index, acc, d -> acc + d * (if (index % 2 == 0) 3 else 1) }
        return (10 - total % 10) % 10 == checkDigit
    }

    override fun onDestroy() {
        super.onDestroy()
        cameraExecutor.shutdown()
    }
}
