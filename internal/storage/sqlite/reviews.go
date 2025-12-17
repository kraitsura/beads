package sqlite

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"github.com/steveyegge/beads/internal/types"
)

// CreateReview creates a new review history entry.
// This also updates the issue's review fields (review_status, reviewed_by, reviewed_at).
func (s *SQLiteStorage) CreateReview(ctx context.Context, review *types.Review, actor string) error {
	return s.withTx(ctx, func(tx *sql.Tx) error {
		// Use a single timestamp for consistency across all operations
		now := time.Now()

		// Set timestamp if not provided
		if review.CreatedAt.IsZero() {
			review.CreatedAt = now
		}

		// Insert review record
		result, err := tx.ExecContext(ctx, `
			INSERT INTO reviews (issue_id, review_type, outcome, reviewer, notes, created_at)
			VALUES (?, ?, ?, ?, ?, ?)
		`, review.IssueID, review.ReviewType, review.Outcome, review.Reviewer, review.Notes, review.CreatedAt)
		if err != nil {
			return fmt.Errorf("failed to insert review: %w", err)
		}

		// Get the inserted ID
		id, err := result.LastInsertId()
		if err != nil {
			return fmt.Errorf("failed to get review id: %w", err)
		}
		review.ID = id

		// Update the issue's review fields (using same timestamp for consistency)
		_, err = tx.ExecContext(ctx, `
			UPDATE issues
			SET review_status = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ?
			WHERE id = ?
		`, review.Outcome, review.Reviewer, now, now, review.IssueID)
		if err != nil {
			return fmt.Errorf("failed to update issue review status: %w", err)
		}

		// Mark issue as dirty for incremental export
		_, err = tx.ExecContext(ctx, `
			INSERT INTO dirty_issues (issue_id, marked_at)
			VALUES (?, ?)
			ON CONFLICT (issue_id) DO UPDATE SET marked_at = excluded.marked_at
		`, review.IssueID, now)
		if err != nil {
			return fmt.Errorf("failed to mark issue dirty: %w", err)
		}

		// Record event
		_, err = tx.ExecContext(ctx, `
			INSERT INTO events (issue_id, event_type, actor, comment)
			VALUES (?, ?, ?, ?)
		`, review.IssueID, "reviewed", actor, fmt.Sprintf("Review: %s by %s", review.Outcome, review.Reviewer))
		if err != nil {
			return fmt.Errorf("failed to record event: %w", err)
		}

		return nil
	})
}

