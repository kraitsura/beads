package main

import (
	"fmt"

	"github.com/spf13/cobra"
	"github.com/steveyegge/beads/internal/storage/sqlite"
	"github.com/steveyegge/beads/internal/types"
	"github.com/steveyegge/beads/internal/ui"
)

var reviewCmd = &cobra.Command{
	Use:   "review <issue-id>",
	Short: "Review an issue (approve, request revision, or defer)",
	Long: `Review an issue by setting its review status.

Examples:
  # Approve an issue
  bd review bd-a1b2 --approve --reviewer alice

  # Request revision with notes
  bd review bd-a1b2 --revise --reviewer alice --notes "Need error handling for edge case"

  # Defer an issue
  bd review bd-a1b2 --defer --reviewer alice --notes "Waiting for API spec"

  # Specify review type
  bd review bd-a1b2 --approve --reviewer alice --type security

The review command:
1. Updates the issue's review_status, reviewed_by, and reviewed_at fields
2. Creates a local review history entry (in the reviews table)
3. Triggers JSONL export for syncing with other clones`,
	Args: cobra.ExactArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		CheckReadonly("review")

		issueID := args[0]

		// Get flags
		approve, _ := cmd.Flags().GetBool("approve")
		revise, _ := cmd.Flags().GetBool("revise")
		deferReview, _ := cmd.Flags().GetBool("defer")
		reviewer, _ := cmd.Flags().GetString("reviewer")
		notes, _ := cmd.Flags().GetString("notes")
		reviewType, _ := cmd.Flags().GetString("type")
		jsonOutput, _ := cmd.Flags().GetBool("json")

		// Validate that exactly one outcome flag is set
		outcomeCount := 0
		if approve {
			outcomeCount++
		}
		if revise {
			outcomeCount++
		}
		if deferReview {
			outcomeCount++
		}

		if outcomeCount == 0 {
			FatalError("must specify one of --approve, --revise, or --defer")
		}
		if outcomeCount > 1 {
			FatalError("can only specify one of --approve, --revise, or --defer")
		}

		// Validate reviewer is provided
		if reviewer == "" {
			FatalError("--reviewer is required")
		}

		// Determine outcome
		var outcome string
		switch {
		case approve:
			outcome = types.ReviewOutcomeApproved
		case revise:
			outcome = types.ReviewOutcomeNeedsRevision
		case deferReview:
			outcome = types.ReviewOutcomeDeferred
		}

		// Validate review type
		rt := types.ReviewType(reviewType)
		if !rt.IsValid() {
			FatalError("invalid review type %q (valid: plan, implementation, security)", reviewType)
		}

		ctx := rootCtx

		// Check if issue exists
		issue, err := store.GetIssue(ctx, issueID)
		if err != nil {
			FatalError("failed to get issue: %v", err)
		}
		if issue == nil {
			FatalError("issue %s not found", issueID)
		}

		// Create the review record
		review := &types.Review{
			IssueID:    issueID,
			ReviewType: rt,
			Outcome:    outcome,
			Reviewer:   reviewer,
			Notes:      notes,
		}

		// Use SQLite store for review creation
		sqliteStore, ok := store.(*sqlite.SQLiteStorage)
		if !ok {
			FatalError("review command requires SQLite storage (not supported in --no-db mode)")
		}

		if err := sqliteStore.CreateReview(ctx, review, actor); err != nil {
			FatalError("failed to create review: %v", err)
		}

		// Schedule auto-flush
		markDirtyAndScheduleFlush()

		if jsonOutput {
			outputJSON(map[string]interface{}{
				"issue_id":      issueID,
				"review_status": outcome,
				"reviewed_by":   reviewer,
				"review_type":   rt,
				"notes":         notes,
			})
		} else {
			fmt.Printf("%s Reviewed issue: %s\n", ui.RenderPass("✓"), issueID)
			fmt.Printf("  Title: %s\n", issue.Title)
			fmt.Printf("  Status: %s -> %s\n", formatReviewStatus(issue.ReviewStatus), outcome)
			fmt.Printf("  Reviewer: %s\n", reviewer)
			fmt.Printf("  Type: %s\n", rt)
			if notes != "" {
				fmt.Printf("  Notes: %s\n", notes)
			}
		}
	},
}

// formatReviewStatus returns a display-friendly version of the review status
func formatReviewStatus(status types.ReviewStatus) string {
	if status == "" {
		return "unreviewed"
	}
	return string(status)
}

func init() {
	reviewCmd.Flags().Bool("approve", false, "Approve the issue")
	reviewCmd.Flags().Bool("revise", false, "Request revision")
	reviewCmd.Flags().Bool("defer", false, "Defer the review")
	reviewCmd.Flags().String("reviewer", "", "Reviewer name (required)")
	reviewCmd.Flags().String("notes", "", "Review notes")
	reviewCmd.Flags().String("type", "plan", "Review type (plan|implementation|security)")
	reviewCmd.Flags().Bool("json", false, "Output JSON format")
	rootCmd.AddCommand(reviewCmd)
}
