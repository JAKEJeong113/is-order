package kr.co.iscream.barcodesite

import android.app.Dialog
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.Window
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.RecyclerView
import androidx.viewpager2.widget.ViewPager2

/**
 * 상단 "?" 버튼을 누르면 뜨는 사용법 안내 - 화면 전체를 덮는 반투명 오버레이
 * 위에 슬라이드 카드를 두고, 옆으로 넘기면서(ViewPager2) 기능을 하나씩
 * 소개하는 튜토리얼 형태. 별도 XML 레이아웃 없이 다른 다이얼로그들
 * (OrderQueenDialogs)과 같은 방식으로 코드에서 뷰를 직접 구성한다.
 */
object HelpDialogs {

    private data class Slide(val icon: String, val title: String, val desc: String)

    private val SLIDES = listOf(
        Slide("🔍", "검색", "바코드 번호나 상품명을 입력하고 \"검색\"을 누르면 등록된 추천판매가를 바로 확인할 수 있어요."),
        Slide("📷", "스캔", "\"스캔\" 버튼을 누르고 카메라로 바코드를 비추면 자동으로 인식해서 검색해줘요."),
        Slide("📋", "바코드 복사", "검색 결과를 탭하면 바코드 번호가 클립보드에 복사돼요. 도매몰 등에 상품을 등록할 때 바로 붙여넣기 하면 됩니다."),
        Slide("🆕", "신제품 안내", "최근 4주 이내 새로 등록된 상품을 카테고리별로 모아볼 수 있어요."),
        Slide("⚙️", "오더퀸 자동등록", "오른쪽 위 톱니 버튼에서 오더퀸 계정을 연결해두면, 검색 결과의 \"오더퀸 등록\" 버튼 하나로 오더퀸 관리자 페이지에 상품을 자동으로 등록할 수 있어요."),
    )

    private fun dp(activity: AppCompatActivity, value: Int): Int =
        (value * activity.resources.displayMetrics.density).toInt()

    private fun dot(active: Boolean) = GradientDrawable().apply {
        shape = GradientDrawable.OVAL
        setColor(if (active) Color.parseColor("#2FC5B3") else Color.parseColor("#8FA39D"))
    }

    private class SlideAdapter(private val activity: AppCompatActivity) :
        RecyclerView.Adapter<SlideAdapter.VH>() {

        inner class VH(val icon: TextView, val title: TextView, val desc: TextView, root: View) :
            RecyclerView.ViewHolder(root)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
            val pad = dp(activity, 28)
            val card = LinearLayout(activity).apply {
                orientation = LinearLayout.VERTICAL
                gravity = Gravity.CENTER
                setPadding(pad, pad, pad, pad)
                background = GradientDrawable().apply {
                    cornerRadius = dp(activity, 20).toFloat()
                    setColor(Color.WHITE)
                }
            }
            val icon = TextView(activity).apply {
                textSize = 40f
                gravity = Gravity.CENTER
            }
            val title = TextView(activity).apply {
                textSize = 19f
                setTextColor(Color.parseColor("#16211F"))
                typeface = Typeface.DEFAULT_BOLD
                gravity = Gravity.CENTER
                setPadding(0, dp(activity, 14), 0, dp(activity, 10))
            }
            val desc = TextView(activity).apply {
                textSize = 14.5f
                setTextColor(Color.parseColor("#5C6A5E"))
                gravity = Gravity.CENTER
                setLineSpacing(dp(activity, 3).toFloat(), 1f)
            }
            card.addView(icon)
            card.addView(title)
            card.addView(desc)

            // ViewPager2 페이지 사이 여백은 카드 좌우 margin으로 흉내낸다
            // (ViewPager2 자체엔 item spacing API가 마땅치 않아 이 방식이 간단함).
            val wrapper = FrameLayout(activity).apply {
                layoutParams = ViewGroup.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT,
                )
                val m = dp(activity, 20)
                addView(card, FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                ).apply { setMargins(m, 0, m, 0); gravity = Gravity.CENTER_VERTICAL })
            }
            return VH(icon, title, desc, wrapper)
        }

        override fun onBindViewHolder(holder: VH, position: Int) {
            val slide = SLIDES[position]
            holder.icon.text = slide.icon
            holder.title.text = slide.title
            holder.desc.text = slide.desc
        }

        override fun getItemCount() = SLIDES.size
    }

    fun showUsageGuide(activity: AppCompatActivity) {
        val dialog = Dialog(activity)
        dialog.requestWindowFeature(Window.FEATURE_NO_TITLE)
        dialog.window?.setBackgroundDrawableResource(android.R.color.transparent)

        val root = FrameLayout(activity).apply {
            setBackgroundColor(Color.parseColor("#CC10201C"))
        }

        val pager = ViewPager2(activity).apply {
            adapter = SlideAdapter(activity)
        }

        val dotsRow = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
        }
        val dotSize = dp(activity, 7)
        SLIDES.indices.forEach { i ->
            dotsRow.addView(View(activity).apply {
                background = dot(i == 0)
                layoutParams = LinearLayout.LayoutParams(dotSize, dotSize).apply {
                    marginStart = dp(activity, 4); marginEnd = dp(activity, 4)
                }
            })
        }
        pager.registerOnPageChangeCallback(object : ViewPager2.OnPageChangeCallback() {
            override fun onPageSelected(position: Int) {
                for (i in 0 until dotsRow.childCount) {
                    dotsRow.getChildAt(i).background = dot(i == position)
                }
            }
        })

        val centerBlock = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            addView(pager, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(activity, 340),
            ))
            addView(dotsRow, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { gravity = Gravity.CENTER_HORIZONTAL; topMargin = dp(activity, 18) })
        }

        val closeBtn = TextView(activity).apply {
            text = "닫기"
            textSize = 15f
            setTextColor(Color.WHITE)
            typeface = Typeface.DEFAULT_BOLD
            gravity = Gravity.CENTER
            val padH = dp(activity, 40); val padV = dp(activity, 13)
            setPadding(padH, padV, padH, padV)
            background = GradientDrawable().apply {
                shape = GradientDrawable.RECTANGLE
                cornerRadius = dp(activity, 999).toFloat()
                setColor(Color.parseColor("#33FFFFFF"))
                setStroke(dp(activity, 1), Color.WHITE)
            }
            setOnClickListener { dialog.dismiss() }
        }

        root.addView(centerBlock, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { gravity = Gravity.CENTER })

        root.addView(closeBtn, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { gravity = Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL; bottomMargin = dp(activity, 40) })

        // 오버레이 바깥(반투명 배경) 탭으로도 닫히게 - 튜토리얼은 흔히 그렇게
        // 동작해서 자연스럽다. 카드/닫기 버튼 클릭은 각자 리스너가 먼저
        // 소비하므로 여기까지 안 내려온다.
        root.setOnClickListener { dialog.dismiss() }

        dialog.setContentView(root)
        dialog.window?.setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
        dialog.show()
    }
}
