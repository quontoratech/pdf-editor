let sessionId = null;
let currentState = null;
let currentUser = null;
let zoom = 1.35;
let toastTimer = null;

const $ = (id) => document.getElementById(id);
const pdfInput = $("pdfInput");
const docxInput = $("docxInput");
const fontInput = $("fontInput");
const pageImage = $("pageImage");
const selectionBox = $("selectionBox");

function setBusy(on, text = "Working…") {
  $("busy").classList.toggle("hidden", !on);
  $("busy").querySelector("span").textContent = text;
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
}

function setLoginError(message = "") {
  const el = $("loginError");
  el.textContent = message;
  el.classList.toggle("hidden", !message);
}

function resetEditorUi() {
  sessionId = null;
  currentState = null;
  zoom = 1.35;
  $("zoomSlider").value = "135";
  setText("zoomLabel", "Zoom 135%");
  setText("pageLabel", "No PDF");
  setText("docxLabel", "DOCX reference: not loaded");
  setText("status", "Open a PDF to begin.");
  $("emptyState").classList.remove("hidden");
  $("pageScroller").classList.add("hidden");
  $("exportExtras").classList.add("hidden");
  selectionBox.classList.add("hidden");
  pageImage.removeAttribute("src");
}

function showLogin(message = "") {
  currentUser = null;
  $("loginView").classList.remove("hidden");
  $("appShell").classList.add("hidden");
  document.querySelectorAll(".admin-only").forEach((el) => el.classList.add("hidden"));
  setLoginError(message);
  resetEditorUi();
  setTimeout(() => $("loginUsername").focus(), 0);
}

function showApp(user) {
  currentUser = user;
  $("loginView").classList.add("hidden");
  $("appShell").classList.remove("hidden");
  $("userBadge").textContent = `${user.username} · ${user.role}`;
  document
    .querySelectorAll(".admin-only")
    .forEach((el) => el.classList.toggle("hidden", user.role !== "admin"));
  setLoginError("");
}

async function api(url, options = {}) {
  const { allowAuthError, ...fetchOptions } = options;
  const response = await fetch(url, fetchOptions);
  if (response.status === 401 && !allowAuthError) {
    showLogin("Please log in.");
    throw new Error("Please log in.");
  }
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const data = await response.json();
      message = data.detail || message;
    } catch (_) {}
    throw new Error(message);
  }
  return response;
}

async function checkAuth() {
  try {
    const res = await api("/api/auth/me", { allowAuthError: true });
    const data = await res.json();
    showApp(data.user);
  } catch (_) {
    showLogin();
  }
}

