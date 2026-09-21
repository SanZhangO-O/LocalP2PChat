package com.zqr.localchat.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(
    entities = [
        SavedGroup::class,
        SavedChatMessage::class,
        DeletedMessage::class,
        CallLogEntity::class,
        MessageReaction::class,
        PinnedMessage::class,
        GroupRead::class,
        PendingOp::class,
    ],
    version = 7,
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
         * v4 -> v5: add the reply/quote + own-message read columns (chat UX),
         * the group-management columns (announcement, kick marker) and the
         * local call-log table (call history). History must survive the
         * upgrade (NEVER destructive): every new column gets its empty/0
         * default and the new table is created empty.
         */
        private val MIGRATION_4_5 = object : Migration(4, 5) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "ALTER TABLE saved_messages ADD COLUMN replyTo TEXT NOT NULL DEFAULT ''"
                )
                db.execSQL(
                    "ALTER TABLE saved_messages ADD COLUMN replyPreview TEXT NOT NULL DEFAULT ''"
                )
                db.execSQL(
                    "ALTER TABLE saved_messages ADD COLUMN replySender TEXT NOT NULL DEFAULT ''"
                )
                db.execSQL(
                    "ALTER TABLE saved_messages ADD COLUMN read INTEGER NOT NULL DEFAULT 0"
                )
                db.execSQL(
                    "ALTER TABLE saved_groups ADD COLUMN announcement TEXT NOT NULL DEFAULT ''"
                )
                db.execSQL(
                    "ALTER TABLE saved_groups ADD COLUMN kickedAt INTEGER NOT NULL DEFAULT 0"
                )
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

        /**
         * v5 -> v6: message-experience tables (edit/reactions/pins/group read
         * receipts) plus the edited/mentions message columns. History must
         * survive the upgrade (NEVER destructive): every new column gets its
         * empty/0 default and the new tables are created empty. All three
         * tables FK-cascade off saved_messages' composite key.
         */
        private val MIGRATION_5_6 = object : Migration(5, 6) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "ALTER TABLE saved_messages ADD COLUMN edited INTEGER NOT NULL DEFAULT 0"
                )
                db.execSQL(
                    "ALTER TABLE saved_messages ADD COLUMN mentions TEXT NOT NULL DEFAULT ''"
                )
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS `message_reactions` (" +
                        "`groupId` TEXT NOT NULL, `msgId` TEXT NOT NULL, " +
                        "`emoji` TEXT NOT NULL, `actorId` TEXT NOT NULL, " +
                        "PRIMARY KEY(`groupId`, `msgId`, `emoji`, `actorId`), " +
                        "FOREIGN KEY(`groupId`, `msgId`) REFERENCES `saved_messages`(`groupId`, `id`) " +
                        "ON UPDATE NO ACTION ON DELETE CASCADE)"
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_message_reactions_groupId` " +
                        "ON `message_reactions` (`groupId`)"
                )
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS `pinned_messages` (" +
                        "`groupId` TEXT NOT NULL, `msgId` TEXT NOT NULL, " +
                        "`pinnedAt` INTEGER NOT NULL, `pinnedBy` TEXT NOT NULL DEFAULT '', " +
                        "PRIMARY KEY(`groupId`, `msgId`), " +
                        "FOREIGN KEY(`groupId`, `msgId`) REFERENCES `saved_messages`(`groupId`, `id`) " +
                        "ON UPDATE NO ACTION ON DELETE CASCADE)"
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_pinned_messages_groupId` " +
                        "ON `pinned_messages` (`groupId`)"
                )
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS `group_reads` (" +
                        "`groupId` TEXT NOT NULL, `msgId` TEXT NOT NULL, " +
                        "`readerId` TEXT NOT NULL, " +
                        "PRIMARY KEY(`groupId`, `msgId`, `readerId`), " +
                        "FOREIGN KEY(`groupId`, `msgId`) REFERENCES `saved_messages`(`groupId`, `id`) " +
                        "ON UPDATE NO ACTION ON DELETE CASCADE)"
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_group_reads_groupId` " +
                        "ON `group_reads` (`groupId`)"
                )
            }
        }

        /**
         * v6 -> v7: the pending_ops table (staged offline message-experience
         * ops, Windows `pending_ops` parity): an edit issued while the group
         * is unreachable is replayed once it becomes reachable. Created
         * empty, FK-cascade off saved_messages — no existing data is touched
         * (NEVER destructive).
         */
        private val MIGRATION_6_7 = object : Migration(6, 7) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS `pending_ops` (" +
                        "`groupId` TEXT NOT NULL, `kind` TEXT NOT NULL, " +
                        "`msgId` TEXT NOT NULL, `emoji` TEXT NOT NULL DEFAULT '', " +
                        "`active` INTEGER NOT NULL DEFAULT 0, " +
                        "`content` TEXT NOT NULL DEFAULT '', " +
                        "`createdAt` INTEGER NOT NULL, " +
                        "PRIMARY KEY(`groupId`, `kind`, `msgId`, `emoji`), " +
                        "FOREIGN KEY(`groupId`, `msgId`) REFERENCES `saved_messages`(`groupId`, `id`) " +
                        "ON UPDATE NO ACTION ON DELETE CASCADE)"
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_pending_ops_groupId` " +
                        "ON `pending_ops` (`groupId`)"
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
                    .addMigrations(
                        MIGRATION_1_2, MIGRATION_2_3, MIGRATION_3_4, MIGRATION_4_5,
                        MIGRATION_5_6, MIGRATION_6_7
                    )
                    .build()
                INSTANCE = instance
                instance
            }
        }
    }
}
