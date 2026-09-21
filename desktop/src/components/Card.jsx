export default function Card({ children, className = "", as: Component = "div", ...rest }) {
  return (
    <Component
      className={`rounded-xl border border-slate-200 bg-white shadow-sm ${className}`}
      {...rest}
    >
      {children}
    </Component>
  );
}
