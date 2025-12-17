package migrations

import (
	"database/sql"
	"fmt"
)

// MigrateReviewColumns adds review workflow columns to the issues table.
// These columns support structured review/approval workflows:
// - review_status: current review state (unreviewed, approved, needs_revision, deferred)
// - reviewed_by: who performed the last review
// - reviewed_at: when the last review occurred
//
// Design: Only these three columns sync via JSONL. Full review history stays in
// the local reviews table (see 020_reviews_table.go).
func MigrateReviewColumns(db *sql.DB) error {
	columns := []struct {
		name       string
		definition string
	}{
		{"review_status", "TEXT DEFAULT 'unreviewed' CHECK(review_status IN ('', 'unreviewed', 'approved', 'needs_revision', 'deferred'))"},
		{"reviewed_by", "TEXT"},
		{"reviewed_at", "DATETIME"},
	}

	for _, col := range columns {
		var columnExists bool
		err := db.QueryRow(`
			SELECT COUNT(*) > 0
			FROM pragma_table_info('issues')
			WHERE name = ?
		`, col.name).Scan(&columnExists)
		if err != nil {
			return fmt.Errorf("failed to check %s column: %w", col.name, err)
		}

		if columnExists {
			continue
		}

		_, err = db.Exec(fmt.Sprintf(`ALTER TABLE issues ADD COLUMN %s %s`, col.name, col.definition))
		if err != nil {
			return fmt.Errorf("failed to add %s column: %w", col.name, err)
		}
	}

	// Add index on review_status for efficient filtering
	_, err := db.Exec(`CREATE INDEX IF NOT EXISTS idx_issues_review_status ON issues(review_status)`)
	if err != nil {
		return fmt.Errorf("failed to create review_status index: %w", err)
	}

	// Add index on reviewed_at for conflict resolution (newer wins)
	_, err = db.Exec(`CREATE INDEX IF NOT EXISTS idx_issues_reviewed_at ON issues(reviewed_at) WHERE reviewed_at IS NOT NULL`)
	if err != nil {
		return fmt.Errorf("failed to create reviewed_at index: %w", err)
	}

	return nil
}
