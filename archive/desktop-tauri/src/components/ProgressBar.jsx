export default function ProgressBar({ percent, colorClassName = "bg-sky-500" }) {
  const clamped = Math.max(0, Math.min(100, percent));
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
      <div
        className={`h-full rounded-full ${colorClassName} transition-all duration-500`}
        style={{ width: `${clamped}%` }}
      />
    </div>
  );
}
