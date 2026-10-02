let sessionId = null;
let currentState = null;
let currentUser = null;
let zoom = 1.35;
let toastTimer = null;
let savedFontsCache = [];
let tableLineDeleteMode = false;
let tableLineDeletePending = false;

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

  toastTimer = setTimeout(
    () => el.classList.add("hidden"),
    4200
  );
}


function setLoginError(message = "") {
  const el = $("loginError");

  el.textContent = message;

  el.classList.toggle(
    "hidden",
    !message
  );
}


function resetEditorUi() {
  sessionId = null;
  currentState = null;
  zoom = 1.35;
  tableLineDeleteMode = false;
  tableLineDeletePending = false;

  $("zoomSlider").value = "135";

  setText(
    "zoomLabel",
    "Zoom 135%"
  );

  setText(
    "pageLabel",
    "No PDF"
  );

  setText(
    "docxLabel",
    "DOCX reference: not loaded"
  );

  setText(
    "status",
    "Open a PDF to begin."
  );

  $("emptyState").classList.remove(
    "hidden"
  );

  $("pageScroller").classList.add(
    "hidden"
  );

  $("exportExtras").classList.add(
    "hidden"
  );

  selectionBox.classList.add(
    "hidden"
  );

  $("addRowBelowBtn").disabled = true;
  $("addRowBelowBtn").textContent = "Add Full Row Below";
  setText(
    "rowInsertHelp",
    "To add a row, select text in an existing table row and click Select Full Line. " +
    "The row is cloned as an exact template, so any number of columns is handled automatically."
  );

  pageImage.removeAttribute(
    "src"
  );

  setExpansionDirection("right");
  setExpansionDirectionEnabled(false);
  syncTableLineDeleteModeUi();
}


function showLogin(message = "") {
  currentUser = null;

  $("loginView").classList.remove(
    "hidden"
  );

  $("appShell").classList.add(
    "hidden"
  );

  document
    .querySelectorAll(".admin-only")
    .forEach(
      (el) => el.classList.add("hidden")
    );

  setLoginError(message);

  resetEditorUi();

  setTimeout(
    () => $("loginUsername").focus(),
    0
  );
}


function showApp(user) {
  currentUser = user;

  $("loginView").classList.add(
    "hidden"
  );

  $("appShell").classList.remove(
    "hidden"
  );

  $("userBadge").textContent =
    `${user.username} · ${user.role}`;

  document
    .querySelectorAll(".admin-only")
    .forEach(
      (el) =>
        el.classList.toggle(
          "hidden",
          user.role !== "admin"
        )
    );

  setLoginError("");
}


async function api(url, options = {}) {
  const {
    allowAuthError,
    ...fetchOptions
  } = options;

  const response = await fetch(
    url,
    fetchOptions
  );

  if (
    response.status === 401 &&
    !allowAuthError
  ) {
    showLogin(
      "Please log in."
    );

    throw new Error(
      "Please log in."
    );
  }

  if (!response.ok) {
    let message =
      `Request failed (${response.status})`;

    try {
      const data =
        await response.json();

      message =
        data.detail ||
        message;

    } catch (_) {}

    throw new Error(message);
  }

  return response;
}


async function checkAuth() {
  try {
    const res =
      await api(
        "/api/auth/me",
        {
          allowAuthError: true
        }
      );

    const data =
      await res.json();

    showApp(data.user);

  } catch (_) {
    showLogin();
  }
}


async function loginUser(event) {
  event.preventDefault();

  setLoginError("");

  const username =
    $("loginUsername").value.trim();

  const password =
    $("loginPassword").value;

  setBusy(
    true,
    "Logging in..."
  );

  try {
    const res =
      await api(
        "/api/auth/login",
        {
          method: "POST",
          allowAuthError: true,

          headers: {
            "Content-Type":
              "application/json"
          },

          body: JSON.stringify({
            username,
            password
          })
        }
      );

    const data =
      await res.json();

    $("loginPassword").value =
      "";

    showApp(data.user);

  } catch (err) {
    setLoginError(
      err.message
    );

  } finally {
    setBusy(false);
  }
}


async function logoutUser() {
  setBusy(
    true,
    "Logging out..."
  );

  try {
    if (sessionId) {
      await api(
        `/api/${sessionId}`,
        {
          method: "DELETE",
          allowAuthError: true
        }
      ).catch(() => {});
    }

    await api(
      "/api/auth/logout",
      {
        method: "POST",
        allowAuthError: true
      }
    ).catch(() => {});

  } finally {
    setBusy(false);

    closeUserModal();

    showLogin();
  }
}


function setText(id, value) {
  $(id).textContent =
    value ?? "-";
}


function setNoticeTone(el, tone) {
  el.classList.remove(
    "success",
    "warning",
    "error",
    "neutral"
  );

  el.classList.add(
    tone || "neutral"
  );
}



function getExpansionDirection() {
  const selected = document.querySelector(
    'input[name="expansionDirection"]:checked'
  );
  return selected?.value === "left" ? "left" : "right";
}


function setExpansionDirection(direction = "right") {
  const value = direction === "left" ? "left" : "right";
  const radio = document.querySelector(
    `input[name="expansionDirection"][value="${value}"]`
  );
  if (radio) radio.checked = true;
}


function setExpansionDirectionEnabled(enabled) {
  document
    .querySelectorAll('input[name="expansionDirection"]')
    .forEach((radio) => {
      radio.disabled = !enabled;
    });
}


