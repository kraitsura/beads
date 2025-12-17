package main

import (
	"fmt"

	"github.com/fatih/color"
	"github.com/spf13/cobra"
	"github.com/steveyegge/beads/internal/storage/sqlite"
)

var reviewHistoryCmd = &cobra.Command{
	Use:   "review-history <issue-id>",
	Short: "Show review history for an issue",
	Long: `Show all reviews from the local review history for an issue.

Examples:
  bd review-history bd-a1b2

Output shows all reviews in chronological order with reviewer, type, outcome, and notes.`,
	Args: cobra.ExactArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		issueID := args[0]
		ctx := rootCtx

		// Check if issue exists
		issue, err := store.GetIssue(ctx, issueID)
		if err != nil {
			FatalError("failed to get issue: %v", err)
		}
		if issue == nil {
			FatalError("issue %s not found", issueID)
		}

		// Get SQLite store for review queries
		sqliteStore, ok := store.(*sqlite.SQLiteStorage)
		if !ok {
			FatalError("review-history requires SQLite storage (not supported in --no-db mode)")
		}

		// Get review history
		reviews, err := sqliteStore.GetReviewsByIssue(ctx, issueID)
		if err != nil {
			FatalError("failed to get review history: %v", err)
		}

		if jsonOutput {
			outputJSON(map[string]interface{}{
				"issue_id": issueID,
				"title":    issue.Title,
				"reviews":  reviews,
			})
			return
		}

		cyan := color.New(color.FgCyan).SprintFunc()
		fmt.Printf("\n%s: %s\n", cyan(issueID), issue.Title)
		fmt.Println("Review History:")

		if len(reviews) == 0 {
			fmt.Println("  (no reviews yet)")
		} else {
			for _, r := range reviews {
				notes := ""
				if r.Notes != "" {
					notes = fmt.Sprintf(" | %q", r.Notes)
				}
				fmt.Printf("  %s | %s | %s | %s%s\n",
					r.CreatedAt.Format("2006-01-02 15:04"),
					r.Reviewer,
					r.ReviewType,
					r.Outcome,
					notes)
			}
		}

		// Show current review status
		if issue.ReviewStatus != "" {
			fmt.Printf("\nCurrent status: %s", issue.ReviewStatus)
			if issue.ReviewedBy != "" {
				fmt.Printf(" by %s", issue.ReviewedBy)
			}
			if issue.ReviewedAt != nil {
				fmt.Printf(" on %s", issue.ReviewedAt.Format("2006-01-02 15:04"))
			}
			fmt.Println()
		}
		fmt.Println()
	},
}

func init() {
	rootCmd.AddCommand(reviewHistoryCmd)
}
