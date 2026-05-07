export function DecisionBadge({ decision }: { decision: string }) {
  const map: Record<string, string> = {
    APPLY_NOW: 'badge-apply',
    TAILOR_RESUME_FIRST: 'badge-tailor',
    SAVE_FOR_LATER: 'badge-save',
    SKIP: 'badge-skip',
    HIGH_RISK: 'badge-risk',
    REVIEW_NEEDED: 'badge-review',
  }
  const labels: Record<string, string> = {
    APPLY_NOW: '🚀 Apply Now',
    TAILOR_RESUME_FIRST: '✏️ Tailor First',
    SAVE_FOR_LATER: '💾 Save',
    SKIP: '⏭️ Skip',
    HIGH_RISK: '⚠️ High Risk',
    REVIEW_NEEDED: '👁️ Review',
  }
  return (
    <span className={map[decision] || 'badge-skip'}>
      {labels[decision] || decision}
    </span>
  )
}
