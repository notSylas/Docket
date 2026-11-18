import { QUERY_MODE_META } from "../constants/queryMode";
import Badge from "./Badge";

/** Colored badge for a `QueryResult.mode` value ("fast" | "agent"). */
export default function ModeBadge({ mode }) {
  const meta = QUERY_MODE_META[mode] || {
    label: mode,
    badgeClass: "bg-slate-100 text-slate-600 ring-slate-500/20",
  };
  return (
    <Badge className={meta.badgeClass} title={meta.description}>
      {meta.label}
    </Badge>
  );
}