function updateSelectionOverlay() {
  const selection =
    currentState?.selection;

  if (
    tableLineDeleteMode ||
    !selection ||
    !pageImage.complete ||
    !pageImage.naturalWidth
  ) {
    selectionBox.classList.add(
      "hidden"
    );

    return;
  }

  const [
    x0,
    y0,
    x1,
    y1
  ] = selection.bbox;

  const scaleX =
    pageImage.clientWidth /
    pageImage.naturalWidth;

  const scaleY =
    pageImage.clientHeight /
    pageImage.naturalHeight;

  const renderedZoomX =
    zoom * scaleX;

  const renderedZoomY =
    zoom * scaleY;

  selectionBox.style.left =
    `${x0 * renderedZoomX}px`;

  selectionBox.style.top =
    `${y0 * renderedZoomY}px`;

  selectionBox.style.width =
    `${Math.max(
      1,
      (x1 - x0) * renderedZoomX
    )}px`;

  selectionBox.style.height =
    `${Math.max(
      1,
      (y1 - y0) * renderedZoomY
    )}px`;

  selectionBox.classList.remove(
    "hidden"
  );
}


function refreshPageImage() {
  if (
    !sessionId ||
    !currentState
  ) {
    return;
  }

  pageImage.src =
    `/api/${sessionId}/page.png` +
    `?zoom=${zoom}` +
    `&_=${Date.now()}`;
}


function applyState(
  state,
  options = {}
) {
  currentState = state;

  const hasPdf =
    !!sessionId &&
    state.page_count > 0;

  const hasSelection =
    !!state.selection;

  const lineSelected =
    !!state.line_selected ||
    !!state.selection?.line_selected;

  const canReplaceSelection =
    hasSelection && !lineSelected;

  $("emptyState").classList.toggle(
    "hidden",
    hasPdf
  );

  $("pageScroller").classList.toggle(
    "hidden",
    !hasPdf
  );

  $("saveBtn").disabled =
    !state.can_save;

  $("undoBtn").disabled =
    !state.undo_available;

  $("prevBtn").disabled =
    !hasPdf ||
    state.page_index <= 0;

  $("nextBtn").disabled =
    !hasPdf ||
    state.page_index >=
      state.page_count - 1;

  $("addPageBtn").disabled =
    !hasPdf;

  $("deletePageBtn").disabled =
    !hasPdf || state.page_count <= 1;

  if (!hasPdf) {
    tableLineDeleteMode = false;
  }

  syncTableLineDeleteModeUi();

  $("selectLineBtn").disabled =
    !hasSelection;

  $("deleteLineBtn").disabled =
    !lineSelected;

  const rowInsert = state.row_insert || {};
  const rowColumnCount = Math.max(
    0,
    Number(rowInsert.column_count || 0)
  );
  const canAddTableRow =
    lineSelected && rowInsert.eligible === true;

  $("addRowBelowBtn").disabled =
    !canAddTableRow;

  $("addRowBelowBtn").textContent =
    canAddTableRow && rowColumnCount > 0
      ? `Add Full Row Below (${rowColumnCount} column${rowColumnCount === 1 ? "" : "s"})`
      : "Add Full Row Below";

  if (!lineSelected) {
    setText(
      "rowInsertHelp",
      "To add a row, select text in an existing table row and click Select Full Line. " +
      "The row is cloned as an exact template, so any number of columns is handled automatically."
    );
  } else if (canAddTableRow) {
    setText(
      "rowInsertHelp",
      `Detected ${rowColumnCount} table column${rowColumnCount === 1 ? "" : "s"}. ` +
      "Add Full Row Below clones the selected row with its exact PDF font resources, " +
      "column positions, alignment, and separator style. Then click each cloned cell to edit its value."
    );
  } else {
    setText(
      "rowInsertHelp",
      rowInsert.reason ||
      "This full line is not a safely detected multi-column table row, so a row cannot be inserted from it."
    );
  }

  $("removeTableRule").disabled =
    !lineSelected;

  if (
    lineSelected &&
    options.resetLineDeleteOptions !== false
  ) {
    $("removeTableRule").checked =
      state.line_delete_options?.remove_table_rule_default === true;
  }

  $("loadFontBtn").disabled =
    !canReplaceSelection;

  $("dbFontBtn").disabled =
    !canReplaceSelection;

  $("pdfGlyphBtn").disabled =
    !canReplaceSelection;

  $("closestFontBtn").disabled =
    !canReplaceSelection;

  $("outlineGlyphBtn").disabled =
    !canReplaceSelection;

  $("findFontBtn").disabled =
    !canReplaceSelection;

  $("checkBtn").disabled =
    !canReplaceSelection;

  $("replaceBtn").disabled =
    !canReplaceSelection;

  $("replaceBtn").textContent =
    state.resolution?.fallback_mode === "outline"
      ? "Replace as Vector Outline"
      : "Replace Text (Clean)";

  $("newText").disabled =
    !canReplaceSelection;

  setExpansionDirectionEnabled(canReplaceSelection);

  if (canReplaceSelection && options.resetReplacement !== false) {
    // Every new text selection / completed replacement returns to the safe default.
    setExpansionDirection("right");
  }

  setText(
    "pageLabel",
    hasPdf
      ? `Page ${state.page_number} / ${state.page_count}`
      : "No PDF"
  );

  setText(
    "docxLabel",
    state.docx_label
  );

  setText(
    "status",
    state.status
  );

  const s =
    state.selection;

  if (s) {
    if (lineSelected) {
      $("newText").value = "";
    } else if (
      options.resetReplacement !== false
    ) {
      $("newText").value =
        s.text;
    }

    $("originalText").value =
      s.text;

    setText(
      "pdfFont",
      s.internal_font
    );

    setText(
      "pdfStyle",
      s.style
    );

    setText(
      "pdfSize",
      s.size
    );

    setText(
      "pdfColor",
      s.color
    );

    setText(
      "pdfPosition",
      s.position
    );

  } else {
    $("originalText").value =
      "";

    $("newText").value =
      "";

    [
      "pdfFont",
      "pdfStyle",
      "pdfSize",
      "pdfColor",
      "pdfPosition"
    ].forEach(
      (id) =>
        setText(
          id,
          "-"
        )
    );
  }

  const d =
    state.docx || {};

  setText(
    "docxFamily",
    d.family || "-"
  );

  setText(
    "docxDeclared",
    d.declared || "-"
  );

  setText(
    "docxSize",
    d.size || "-"
  );

  setText(
    "docxWeight",
    d.weight || "-"
  );

  setText(
    "docxItalic",
    d.italic || "-"
  );

  setText(
    "docxConfidence",
    d.confidence || "-"
  );

  setText(
    "docxReason",
    d.reason || "-"
  );

  $("docxConfidence").className =
    `pill ${d.tone || "neutral"}`;

  const r =
    state.resolution || {};

  setText(
    "fontMode",
    r.mode || "Automatic"
  );

  setText(
    "resolvedFamily",
    r.family || "-"
  );

  setText(
    "resolvedFace",
    r.face || "-"
  );

  setText(
    "resolvedWeight",
    r.weight || "-"
  );

  setText(
    "resolvedSource",
    r.source || "-"
  );

  setText(
    "matchStatus",
    state.match_status ||
      "Select text to begin."
  );

  setNoticeTone(
    $("matchStatus"),
    state.match_tone ||
      "neutral"
  );

  $("exportExtras").classList.toggle(
    "hidden",
    !state.audit_available &&
      !state.validation_available
  );

  if (
    options.refreshImage !== false &&
    hasPdf
  ) {
    refreshPageImage();
  } else {
    updateSelectionOverlay();
  }
}


