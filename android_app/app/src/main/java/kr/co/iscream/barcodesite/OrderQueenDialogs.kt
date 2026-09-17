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

    /** 폼 아래 붙는 보조 안내문구(동의 안내, 저장 소요시간 안내, 사용법
     * 안내 등)를 전부 같은 크기/색/줄바꿈 방식으로 통일한다 - 문구마다
     * 따로 스타일을 주면 줄바꿈 위치가 들쭉날쭉해 보인다(실측 확인).
     * topPaddingDp로 문구 앞 간격만 조절한다. */
    private fun hintText(activity: AppCompatActivity, text: String, topPaddingDp: Int = 10): TextView =
        TextView(activity).apply {
            this.text = text
            textSize = 11.5f
            alpha = 0.65f
            setPadding(dp(activity, 4), dp(activity, topPaddingDp), dp(activity, 4), 0)
        }

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
        // 판매 데이터 활용 동의(옵트인, 기본 미동의) - 개인정보 목적 외 이용
        // 금지 원칙에 따라 계정 저장과 같은 화면에서 명확히 별도로 받는다.
        // 동의 여부와 무관하게 바코드 등록 등 계정의 다른 기능은 그대로
        // 쓸 수 있다는 점을 바로 밑에 명시한다(사용자 확인).
        val salesConsentCheck = CheckBox(activity).apply {
            text = "매장 판매 데이터를 전체 가맹점 인기 판매 순위 산출에 활용하는 데 동의합니다"
            textSize = 12.5f
            setPadding(0, dp(activity, 10), 0, 0)
        }
        val salesConsentNote = hintText(
            activity,
            "동의하지 않아도 바코드 등록 기능은 그대로 사용할 수 있습니다.\n" +
                "수집된 데이터는 외부 업체에 제공·판매되지 않으며,\n개별 매장을 알 수 없는 집계 형태로만 활용됩니다.",
            topPaddingDp = 2,
        )
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
        // 저장이 곧바로 안 끝나고 몇 초~20여 초 걸릴 수 있다는 걸 누르기
        // 전에 미리 알려서, 버튼이 "확인 중…"으로 바뀌는 게 멈춘 것처럼
        // 보이지 않게 한다.
        val formSaveHint = hintText(
            activity,
            "저장을 누르면 오더퀸 로그인 확인 절차가 진행됩니다(몇 초~20여 초 소요).",
            topPaddingDp = 2,
        )
        val addForm = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
            addView(nicknameInput)
            addView(idInput)
            addView(pwInput)
            addView(salesConsentCheck)
            addView(salesConsentNote)
            addView(formBtnRow)
            addView(formSaveHint)
        }
        root.addView(addForm)

        root.addView(hintText(
            activity,
            "등록한 계정은 검색결과의 \"오더퀸 등록\" 버튼을 누를 때 어느 매장에 등록할지 고를 수 있습니다.\n" +
                "아이디/비밀번호는 암호화되어 서버에 저장됩니다.",
            topPaddingDp = 14,
        ))

        val dialog = AlertDialog.Builder(activity)
            .setTitle("오더퀸 자동등록 설정")
            .setView(root)
            .setNegativeButton("닫기", null)
            .create()
        dialog.show()

        fun clearForm() {
            nicknameInput.setText(""); idInput.setText(""); pwInput.setText("")
            salesConsentCheck.isChecked = false
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
                val itemContainer = LinearLayout(activity).apply {
                    orientation = LinearLayout.VERTICAL
                    setPadding(0, dp(activity, 7), 0, dp(activity, 7))
                }
                val row = LinearLayout(activity).apply {
                    orientation = LinearLayout.HORIZONTAL
                    gravity = Gravity.CENTER_VERTICAL
                }
                row.addView(
                    TextView(activity).apply { text = acc.nickname; textSize = 14f },
                    LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f),
                )
                row.addView(TextView(activity).apply {
                    text = "설정"
                    textSize = 13f
                    setTextColor(android.graphics.Color.parseColor("#08796F"))
                    setPadding(0, 0, dp(activity, 14), 0)
                    setOnClickListener {
                        showEditAccountDialog(activity, acc.id) {
                            thread {
                                val updated = OrderQueenManager.fetchAccounts(activity)
                                activity.runOnUiThread {
                                    renderAccounts(updated)
                                    onChanged()
                                }
                            }
                        }
                    }
                })
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
                itemContainer.addView(row)
                listContainer.addView(itemContainer)
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
            val salesDataConsent = salesConsentCheck.isChecked
            formSaveBtn.isEnabled = false
            // 저장 전에 서버가 실제로 오더퀸에 로그인해 아이디/비밀번호를
            // 확인하느라 몇 초~20여 초 걸릴 수 있어(실측), 버튼이 멈춘 것처럼
            // 보이지 않게 진행 중임을 알린다.
            formSaveBtn.text = "확인 중…"
            thread {
                val result = OrderQueenManager.saveAccount(activity, nickname, loginId, loginPwd, salesDataConsent)
                val updated = if (result.isSuccess) OrderQueenManager.fetchAccounts(activity) else null
                activity.runOnUiThread {
                    formSaveBtn.isEnabled = true
                    formSaveBtn.text = "저장"
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

    /** 계정 하나의 "설정"(수정) 다이얼로그 - 계정명/아이디/판매 데이터 활용
     * 동의를 고칠 수 있고, 비밀번호는 바꾸고 싶을 때만 입력한다(빈칸이면
     * 기존 비밀번호 유지). onSaved는 저장 성공 시 목록을 새로고침하도록
     * 호출부(showSettingsDialog)가 넘겨준다. */
    private fun showEditAccountDialog(activity: AppCompatActivity, accountId: Int, onSaved: () -> Unit) {
        val padding = dp(activity, 20)
        val root = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, padding)
        }

        val loadingLabel = TextView(activity).apply {
            text = "불러오는 중..."
            alpha = 0.65f
        }
        root.addView(loadingLabel)

        val nicknameInput = EditText(activity).apply { hint = "계정명"; visibility = View.GONE }
        val idInput = EditText(activity).apply { hint = "오더퀸 아이디"; visibility = View.GONE }
        val pwInput = EditText(activity).apply {
            hint = "비밀번호 (변경 시에만 입력)"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            visibility = View.GONE
        }
        val consentCheck = CheckBox(activity).apply {
            text = "매장 판매 데이터를 전체 가맹점 인기 판매 순위 산출에 활용하는 데 동의합니다"
            textSize = 12.5f
            setPadding(0, dp(activity, 10), 0, 0)
            visibility = View.GONE
        }
        val consentNote = hintText(
            activity,
            "동의하지 않아도 바코드 등록 기능은 그대로 사용할 수 있습니다.\n" +
                "수집된 데이터는 외부 업체에 제공·판매되지 않으며,\n개별 매장을 알 수 없는 집계 형태로만 활용됩니다.",
            topPaddingDp = 2,
        ).apply { visibility = View.GONE }
        val saveHint = hintText(
            activity,
            "아이디 또는 비밀번호를 변경하면 저장 시 오더퀸 로그인 확인 절차가 진행됩니다(몇 초~20여 초 소요).",
        ).apply { visibility = View.GONE }
        root.addView(nicknameInput)
        root.addView(idInput)
        root.addView(pwInput)
        root.addView(consentCheck)
        root.addView(consentNote)
        root.addView(saveHint)

        val dialog = AlertDialog.Builder(activity)
            .setTitle("계정 설정")
            .setView(root)
            .setPositiveButton("저장", null)
            .setNegativeButton("취소", null)
            .create()
        dialog.show()

        // 로딩 끝나기 전엔 저장을 못 누르게 막는다(아직 원래 값을 모르는
        // 채로 저장하면 아이디/비밀번호를 빈 값으로 덮어쓸 위험이 있음).
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false

        thread {
            val result = OrderQueenManager.fetchAccountDetail(activity, accountId)
            activity.runOnUiThread {
                result.onSuccess { detail ->
                    loadingLabel.visibility = View.GONE
                    nicknameInput.visibility = View.VISIBLE
                    idInput.visibility = View.VISIBLE
                    pwInput.visibility = View.VISIBLE
                    consentCheck.visibility = View.VISIBLE
                    consentNote.visibility = View.VISIBLE
                    saveHint.visibility = View.VISIBLE
                    nicknameInput.setText(detail.nickname)
                    idInput.setText(detail.loginId)
                    consentCheck.isChecked = detail.salesDataConsent
                    dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
                }.onFailure { e ->
                    loadingLabel.text = "불러오기 실패: ${e.message}"
                }
            }
        }

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
            val nickname = nicknameInput.text.toString().trim()
            val loginId = idInput.text.toString().trim()
            val loginPwd = pwInput.text.toString()
            if (nickname.isEmpty() || loginId.isEmpty()) {
                Toast.makeText(activity, "계정명/아이디를 입력해주세요.", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            val consent = consentCheck.isChecked
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).text = "확인 중…"
            thread {
                val result = OrderQueenManager.updateAccount(activity, accountId, nickname, loginId, loginPwd, consent)
                activity.runOnUiThread {
                    dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
                    dialog.getButton(AlertDialog.BUTTON_POSITIVE).text = "저장"
                    result.onSuccess {
                        dialog.dismiss()
                        onSaved()
                        Toast.makeText(activity, "저장했습니다.", Toast.LENGTH_SHORT).show()
                    }.onFailure { e ->
                        Toast.makeText(activity, "저장 실패: ${e.message}", Toast.LENGTH_LONG).show()
                    }
                }
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
        val timeNote = TextView(activity).apply {
            text = "계정 2개까지 약 30초, 이후 2개 늘 때마다 30초씩 늘어납니다."
            textSize = 12f
            alpha = 0.65f
            setPadding(0, dp(activity, 6), 0, 0)
            visibility = View.GONE
        }
        root.addView(accountsLabel)
        root.addView(allAccountsCheck)
        root.addView(accountsListContainer)
        root.addView(timeNote)

        // 등록은 계정마다 로그인+폼입력이 이어져서 시간이 걸린다 - 정확한
        // 서버 진행률을 실시간으로 받으려면 스트리밍이 필요하지만, 소요
        // 시간이 어느 정도 예측 가능해서(서버가 2개씩 병렬 처리) 예상 시간
        // 기반으로 막대를 채우고 응답이 오면 100%로 스냅한다. 예상 시간을
        // 넘겨도 멈춘 것처럼 보이지 않게, 옆에 계속 도는 원형 스피너를 둔다.
        val progress = ProgressBar(activity, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            visibility = View.GONE
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(activity, 14) }
        }
        val progressSpinner = ProgressBar(activity).apply {
            val s = dp(activity, 16)
            layoutParams = LinearLayout.LayoutParams(s, s)
        }
        val progressLabel = TextView(activity).apply {
            textSize = 12f
            alpha = 0.75f
        }
        val progressRow = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            visibility = View.GONE
            setPadding(0, dp(activity, 8), 0, 0)
            addView(progressSpinner, LinearLayout.LayoutParams(dp(activity, 16), dp(activity, 16)).apply {
                marginEnd = dp(activity, 8)
            })
            addView(progressLabel)
        }
        root.addView(progress)
        root.addView(progressRow)

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
            timeNote.visibility = View.VISIBLE
        }

        thread {
            val accounts = OrderQueenManager.fetchAccounts(activity)
            activity.runOnUiThread { renderAccountsUi(accounts) }
        }

        // 등록 소요 시간 예상치(ms) - 서버가 계정을 2개씩 병렬 처리하므로
        // 2개 묶음마다 약 28초. 응답이 오기 전까지 이 값 기준으로 막대를
        // 채운다(실제 서버 진행률이 아니라 어림치).
        fun estimateMs(n: Int): Long = ((n + 1) / 2).coerceAtLeast(1) * 28_000L
        var progressAnim: android.animation.ValueAnimator? = null
        fun startProgress(n: Int) {
            val total = estimateMs(n)
            progress.isIndeterminate = false
            progress.progress = 0
            progress.visibility = View.VISIBLE
            progressRow.visibility = View.VISIBLE
            progressLabel.text = "오더퀸에 등록 중… 예상 약 ${total / 1000}초"
            progressAnim = android.animation.ValueAnimator.ofInt(0, 95).apply {
                duration = total
                interpolator = android.view.animation.LinearInterpolator()
                addUpdateListener { a ->
                    progress.progress = a.animatedValue as Int
                    val remain = ((total * (1f - a.animatedFraction)) / 1000f).toInt()
                    progressLabel.text = if (remain > 1) "오더퀸에 등록 중… 약 ${remain}초 남음" else "거의 다 됐어요…"
                    // 예상 시간을 넘기면(막대가 95%에서 멈춤) 가로 막대를
                    // 무한 진행 모드로 바꿔서, 옆 스피너와 함께 "아직 처리
                    // 중"이 확실히 보이게 한다.
                    if (a.animatedFraction >= 1f) progress.isIndeterminate = true
                }
                start()
            }
        }
        fun stopProgress() {
            progressAnim?.cancel()
            progressAnim = null
            progress.isIndeterminate = false
            progress.progress = 100
            progress.visibility = View.GONE
            progressRow.visibility = View.GONE
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
            startProgress(accountIds.size)

            thread {
                val result = OrderQueenManager.registerItem(
                    activity, barcode, menuName, salePrice, classCd, className, accountIds,
                )
                activity.runOnUiThread {
                    stopProgress()
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
