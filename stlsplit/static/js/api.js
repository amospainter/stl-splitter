// Thin wrappers around the backend's HTTP surface. Kept free of any Vue
// reactivity so components/state.js can await/subscribe without caring how
// the request was made.

// Starts a split job and returns its job_id, or throws with the server's
// error detail on a non-2xx response.
export async function startJob(formData) {
  const resp = await fetch("/jobs", { method: "POST", body: formData });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || resp.statusText);
  return data.job_id;
}

// Subscribes to a job's progress over Server-Sent Events (replaces the old
// client-side setTimeout poll loop against GET /jobs/{id} — the browser's
// native EventSource keeps one connection open and the server pushes a
// message per state change instead of the client re-asking every 400ms).
// `onUpdate` is called for every message (running/done/error); the stream
// closes itself once a terminal (done/error) message arrives.
export function streamJob(jobId, onUpdate) {
  const source = new EventSource(`/jobs/${jobId}/stream`);
  source.onmessage = (event) => {
    // The server closes its side of the connection right after sending a
    // terminal (done/error) message — from a plain EventSource's
    // perspective, a server-initiated close is indistinguishable from a
    // dropped connection, so the browser auto-reconnects and re-delivers
    // the same final message every retry interval (~3s) forever unless we
    // close it from the client side first. Closing is done *before*
    // calling onUpdate specifically so an exception in the caller's
    // handling (e.g. a reactive-state bug) can never leave the connection
    // open to loop like that.
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (err) {
      return; // malformed chunk; wait for the next message rather than crash the stream
    }
    if (data.status !== "running") source.close();
    onUpdate(data);
  };
  source.onerror = () => {
    // The connection can drop for reasons other than a terminal message
    // (e.g. the server process restarting); surface it once and stop.
    source.close();
    onUpdate({ status: "error", error: "Lost connection to the server." });
  };
  return () => source.close();
}

// Computes (smart, auto-placed) cut plane positions for one axis, without
// running the full split/connector pipeline — used to keep the interactive
// plane editor's gizmos in sync as scale/spacing/piece-count inputs change.
export async function planePreview(formData) {
  const resp = await fetch("/plane_preview", { method: "POST", body: formData });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || "preview failed");
  return data;
}
