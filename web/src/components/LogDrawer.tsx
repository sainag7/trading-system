import { useEffect, useRef } from "react";
import { CheckCircle2, XCircle, Loader2, X } from "lucide-react";
import { useJob } from "../lib/useJob";

export function LogDrawer() {
  const { job, dismiss } = useJob();
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Auto-scroll to the newest output.
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight });
  }, [job?.log]);

  if (!job) return null;

  const running = job.status === "running" || job.status === "starting";
  const icon = running ? <Loader2 className="spin" size={16} />
    : job.status === "done" ? <CheckCircle2 size={16} color="var(--green)" />
    : <XCircle size={16} color="var(--red)" />;

  return (
    <div className="drawer" role="status" aria-live="polite">
      <div className="drawer-head">
        {icon}
        <strong style={{ fontSize: 13 }}>{job.label}</strong>
        <span className="badge badge-neutral" style={{ marginLeft: 4 }}>{job.status}</span>
        <div className="spacer" />
        <button className="btn btn-icon btn-ghost" onClick={dismiss} aria-label="Dismiss"
          disabled={running} title={running ? "Runs in the background" : "Dismiss"}>
          <X size={15} />
        </button>
      </div>
      <div className="drawer-body" ref={bodyRef}>
        <pre className="log">{job.log || (running ? "starting…" : "")}</pre>
        {job.error && <div className="callout callout-danger" style={{ marginTop: 10 }}>{job.error}</div>}
      </div>
    </div>
  );
}