async function uploadPdf(file) {
  if (!file) return;

  const fd =
    new FormData();

  fd.append(
    "file",
    file
  );

  setBusy(
    true,
    "Opening PDF…"
  );

  try {
    const res =
      await api(
        "/api/pdf",
        {
          method: "POST",
          body: fd
        }
      );

    const data =
      await res.json();

    sessionId =
      data.session_id;

    // A delete mode belongs to the PDF session in which it was activated.
    tableLineDeleteMode = false;
    tableLineDeletePending = false;

    zoom =
      1.35;

    $("zoomSlider").value =
      "135";

    setText(
      "zoomLabel",
      "Zoom 135%"
    );

    applyState(
      data.state,
      {
        resetReplacement: true,
        refreshImage: true
      }
    );

  } catch (err) {
    toast(
      err.message
    );

  } finally {
    setBusy(false);

    pdfInput.value =
      "";
  }
}


async function uploadDocx(file) {
  if (
    !sessionId ||
    !file
  ) {
    return;
  }

  const fd =
    new FormData();

  fd.append(
    "file",
    file
  );

  setBusy(
    true,
    "Loading DOCX reference…"
  );

  try {
    const res =
      await api(
        `/api/${sessionId}/docx`,
        {
          method: "POST",
          body: fd
        }
      );

    const data =
      await res.json();

    applyState(
      data.state,
      {
        resetReplacement: false,
        refreshImage: false
      }
    );

    toast(
      `DOCX reference loaded (${data.count} formatted runs).`
    );

  } catch (err) {
    toast(
      err.message
    );

  } finally {
    setBusy(false);

    docxInput.value =
      "";
  }
}


async function uploadFont(file) {
  if (
    !sessionId ||
    !file
  ) {
    return;
  }

  const fd =
    new FormData();

  fd.append(
    "file",
    file
  );

  setBusy(
    true,
    "Loading exact font…"
  );

  try {
    const res =
      await api(
        `/api/${sessionId}/font`,
        {
          method: "POST",
          body: fd
        }
      );

    const data =
      await res.json();

    applyState(
      data.state,
      {
        resetReplacement: false,
        refreshImage: false
      }
    );

    // Normal verification only.
    await resolveFont();

    const dbMessage = data.database?.created
      ? " Saved to MongoDB."
      : " Already available in MongoDB.";

    toast(
      `Font loaded: ${data.font.face}.${dbMessage}`
    );

  } catch (err) {
    toast(
      err.message
    );

  } finally {
    setBusy(false);

    fontInput.value =
      "";
  }
}


async function changePage(delta) {
  if (
    !currentState ||
    !sessionId
  ) {
    return;
  }

  const target =
    currentState.page_index +
    delta;

  setBusy(
    true,
    "Loading page…"
  );

  try {
    const res =
      await api(
        `/api/${sessionId}/page`,
        {
          method: "POST",

          headers: {
            "Content-Type":
              "application/json"
          },

          body:
            JSON.stringify({
              page_index:
                target
            })
        }
      );

    applyState(
      await res.json(),
      {
        resetReplacement: true,
        refreshImage: true
      }
    );

  } catch (err) {
    toast(
      err.message
    );

  } finally {
    setBusy(false);
  }
}


async function addPageAfterCurrent() {
  if (!sessionId || !currentState) return;

  setBusy(true, "Adding blank page…");
  try {
    const res = await api(`/api/${sessionId}/page/add`, {
      method: "POST",
    });

    applyState(await res.json(), {
      resetReplacement: true,
      refreshImage: true,
    });
    toast("Blank page added after the current page.");
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(false);
  }
}


async function deleteCurrentPage() {
  if (!sessionId || !currentState) return;

  if (currentState.page_count <= 1) {
    toast("The PDF must keep at least one page.");
    return;
  }

  const pageNumber = currentState.page_number;
  if (!window.confirm(`Delete page ${pageNumber}? This removes the whole page.`)) {
    return;
  }

  setBusy(true, "Deleting page…");
  try {
    const res = await api(`/api/${sessionId}/page/current`, {
      method: "DELETE",
    });

    applyState(await res.json(), {
      resetReplacement: true,
      refreshImage: true,
    });
    toast(`Page ${pageNumber} deleted.`);
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(false);
  }
}


