package kr.co.iscream.barcodesite

import android.graphics.RectF
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.doOnLayout
import org.json.JSONObject
import kotlin.math.abs

/**
 * "?" 버튼 튜토리얼 - 카드 없이, 실제 버튼/입력창 자리를 화면에서 그대로
 * 밝게 뚫어서 보여주고(SpotlightOverlayView) 선 하나로 설명 텍스트와
 * 잇는다(참고: idus 등 앱의 온보딩 스타일).
 *
 * 검색창/스캔/신제품 안내 버튼처럼 WebView 안의 HTML 요소는 id로 찾아
 * getBoundingClientRect()로 화면 좌표(CSS px)를 얻은 뒤 density를 곱해
 * 기기 px로 바꾸고, 오더퀸 설정(톱니) 버튼처럼 네이티브 뷰는
 * getLocationOnScreen()으로 바로 얻는다. 두 좌표 모두 최종적으로
 * coachMarkRoot 프레임 기준 로컬 좌표로 맞춰서 한 오버레이에 자유롭게
 * 섞어 쓴다.
 *
 * "바코드 복사" 단계는 검색 결과가 있어야 가리킬 대상이 생기므로, 그
 * 단계에 들어갈 때만 예시 상품 1개를 #results에 임시로 넣고 다른 단계로
 * 넘어가거나 튜토리얼을 닫으면 지운다(실제 데이터 아님).
 */
object CoachMarkTutorial {

    private sealed class Target {
        data class Native(val view: View) : Target()
        data class Web(val elementId: String, val injectSample: Boolean = false) : Target()
    }

    private data class Step(val icon: String, val title: String, val desc: String, val target: Target)

    private const val SAMPLE_ROW_ID = "__coachSampleRow"

    /** 뒤로가기 등 외부에서 튜토리얼을 강제로 닫을 때 쓴다(진행 상태와 무관). */
    fun forceClose(webView: WebView, root: FrameLayout) {
        root.removeAllViews()
        root.visibility = View.GONE
        val js = "(function(){var el=document.getElementById('$SAMPLE_ROW_ID');" +
            "if(el && el.parentElement){el.parentElement.innerHTML='';}})();"
        webView.evaluateJavascript(js, null)
    }

    fun start(activity: AppCompatActivity, webView: WebView, root: FrameLayout, gearBtn: View) {
        val steps = listOf(
            Step(
                "🔍", "검색",
                "바코드 번호나 상품명을 입력하고 \"검색\"을 누르면 등록된 추천판매가를 바로 확인할 수 있어요.",
                Target.Web("q"),
            ),
            Step(
                "📷", "스캔",
                "\"스캔\" 버튼을 누르고 카메라로 바코드를 비추면 자동으로 인식해서 검색해줘요.",
                Target.Web("scanBtn"),
            ),
            Step(
                "📋", "바코드 복사",
                "검색 결과를 탭하면 바코드 번호가 클립보드에 복사돼요. 키오스크에 상품을 등록할 때 바로 붙여넣기 하면 됩니다.",
                Target.Web(SAMPLE_ROW_ID, injectSample = true),
            ),
            Step(
                "🆕", "신제품 안내",
                "최근 4주 이내 새로 등록된 상품을 카테고리별로 모아볼 수 있어요.",
                Target.Web("newProductsBtn"),
            ),
            Step(
                "⚙️", "오더퀸 자동등록",
                "오른쪽 위 톱니 버튼에서 오더퀸 계정을 연결해두면, 검색 결과의 \"오더퀸 등록\" 버튼 하나로 오더퀸 관리자 페이지에 상품을 자동으로 등록할 수 있어요.",
                Target.Native(gearBtn),
            ),
        )

        val density = activity.resources.displayMetrics.density
        fun dp(v: Int): Float = v * density

        val spotlight = SpotlightOverlayView(activity)
        root.addView(
            spotlight,
            FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT),
        )

