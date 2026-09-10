package kr.co.iscream.barcodesite

import android.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity

/**
 * 상단 "?" 버튼을 누르면 뜨는 간단한 사용법 안내 - 별도 화면/웹페이지를
 * 만들지 않고 다이얼로그 하나로 끝낸다(기능이 4개뿐이라 그 정도면 충분).
 */
object HelpDialogs {

    fun showUsageGuide(activity: AppCompatActivity) {
        val message = """
            🔍 검색
            바코드 번호나 상품명을 입력하고 "검색"을 누르면 등록된 추천판매가를 바로 확인할 수 있어요.

            📷 스캔
            "스캔" 버튼을 누르고 카메라로 바코드를 비추면 자동으로 인식해서 검색해줘요.

            📋 바코드 복사
            검색 결과를 탭하면 바코드 번호가 클립보드에 복사돼요. 도매몰 등에 상품을 등록할 때 바로 붙여넣기 하면 됩니다.

            🆕 신제품 안내
            최근 4주 이내 새로 등록된 상품을 카테고리별로 모아볼 수 있어요.

            ⚙️ 오더퀸 자동등록
            오른쪽 위 톱니 버튼에서 오더퀸 계정을 연결해두면, 검색 결과의 "오더퀸 등록" 버튼 하나로 오더퀸 관리자 페이지에 상품을 자동으로 등록할 수 있어요.
        """.trimIndent()

        AlertDialog.Builder(activity)
            .setTitle("사용법 안내")
            .setMessage(message)
            .setPositiveButton("확인", null)
            .show()
    }
}