async function loginUser(event) {
  event.preventDefault();
  setLoginError("");
  const username = $("loginUsername").value.trim();
  const password = $("loginPassword").value;
  setBusy(true, "Logging in...");
  try {
    const res = await api("/api/auth/login", {
      method: "POST",
      allowAuthError: true,
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    $("loginPassword").value = "";
    showApp(data.user);
  } catch (err) {
    setLoginError(err.message);
  } finally {
    setBusy(false);
  }
}

async function logoutUser() {
  setBusy(true, "Logging out...");
  try {
    if (sessionId) {
      await api(`/api/${sessionId}`, { method: "DELETE", allowAuthError: true }).catch(() => {});
    }
    await api("/api/auth/logout", { method: "POST", allowAuthError: true }).catch(() => {});
  } finally {
    setBusy(false);
    closeUserModal();
    showLogin();
  }
}

function setText(id, value) {
  $(id).textContent = value ?? "-";
}

function setNoticeTone(el, tone) {
  el.classList.remove("success", "warning", "error", "neutral");
  el.classList.add(tone || "neutral");
}

function updateSelectionOverlay() {
  const selection = currentState?.selection;
  if (!selection || !pageImage.complete || !pageImage.naturalWidth) {
    selectionBox.classList.add("hidden");
    return;
  }
  const [x0, y0, x1, y1] = selection.bbox;
  const scaleX = pageImage.clientWidth / pageImage.naturalWidth;
  const scaleY = pageImage.clientHeight / pageImage.naturalHeight;
  const renderedZoomX = zoom * scaleX;
  const renderedZoomY = zoom * scaleY;
  selectionBox.style.left = `${x0 * renderedZoomX}px`;
  selectionBox.style.top = `${y0 * renderedZoomY}px`;
  selectionBox.style.width = `${Math.max(1, (x1 - x0) * renderedZoomX)}px`;
  selectionBox.style.height = `${Math.max(1, (y1 - y0) * renderedZoomY)}px`;
  selectionBox.classList.remove("hidden");
}

function refreshPageImage() {
  if (!sessionId || !currentState) return;
  pageImage.src = `/api/${sessionId}/page.png?zoom=${zoom}&_=${Date.now()}`;
}

function applyState(state, options = {}) {
  currentState = state;
  const hasPdf = !!sessionId && state.page_count > 0;
  const hasSelection = !!state.selection;

  $("emptyState").classList.toggle("hidden", hasPdf);
  $("pageScroller").classList.toggle("hidden", !hasPdf);

  $("saveBtn").disabled = !state.can_save;
  $("undoBtn").disabled = !state.undo_available;
  $("prevBtn").disabled = !hasPdf || state.page_index <= 0;
  $("nextBtn").disabled = !hasPdf || state.page_index >= state.page_count - 1;
  $("loadFontBtn").disabled = !hasSelection;
  $("findFontBtn").disabled = !hasSelection;
  $("checkBtn").disabled = !hasSelection;
  $("replaceBtn").disabled = !hasSelection;
  $("newText").disabled = !hasSelection;

  setText("pageLabel", hasPdf ? `Page ${state.page_number} / ${state.page_count}` : "No PDF");
  setText("docxLabel", state.docx_label);
  setText("status", state.status);

  const s = state.selection;
  if (s) {
    if (options.resetReplacement !== false) $("newText").value = s.text;
    $("originalText").value = s.text;
    setText("pdfFont", s.internal_font);
    setText("pdfStyle", s.style);
    setText("pdfSize", s.size);
    setText("pdfColor", s.color);
    setText("pdfPosition", s.position);
  } else {
    $("originalText").value = "";
    $("newText").value = "";
    ["pdfFont", "pdfStyle", "pdfSize", "pdfColor", "pdfPosition"].forEach(id => setText(id, "-"));
  }

  const d = state.docx || {};
  setText("docxFamily", d.family || "-");
  setText("docxDeclared", d.declared || "-");
  setText("docxSize", d.size || "-");
  setText("docxWeight", d.weight || "-");
  setText("docxItalic", d.italic || "-");
  setText("docxConfidence", d.confidence || "-");
  setText("docxReason", d.reason || "-");
  $("docxConfidence").className = `pill ${d.tone || "neutral"}`;

  const r = state.resolution || {};
  setText("fontMode", r.mode || "Automatic");
  setText("resolvedFamily", r.family || "-");
  setText("resolvedFace", r.face || "-");
  setText("resolvedWeight", r.weight || "-");
  setText("resolvedSource", r.source || "-");

  setText("matchStatus", state.match_status || "Select text to begin.");
  setNoticeTone($("matchStatus"), state.match_tone || "neutral");

  $("exportExtras").classList.toggle(
    "hidden",
    !state.audit_available && !state.validation_available
  );

  if (options.refreshImage !== false && hasPdf) refreshPageImage();
  else updateSelectionOverlay();
}

async function uploadPdf(file) {
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  setBusy(true, "Opening PDF…");
  try {
    const res = await api("/api/pdf", { method: "POST", body: fd });
    const data = await res.json();
    sessionId = data.session_id;
    zoom = 1.35;
    $("zoomSlider").value = "135";
    setText("zoomLabel", "Zoom 135%");
    applyState(data.state, { resetReplacement: true, refreshImage: true });
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(false);
    pdfInput.value = "";
  }
}

async function uploadDocx(file) {
  if (!sessionId || !file) return;
  const fd = new FormData(); fd.append("file", file);
  setBusy(true, "Loading DOCX reference…");
  try {
    const res = await api(`/api/${sessionId}/docx`, { method: "POST", body: fd });
    const data = await res.json();
    applyState(data.state, { resetReplacement: false, refreshImage: false });
    toast(`DOCX reference loaded (${data.count} formatted runs).`);
  } catch (err) { toast(err.message); }
  finally { setBusy(false); docxInput.value = ""; }
}

async function uploadFont(file) {
  if (!sessionId || !file) return;
  const fd = new FormData(); fd.append("file", file);
  setBusy(true, "Loading exact font…");
  try {
    const res = await api(`/api/${sessionId}/font`, { method: "POST", body: fd });
    const data = await res.json();
    applyState(data.state, { resetReplacement: false, refreshImage: false });
    toast(`Font loaded: ${data.font.face}`);
  } catch (err) { toast(err.message); }
  finally { setBusy(false); fontInput.value = ""; }
}

async function changePage(delta) {
  if (!currentState || !sessionId) return;
  const target = currentState.page_index + delta;
  setBusy(true, "Loading page…");
  try {
    const res = await api(`/api/${sessionId}/page`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ page_index: target })
    });
    applyState(await res.json(), { resetReplacement: true, refreshImage: true });
  } catch (err) { toast(err.message); }
  finally { setBusy(false); }
}

