package com.zqr.localchat.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(
    entities = [SavedGroup::class, SavedChatMessage::class, DeletedMessage::class, CallLogEntity::class],
    version = 5,
    exportSchema = false
)
abstract class ChatDatabase : RoomDatabase() {
    abstract fun chatDao(): ChatDao

    companion object {
        /**
         * v1 -> v2: add the file-message kind column. History must survive the
         * upgrade, so this is a real migration — NEVER destructive. (The
         * destructive fallback was removed for the same reason: a schema the
         * migrations cannot explain must fail loudly, not wipe chat history.)
         */
        private val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "ALTER TABLE saved_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'file'"
                )
            }
        }

        /**
         * v2 -> v3: adds the deleted_messages tombstone table (offline-member
         * delete convergence). CREATE TABLE + index only — no existing data is
         * touched. The `kind` guard keeps a v2 database from the interim
         * tombstone-only build upgradeable too (that build shipped the table
         * but not the column).
         */
        private val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS `deleted_messages` (" +
                        "`groupId` TEXT NOT NULL, `msgId` TEXT NOT NULL, `deletedAt` INTEGER NOT NULL, " +
                        "PRIMARY KEY(`groupId`, `msgId`), " +
                        "FOREIGN KEY(`groupId`) REFERENCES `saved_groups`(`groupId`) " +
                        "ON UPDATE NO ACTION ON DELETE CASCADE)"
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_deleted_messages_groupId` " +
                        "ON `deleted_messages` (`groupId`)"
                )
                var hasKind = false
                db.query("PRAGMA table_info(`saved_messages`)").use { c ->
                    val nameIndex = c.getColumnIndex("name")
                    while (c.moveToNext()) {
                        if (nameIndex >= 0 && c.getString(nameIndex) == "kind") {
                            hasKind = true
                        }
                    }
                }
                if (!hasKind) {
                    db.execSQL(
                        "ALTER TABLE saved_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'file'"
                    )
                }
            }
        }

        /**
         * v3 -> v4: add the folder-transfer columns. History must survive the
         * upgrade (NEVER destructive): each column is added with its empty/0
         * default, so pre-folder rows keep behaving as plain files.
         */
        private val MIGRATION_3_4 = object : Migration(3, 4) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "ALTER TABLE saved_messages ADD COLUMN folderId TEXT NOT NULL DEFAULT ''"
                )
                db.execSQL(
                    "ALTER TABLE saved_messages ADD COLUMN folderName TEXT NOT NULL DEFAULT ''"
                )
                db.execSQL(
                    "ALTER TABLE saved_messages ADD COLUMN relativePath TEXT NOT NULL DEFAULT ''"
                )
                db.execSQL(
                    "ALTER TABLE saved_messages ADD COLUMN folderTotal INTEGER NOT NULL DEFAULT 0"
                )
            }
        }

        /**
         * v4 -> v5: add the local call-log table (call history). CREATE TABLE +
         * index only — no existing data is touched, history survives.
         */
        private val MIGRATION_4_5 = object : Migration(4, 5) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS `call_logs` (" +
                        "`id` TEXT NOT NULL, `conversationKey` TEXT NOT NULL, " +
                        "`peerId` TEXT NOT NULL, `peerName` TEXT NOT NULL, " +
                        "`direction` TEXT NOT NULL, `result` TEXT NOT NULL, " +
                        "`media` TEXT NOT NULL DEFAULT 'video', " +
                        "`startTime` INTEGER NOT NULL, `duration` INTEGER NOT NULL DEFAULT 0, " +
                        "PRIMARY KEY(`id`))"
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_call_logs_conversationKey` " +
                        "ON `call_logs` (`conversationKey`)"
                )
            }
        }

        @Volatile
        private var INSTANCE: ChatDatabase? = null

        fun getInstance(context: Context): ChatDatabase {
            return INSTANCE ?: synchronized(this) {
                val instance = Room.databaseBuilder(
                    context.applicationContext,
                    ChatDatabase::class.java,
                    "localchat_database"
                )
                    .addMigrations(MIGRATION_1_2, MIGRATION_2_3, MIGRATION_3_4, MIGRATION_4_5)
                    .build()
                INSTANCE = instance
                instance
            }
        }
    }
}
