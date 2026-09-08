package com.zqr.localchat

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

class ChatService : Service() {

    companion object {
        private const val CHANNEL_ID = "localchat_connection"
        private const val NOTIFICATION_ID = 1
        const val ACTION_REFRESH = "com.zqr.localchat.REFRESH_NOTIFICATION"
        const val EXTRA_GROUP_COUNT = "group_count"
    }

    override fun onCreate() {
        super.onCreate()
        createChannel()
        startForegroundCompat(buildNotification())
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val count = intent?.getIntExtra(EXTRA_GROUP_COUNT, 0) ?: 0
        startForegroundCompat(buildNotification(count))
        return START_STICKY
    }

    private fun startForegroundCompat(notification: Notification) {
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "连接保持",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "保持局域网聊天连接处于活跃状态"
                setShowBadge(false)
            }
            val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
            nm.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(groupCount: Int = 0): Notification {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("LocalChat 正在运行")
            .setContentText(if (groupCount > 0) "保持 $groupCount 个群组连接活跃" else "保持群组连接活跃")
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
