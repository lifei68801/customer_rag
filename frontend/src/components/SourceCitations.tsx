interface SourceCitationsProps {
  sources: string[]
}

export function SourceCitations({ sources }: SourceCitationsProps) {
  return (
    <div className="mt-3 flex flex-wrap gap-2 border-t border-ink pt-2">
      {sources.map((source) => (
        <span
          key={source}
          className="border border-ink bg-accent-yellow px-2.5 py-1 text-xs text-ink shadow-brutal-sm"
        >
          📄 {source}
        </span>
      ))}
    </div>
  )
}
