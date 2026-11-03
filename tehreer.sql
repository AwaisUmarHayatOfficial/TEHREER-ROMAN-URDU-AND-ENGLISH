-- =============================================================================
--  tehreer.sql  -  database schema for the Tehreer website (MySQL 8 / MariaDB 10.6+)
--
--  * Created automatically by the MySQL container on first start
--    (it is mounted into /docker-entrypoint-initdb.d/).
--  * Also applied by the app at startup. Every statement is idempotent
--    (IF NOT EXISTS), so it is safe to run again at any time.
--  * To create the database by hand:   mysql -u root -p < tehreer.sql
--
--  Tables
--    users         one row per person: name, username, phone, Gmail address, password hash
--                  (accounts created with "Sign in with Google" have no password hash)
--    login_codes   one-time e-mail codes for verifying a new account and for password reset
--                  (stored hashed, never in clear text)
--    sessions      signed-in browsers (only a hash of the cookie token is stored)
--    jobs          every conversion a user has started (history + download info)
--    auth_events   sign-in audit trail (code requested / sign-in / failure ...)
--    user_overview a read-only view for phpMyAdmin dashboards
-- =============================================================================

CREATE DATABASE IF NOT EXISTS `tehreer` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `tehreer`;

-- ----------------------------------------------------------------------------
-- users
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `users` (
  `id`                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `email`               VARCHAR(254)    NOT NULL COMMENT 'Gmail address as typed (lower-cased)',
  `email_normalized`    VARCHAR(254)    NOT NULL COMMENT 'gmail dots / +tags removed: one account per mailbox',
  `first_name`          VARCHAR(60)     NULL,
  `last_name`           VARCHAR(60)     NULL,
  `username`            VARCHAR(30)     NULL COMMENT 'unique, lower-case',
  `phone`               VARCHAR(20)     NULL COMMENT 'not verified',
  `display_name`        VARCHAR(80)     NULL COMMENT 'first + last name',
  `password_hash`       VARCHAR(255)    NULL COMMENT 'Argon2id hash, NULL for Google-only accounts',
  `google_sub`          VARCHAR(64)     NULL COMMENT 'Google account id when signed in with Google',
  `avatar_url`          VARCHAR(500)    NULL COMMENT 'profile picture from Google',
  `status`              ENUM('pending','active','blocked') NOT NULL DEFAULT 'active' COMMENT 'pending = e-mail not verified yet',
  `is_admin`            TINYINT(1)      NOT NULL DEFAULT 0,
  `email_verified_at`   DATETIME        NULL,
  `failed_logins`       SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  `locked_until`        DATETIME        NULL,
  `password_changed_at` DATETIME        NULL,
  `login_count`         INT UNSIGNED    NOT NULL DEFAULT 0,
  `created_at`          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_login_at`       DATETIME        NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_users_email_normalized` (`email_normalized`),
  UNIQUE KEY `uq_users_username` (`username`),
  UNIQUE KEY `uq_users_google_sub` (`google_sub`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- login_codes: the 6-digit code e-mailed to the user (verify new account / reset password)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `login_codes` (
  `id`               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `email_normalized` VARCHAR(254)    NOT NULL,
  `purpose`          VARCHAR(20)     NOT NULL DEFAULT 'verify_email' COMMENT 'verify_email | reset_password',
  `code_hash`        CHAR(64)        NOT NULL COMMENT 'HMAC-SHA256 of the code',
  `attempts`         TINYINT UNSIGNED NOT NULL DEFAULT 0,
  `ip`               VARCHAR(45)     NULL,
  `created_at`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `expires_at`       DATETIME        NOT NULL,
  `used_at`          DATETIME        NULL,
  PRIMARY KEY (`id`),
  KEY `idx_login_codes_email_created` (`email_normalized`, `created_at`),
  KEY `idx_login_codes_expires` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- sessions: one row per signed-in browser
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `sessions` (
  `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id`      BIGINT UNSIGNED NOT NULL,
  `token_hash`   CHAR(64)        NOT NULL COMMENT 'SHA-256 of the cookie token',
  `ip`           VARCHAR(45)     NULL,
  `user_agent`   VARCHAR(255)    NULL,
  `created_at`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_seen_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `expires_at`   DATETIME        NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_sessions_token` (`token_hash`),
  KEY `idx_sessions_user` (`user_id`),
  KEY `idx_sessions_expires` (`expires_at`),
  CONSTRAINT `fk_sessions_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- jobs: conversion history of every user
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `jobs` (
  `id`            VARCHAR(32)     NOT NULL COMMENT 'random public job id',
  `user_id`       BIGINT UNSIGNED NOT NULL,
  `video_id`      CHAR(11)        NOT NULL,
  `video_url`     VARCHAR(300)    NOT NULL,
  `language`      VARCHAR(20)     NOT NULL DEFAULT 'roman_urdu' COMMENT 'notes language: english | roman_urdu',
  `title`         VARCHAR(500)    NULL,
  `status`        ENUM('queued','running','done','failed','cancelled') NOT NULL DEFAULT 'queued',
  `percent`       DECIMAL(5,1)    NOT NULL DEFAULT 0,
  `chunk_total`   INT UNSIGNED    NOT NULL DEFAULT 0,
  `error`         VARCHAR(500)    NULL,
  `warning`       VARCHAR(500)    NULL,
  `docx_name`     VARCHAR(300)    NULL,
  `txt_name`      VARCHAR(300)    NULL,
  `files_deleted` TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '1 once the generated files were removed from disk',
  `created_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `started_at`    DATETIME        NULL,
  `finished_at`   DATETIME        NULL,
  PRIMARY KEY (`id`),
  KEY `idx_jobs_user_created` (`user_id`, `created_at`),
  KEY `idx_jobs_status` (`status`),
  KEY `idx_jobs_finished` (`finished_at`),
  CONSTRAINT `fk_jobs_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- auth_events: audit trail (no codes, no tokens, no API keys are ever stored)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `auth_events` (
  `id`               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id`          BIGINT UNSIGNED NULL,
  `email_normalized` VARCHAR(254)    NULL,
  `event`            VARCHAR(40)     NOT NULL COMMENT 'register, email_verified, login_ok, login_failed, google_login, password_reset, logout ...',
  `ip`               VARCHAR(45)     NULL,
  `user_agent`       VARCHAR(255)    NULL,
  `created_at`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_events_user` (`user_id`),
  KEY `idx_events_created` (`created_at`),
  KEY `idx_events_email` (`email_normalized`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- user_overview: handy read-only summary for phpMyAdmin
-- ----------------------------------------------------------------------------
CREATE OR REPLACE ALGORITHM=UNDEFINED SQL SECURITY INVOKER VIEW `user_overview` AS
SELECT u.`id`, u.`username`, u.`first_name`, u.`last_name`, u.`email`, u.`phone`, u.`status`,
       (u.`password_hash` IS NOT NULL) AS `has_password`, (u.`google_sub` IS NOT NULL) AS `google_linked`,
       u.`is_admin`, u.`created_at`, u.`last_login_at`, u.`login_count`,
       COUNT(j.`id`) AS `total_jobs`,
       SUM(j.`status` = 'done') AS `done_jobs`,
       SUM(j.`status` = 'failed') AS `failed_jobs`,
       MAX(j.`created_at`) AS `last_job_at`
FROM `users` u
LEFT JOIN `jobs` j ON j.`user_id` = u.`id`
GROUP BY u.`id`;