async function selectFullLine() {
  if (!sessionId || !currentState?.selection) {
    toast("Select text in the line first.");
    return;
  }

  setBusy(true, "Selecting full line…");
  try {
    const res = await api(`/api/${sessionId}/line/select`, {
      method: "POST",
    });

    applyState(await res.json(), {
      resetReplacement: true,
      refreshImage: false,
    });
    toast("Full text line selected.");
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(false);
  }
}


async function deleteFullLine() {
  if (!sessionId || !currentState?.line_selected) {
    toast("Select a full line first.");
    return;
  }

  const preview = currentState.selection?.text || "this line";
  const removeTableRule = $("removeTableRule").checked;
  const tableRuleSummary = removeTableRule
    ? "If this is a table row, its separator will be removed and later table lines will move upward with the remaining rows."
    : "If this is a table row, all detected table lines will move upward with the remaining rows.";
  if (!window.confirm(
    `Delete the full selected line?\n\n${preview}\n\n${tableRuleSummary}`
  )) {
    return;
  }

  setBusy(true, "Deleting full text line…");
  try {
    const res = await api(`/api/${sessionId}/line/delete`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        remove_table_rule: removeTableRule,
      }),
    });

    applyState(await res.json(), {
      resetReplacement: true,
      refreshImage: true,
    });
    toast("Full text line and table geometry deleted/reflowed and validated.");
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(false);
  }
}


async function addFullRowBelow() {
  if (!sessionId || !currentState?.line_selected) {
    toast("Select a full table row first.");
    return;
  }

  const rowInsert = currentState.row_insert || {};
  if (rowInsert.eligible !== true) {
    toast(
      rowInsert.reason ||
      "The selected line is not a safely detected multi-column table row."
    );
    return;
  }

  const columnCount = Math.max(0, Number(rowInsert.column_count || 0));
  const preview = currentState.selection?.text || "the selected row";
  const accepted = window.confirm(
    `Add a new row below this ${columnCount}-column table row?\n\n` +
    `${preview}\n\n` +
    "The tool will clone the selected row's existing PDF content and separator style, " +
    "then move later rows down without substituting fonts. Afterward, click each cloned cell " +
    "and replace its text normally."
  );
  if (!accepted) return;

  setBusy(true, `Adding ${columnCount}-column table row…`);
  try {
    const res = await api(`/api/${sessionId}/line/add-row`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({position: "below"}),
    });

    applyState(await res.json(), {
      resetReplacement: true,
      refreshImage: true,
    });
    toast(
      `Added a ${columnCount}-column row. Click each new cell to edit its copied value.`
    );
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(false);
  }
}


function syncTableLineDeleteModeUi() {
  const button = $("deleteTableLineModeBtn");
  if (!button) return;

  const hasPdf =
    !!sessionId &&
    !!currentState &&
    currentState.page_count > 0;

  if (!hasPdf) {
    tableLineDeleteMode = false;
  }

  button.disabled = !hasPdf || tableLineDeletePending;
  button.classList.toggle("primary", tableLineDeleteMode);
  button.setAttribute(
    "aria-pressed",
    tableLineDeleteMode ? "true" : "false"
  );
  button.textContent = tableLineDeleteMode
    ? "Cancel Table-Line Delete"
    : "Delete Table Line…";

  pageImage.style.cursor = tableLineDeleteMode
    ? "cell"
    : "crosshair";
}


function setTableLineDeleteMode(active, announce = true) {
  const hasPdf =
    !!sessionId &&
    !!currentState &&
    currentState.page_count > 0;

  tableLineDeleteMode = Boolean(active && hasPdf);
  syncTableLineDeleteModeUi();
  updateSelectionOverlay();

  if (!announce) return;

  if (tableLineDeleteMode) {
    setText(
      "status",
      "Table-line delete mode: click a horizontal table line on the PDF. " +
      "The mode stays active for additional lines."
    );
    toast("Click a horizontal table line to delete it. Click Cancel when finished.");
  } else {
    setText(
      "status",
      currentState?.status || "Table-line delete mode cancelled."
    );
    toast("Table-line delete mode cancelled.");
  }
}


function pdfPointFromImageEvent(event) {
  const rect = pageImage.getBoundingClientRect();
  const renderedX = event.clientX - rect.left;
  const renderedY = event.clientY - rect.top;
  const scaleX = pageImage.naturalWidth / pageImage.clientWidth;
  const scaleY = pageImage.naturalHeight / pageImage.clientHeight;

  return {
    x: (renderedX * scaleX) / zoom,
    y: (renderedY * scaleY) / zoom,
  };
}


async function deleteTableLineAt(event) {
  if (
    !tableLineDeleteMode ||
    tableLineDeletePending ||
    !sessionId ||
    !currentState
  ) {
    return;
  }

  const {x, y} = pdfPointFromImageEvent(event);
  tableLineDeletePending = true;
  syncTableLineDeleteModeUi();
  setBusy(true, "Deleting horizontal table line…");

  try {
    const res = await api(`/api/${sessionId}/table-line/delete`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({x, y}),
    });

    applyState(await res.json(), {
      resetReplacement: true,
      refreshImage: true,
    });
    toast("Table line deleted. Click another line, or cancel the mode.");
  } catch (err) {
    // A miss is non-destructive. Keep the mode active so the user can click
    // closer to the intended rule without reopening the tool.
    try {
      const stateRes = await api(`/api/${sessionId}/state`);
      applyState(await stateRes.json(), {
        resetReplacement: true,
        refreshImage: false,
      });
    } catch (_) {
      // Keep the original, more useful hit-test error below.
    }
    toast(err.message);
  } finally {
    tableLineDeletePending = false;
    syncTableLineDeleteModeUi();
    setBusy(false);
  }
}


