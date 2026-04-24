import { forwardRef } from 'react';
import { Search, X } from 'lucide-react';
import { useUIStore } from '@/stores/ui-store';

export const SearchBar = forwardRef<HTMLInputElement>(function SearchBar(_props, ref) {
  const filterText = useUIStore((s) => s.filterText);
  const setFilterText = useUIStore((s) => s.setFilterText);

  return (
    <div className="relative mb-4">
      <Search
        size={16}
        className="absolute left-3 top-1/2 -translate-y-1/2 text-[--color-text2] pointer-events-none"
      />
      <input
        ref={ref}
        type="text"
        value={filterText}
        onChange={(e) => setFilterText(e.target.value)}
        placeholder="Filter runs by name, work item, purpose..."
        className="w-full bg-[--color-surface] border border-[--color-border] rounded-lg
                   pl-9 pr-9 py-2 text-sm text-[--color-text] placeholder:text-[--color-text2]
                   outline-none focus:border-[--color-accent] transition-colors"
      />
      {filterText && (
        <button
          onClick={() => setFilterText('')}
          className="absolute right-3 top-1/2 -translate-y-1/2 text-[--color-text2]
                     hover:text-[--color-text] transition-colors"
          aria-label="Clear filter"
        >
          <X size={16} />
        </button>
      )}
    </div>
  );
});
