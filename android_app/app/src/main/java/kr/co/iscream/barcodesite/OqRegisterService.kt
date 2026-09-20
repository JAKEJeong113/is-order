package kr.co.iscream.barcodesite

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject

/**
 * 다중선택으로 여러 상품을 오더퀸에 등록할 때 실제 네트워크 작업을 맡는
 * 포그라운드 서비스. 상품이 여러 개면 계정당 20~30초씩 순차로 걸려 전체
 * 몇 분까지 걸릴 수 있는데, 그 사이 사용자가 홈 화면으로 나가면 안드로이드가
 * 백그라운드 앱의 네트워크 접근을 제한해(실측: DNS 조회 자체가 실패,
 * "Unable to resolve host") 등록이 통째로 실패하는 문제가 있었다. 포그라운드
 * 서비스로 돌리면 알림이 떠 있는 동안은 이 제한을 받지 않는다.
 *
 * OrderQueenDialogs.showRegisterMultipleDialog는 "등록"을 누르면 다이얼로그
 * 자체를 계속 띄워두는 대신 이 서비스에 작업을 넘기고 바로 닫는다 - 사용자가
 * 앱에 머무르든 나가든 상관없이 알림으로 진행 상황을 보여주고, 끝나면 결과를
 * 알림 문구로 알려준다.
 */
class OqRegisterService : Service() {

    companion object {
        private const val CHANNEL_ID = "oq_register"
        private const val NOTIF_ID = 4201

        private const val EXTRA_ITEMS = "items"
        private const val EXTRA_CLASS_CD = "class_cd"
        private const val EXTRA_CLASS_NAME = "class_name"
        private const val EXTRA_ACCOUNT_IDS = "account_ids"

        fun start(
            context: Context, items: List<OqBulkItem>, classCd: String, className: String, accountIds: List<Int>,
        ) {
            val itemsJson = JSONArray().apply {
                items.forEach { item ->
                    put(JSONObject().put("barcode", item.barcode).put("name", item.name).put("price", item.price))
                }
            }
            val intent = Intent(context, OqRegisterService::class.java).apply {
                putExtra(EXTRA_ITEMS, itemsJson.toString())
                putExtra(EXTRA_CLASS_CD, classCd)
                putExtra(EXTRA_CLASS_NAME, className)
                putExtra(EXTRA_ACCOUNT_IDS, accountIds.toIntArray())
            }
            ContextCompat.startForegroundService(context, intent)
        }
    }

    private fun notificationManager(): NotificationManager =
        getSystemService(NotificationManager::class.java)

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = notificationManager()
        if (manager.getNotificationChannel(CHANNEL_ID) == null) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "오더퀸 등록", NotificationManager.IMPORTANCE_LOW),
            )
        }
    }

    private fun openAppPendingIntent(): PendingIntent? {
        val launchIntent = packageManager.getLaunchIntentForPackage(packageName) ?: return null
        return PendingIntent.getActivity(
            this, 0, launchIntent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    private fun progressNotification(text: String, completed: Int, total: Int): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("오더퀸에 등록 중")
            .setContentText(text)
            .setContentIntent(openAppPendingIntent())
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setProgress(total, completed, false)
            .build()

    private fun updateProgressNotification(text: String, completed: Int, total: Int) {
        notificationManager().notify(NOTIF_ID, progressNotification(text, completed, total))
    }

    private fun showResultNotification(text: String) {
        notificationManager().notify(
            NOTIF_ID,
            NotificationCompat.Builder(this, CHANNEL_ID)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle("오더퀸 등록 완료")
                .setContentText(text)
                .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                .setContentIntent(openAppPendingIntent())
                .setOngoing(false)
                .setAutoCancel(true)
                .build(),
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val items = parseItems(intent)
        val classCd = intent?.getStringExtra(EXTRA_CLASS_CD) ?: ""
        val className = intent?.getStringExtra(EXTRA_CLASS_NAME) ?: ""
        val accountIds = intent?.getIntArrayExtra(EXTRA_ACCOUNT_IDS)?.toList() ?: emptyList()

        if (items.isEmpty() || accountIds.isEmpty()) {
            stopSelf(startId)
            return START_NOT_STICKY
        }

        ensureChannel()
        startForeground(NOTIF_ID, progressNotification("상품 1/${items.size} 등록 중… (${items[0].name})", 0, items.size))

        Thread {
            val successNames = mutableListOf<String>()
            val failedDetails = mutableListOf<String>()
            items.forEachIndexed { index, item ->
                updateProgressNotification("상품 ${index + 1}/${items.size} 등록 중… (${item.name})", index, items.size)
                val result = OrderQueenManager.registerItem(
                    this, item.barcode, item.name, item.price, classCd, className, accountIds,
                )
                result.fold(
                    onSuccess = { results ->
                        if (results.all { it.ok }) {
                            successNames.add(item.name)
                        } else {
                            val failed = results.filter { !it.ok }
                                .joinToString(", ") { it.nickname ?: "계정 ${it.accountId}" }
                            failedDetails.add("${item.name}: 일부 실패 ($failed)")
                        }
                    },
                    onFailure = { e -> failedDetails.add("${item.name}: 실패 - ${e.message}") },
                )
            }

            val summary = if (failedDetails.isEmpty()) {
                "${items.size}개 상품을 모두 등록했습니다."
            } else {
                "${successNames.size}/${items.size}개 성공. 실패 - ${failedDetails.joinToString(" / ")}"
            }
            showResultNotification(summary)
            // 알림은 사용자가 실수로 스와이프해 지우면 등록 결과를 놓칠 수 있다
            // (사용자 확인) - 앱을 다시 열었을 때 확실히 확인할 수 있게, 앱이
            // 지금 떠 있으면 바로 팝업으로, 아니면 다음에 열 때 보여주도록
            // OqRegisterResultBus에 결과를 전달한다.
            OqRegisterResultBus.deliver(applicationContext, summary)
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf(startId)
        }.start()

        return START_NOT_STICKY
    }

    private fun parseItems(intent: Intent?): List<OqBulkItem> {
        val itemsJson = intent?.getStringExtra(EXTRA_ITEMS) ?: return emptyList()
        return try {
            val arr = JSONArray(itemsJson)
            (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                OqBulkItem(o.getString("barcode"), o.getString("name"), o.optInt("price", 0))
            }
        } catch (e: Exception) {
            emptyList()
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