async function selectAt(event) {
  if (
    !sessionId ||
    !currentState
  ) {
    return;
  }

  if (tableLineDeleteMode) {
    await deleteTableLineAt(event);
    return;
  }

  const {x, y} = pdfPointFromImageEvent(event);

  try {
    const res =
      await api(
        `/api/${sessionId}/select`,
        {
          method: "POST",

          headers: {
            "Content-Type":
              "application/json"
          },

          body:
            JSON.stringify({
              x,
              y
            })
        }
      );

    applyState(
      await res.json(),
      {
        resetReplacement: true,
        refreshImage: false
      }
    );

  } catch (err) {
    toast(
      err.message
    );
  }
}


// ---------------------------------------------------------
// SAVED MONGODB FONT LIBRARY
// ---------------------------------------------------------

function closeFontDbModal() {
  $("fontDbModal").classList.add("hidden");
  $("fontDbModal").setAttribute("aria-hidden", "true");
}

function fontSizeLabel(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function renderSavedFonts() {
  const rows = $("fontDbRows");
  rows.replaceChildren();

  const query = $("fontDbSearch").value.trim().toLowerCase();
  const filtered = savedFontsCache.filter((font) => {
    if (!query) return true;
    return [
      font.family,
      font.full_name,
      font.postscript,
      font.filename,
      font.subfamily,
    ].some((value) => String(value || "").toLowerCase().includes(query));
  });

  if (!filtered.length) {
    $("fontDbStatus").textContent = savedFontsCache.length
      ? "No saved fonts match your search."
      : "No fonts are saved yet. Upload a TTF/OTF font first.";
    return;
  }

  $("fontDbStatus").textContent =
    `${filtered.length} saved font${filtered.length === 1 ? "" : "s"}. ` +
    "Choose one; exact verification still runs before it can be used.";

  filtered.forEach((font) => {
    const row = document.createElement("div");
    row.className = "user-row";

    const main = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = font.full_name || font.family || font.filename;

    const meta = document.createElement("span");
    const style = font.italic ? "Italic" : "Normal";
    meta.textContent =
      `${font.family || "Unknown"} · ${font.weight_label || font.weight || "-"} · ` +
      `${style} · ${fontSizeLabel(font.size_bytes)} · ${font.filename}`;

    main.append(title, meta);

    const useBtn = document.createElement("button");
    useBtn.type = "button";
    useBtn.className = "btn compact";
    useBtn.textContent = "Use Font";
    useBtn.addEventListener("click", () => useSavedFont(font.id, useBtn));

    row.append(main, useBtn);
    rows.append(row);
  });
}

async function loadSavedFonts() {
  $("fontDbStatus").textContent = "Loading saved fonts from MongoDB...";
  $("fontDbRows").replaceChildren();

  try {
    const res = await api("/api/fonts");
    const data = await res.json();
    savedFontsCache = Array.isArray(data.fonts) ? data.fonts : [];
    renderSavedFonts();
  } catch (err) {
    $("fontDbStatus").textContent = err.message;
  }
}

async function openFontDbModal() {
  if (!sessionId || !currentState?.selection) {
    toast("Select PDF text first.");
    return;
  }

  $("fontDbModal").classList.remove("hidden");
  $("fontDbModal").setAttribute("aria-hidden", "false");
  $("fontDbSearch").value = "";
  await loadSavedFonts();
  setTimeout(() => $("fontDbSearch").focus(), 0);
}

async function useSavedFont(fontId, button) {
  if (!sessionId || !currentState?.selection) {
    toast("Select PDF text first.");
    return;
  }

  const oldText = button.textContent;
  button.disabled = true;
  button.textContent = "Checking...";
  setBusy(true, "Loading saved font and checking exact match…");

  try {
    const res = await api(`/api/${sessionId}/font-db/${fontId}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        replacement: $("newText").value,
        direction: getExpansionDirection(),
      }),
    });

    const data = await res.json();
    applyState(data.state, {
      resetReplacement: false,
      refreshImage: false,
    });

    if (data.accepted) {
      closeFontDbModal();
      toast(`Saved font accepted: ${data.font.full_name || data.font.family}`);
    } else {
      toast("That saved font did not pass the exact font verification.");
    }
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(false);
    button.disabled = false;
    button.textContent = oldText;
  }
}

async function findGlyphElsewhereInPdf() {
  if (!sessionId || !currentState?.selection) return;
  setBusy(true, "Scanning all PDF font subsets…");
  try {
    const res = await api(`/api/${sessionId}/font-find-pdf`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        replacement: $("newText").value,
        direction: getExpansionDirection(),
      }),
    });
    const data = await res.json();
    applyState(data, {
      resetReplacement: false,
      refreshImage: false,
    });
    toast(
      data.resolution?.fallback_mode === "adaptive_pdf_glyph"
        ? "Missing PDF glyphs were calibrated to the selected font weight."
        : "A verified same-family glyph subset was found inside this PDF."
    );
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(false);
  }
}

async function prepareDatabaseFallback(mode) {
  if (!sessionId || !currentState?.selection) return;

  if (mode === "closest_font") {
    const accepted = window.confirm(
      "Match missing glyphs to the original PDF font from MongoDB?\n\n" +
      "An exact face is preferred. If only a compatible lighter face contains the glyph, the tool measures shared glyphs and calibrates the missing outline to the selected font's actual weight. " +
      "Only missing glyphs are appended to the original Type1C subset. If the measured shape is not compatible, the edit is rejected; the whole line is never substituted."
    );
    if (!accepted) return;
  } else {
    const accepted = window.confirm(
      "Convert the replacement to vector outlines?\n\n" +
      "The result stays visible and printable but will NOT be searchable, selectable, or editable as normal PDF text."
    );
    if (!accepted) return;
  }

  const endpoint = mode === "outline" ? "font-outline" : "font-db-closest";
  const busyText = mode === "outline"
    ? "Preparing vector glyph outlines…"
    : "Finding donor glyphs for the original PDF subset…";
  setBusy(true, busyText);

  try {
    const res = await api(`/api/${sessionId}/${endpoint}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        replacement: $("newText").value,
        direction: getExpansionDirection(),
      }),
    });
    const data = await res.json();
    applyState(data.state, {
      resetReplacement: false,
      refreshImage: false,
    });
    toast(
      mode === "outline"
        ? "Outline mode is ready. Click Replace as Vector Outline."
        : data.score?.completion_mode === "adaptive_font"
        ? `Database glyph weight matched: ${data.font.full_name || data.font.family}.`
        : `Exact database glyph donor ready: ${data.font.full_name || data.font.family}.`
    );
  } catch (err) {
    if (mode === "closest_font") {
      setText(
        "matchStatus",
        "✗ NO SAFE GLYPH MATCH FOUND\n\n" +
        "The saved database fonts could not supply a glyph whose outline can be safely calibrated to the selected font. The PDF was not changed.\n\n" +
        "Safe path:\n" +
        "1. Obtain the licensed full TTF/OTF for the exact face used by this PDF.\n" +
        "2. Click '1. Upload Exact Font...' and select that file.\n" +
        "3. Keep the replacement text entered, then run 'Re-check Font Match'.\n" +
        "4. Replace only after every exact-font check passes.\n\n" +
        "Required font details:\n" + err.message
      );
      setNoticeTone($("matchStatus"), "error");
      setText(
        "status",
        "The PDF was not changed. Upload the exact full font face shown in the red panel, then re-check the match."
      );
      toast("Exact full font required. Follow the safe path in the red panel.");
    } else {
      toast(err.message);
    }
  } finally {
    setBusy(false);
  }
}

