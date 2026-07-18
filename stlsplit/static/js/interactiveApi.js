// Thin wrappers around the interactive-split session endpoints (see
// stlsplit/web.py's "Interactive split mode" section and
// stlsplit/sessions.py). Mirrors api.js's style: plain functions, no Vue
// reactivity, so interactiveState.js can await/subscribe without caring how
// the request was made.

export async function createSession(formData) {
  const resp = await fetch("/sessions", { method: "POST", body: formData });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || "failed to create session");
  return data;
}

export async function getSessionTree(sessionId) {
  const resp = await fetch(`/sessions/${sessionId}`);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || "failed to fetch session");
  return data;
}

export async function getPiecePreview(sessionId, pieceId) {
  const resp = await fetch(`/sessions/${sessionId}/pieces/${pieceId}/preview`);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || "failed to fetch piece preview");
  return data;
}

export async function piecePlanePreview(sessionId, pieceId, formData) {
  const resp = await fetch(`/sessions/${sessionId}/pieces/${pieceId}/plane_preview`, {
    method: "POST", body: formData,
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || "plane preview failed");
  return data;
}

export async function cutPiece(sessionId, pieceId, formData) {
  const resp = await fetch(`/sessions/${sessionId}/pieces/${pieceId}/cut`, {
    method: "POST", body: formData,
  });
  const data = await resp.json();
  if (!resp.ok) {
    const err = new Error(data.detail || "cut failed");
    err.axis = data.axis;
    err.positions = data.positions;
    throw err;
  }
  return data;
}

export async function rotatePiece(sessionId, pieceId, formData) {
  const resp = await fetch(`/sessions/${sessionId}/pieces/${pieceId}/rotate`, {
    method: "POST", body: formData,
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || "rotate failed");
  return data;
}

export async function alignPiece(sessionId, pieceId) {
  const resp = await fetch(`/sessions/${sessionId}/pieces/${pieceId}/align`, { method: "POST" });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || "align failed");
  return data;
}

export async function undoCut(sessionId, pieceId) {
  const resp = await fetch(`/sessions/${sessionId}/pieces/${pieceId}/undo`, { method: "POST" });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || "undo failed");
  return data;
}

export async function exportSession(sessionId) {
  // Returns the raw Blob (not JSON-parsed) plus the filename the server
  // suggested via Content-Disposition, so the caller can trigger a browser
  // download without re-serializing anything client-side.
  const resp = await fetch(`/sessions/${sessionId}/export`);
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || "export failed");
  }
  const disposition = resp.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/);
  const filename = match ? match[1] : "session.json";
  const blob = await resp.blob();
  return { blob, filename };
}

export async function resumeSession(originalFile, sessionJsonFile) {
  const fd = new FormData();
  fd.set("file", originalFile);
  fd.set("session_json", sessionJsonFile);
  const resp = await fetch("/sessions/resume", { method: "POST", body: fd });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || "resume failed");
  return data;
}

export async function deleteSession(sessionId) {
  try {
    await fetch(`/sessions/${sessionId}`, { method: "DELETE" });
  } catch (err) {
    // best-effort -- the server sweeps idle sessions on its own anyway
  }
}

export async function finishSession(sessionId, formData) {
  const resp = await fetch(`/sessions/${sessionId}/finish`, { method: "POST", body: formData });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || "failed to start finish job");
  return data.job_id;
}
