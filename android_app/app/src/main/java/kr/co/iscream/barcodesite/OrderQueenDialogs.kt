package kr.co.iscream.barcodesite

import android.app.AlertDialog
import android.graphics.Typeface
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
 * 오더퀸 자동등록 관련 다이얼로그 2개 - 설정(계정 여러 개 등록/삭제)과
 * 상품 등록(계정 선택 포함). 레이아웃 XML 없이 코드로 직접 구성한다(필드가
 * 몇 개 안 되는 단순한 폼이라 별도 XML을 두는 것보다 여기 한 파일로 모아두는
 * 쪽이 관리하기 쉽다).
 */
object OrderQueenDialogs {

    private fun dp(activity: AppCompatActivity, value: Int): Int =
        (value * activity.resources.displayMetrics.density).toInt()

    // 다매장 점주는 매장마다 오더퀸 계정이 달라 계정을 여러 개 등록해야
    // 한다 - 계정명(별명)으로 구분해서 리스트로 관리하고, 계정이 하나라도
    // 있으면 "자동등록 사용" 상태로 본다(별도 온/오프 스위치 없음 - 전부
    // 지우면 자연히 꺼진 것과 같다).
    fun showSettingsDialog(activity: AppCompatActivity, onChanged: () -> Unit = {}) {
        val padding = dp(activity, 20)
        val root = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, padding)
        }

        val listContainer = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        root.addView(listContainer)

        val emptyHint = TextView(activity).apply {
            text = "등록된 계정이 없습니다. 아래에서 계정을 추가해주세요."
            textSize = 13f
            alpha = 0.65f
            setPadding(0, dp(activity, 4), 0, dp(activity, 8))
        }
        root.addView(emptyHint)

        val addAccountBtn = TextView(activity).apply {
            text = "+ 계정 추가"
            textSize = 14f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(android.graphics.Color.parseColor("#08796F"))
            setPadding(0, dp(activity, 10), 0, dp(activity, 10))
        }
        root.addView(addAccountBtn)

        val nicknameInput = EditText(activity).apply { hint = "계정명 (예: 강남점)" }
        val idInput = EditText(activity).apply { hint = "오더퀸 아이디" }
        val pwInput = EditText(activity).apply {
            hint = "오더퀸 비밀번호"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        val formCancelBtn = TextView(activity).apply {
            text = "취소"; textSize = 13f; alpha = 0.7f
            setPadding(dp(activity, 4), dp(activity, 10), dp(activity, 16), dp(activity, 4))
        }
        val formSaveBtn = TextView(activity).apply {
            text = "저장"; textSize = 13f; typeface = Typeface.DEFAULT_BOLD
            setTextColor(android.graphics.Color.parseColor("#08796F"))
            setPadding(dp(activity, 4), dp(activity, 10), dp(activity, 4), dp(activity, 4))
        }
        val formBtnRow = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.END
            addView(formCancelBtn)
            addView(formSaveBtn)
        }
        val addForm = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
            addView(nicknameInput)
            addView(idInput)
            addView(pwInput)
            addView(formBtnRow)
        }
        root.addView(addForm)

        root.addView(TextView(activity).apply {
            text = "등록한 계정은 검색결과의 \"오더퀸 등록\" 버튼을 누를 때 어느 매장에 등록할지 고를 수 있습니다. " +
                "아이디/비밀번호는 암호화되어 서버에 저장됩니다."
            textSize = 12f
            setPadding(0, dp(activity, 14), 0, 0)
            alpha = 0.7f
        })

        val dialog = AlertDialog.Builder(activity)
            .setTitle("오더퀸 자동등록 설정")
            .setView(root)
            .setNegativeButton("닫기", null)
            .create()
        dialog.show()

        fun clearForm() {
            nicknameInput.setText(""); idInput.setText(""); pwInput.setText("")
        }
        fun closeForm() {
            clearForm()
            addForm.visibility = View.GONE
            addAccountBtn.visibility = View.VISIBLE
        }

        fun renderAccounts(accounts: List<OqAccount>) {
            listContainer.removeAllViews()
            emptyHint.visibility = if (accounts.isEmpty()) View.VISIBLE else View.GONE
            accounts.forEach { acc ->
                val row = LinearLayout(activity).apply {
                    orientation = LinearLayout.HORIZONTAL
                    gravity = Gravity.CENTER_VERTICAL
                    setPadding(0, dp(activity, 7), 0, dp(activity, 7))
                }
                row.addView(
                    TextView(activity).apply { text = acc.nickname; textSize = 14f },
                    LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f),
                )
                row.addView(TextView(activity).apply {
                    text = "삭제"
                    textSize = 13f
                    setTextColor(android.graphics.Color.parseColor("#993C1D"))
                    setOnClickListener {
                        AlertDialog.Builder(activity)
                            .setMessage("\"${acc.nickname}\" 계정을 삭제할까요?")
                            .setPositiveButton("삭제") { _, _ ->
                                thread {
                                    OrderQueenManager.deleteAccount(activity, acc.id)
                                    val updated = OrderQueenManager.fetchAccounts(activity)
                                    activity.runOnUiThread {
                                        renderAccounts(updated)
                                        onChanged()
                                    }
                                }
                            }
                            .setNegativeButton("취소", null)
                            .show()
                    }
                })
                listContainer.addView(row)
            }
        }

        addAccountBtn.setOnClickListener {
            addForm.visibility = View.VISIBLE
            addAccountBtn.visibility = View.GONE
        }
        formCancelBtn.setOnClickListener { closeForm() }
        formSaveBtn.setOnClickListener {
            val nickname = nicknameInput.text.toString().trim()
            val loginId = idInput.text.toString().trim()
            val loginPwd = pwInput.text.toString()
            if (nickname.isEmpty() || loginId.isEmpty() || loginPwd.isEmpty()) {
                Toast.makeText(activity, "계정명/아이디/비밀번호를 모두 입력해주세요.", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            formSaveBtn.isEnabled = false
            thread {
                val result = OrderQueenManager.saveAccount(activity, nickname, loginId, loginPwd)
                val updated = if (result.isSuccess) OrderQueenManager.fetchAccounts(activity) else null
                activity.runOnUiThread {
                    formSaveBtn.isEnabled = true
                    result.onSuccess {
                        closeForm()
                        updated?.let(::renderAccounts)
                        onChanged()
                        Toast.makeText(activity, "\"$nickname\" 계정을 추가했습니다.", Toast.LENGTH_SHORT).show()
                    }.onFailure { e ->
                        Toast.makeText(activity, "저장 실패: ${e.message}", Toast.LENGTH_LONG).show()
                    }
                }
            }
        }

        thread {
            val accounts = OrderQueenManager.fetchAccounts(activity)
            activity.runOnUiThread { renderAccounts(accounts) }
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

        // 계정이 2개 이상일 때만 보여준다 - 매장이 하나뿐인 대다수 사용자는
        // 이 영역 자체를 볼 일이 없다(기존 흐름 그대로 유지).
        val accountsLabel = TextView(activity).apply {
            text = "등록할 계정"
            setPadding(0, dp(activity, 14), 0, dp(activity, 4))
            visibility = View.GONE
        }
        val allAccountsCheck = CheckBox(activity).apply {
            text = "모든 계정에 추가"
            visibility = View.GONE
        }
        val accountsListContainer = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
        }
        root.addView(accountsLabel)
        root.addView(allAccountsCheck)
        root.addView(accountsListContainer)

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

        var loadedAccounts: List<OqAccount> = emptyList()
        var accountPairs: List<Pair<OqAccount, CheckBox>> = emptyList()

        fun applyAllAccountsState(checkedAll: Boolean) {
            accountPairs.forEach { (_, cb) ->
                cb.isEnabled = !checkedAll
                if (checkedAll) cb.isChecked = true
            }
        }
        allAccountsCheck.setOnCheckedChangeListener { _, checked -> applyAllAccountsState(checked) }

        fun renderAccountsUi(accounts: List<OqAccount>) {
            loadedAccounts = accounts
            accountsListContainer.removeAllViews()
            if (accounts.size <= 1) {
                accountsLabel.visibility = View.GONE
                allAccountsCheck.visibility = View.GONE
                accountsListContainer.visibility = View.GONE
                accountPairs = emptyList()
                return
            }
            accountsLabel.visibility = View.VISIBLE
            allAccountsCheck.visibility = View.VISIBLE
            accountsListContainer.visibility = View.VISIBLE
            accountPairs = accounts.map { acc ->
                val cb = CheckBox(activity).apply { text = acc.nickname; isChecked = true; isEnabled = false }
                accountsListContainer.addView(cb)
                acc to cb
            }
            allAccountsCheck.isChecked = true
        }

        thread {
            val accounts = OrderQueenManager.fetchAccounts(activity)
            activity.runOnUiThread { renderAccountsUi(accounts) }
        }

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
            val className = classNames[spinner.selectedItemPosition]
            val classCd = classCodes[className] ?: classCodes.values.firstOrNull() ?: ""

            val accountIds = when {
                loadedAccounts.isEmpty() -> emptyList()
                loadedAccounts.size == 1 -> listOf(loadedAccounts[0].id)
                allAccountsCheck.isChecked -> loadedAccounts.map { it.id }
                else -> accountPairs.filter { it.second.isChecked }.map { it.first.id }
            }
            if (accountIds.isEmpty()) {
                Toast.makeText(activity, "등록할 계정을 선택해주세요.", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }

            dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false
            dialog.getButton(AlertDialog.BUTTON_NEGATIVE).isEnabled = false
            progress.visibility = View.VISIBLE

            thread {
                val result = OrderQueenManager.registerItem(
                    activity, barcode, menuName, salePrice, classCd, className, accountIds,
                )
                activity.runOnUiThread {
                    progress.visibility = View.GONE
                    result.onSuccess { results ->
                        val successCount = results.count { it.ok }
                        if (successCount == results.size) {
                            val msg = if (results.size == 1) "오더퀸에 등록됐습니다." else "${results.size}개 계정에 모두 등록했습니다."
                            Toast.makeText(activity, msg, Toast.LENGTH_SHORT).show()
                            dialog.dismiss()
                        } else {
                            dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
                            dialog.getButton(AlertDialog.BUTTON_NEGATIVE).isEnabled = true
                            val detail = results.joinToString("\n") { r ->
                                val label = r.nickname ?: "계정 ${r.accountId}"
                                "$label: ${if (r.ok) "성공" else "실패 - ${r.message}"}"
                            }
                            AlertDialog.Builder(activity)
                                .setTitle("등록 결과 ($successCount/${results.size} 성공)")
                                .setMessage(detail)
                                .setPositiveButton("확인", null)
                                .show()
                        }
                    }.onFailure { e ->
                        dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
                        dialog.getButton(AlertDialog.BUTTON_NEGATIVE).isEnabled = true
                        Toast.makeText(activity, "등록 실패: ${e.message}", Toast.LENGTH_LONG).show()
                    }
                }
            }
        }
    }
}
