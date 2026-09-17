package com.zqr.localchat.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(
    entities = [SavedGroup::class, SavedChatMessage::class, DeletedMessage::class],
    version = 2,
    exportSchema = false
)
abstract class ChatDatabase : RoomDatabase() {
    abstract fun chatDao(): ChatDao

    companion object {
        /**
         * v1 -> v2: adds the deleted_messages tombstone table (offline-member
         * delete convergence). CREATE TABLE + index only — no existing data is
         * touched, so the destructive fallback was removed: an upgrade must
         * never silently wipe the user's chat history.
         */
        private val MIGRATION_1_2 = object : Migration(1, 2) {
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
                    .addMigrations(MIGRATION_1_2)
                    .build()
                INSTANCE = instance
                instance
            }
        }
    }
}