// ---------------------------------------------------------
// NORMAL FONT CHECK
// THIS DOES NOT USE THE AI ENDPOINT
// ---------------------------------------------------------

async function resolveFont() {
  if (
    !sessionId ||
    !currentState?.selection
  ) {
    return;
  }

  setBusy(
    true,
    "Checking exact font…"
  );

  try {
    const res =
      await api(
        `/api/${sessionId}/resolve`,
        {
          method: "POST",

          headers: {
            "Content-Type":
              "application/json"
          },

          body:
            JSON.stringify({
              replacement:
                $("newText").value
            })
        }
      );

    applyState(
      await res.json(),
      {
        resetReplacement: false,
        refreshImage: false
      }
    );

  } catch (err) {
    toast(
      err.message
    );

  } finally {
    setBusy(false);
  }
}


// ---------------------------------------------------------
// AI SEARCH
// OPENAI IS CALLED ONLY FROM THIS FUNCTION
// ---------------------------------------------------------

async function searchFontWithAI() {
  if (
    !sessionId ||
    !currentState?.selection
  ) {
    toast(
      "Select PDF text first."
    );

    return;
  }

  const button =
    $("findFontBtn");

  const previousText =
    button.textContent;

  button.disabled =
    true;

  button.textContent =
    "Searching with AI...";

  setBusy(
    true,
    "Searching exact font with AI…"
  );

  try {
    const res =
      await api(
        `/api/${sessionId}/ai-font-search`,
        {
          method: "POST",

          headers: {
            "Content-Type":
              "application/json"
          },

          body:
            JSON.stringify({
              replacement:
                $("newText").value
            })
        }
      );

    const state =
      await res.json();

    applyState(
      state,
      {
        resetReplacement: false,
        refreshImage: false
      }
    );

    if (
      state.resolution?.ok
    ) {
      toast(
        "AI search found a font that passed exact verification."
      );
    } else {
      toast(
        "AI search finished. See the font status for details."
      );
    }

  } catch (err) {
    toast(
      err.message
    );

  } finally {
    setBusy(false);

    button.textContent =
      "Search Exact Font with AI...";

    button.disabled =
      !currentState?.selection;
  }
}


async function replaceText() {
  if (
    !sessionId ||
    !currentState?.selection
  ) {
    return;
  }

  setBusy(
    true,
    "Replacing text…"
  );

  try {
    const res =
      await api(
        `/api/${sessionId}/replace`,
        {
          method: "POST",

          headers: {
            "Content-Type":
              "application/json"
          },

          body:
            JSON.stringify({
              replacement:
                $("newText").value,
              direction:
                getExpansionDirection(),
              fallback_token:
                currentState?.resolution?.fallback_token || null
            })
        }
      );

    applyState(
      await res.json(),
      {
        resetReplacement: true,
        refreshImage: true
      }
    );

  } catch (err) {
    toast(
      err.message
    );

  } finally {
    setBusy(false);
  }
}


async function undoLast() {
  if (!sessionId) {
    return;
  }

  setBusy(
    true,
    "Undoing last replacement…"
  );

  try {
    const res =
      await api(
        `/api/${sessionId}/undo`,
        {
          method: "POST"
        }
      );

    applyState(
      await res.json(),
      {
        resetReplacement: true,
        refreshImage: true
      }
    );

  } catch (err) {
    toast(
      err.message
    );

  } finally {
    setBusy(false);
  }
}


async function downloadEndpoint(
  path,
  fallbackName
) {
  setBusy(
    true,
    "Preparing download…"
  );

  try {
    const res =
      await api(path);

    const blob =
      await res.blob();

    const disposition =
      res.headers.get(
        "content-disposition"
      ) || "";

    const match =
      disposition.match(
        /filename="?([^";]+)"?/i
      );

    const filename =
      match
        ? match[1]
        : fallbackName;

    const url =
      URL.createObjectURL(
        blob
      );

    const a =
      document.createElement(
        "a"
      );

    a.href =
      url;

    a.download =
      filename;

    document.body.appendChild(
      a
    );

    a.click();

    a.remove();

    setTimeout(
      () =>
        URL.revokeObjectURL(
          url
        ),
      1200
    );

  } catch (err) {
    toast(
      err.message
    );

    throw err;

  } finally {
    setBusy(false);
  }
}