        val shadowColor = 0x59000000
        val titleView = TextView(activity).apply {
            textSize = 15.5f
            setTextColor(0xFFFFFFFF.toInt())
            typeface = Typeface.DEFAULT_BOLD
            setShadowLayer(dp(6), 0f, dp(1), shadowColor)
        }
        val descView = TextView(activity).apply {
            textSize = 12.5f
            setTextColor(0xFFEEF3F1.toInt())
            setShadowLayer(dp(6), 0f, dp(1), shadowColor)
            setLineSpacing(dp(3), 1f)
            setPadding(0, dp(4).toInt(), 0, 0)
        }
        val captionBox = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            addView(titleView)
            addView(descView)
        }
        root.addView(
            captionBox,
            FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT),
        )

        fun dotDrawable(active: Boolean) = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(if (active) 0xFF2FC5B3.toInt() else 0x59FFFFFF)
        }
        val dotsRow = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.START or Gravity.CENTER_VERTICAL
        }
        val dotSize = dp(7).toInt()
        steps.indices.forEach {
            dotsRow.addView(
                View(activity).apply { background = dotDrawable(false) },
                LinearLayout.LayoutParams(dotSize, dotSize).apply {
                    marginStart = dp(4).toInt(); marginEnd = dp(4).toInt()
                },
            )
        }
        val closeBtn = TextView(activity).apply {
            text = "닫기"
            textSize = 12.5f
            setTextColor(0xBFFFFFFF.toInt())
            typeface = Typeface.DEFAULT_BOLD
        }
        val nextBtn = TextView(activity).apply {
            text = "다음"
            textSize = 12.5f
            setTextColor(0xFFFFFFFF.toInt())
            typeface = Typeface.DEFAULT_BOLD
            val padH = dp(18).toInt(); val padV = dp(9).toInt()
            setPadding(padH, padV, padH, padV)
            background = GradientDrawable().apply {
                cornerRadius = dp(999)
                setColor(0xFF2FC5B3.toInt())
            }
        }
        val ctrlsRow = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(closeBtn, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { marginEnd = dp(18).toInt() })
            addView(nextBtn)
        }
        val bottomBar = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(20).toInt(), 0, dp(20).toInt(), 0)
            addView(dotsRow, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(ctrlsRow)
        }
        root.addView(
            bottomBar,
            FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply {
                gravity = Gravity.BOTTOM
                bottomMargin = dp(24).toInt()
            },
        )

        var step = 0
        val rootLoc = IntArray(2)

        fun cleanupSampleRow(after: () -> Unit) {
            val js = "(function(){var el=document.getElementById('$SAMPLE_ROW_ID');" +
                "if(el && el.parentElement){el.parentElement.innerHTML='';}})();"
            webView.evaluateJavascript(js) { after() }
        }

        fun close() {
            root.removeView(spotlight)
            root.removeView(captionBox)
            root.removeView(bottomBar)
            root.visibility = View.GONE
            cleanupSampleRow {}
        }

        fun measureNative(view: View): RectF {
            val loc = IntArray(2)
            view.getLocationOnScreen(loc)
            root.getLocationOnScreen(rootLoc)
            val l = (loc[0] - rootLoc[0]).toFloat()
            val t = (loc[1] - rootLoc[1]).toFloat()
            return RectF(l, t, l + view.width, t + view.height)
        }

        fun measureWeb(elementId: String, injectSample: Boolean, cb: (RectF?) -> Unit) {
            val js = buildString {
                append("(function(){")
                if (injectSample) {
                    append("var results=document.getElementById('results');")
                    append("if(results && !document.getElementById('$elementId')){")
                    append(
                        "results.innerHTML='<div class=\"result-row\" id=\"$elementId\">" +
                            "<div><p class=\"result-name\">메로나</p><p class=\"result-barcode\">8801019110113</p></div>" +
                            "<div class=\"result-side\"><span class=\"result-price\">1,000원</span></div></div>';",
                    )
                    append("}")
                }
                append("var el=document.getElementById('$elementId');")
                append("if(!el) return null;")
                append("var r=el.getBoundingClientRect();")
                append("return {left:r.left, top:r.top, right:r.right, bottom:r.bottom};")
                append("})();")
            }
            webView.evaluateJavascript(js) { raw ->
                if (raw == null || raw == "null") { cb(null); return@evaluateJavascript }
                try {
                    val obj = JSONObject(raw)
                    val wLoc = IntArray(2); webView.getLocationOnScreen(wLoc)
                    root.getLocationOnScreen(rootLoc)
                    val left = wLoc[0] - rootLoc[0] + obj.getDouble("left").toFloat() * density
                    val top = wLoc[1] - rootLoc[1] + obj.getDouble("top").toFloat() * density
                    val right = wLoc[0] - rootLoc[0] + obj.getDouble("right").toFloat() * density
                    val bottom = wLoc[1] - rootLoc[1] + obj.getDouble("bottom").toFloat() * density
                    cb(RectF(left, top, right, bottom))
                } catch (e: Exception) {
                    cb(null)
                }
            }
        }

        fun layoutCaption(rect: RectF) {
            val rootW = root.width.toFloat()
            val rootH = root.height.toFloat()
            if (rootW <= 0 || rootH <= 0) return

            val maxW = (rootW * 0.76f).toInt()
            captionBox.measure(
                View.MeasureSpec.makeMeasureSpec(maxW, View.MeasureSpec.AT_MOST),
                View.MeasureSpec.UNSPECIFIED,
            )
            val cW = captionBox.measuredWidth
            val cH = captionBox.measuredHeight

            val tCenterX = (rect.left + rect.right) / 2f
            val hAlign = when {
                tCenterX < rootW * 0.38f -> 0 // left
                tCenterX > rootW * 0.62f -> 2 // right
                else -> 1 // center
            }
            val textGravity = when (hAlign) { 0 -> Gravity.START; 2 -> Gravity.END; else -> Gravity.CENTER_HORIZONTAL }
            titleView.gravity = textGravity
            descView.gravity = textGravity

            val gap = dp(16)
            val reservedBottom = dp(80)
            var placeBelow = rect.top < rootH * 0.5f
            if (placeBelow && rect.bottom + gap + cH > rootH - reservedBottom) placeBelow = false
            val top = if (placeBelow) rect.bottom + gap else rect.top - gap - cH

            val lp = FrameLayout.LayoutParams(cW, cH)
            lp.topMargin = maxOf(dp(8), top).toInt()
            lp.leftMargin = when (hAlign) {
                0 -> dp(18).toInt()
                2 -> (rootW - cW - dp(18)).toInt()
                else -> ((rootW - cW) / 2f).toInt()
            }
            captionBox.layoutParams = lp

            val lineX = tCenterX.coerceIn(lp.leftMargin + dp(6), (lp.leftMargin + cW) - dp(6))
            val fromY = if (placeBelow) rect.bottom else rect.top
            val toY = if (placeBelow) lp.topMargin.toFloat() else (lp.topMargin + cH).toFloat()
            spotlight.update(rect, Pair(Pair(tCenterX, fromY), Pair(lineX, toY)))
        }

        fun renderStep(i: Int) {
            step = i
            val s = steps[i]
            titleView.text = "${s.icon} ${s.title}"
            descView.text = s.desc
            nextBtn.text = if (i == steps.size - 1) "확인" else "다음"
            for (d in 0 until dotsRow.childCount) {
                dotsRow.getChildAt(d).background = dotDrawable(d == i)
            }

            cleanupSampleRow {
                when (val t = s.target) {
                    is Target.Native -> layoutCaption(measureNative(t.view))
                    is Target.Web -> measureWeb(t.elementId, t.injectSample) { rect ->
                        activity.runOnUiThread { if (rect != null) layoutCaption(rect) }
                    }
                }
            }
        }

        fun goNext() { if (step >= steps.size - 1) close() else renderStep(step + 1) }
        fun goPrev() { if (step > 0) renderStep(step - 1) }

        nextBtn.setOnClickListener { goNext() }
        closeBtn.setOnClickListener { close() }

        var downX = 0f
        var downY = 0f
        var downT = 0L
        spotlight.setOnTouchListener { _, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downX = event.rawX; downY = event.rawY; downT = System.currentTimeMillis()
                }
                MotionEvent.ACTION_UP -> {
                    val dx = event.rawX - downX
                    val dy = event.rawY - downY
                    val dt = System.currentTimeMillis() - downT
                    if (abs(dx) > dp(40) && abs(dx) > abs(dy) * 1.5f && dt < 600) {
                        if (dx < 0) goNext() else goPrev()
                    }
                }
            }
            true
        }

        root.visibility = View.VISIBLE
        root.doOnLayout { renderStep(0) }
    }
}
