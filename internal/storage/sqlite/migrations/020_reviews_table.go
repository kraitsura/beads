package migrations

import (
	"database/sql"
	"fmt"
)

// MigrateReviewsTable creates the reviews table for storing local review history.
// This table is NOT exported to JSONL - it provides local audit trail while keeping
// the portable format lightweight.
//
// Review types:
// - plan: Pre-implementation plan review (is this the right approach?)
// - implementation: Post-implementation review (does output match plan?)
// - security: Security-focused review
// - Custom types as needed
//
// Review outcomes match the review_status values in issues table:
// - approved: Reviewer has approved this item
// - needs_revision: Reviewer has requested changes
// - deferred: Needs discussion or more context
func MigrateReviewsTable(db *sql.DB) error {
	// Check if table already exists
	var tableExists bool
	err := db.QueryRow(`
		SELECT COUNT(*) > 0
		FROM sqlite_master
		WHERE type='table' AND name='reviews'
	`).Scan(&tableExists)
	if err != nil {
		return fmt.Errorf("failed to check for reviews table: %w", err)
	}

	if !tableExists {
		_, err := db.Exec(`
			CREATE TABLE reviews (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				issue_id TEXT NOT NULL,
				review_type TEXT NOT NULL DEFAULT 'plan',
				outcome TEXT NOT NULL CHECK(outcome IN ('approved', 'needs_revision', 'deferred')),
				reviewer TEXT NOT NULL,
				notes TEXT DEFAULT '',
				created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
				FOREIGN KEY (issue_id) REFERENCES issues(id) ON DELETE CASCADE
			)
		`)
		if err != nil {
			return fmt.Errorf("failed to create reviews table: %w", err)
		}
	}

	// Create indexes (idempotent)
	indexes := []struct {
		name string
		sql  string
	}{
		{"idx_reviews_issue", "CREATE INDEX IF NOT EXISTS idx_reviews_issue ON reviews(issue_id)"},
		{"idx_reviews_reviewer", "CREATE INDEX IF NOT EXISTS idx_reviews_reviewer ON reviews(reviewer)"},
		{"idx_reviews_created", "CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews(created_at)"},
	}

	for _, idx := range indexes {
		_, err := db.Exec(idx.sql)
		if err != nil {
			return fmt.Errorf("failed to create %s index: %w", idx.name, err)
		}
	}

	return nil
}