// GetReviewsByIssue retrieves all reviews for a specific issue, ordered by creation time.
func (s *SQLiteStorage) GetReviewsByIssue(ctx context.Context, issueID string) ([]*types.Review, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, issue_id, review_type, outcome, reviewer, notes, created_at
		FROM reviews
		WHERE issue_id = ?
		ORDER BY created_at ASC
	`, issueID)
	if err != nil {
		return nil, fmt.Errorf("failed to query reviews: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var reviews []*types.Review
	for rows.Next() {
		var r types.Review
		var notes sql.NullString
		err := rows.Scan(&r.ID, &r.IssueID, &r.ReviewType, &r.Outcome, &r.Reviewer, &notes, &r.CreatedAt)
		if err != nil {
			return nil, fmt.Errorf("failed to scan review: %w", err)
		}
		if notes.Valid {
			r.Notes = notes.String
		}
		reviews = append(reviews, &r)
	}

	return reviews, rows.Err()
}

// GetReviewHistory retrieves all reviews across all issues, optionally filtered by reviewer.
func (s *SQLiteStorage) GetReviewHistory(ctx context.Context, reviewer string, limit int) ([]*types.Review, error) {
	var rows *sql.Rows
	var err error

	if reviewer != "" {
		rows, err = s.db.QueryContext(ctx, `
			SELECT id, issue_id, review_type, outcome, reviewer, notes, created_at
			FROM reviews
			WHERE reviewer = ?
			ORDER BY created_at DESC
			LIMIT ?
		`, reviewer, limit)
	} else {
		rows, err = s.db.QueryContext(ctx, `
			SELECT id, issue_id, review_type, outcome, reviewer, notes, created_at
			FROM reviews
			ORDER BY created_at DESC
			LIMIT ?
		`, limit)
	}
	if err != nil {
		return nil, fmt.Errorf("failed to query review history: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var reviews []*types.Review
	for rows.Next() {
		var r types.Review
		var notes sql.NullString
		err := rows.Scan(&r.ID, &r.IssueID, &r.ReviewType, &r.Outcome, &r.Reviewer, &notes, &r.CreatedAt)
		if err != nil {
			return nil, fmt.Errorf("failed to scan review: %w", err)
		}
		if notes.Valid {
			r.Notes = notes.String
		}
		reviews = append(reviews, &r)
	}

	return reviews, rows.Err()
}

// CreateReviewSession starts a new review session.
func (s *SQLiteStorage) CreateReviewSession(ctx context.Context, session *types.ReviewSession) error {
	if session.StartedAt.IsZero() {
		session.StartedAt = time.Now()
	}

	result, err := s.db.ExecContext(ctx, `
		INSERT INTO review_sessions (root_issue_id, reviewer, started_at, summary)
		VALUES (?, ?, ?, ?)
	`, session.RootIssueID, session.Reviewer, session.StartedAt, session.Summary)
	if err != nil {
		return fmt.Errorf("failed to create review session: %w", err)
	}

	id, err := result.LastInsertId()
	if err != nil {
		return fmt.Errorf("failed to get session id: %w", err)
	}
	session.ID = id

	return nil
}

// UpdateReviewSession updates a review session with completion stats.
func (s *SQLiteStorage) UpdateReviewSession(ctx context.Context, session *types.ReviewSession) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE review_sessions
		SET completed_at = ?, summary = ?, items_reviewed = ?,
		    items_approved = ?, items_needs_revision = ?, items_deferred = ?
		WHERE id = ?
	`, session.CompletedAt, session.Summary, session.ItemsReviewed,
		session.ItemsApproved, session.ItemsNeedsRevision, session.ItemsDeferred, session.ID)
	if err != nil {
		return fmt.Errorf("failed to update review session: %w", err)
	}
	return nil
}

// GetReviewSession retrieves a review session by ID.
func (s *SQLiteStorage) GetReviewSession(ctx context.Context, id int64) (*types.ReviewSession, error) {
	var session types.ReviewSession
	var completedAt sql.NullTime
	var summary sql.NullString

	err := s.db.QueryRowContext(ctx, `
		SELECT id, root_issue_id, reviewer, started_at, completed_at, summary,
		       items_reviewed, items_approved, items_needs_revision, items_deferred
		FROM review_sessions
		WHERE id = ?
	`, id).Scan(&session.ID, &session.RootIssueID, &session.Reviewer, &session.StartedAt,
		&completedAt, &summary, &session.ItemsReviewed, &session.ItemsApproved,
		&session.ItemsNeedsRevision, &session.ItemsDeferred)
	if err != nil {
		if err == sql.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("failed to get review session: %w", err)
	}

	if completedAt.Valid {
		session.CompletedAt = &completedAt.Time
	}
	if summary.Valid {
		session.Summary = summary.String
	}

	return &session, nil
}

// GetReviewSessionsByReviewer retrieves review sessions for a specific reviewer.
func (s *SQLiteStorage) GetReviewSessionsByReviewer(ctx context.Context, reviewer string, limit int) ([]*types.ReviewSession, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, root_issue_id, reviewer, started_at, completed_at, summary,
		       items_reviewed, items_approved, items_needs_revision, items_deferred
		FROM review_sessions
		WHERE reviewer = ?
		ORDER BY started_at DESC
		LIMIT ?
	`, reviewer, limit)
	if err != nil {
		return nil, fmt.Errorf("failed to query review sessions: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var sessions []*types.ReviewSession
	for rows.Next() {
		var session types.ReviewSession
		var completedAt sql.NullTime
		var summary sql.NullString

		err := rows.Scan(&session.ID, &session.RootIssueID, &session.Reviewer, &session.StartedAt,
			&completedAt, &summary, &session.ItemsReviewed, &session.ItemsApproved,
			&session.ItemsNeedsRevision, &session.ItemsDeferred)
		if err != nil {
			return nil, fmt.Errorf("failed to scan review session: %w", err)
		}

		if completedAt.Valid {
			session.CompletedAt = &completedAt.Time
		}
		if summary.Valid {
			session.Summary = summary.String
		}

		sessions = append(sessions, &session)
	}

	return sessions, rows.Err()
}
