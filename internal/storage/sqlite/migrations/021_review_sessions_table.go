package migrations

import (
	"database/sql"
	"fmt"
)

// MigrateReviewSessionsTable creates the review_sessions table for grouping related reviews.
// This table is NOT exported to JSONL - it provides local workflow tracking.
//
// Review sessions allow tracking batch review workflows where a reviewer goes through
// multiple related issues (e.g., reviewing an entire epic and its children).
//
// The session tracks:
// - Which root issue was being reviewed (and implicitly its children)
// - Who performed the review
// - When it started/completed
// - Summary statistics
func MigrateReviewSessionsTable(db *sql.DB) error {
	// Check if table already exists
	var tableExists bool
	err := db.QueryRow(`
		SELECT COUNT(*) > 0
		FROM sqlite_master
		WHERE type='table' AND name='review_sessions'
	`).Scan(&tableExists)
	if err != nil {
		return fmt.Errorf("failed to check for review_sessions table: %w", err)
	}

	if !tableExists {
		_, err := db.Exec(`
			CREATE TABLE review_sessions (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				root_issue_id TEXT NOT NULL,
				reviewer TEXT NOT NULL,
				started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
				completed_at DATETIME,
				summary TEXT DEFAULT '',
				items_reviewed INTEGER DEFAULT 0,
				items_approved INTEGER DEFAULT 0,
				items_needs_revision INTEGER DEFAULT 0,
				items_deferred INTEGER DEFAULT 0,
				FOREIGN KEY (root_issue_id) REFERENCES issues(id) ON DELETE CASCADE
			)
		`)
		if err != nil {
			return fmt.Errorf("failed to create review_sessions table: %w", err)
		}
	}

	// Create indexes (idempotent)
	indexes := []struct {
		name string
		sql  string
	}{
		{"idx_review_sessions_root", "CREATE INDEX IF NOT EXISTS idx_review_sessions_root ON review_sessions(root_issue_id)"},
		{"idx_review_sessions_reviewer", "CREATE INDEX IF NOT EXISTS idx_review_sessions_reviewer ON review_sessions(reviewer)"},
	}

	for _, idx := range indexes {
		_, err := db.Exec(idx.sql)
		if err != nil {
			return fmt.Errorf("failed to create %s index: %w", idx.name, err)
		}
	}

	return nil
}