async function selectAt(event) {
  if (!sessionId || !currentState) return;
  const rect = pageImage.getBoundingClientRect();
  const renderedX = event.clientX - rect.left;
  const renderedY = event.clientY - rect.top;
  const scaleX = pageImage.naturalWidth / pageImage.clientWidth;
  const scaleY = pageImage.naturalHeight / pageImage.clientHeight;
  const x = (renderedX * scaleX) / zoom;
  const y = (renderedY * scaleY) / zoom;
  try {
    const res = await api(`/api/${sessionId}/select`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ x, y })
    });
    applyState(await res.json(), { resetReplacement: true, refreshImage: false });
  } catch (err) { toast(err.message); }
}

async function resolveFont() {
  if (!sessionId || !currentState?.selection) return;
  setBusy(true, "Checking exact font…");
  try {
    const res = await api(`/api/${sessionId}/resolve`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ replacement: $("newText").value })
    });
    applyState(await res.json(), { resetReplacement: false, refreshImage: false });
  } catch (err) { toast(err.message); }
  finally { setBusy(false); }
}

async function replaceText() {
  if (!sessionId || !currentState?.selection) return;
  setBusy(true, "Replacing text…");
  try {
    const res = await api(`/api/${sessionId}/replace`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ replacement: $("newText").value })
    });
    applyState(await res.json(), { resetReplacement: true, refreshImage: true });
  } catch (err) { toast(err.message); }
  finally { setBusy(false); }
}

async function undoLast() {
  if (!sessionId) return;
  setBusy(true, "Undoing last replacement…");
  try {
    const res = await api(`/api/${sessionId}/undo`, { method: "POST" });
    applyState(await res.json(), { resetReplacement: true, refreshImage: true });
  } catch (err) { toast(err.message); }
  finally { setBusy(false); }
}

async function downloadEndpoint(path, fallbackName) {
  setBusy(true, "Preparing download…");
  try {
    const res = await api(path);
    const blob = await res.blob();
    const disposition = res.headers.get("content-disposition") || "";
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const filename = match ? match[1] : fallbackName;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1200);
  } catch (err) { toast(err.message); throw err; }
  finally { setBusy(false); }
}

async function savePdf() {
  if (!sessionId) return;
  try {
    await downloadEndpoint(`/api/${sessionId}/export`, "edited_clean.pdf");
    const res = await api(`/api/${sessionId}/state`);
    applyState(await res.json(), { resetReplacement: false, refreshImage: false });
    toast("Edited PDF downloaded. Audit and validation files are now available too.");
  } catch (_) {}
}

function markReplacementChanged() {
  if (!currentState?.selection) return;
  currentState.resolution = { ...currentState.resolution, family: "-", face: "-", weight: "-", source: "-", ok: false };
  setText("resolvedFamily", "-"); setText("resolvedFace", "-"); setText("resolvedWeight", "-");
  setText("resolvedSource", "Replacement changed — exact font must be re-verified.");
  setText("matchStatus", "Text changed. Click 'Re-check Font Match' before replacing.\nThe previous font result will not be reused.");
  setNoticeTone($("matchStatus"), "warning");
}

function openUserModal(mode) {
  if (!currentUser || currentUser.role !== "admin") return;
  $("userModal").classList.remove("hidden");
  $("userModal").setAttribute("aria-hidden", "false");
  const createMode = mode === "create";
  $("userModalTitle").textContent = createMode ? "Make User" : "All Users";
  $("createUserForm").classList.toggle("hidden", !createMode);
  $("userListView").classList.toggle("hidden", createMode);
  if (createMode) {
    $("createUserForm").reset();
    setTimeout(() => $("newUsername").focus(), 0);
  } else {
    loadUsers();
  }
}

function closeUserModal() {
  $("userModal").classList.add("hidden");
  $("userModal").setAttribute("aria-hidden", "true");
}

