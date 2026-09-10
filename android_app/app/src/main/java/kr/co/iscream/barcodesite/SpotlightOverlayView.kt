package kr.co.iscream.barcodesite

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.PorterDuff
import android.graphics.PorterDuffXfermode
import android.graphics.RectF
import android.view.View

/**
 * 코치마크 튜토리얼의 어두운 배경 + "구멍" 하나 - 화면 전체를 반투명하게
 * 덮되, 지금 가리키는 버튼 자리만 뚫어서 원래 밝기 그대로 보이게 한다
 * (전형적인 스포트라이트 기법: 자체 비트맵에 어둠을 채운 뒤 그 자리만
 * PorterDuff.CLEAR로 지우고, 그 비트맵을 뷰의 실제 캔버스에 합성한다 -
 * 하드웨어 가속 캔버스는 CLEAR를 직접 지원하지 않아서 비트맵을 거쳐야 함).
 * 구멍 테두리에 포인트 컬러 링을, 구멍에서 설명 텍스트까지 연결선을 같이
 * 그린다.
 */
class SpotlightOverlayView(context: Context) : View(context) {

    private val density = context.resources.displayMetrics.density
    private fun dp(v: Float) = v * density

    private val scrimColor = Color.argb(189, 15, 26, 23) // rgba(15,26,23,.74) 근사
    private val ringColor = Color.parseColor("#2FC5B3")
    private val lineColor = Color.parseColor("#2FC5B3")

    private val clearPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        xfermode = PorterDuffXfermode(PorterDuff.Mode.CLEAR)
    }
    private val ringPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = dp(2.5f)
        color = ringColor
    }
    private val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = dp(2f)
        color = lineColor
    }
    private val dotPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = lineColor
    }

    private var bitmap: Bitmap? = null
    private var bitmapCanvas: Canvas? = null

    /** 구멍(하이라이트) 위치/모양. null이면 전체를 그냥 어둡게만 덮는다. */
    var holeRect: RectF? = null
    var holeRadius: Float = dp(10f)

    /** 구멍 가장자리(라인이 시작하는 점)에서 캡션 쪽 한 점까지 이어지는 선. null이면 선을 안 그림. */
    var linePoints: Pair<Pair<Float, Float>, Pair<Float, Float>>? = null

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        if (w <= 0 || h <= 0) return
        bitmap = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        bitmapCanvas = Canvas(bitmap!!)
    }

    fun update(hole: RectF?, line: Pair<Pair<Float, Float>, Pair<Float, Float>>?) {
        holeRect = hole
        linePoints = line
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        val bmp = bitmap ?: return
        val bc = bitmapCanvas ?: return
        bmp.eraseColor(Color.TRANSPARENT)
        bc.drawColor(scrimColor)
        holeRect?.let { r -> bc.drawRoundRect(r, holeRadius, holeRadius, clearPaint) }
        canvas.drawBitmap(bmp, 0f, 0f, null)

        holeRect?.let { r -> canvas.drawRoundRect(r, holeRadius, holeRadius, ringPaint) }
        linePoints?.let { (from, to) ->
            canvas.drawLine(from.first, from.second, to.first, to.second, linePaint)
            canvas.drawCircle(from.first, from.second, dp(3.2f), dotPaint)
        }
    }
}
