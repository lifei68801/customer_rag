interface SourceCitationsProps {
  sources: string[]
}

export function SourceCitations({ sources }: SourceCitationsProps) {
  return (
    <div className="mt-3 flex flex-wrap gap-2 border-t border-surface-border pt-2">
      {sources.map((source) => (
        <span
          key={source}
          className="rounded-full bg-surface-raised px-2.5 py-1 text-xs text-content-secondary"
        >
          📄 {source}
        </span>
      ))}
    </div>
  )
}
