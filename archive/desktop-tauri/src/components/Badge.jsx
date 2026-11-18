// Generic pill badge. StatusBadge/ModeBadge build on this with fixed
// color maps; use Badge directly for one-off labels (claim type, authority,
// historical marker, etc.) where a dedicated component would be overkill.
export default function Badge({ children, className = "", dotClassName = null, ...rest }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${className}`}
      {...rest}
    >
      {dotClassName && <span className={`h-1.5 w-1.5 rounded-full ${dotClassName}`} />}
      {children}
    </span>
  );
}
