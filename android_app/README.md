# 무인매장 바코드 조회 (Android)

`barcode_site` 웹사이트를 그대로 감싸는 가벼운 WebView 앱. 카메라 바코드
스캔은 웹페이지 자체(html5-qrcode)에 이미 들어있어서, 이 앱은 그 웹페이지의
카메라 요청을 실제 안드로이드 카메라 권한과 연결해주는 역할만 한다 - 앱
쪽에 스캔 로직을 따로 만들지 않았다.

**⚠️ 중요**: 이 프로젝트는 제가 코드까지만 작성했고, 이 환경엔 Android
SDK가 없어서 실제로 빌드하거나 에뮬레이터/기기에서 실행해본 적은 없습니다.
Android Studio로 열어서 처음 빌드하실 때 사소한 버전 충돌(Gradle/AGP/Kotlin
버전 등)이 있을 수 있어요 - Android Studio가 보통 자동으로 "Upgrade" 버튼을
띄워주니 그대로 따라가시면 됩니다. 에러 메시지 보내주시면 바로 고쳐드릴게요.

## 여는 방법

1. Android Studio 설치 (없으면 [developer.android.com/studio](https://developer.android.com/studio))
2. **Open** → 이 `android_app` 폴더 선택
3. 처음 열면 Gradle Sync가 자동으로 시작됨 (인터넷 필요, 몇 분 걸릴 수 있음)
4. 에뮬레이터 만들거나(Device Manager) 실제 폰을 USB로 연결
5. 상단 ▶ **Run** 버튼

## 카메라 스캔이 실제 기기에서만 되는 이유

에뮬레이터는 웹캠이 연결돼 있지 않으면 실제 카메라 영상이 안 나올 수
있어요. 실제 스캔 테스트는 **실제 안드로이드 폰**에서 해보시는 걸
추천드립니다. USB로 연결하고 개발자 모드(USB 디버깅)만 켜면 바로 설치/실행
가능합니다.

## 도메인이 바뀌면

지금은 Render의 임시 주소(`barcod-site.onrender.com`)를 보고 있습니다.
나중에 정식 도메인(예: `barcode.is-cream.co.kr`)을 연결하면
[`MainActivity.kt`](app/src/main/java/kr/co/iscream/barcodesite/MainActivity.kt)의
`BASE_URL` 상수랑, `shouldOverrideUrlLoading`의 도메인 체크 부분 두 곳을
새 도메인으로 바꿔주세요.

## 앱 아이콘

지금 아이콘은 민트색 배경에 흰색 바코드 막대 모양으로 최소한으로만
만들어뒀습니다. 실제 로고로 바꾸고 싶으면 `res` 폴더 우클릭 → **New → Image
Asset**에서 원하는 이미지를 넣으면 자동으로 모든 해상도가 생성됩니다.

## Play Store에 올리려면

1. Android Studio 메뉴: **Build → Generate Signed Bundle / APK**
2. **Android App Bundle** 선택 (Play Store 업로드용 권장 형식)
3. 서명 키(keystore)가 없으면 이 화면에서 새로 만들 수 있음 - **이 키는
   반드시 안전하게 보관**해야 합니다(분실하면 같은 앱으로 업데이트를 못 냄).
4. 생성된 `.aab` 파일을 [Google Play Console](https://play.google.com/console)에
   업로드 (Play Console 개발자 계정 등록비 $25 1회 필요)

## 왜 완전 네이티브 앱이 아니라 WebView인가

- 바코드 검색/스캔 기능이 이미 웹에서 완성돼 있고, 여기서 다시 만들면 웹과
  앱 두 곳을 따로 유지보수해야 함.
- 웹사이트를 고치면(가격 표시 방식, 새 기능 추가 등) 앱 업데이트 없이 바로
  반영됨 - 스토어 심사를 다시 안 받아도 됨.
- 이 앱의 목적(가벼운 무료 도구 + i's ORDER 유입 통로)에는 완전 네이티브의
  이점(부드러운 애니메이션 등)이 크게 중요하지 않음.