async function savePdf() {
  if (!sessionId) {
    return;
  }

  try {
    await downloadEndpoint(
      `/api/${sessionId}/export`,
      "edited_clean.pdf"
    );

    const res =
      await api(
        `/api/${sessionId}/state`
      );

    applyState(
      await res.json(),
      {
        resetReplacement: false,
        refreshImage: false
      }
    );

    toast(
      "Edited PDF downloaded. Audit and validation files are now available too."
    );

  } catch (err) {
    const message = err?.message || "Save validation failed.";

    setText(
      "matchStatus",
      "✗ SAVE VALIDATION FAILED\n\n" +
        message +
        "\n\nYour edits are still kept in the current session. Fix the listed edit(s) and click Save As again."
    );

    setNoticeTone(
      $("matchStatus"),
      "error"
    );

    setText(
      "status",
      "Save blocked because one or more previous edits failed final text-layer validation. The editing session was not rolled back."
    );

    toast("Save blocked. See the validation details in the panel.");
  }
}


function markReplacementChanged() {
  if (
    !currentState?.selection
  ) {
    return;
  }

  currentState.resolution = {
    ...currentState.resolution,
    fallback_mode: null,
    fallback_token: null,
    family: "-",
    face: "-",
    weight: "-",
    source: "-",
    ok: false
  };

  setText(
    "resolvedFamily",
    "-"
  );

  setText(
    "resolvedFace",
    "-"
  );

  setText(
    "resolvedWeight",
    "-"
  );

  setText(
    "resolvedSource",
    "Replacement changed — exact font must be re-verified."
  );

  setText(
    "matchStatus",
    "Text changed. Click 'Re-check Font Match' before replacing.\n" +
      "The previous font result will not be reused."
  );

  setNoticeTone(
    $("matchStatus"),
    "warning"
  );

  $("replaceBtn").textContent =
    "Replace Text (Clean)";
}


function openUserModal(mode) {
  if (
    !currentUser ||
    currentUser.role !== "admin"
  ) {
    return;
  }

  $("userModal").classList.remove(
    "hidden"
  );

  $("userModal").setAttribute(
    "aria-hidden",
    "false"
  );

  const createMode =
    mode === "create";

  $("userModalTitle").textContent =
    createMode
      ? "Make User"
      : "All Users";

  $("createUserForm").classList.toggle(
    "hidden",
    !createMode
  );

  $("userListView").classList.toggle(
    "hidden",
    createMode
  );

  if (createMode) {
    $("createUserForm").reset();

    setTimeout(
      () =>
        $("newUsername").focus(),
      0
    );

  } else {
    loadUsers();
  }
}


function closeUserModal() {
  $("userModal").classList.add(
    "hidden"
  );

  $("userModal").setAttribute(
    "aria-hidden",
    "true"
  );
}


function formatCreated(value) {
  if (!value) {
    return "-";
  }

  const date =
    new Date(value);

  if (
    Number.isNaN(
      date.getTime()
    )
  ) {
    return "-";
  }

  return date.toLocaleString();
}


function renderUsers(users) {
  const rows =
    $("userListRows");

  rows.replaceChildren();

  if (!users.length) {
    $("userListStatus").textContent =
      "No users yet.";

    return;
  }

  $("userListStatus").textContent =
    `${users.length} user${users.length === 1 ? "" : "s"}`;

  users.forEach(
    (user) => {
      const row =
        document.createElement(
          "div"
        );

      row.className =
        "user-row";

      const main =
        document.createElement(
          "div"
        );

      const name =
        document.createElement(
          "strong"
        );

      name.textContent =
        user.username;

      const meta =
        document.createElement(
          "span"
        );

      meta.textContent =
        `Created ${formatCreated(user.created_at)}`;

      main.append(
        name,
        meta
      );

      const role =
        document.createElement(
          "div"
        );

      role.className =
        "role-pill";

      role.textContent =
        user.role;

      row.append(
        main,
        role
      );

      rows.append(
        row
      );
    }
  );
}


async function loadUsers() {
  $("userListStatus").textContent =
    "Loading users...";

  $("userListRows").replaceChildren();

  try {
    const res =
      await api(
        "/api/admin/users"
      );

    const data =
      await res.json();

    renderUsers(
      data.users || []
    );

  } catch (err) {
    $("userListStatus").textContent =
      err.message;
  }
}


async function createUser(event) {
  event.preventDefault();

  const username =
    $("newUsername").value.trim();

  const password =
    $("newUserPassword").value;

  setBusy(
    true,
    "Creating user..."
  );

  try {
    const res =
      await api(
        "/api/admin/users",
        {
          method: "POST",

          headers: {
            "Content-Type":
              "application/json"
          },

          body:
            JSON.stringify({
              username,
              password
            })
        }
      );

    const data =
      await res.json();

    $("createUserForm").reset();

    $("newUsername").focus();

    toast(
      `User created: ${data.user.username}`
    );

  } catch (err) {
    toast(
      err.message
    );

  } finally {
    setBusy(false);
  }
}


// ---------------------------------------------------------
// EVENT LISTENERS
// ---------------------------------------------------------

$("loginForm")
  .addEventListener(
    "submit",
    loginUser
  );

$("logoutBtn")
  .addEventListener(
    "click",
    logoutUser
  );

$("makeUserBtn")
  .addEventListener(
    "click",
    () =>
      openUserModal(
        "create"
      )
  );

$("allUsersBtn")
  .addEventListener(
    "click",
    () =>
      openUserModal(
        "list"
      )
  );

$("closeUserModalBtn")
  .addEventListener(
    "click",
    closeUserModal
  );

$("createUserForm")
  .addEventListener(
    "submit",
    createUser
  );

