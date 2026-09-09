package kr.co.iscream.barcodesite

import android.app.AlertDialog
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.ArrayAdapter
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import kotlin.concurrent.thread

/**
 * 오더퀸 자동등록 관련 다이얼로그 2개 - 설정(계정 저장/해제)과 상품 등록.
 * 레이아웃 XML 없이 코드로 직접 구성한다(다이얼로그 안에 필드가 몇 개 안
 * 되는 단순한 폼이라 별도 XML을 두는 것보다 여기 한 파일로 모아두는 쪽이
 * 관리하기 쉽다).
 */
object OrderQueenDialogs {

    private fun dp(activity: AppCompatActivity, value: Int): Int =
        (value * activity.resources.displayMetrics.density).toInt()

    fun showSettingsDialog(activity: AppCompatActivity) {
        val padding = dp(activity, 20)
        val root = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, 0)
        }

        val statusText = TextView(activity).apply {
            text = "현재 상태 확인 중..."
            setPadding(0, 0, 0, dp(activity, 16))
        }
        root.addView(statusText)

        val enableCheck = CheckBox(activity).apply {
            text = "오더퀸 자동등록 사용"
            isEnabled = false
        }
        root.addView(enableCheck)

        val idInput = EditText(activity).apply {
            hint = "오더퀸 아이디"
            visibility = View.GONE
        }
        root.addView(idInput)

        val pwInput = EditText(activity).apply {
            hint = "오더퀸 비밀번호"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            visibility = View.GONE
        }
        root.addView(pwInput)

        val hintText = TextView(activity).apply {
            text = "켜면 스캔한 상품을 오더퀸에 바로 등록하는 버튼이 검색결과에 나타납니다. " +
                "아이디/비밀번호는 암호화되어 서버에 저장됩니다."
            textSize = 12f
            setPadding(0, dp(activity, 8), 0, 0)
            alpha = 0.7f
        }
        root.addView(hintText)

        fun applyIdPwVisibility(show: Boolean) {
            idInput.visibility = if (show) View.VISIBLE else View.GONE
            pwInput.visibility = if (show) View.VISIBLE else View.GONE
        }

        enableCheck.setOnCheckedChangeListener { _, checked -> applyIdPwVisibility(checked) }

        val dialog = AlertDialog.Builder(activity)
            .setTitle("오더퀸 자동등록 설정")
            .setView(root)
            .setPositiveButton("저장", null)
            .setNegativeButton("취소", null)
            .create()
        dialog.show()

        // 저장 버튼을 누르자마자 dialog가 닫히면 실패 메시지를 보여줄 수 없으니,
        // setOnShowListener에서 버튼 클릭 리스너를 직접 걸어 필요할 때만 닫는다.
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
            val wantsEnabled = enableCheck.isChecked
            if (!wantsEnabled) {
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false
                thread {
                    OrderQueenManager.deleteCredentials(activity)
                    OrderQueenManager.setEnabledLocally(activity, false)
                    activity.runOnUiThread {
                        Toast.makeText(activity, "오더퀸 자동등록을 껐습니다.", Toast.LENGTH_SHORT).show()
                        dialog.dismiss()
                    }
                }
                return@setOnClickListener
            }

            val loginId = idInput.text.toString().trim()
            val loginPwd = pwInput.text.toString()
            if (loginId.isEmpty() || loginPwd.isEmpty()) {
                Toast.makeText(activity, "아이디와 비밀번호를 입력해주세요.", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }

            dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false
            thread {
                val result = OrderQueenManager.saveCredentials(activity, loginId, loginPwd)
                activity.runOnUiThread {
                    result.onSuccess {
                        OrderQueenManager.setEnabledLocally(activity, true)
                        Toast.makeText(activity, "오더퀸 자동등록을 켰습니다.", Toast.LENGTH_SHORT).show()
                        dialog.dismiss()
                    }.onFailure { e ->
                        dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
                        Toast.makeText(activity, "저장 실패: ${e.message}", Toast.LENGTH_LONG).show()
                    }
                }
            }
        }

        // 현재 서버에 저장된 상태를 비동기로 조회해서 체크박스 초기값을 맞춘다.
        thread {
            val registered = OrderQueenManager.fetchRegisteredStatus(activity)
            OrderQueenManager.setEnabledLocally(activity, registered)
            activity.runOnUiThread {
                statusText.text = if (registered) "현재 자동등록이 켜져 있습니다." else "현재 자동등록이 꺼져 있습니다."
                enableCheck.isEnabled = true
                enableCheck.isChecked = registered
                applyIdPwVisibility(registered)
            }
        }
    }

    fun showRegisterDialog(activity: AppCompatActivity, barcode: String, name: String, price: String) {
        val padding = dp(activity, 20)
        val root = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, 0)
        }

        root.addView(TextView(activity).apply {
            text = "바코드: $barcode"
            setPadding(0, 0, 0, dp(activity, 12))
            alpha = 0.7f
        })

        val nameInput = EditText(activity).apply {
            hint = "상품명"
            setText(name)
        }
        root.addView(nameInput)

        val priceInput = EditText(activity).apply {
            hint = "판매가"
            inputType = InputType.TYPE_CLASS_NUMBER
            setText(price.filter { it.isDigit() })
        }
        root.addView(priceInput)

        root.addView(TextView(activity).apply {
            text = "분류"
            setPadding(0, dp(activity, 12), 0, dp(activity, 4))
        })

        val classCodes = OrderQueenManager.DEFAULT_CLASS_CODES
        val classNames = classCodes.keys.toList()
        val spinner = Spinner(activity).apply {
            adapter = ArrayAdapter<String>(activity, android.R.layout.simple_spinner_dropdown_item, classNames)
        }
        root.addView(spinner)

        val progress = ProgressBar(activity).apply {
            visibility = View.GONE
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { gravity = Gravity.CENTER; topMargin = dp(activity, 12) }
        }
        root.addView(progress)

        val dialog = AlertDialog.Builder(activity)
            .setTitle("오더퀸에 등록")
            .setView(root)
            .setPositiveButton("등록", null)
            .setNegativeButton("취소", null)
            .create()
        dialog.show()

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
            val menuName = nameInput.text.toString().trim()
            val salePrice = priceInput.text.toString().toIntOrNull()
            if (menuName.isEmpty()) {
                Toast.makeText(activity, "상품명을 입력해주세요.", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            if (salePrice == null) {
                Toast.makeText(activity, "판매가를 숫자로 입력해주세요.", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            val classCd = classCodes[classNames[spinner.selectedItemPosition]] ?: classNames.firstOrNull()

            dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false
            dialog.getButton(AlertDialog.BUTTON_NEGATIVE).isEnabled = false
            progress.visibility = View.VISIBLE

            thread {
                val result = OrderQueenManager.registerItem(activity, barcode, menuName, salePrice, classCd ?: "")
                activity.runOnUiThread {
                    result.onSuccess {
                        Toast.makeText(activity, "오더퀸에 등록됐습니다.", Toast.LENGTH_SHORT).show()
                        dialog.dismiss()
                    }.onFailure { e ->
                        progress.visibility = View.GONE
                        dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
                        dialog.getButton(AlertDialog.BUTTON_NEGATIVE).isEnabled = true
                        Toast.makeText(activity, "등록 실패: ${e.message}", Toast.LENGTH_LONG).show()
                    }
                }
            }
        }
    }
}
