"use client";
import type { JobFilters } from "@/types";

interface Props {
  filters: JobFilters;
  onChange: (f: JobFilters) => void;
}

const EXPERIENCE_OPTIONS = [
  { value: "entry",  label: "Entry level" },
  { value: "mid",    label: "Mid level" },
  { value: "senior", label: "Senior" },
  { value: "lead",   label: "Lead" },
] as const;

const REMOTE_OPTIONS = [
  { value: "remote", label: "🌍 Remote" },
  { value: "hybrid", label: "🏢 Hybrid" },
  { value: "onsite", label: "📌 On-site" },
] as const;

const FRESHNESS_OPTIONS = [
  { value: "last_24h",    label: "Last 24 hours" },
  { value: "last_7_days", label: "Last 7 days" },
] as const;

function Chip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
        active
          ? "bg-brand text-white border-brand"
          : "bg-white text-gray-600 border-gray-200 hover:border-brand hover:text-brand"
      }`}
    >
      {label}
    </button>
  );
}

function FilterGroup({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <h3 className="text-[11px] font-semibold text-gray-400 uppercase tracking-widest mb-2">
        {title}
      </h3>
      {children}
    </div>
  );
}

export default function FilterPanel({ filters, onChange }: Props) {
  // Toggle a single-select filter (click same value → deselect)
  function toggle(key: keyof JobFilters, value: string) {
    onChange({
      ...filters,
      [key]: filters[key] === value ? undefined : value,
    });
  }

  function clearAll() {
    onChange({ q: filters.q, location: filters.location });
  }

  const hasActive =
    filters.experience || filters.remote || filters.freshness || filters.country;

  return (
    <aside className="space-y-5">
      {/* Experience */}
      <FilterGroup title="Experience">
        <div className="flex flex-wrap gap-2">
          {EXPERIENCE_OPTIONS.map((opt) => (
            <Chip
              key={opt.value}
              label={opt.label}
              active={filters.experience === opt.value}
              onClick={() => toggle("experience", opt.value)}
            />
          ))}
        </div>
      </FilterGroup>

      {/* Work type */}
      <FilterGroup title="Work type">
        <div className="flex flex-wrap gap-2">
          {REMOTE_OPTIONS.map((opt) => (
            <Chip
              key={opt.value}
              label={opt.label}
              active={filters.remote === opt.value}
              onClick={() => toggle("remote", opt.value)}
            />
          ))}
        </div>
      </FilterGroup>

      {/* Date posted */}
      <FilterGroup title="Date posted">
        <div className="flex flex-wrap gap-2">
          {FRESHNESS_OPTIONS.map((opt) => (
            <Chip
              key={opt.value}
              label={opt.label}
              active={filters.freshness === opt.value}
              onClick={() => toggle("freshness", opt.value)}
            />
          ))}
        </div>
      </FilterGroup>

      {/* Country */}
      <FilterGroup title="Country (ISO-2)">
        <input
          type="text"
          value={filters.country ?? ""}
          onChange={(e) =>
            onChange({ ...filters, country: e.target.value.toUpperCase() || undefined })
          }
          placeholder="US, GB, DE …"
          maxLength={2}
          className="w-full px-3 py-2 text-sm rounded-lg border border-gray-200
                     focus:outline-none focus:ring-2 focus:ring-brand uppercase"
        />
      </FilterGroup>

      {/* Clear */}
      {hasActive && (
        <button
          type="button"
          onClick={clearAll}
          className="text-xs text-gray-400 hover:text-brand underline transition-colors"
        >
          Clear filters
        </button>
      )}
    </aside>
  );
}
