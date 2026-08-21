interface SourceCitationsProps {
  sources: string[]
}

function DocumentIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-3.5 w-3.5 flex-shrink-0"
      aria-hidden="true"
    >
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2v6h6" />
    </svg>
  )
}

export function SourceCitations({ sources }: SourceCitationsProps) {
  return (
    <div className="mt-3 flex flex-wrap gap-2 border-t border-subtle pt-2">
      {sources.map((source) => (
        <span
          key={source}
          className="flex items-center gap-1 rounded-chip border border-subtle bg-accent-yellow px-2.5 py-1 text-xs text-ink shadow-soft-sm"
        >
          <DocumentIcon />
          {source}
        </span>
      ))}
    </div>
  )
}
