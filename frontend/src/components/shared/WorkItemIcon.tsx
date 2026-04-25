/**
 * SVG icons matching Azure DevOps work item type icons.
 * Maps icon_id values from twig DB process_types to inline SVGs.
 */

interface Props {
  iconId: string;
  color?: string;
  size?: number;
  className?: string;
}

/** ADO-style crown icon (Epic, Feature, Scenario) */
function CrownIcon({ color, size }: { color: string; size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <path
        d="M2 12h12V11L11.5 7 8 9.5 4.5 7 2 11v1z"
        fill={color}
      />
      <path
        d="M2 11l2.5-4L8 9.5 11.5 7 14 11"
        stroke={color}
        strokeWidth="1.2"
        fill="none"
      />
      <circle cx="2" cy="11" r="1" fill={color} />
      <circle cx="8" cy="9.5" r="1" fill={color} />
      <circle cx="14" cy="11" r="1" fill={color} />
      <circle cx="4.5" cy="7" r="1" fill={color} />
      <circle cx="11.5" cy="7" r="1" fill={color} />
      <rect x="2" y="12" width="12" height="1.5" rx="0.5" fill={color} />
    </svg>
  );
}

/** ADO-style checkbox icon (Task) */
function CheckBoxIcon({ color, size }: { color: string; size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="2" y="2" width="12" height="12" rx="2" fill={color} />
      <path
        d="M5 8l2 2 4-4"
        stroke="white"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** ADO-style bug/insect icon (Bug) */
function InsectIcon({ color, size }: { color: string; size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <ellipse cx="8" cy="9.5" rx="3.5" ry="4" fill={color} />
      <circle cx="8" cy="5" r="2.2" fill={color} />
      <line x1="4" y1="4" x2="5.5" y2="5.5" stroke={color} strokeWidth="1.2" strokeLinecap="round" />
      <line x1="12" y1="4" x2="10.5" y2="5.5" stroke={color} strokeWidth="1.2" strokeLinecap="round" />
      <line x1="3" y1="8" x2="4.5" y2="8.5" stroke={color} strokeWidth="1.2" strokeLinecap="round" />
      <line x1="13" y1="8" x2="11.5" y2="8.5" stroke={color} strokeWidth="1.2" strokeLinecap="round" />
      <line x1="3.5" y1="11" x2="4.8" y2="10.5" stroke={color} strokeWidth="1.2" strokeLinecap="round" />
      <line x1="12.5" y1="11" x2="11.2" y2="10.5" stroke={color} strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

/** ADO-style trophy icon (Deliverable) */
function TrophyIcon({ color, size }: { color: string; size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <path d="M5 3h6v5a3 3 0 01-6 0V3z" fill={color} />
      <path d="M5 4H3.5a1.5 1.5 0 000 3H5" stroke={color} strokeWidth="1.2" fill="none" />
      <path d="M11 4h1.5a1.5 1.5 0 010 3H11" stroke={color} strokeWidth="1.2" fill="none" />
      <line x1="8" y1="11" x2="8" y2="13" stroke={color} strokeWidth="1.2" />
      <rect x="5.5" y="13" width="5" height="1.2" rx="0.5" fill={color} />
    </svg>
  );
}

/** ADO-style book icon (Story) */
function BookIcon({ color, size }: { color: string; size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <path
        d="M3 2.5h4.5v11H4a1 1 0 01-1-1V2.5z"
        fill={color}
        opacity="0.8"
      />
      <path
        d="M7.5 2.5H12a1 1 0 011 1v9a1 1 0 01-1 1H7.5V2.5z"
        fill={color}
      />
      <line x1="9.5" y1="5" x2="11.5" y2="5" stroke="white" strokeWidth="0.8" strokeLinecap="round" />
      <line x1="9.5" y1="7" x2="11.5" y2="7" stroke="white" strokeWidth="0.8" strokeLinecap="round" />
    </svg>
  );
}

/** ADO-style list icon (Task Group) */
function ListIcon({ color, size }: { color: string; size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="2" y="2" width="12" height="12" rx="2" fill={color} opacity="0.2" stroke={color} strokeWidth="1" />
      <line x1="5" y1="5.5" x2="11" y2="5.5" stroke={color} strokeWidth="1.2" strokeLinecap="round" />
      <line x1="5" y1="8" x2="11" y2="8" stroke={color} strokeWidth="1.2" strokeLinecap="round" />
      <line x1="5" y1="10.5" x2="11" y2="10.5" stroke={color} strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

/** ADO-style diamond icon (Objective) */
function DiamondIcon({ color, size }: { color: string; size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="4" y="4" width="8" height="8" rx="1" fill={color} transform="rotate(45 8 8)" />
    </svg>
  );
}

/** ADO-style chart icon (Measure, Key Result) */
function ChartIcon({ color, size }: { color: string; size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="2.5" y="9" width="2.5" height="4.5" rx="0.5" fill={color} />
      <rect x="6.75" y="5" width="2.5" height="8.5" rx="0.5" fill={color} />
      <rect x="11" y="2.5" width="2.5" height="11" rx="0.5" fill={color} />
    </svg>
  );
}

/** ADO-style clipboard icon (generic fallback) */
function ClipboardIcon({ color, size }: { color: string; size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="3" y="3" width="10" height="11" rx="1.5" stroke={color} strokeWidth="1.2" fill="none" />
      <rect x="5.5" y="1.5" width="5" height="2.5" rx="1" fill={color} />
      <line x1="5.5" y1="7" x2="10.5" y2="7" stroke={color} strokeWidth="0.9" strokeLinecap="round" />
      <line x1="5.5" y1="9.5" x2="10.5" y2="9.5" stroke={color} strokeWidth="0.9" strokeLinecap="round" />
      <line x1="5.5" y1="12" x2="8.5" y2="12" stroke={color} strokeWidth="0.9" strokeLinecap="round" />
    </svg>
  );
}

/** ADO-style gift icon (Customer Promise) */
function GiftIcon({ color, size }: { color: string; size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="2" y="6" width="12" height="3" rx="1" fill={color} />
      <rect x="3" y="9" width="10" height="5" rx="1" fill={color} opacity="0.85" />
      <line x1="8" y1="6" x2="8" y2="14" stroke="white" strokeWidth="1" />
      <path d="M8 6C8 4 6 3 5 3.5S4.5 5.5 8 6" fill={color} />
      <path d="M8 6C8 4 10 3 11 3.5S11.5 5.5 8 6" fill={color} />
    </svg>
  );
}

const ICON_MAP: Record<string, React.FC<{ color: string; size: number }>> = {
  icon_crown: CrownIcon,
  icon_check_box: CheckBoxIcon,
  icon_insect: InsectIcon,
  icon_trophy: TrophyIcon,
  icon_book: BookIcon,
  icon_list: ListIcon,
  icon_diamond: DiamondIcon,
  icon_chart: ChartIcon,
  icon_clipboard: ClipboardIcon,
  icon_gift: GiftIcon,
  // Test-related icons mapped to clipboard as fallback
  icon_test_beaker: ClipboardIcon,
  icon_test_plan: ClipboardIcon,
  icon_test_suite: ClipboardIcon,
};

export function WorkItemIcon({ iconId, color = '#888', size = 14, className }: Props) {
  const Icon = ICON_MAP[iconId] ?? ClipboardIcon;
  return (
    <span className={`inline-flex shrink-0 ${className ?? ''}`} style={{ lineHeight: 0 }}>
      <Icon color={color} size={size} />
    </span>
  );
}