$("userModal")
  .addEventListener(
    "click",
    (event) => {
      if (
        event.target ===
        $("userModal")
      ) {
        closeUserModal();
      }
    }
  );

document.addEventListener(
  "keydown",
  (event) => {
    if (
      event.key === "Escape"
    ) {
      closeUserModal();
      closeFontDbModal();
    }
  }
);

$("openPdfBtn")
  .addEventListener(
    "click",
    () =>
      pdfInput.click()
  );

$("emptyOpenBtn")
  .addEventListener(
    "click",
    () =>
      pdfInput.click()
  );

$("loadDocxBtn")
  .addEventListener(
    "click",
    () =>
      sessionId
        ? docxInput.click()
        : toast(
            "Open a PDF first."
          )
  );

$("loadFontBtn")
  .addEventListener(
    "click",
    () =>
      fontInput.click()
  );


$("dbFontBtn")
  .addEventListener(
    "click",
    openFontDbModal
  );

$("pdfGlyphBtn")
  .addEventListener(
    "click",
    findGlyphElsewhereInPdf
  );

$("closestFontBtn")
  .addEventListener(
    "click",
    () => prepareDatabaseFallback("closest_font")
  );

$("outlineGlyphBtn")
  .addEventListener(
    "click",
    () => prepareDatabaseFallback("outline")
  );

$("closeFontDbModalBtn")
  .addEventListener(
    "click",
    closeFontDbModal
  );

$("fontDbSearch")
  .addEventListener(
    "input",
    renderSavedFonts
  );

$("fontDbModal")
  .addEventListener(
    "click",
    (event) => {
      if (event.target === $("fontDbModal")) {
        closeFontDbModal();
      }
    }
  );

pdfInput.addEventListener(
  "change",
  () =>
    uploadPdf(
      pdfInput.files[0]
    )
);

docxInput.addEventListener(
  "change",
  () =>
    uploadDocx(
      docxInput.files[0]
    )
);

fontInput.addEventListener(
  "change",
  () =>
    uploadFont(
      fontInput.files[0]
    )
);

$("prevBtn")
  .addEventListener(
    "click",
    () =>
      changePage(-1)
  );

$("nextBtn")
  .addEventListener(
    "click",
    () =>
      changePage(1)
  );

$("addPageBtn")
  .addEventListener(
    "click",
    addPageAfterCurrent
  );

$("deletePageBtn")
  .addEventListener(
    "click",
    deleteCurrentPage
  );

$("deleteTableLineModeBtn")
  .addEventListener(
    "click",
    () => setTableLineDeleteMode(!tableLineDeleteMode)
  );

$("selectLineBtn")
  .addEventListener(
    "click",
    selectFullLine
  );

$("deleteLineBtn")
  .addEventListener(
    "click",
    deleteFullLine
  );

$("addRowBelowBtn")
  .addEventListener(
    "click",
    addFullRowBelow
  );

pageImage.addEventListener(
  "click",
  selectAt
);

pageImage.addEventListener(
  "load",
  updateSelectionOverlay
);

window.addEventListener(
  "resize",
  updateSelectionOverlay
);

document.addEventListener(
  "keydown",
  (event) => {
    if (event.key === "Escape" && tableLineDeleteMode) {
      setTableLineDeleteMode(false);
    }
  }
);


// NORMAL CHECK
$("checkBtn")
  .addEventListener(
    "click",
    resolveFont
  );


// REPLACE
$("replaceBtn")
  .addEventListener(
    "click",
    replaceText
  );


$("undoBtn")
  .addEventListener(
    "click",
    undoLast
  );


$("saveBtn")
  .addEventListener(
    "click",
    savePdf
  );


$("newText")
  .addEventListener(
    "input",
    markReplacementChanged
  );


document
  .querySelectorAll('input[name="expansionDirection"]')
  .forEach((radio) => {
    radio.addEventListener("change", () => {
      if (!currentState?.selection) return;

      const direction = getExpansionDirection();

      setText(
        "status",
        direction === "left"
          ? "Expansion direction: Move Left. The original right edge will stay fixed."
          : "Expansion direction: Move Right. The original left edge will stay fixed."
      );
    });
  });


$("zoomSlider")
  .addEventListener(
    "input",
    (event) => {
      zoom =
        Number(
          event.target.value
        ) / 100;

      setText(
        "zoomLabel",
        `Zoom ${event.target.value}%`
      );
    }
  );


$("zoomSlider")
  .addEventListener(
    "change",
    () =>
      refreshPageImage()
  );


// ---------------------------------------------------------
// AI BUTTON
// This replaces old Google Search behavior.
// ---------------------------------------------------------

$("findFontBtn").textContent =
  "Search Exact Font with AI...";

$("findFontBtn")
  .addEventListener(
    "click",
    searchFontWithAI
  );


$("fullscreenBtn")
  .addEventListener(
    "click",
    async () => {
      try {
        if (
          !document.fullscreenElement
        ) {
          await document
            .documentElement
            .requestFullscreen();

        } else {
          await document
            .exitFullscreen();
        }

      } catch (err) {
        toast(
          err.message
        );
      }
    }
  );


document.addEventListener(
  "fullscreenchange",
  () => {
    $("fullscreenBtn").textContent =
      document.fullscreenElement
        ? "Exit Full Screen"
        : "Full Screen";
  }
);


$("auditBtn")
  .addEventListener(
    "click",
    () =>
      downloadEndpoint(
        `/api/${sessionId}/audit`,
        "editlog.json"
      )
  );


$("validationBtn")
  .addEventListener(
    "click",
    () =>
      downloadEndpoint(
        `/api/${sessionId}/validation`,
        "validation.json"
      )
  );


window.addEventListener(
  "beforeunload",
  () => {
    if (sessionId) {
      navigator.sendBeacon?.(
        `/api/${sessionId}/close`
      );
    }
  }
);


checkAuth();
