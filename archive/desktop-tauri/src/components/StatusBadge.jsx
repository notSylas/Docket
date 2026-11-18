import { SOURCE_STATUS_META } from "../constants/sourceStatus";
import Badge from "./Badge";

/** Colored badge for a `Source.status` value (SourceStatus enum). */
export default function StatusBadge({ status }) {
  const meta = SOURCE_STATUS_META[status] || {
    label: status,
    badgeClass: "bg-slate-100 text-slate-600 ring-slate-500/20",
    dotClass: "bg-slate-400",
  };
  return (
    <Badge className={meta.badgeClass} dotClassName={meta.dotClass}>
      {meta.label}
    </Badge>
  );
}