function formatCreated(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString();
}

function renderUsers(users) {
  const rows = $("userListRows");
  rows.replaceChildren();
  if (!users.length) {
    $("userListStatus").textContent = "No users yet.";
    return;
  }
  $("userListStatus").textContent = `${users.length} user${users.length === 1 ? "" : "s"}`;
  users.forEach((user) => {
    const row = document.createElement("div");
    row.className = "user-row";

    const main = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = user.username;
    const meta = document.createElement("span");
    meta.textContent = `Created ${formatCreated(user.created_at)}`;
    main.append(name, meta);

    const role = document.createElement("div");
    role.className = "role-pill";
    role.textContent = user.role;

    row.append(main, role);
    rows.append(row);
  });
}

async function loadUsers() {
  $("userListStatus").textContent = "Loading users...";
  $("userListRows").replaceChildren();
  try {
    const res = await api("/api/admin/users");
    const data = await res.json();
    renderUsers(data.users || []);
  } catch (err) {
    $("userListStatus").textContent = err.message;
  }
}

async function createUser(event) {
  event.preventDefault();
  const username = $("newUsername").value.trim();
  const password = $("newUserPassword").value;
  setBusy(true, "Creating user...");
  try {
    const res = await api("/api/admin/users", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    $("createUserForm").reset();
    $("newUsername").focus();
    toast(`User created: ${data.user.username}`);
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(false);
  }
}

$("loginForm").addEventListener("submit", loginUser);
$("logoutBtn").addEventListener("click", logoutUser);
$("makeUserBtn").addEventListener("click", () => openUserModal("create"));
$("allUsersBtn").addEventListener("click", () => openUserModal("list"));
$("closeUserModalBtn").addEventListener("click", closeUserModal);
$("createUserForm").addEventListener("submit", createUser);
$("userModal").addEventListener("click", (event) => {
  if (event.target === $("userModal")) closeUserModal();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeUserModal();
});
$("openPdfBtn").addEventListener("click", () => pdfInput.click());
$("emptyOpenBtn").addEventListener("click", () => pdfInput.click());
$("loadDocxBtn").addEventListener("click", () => sessionId ? docxInput.click() : toast("Open a PDF first."));
$("loadFontBtn").addEventListener("click", () => fontInput.click());
pdfInput.addEventListener("change", () => uploadPdf(pdfInput.files[0]));
docxInput.addEventListener("change", () => uploadDocx(docxInput.files[0]));
fontInput.addEventListener("change", () => uploadFont(fontInput.files[0]));
$("prevBtn").addEventListener("click", () => changePage(-1));
$("nextBtn").addEventListener("click", () => changePage(1));
pageImage.addEventListener("click", selectAt);
pageImage.addEventListener("load", updateSelectionOverlay);
window.addEventListener("resize", updateSelectionOverlay);
$("checkBtn").addEventListener("click", resolveFont);
$("replaceBtn").addEventListener("click", replaceText);
$("undoBtn").addEventListener("click", undoLast);
$("saveBtn").addEventListener("click", savePdf);
$("newText").addEventListener("input", markReplacementChanged);

$("zoomSlider").addEventListener("input", (event) => {
  zoom = Number(event.target.value) / 100;
  setText("zoomLabel", `Zoom ${event.target.value}%`);
});
$("zoomSlider").addEventListener("change", () => refreshPageImage());

$("findFontBtn").addEventListener("click", () => {
  const url = currentState?.selection?.font_search_url;
  if (url) window.open(url, "_blank", "noopener,noreferrer");
  else toast("The PDF does not expose a reliable font family name.");
});

$("fullscreenBtn").addEventListener("click", async () => {
  try {
    if (!document.fullscreenElement) await document.documentElement.requestFullscreen();
    else await document.exitFullscreen();
  } catch (err) { toast(err.message); }
});

document.addEventListener("fullscreenchange", () => {
  $("fullscreenBtn").textContent = document.fullscreenElement ? "Exit Full Screen" : "Full Screen";
});

$("auditBtn").addEventListener("click", () => downloadEndpoint(`/api/${sessionId}/audit`, "editlog.json"));
$("validationBtn").addEventListener("click", () => downloadEndpoint(`/api/${sessionId}/validation`, "validation.json"));

window.addEventListener("beforeunload", () => {
  if (sessionId) navigator.sendBeacon?.(`/api/${sessionId}/close`);
});

checkAuth();
