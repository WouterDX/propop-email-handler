let items = [];
let currentIndex = 0;
let dragStartX = null;
let logsVisible = false;
let lastHandledRunEnd = null;
let statusPollTimer = null;

const statusLabels = {
  pending: "in behandeling",
  approved: "goedgekeurd",
  rejected: "afgekeurd",
};

const countsEl = document.getElementById("counts");
const runBtn = document.getElementById("run-btn");
const logsToggleBtn = document.getElementById("logs-toggle-btn");
const runStateEl = document.getElementById("run-state");
const logsPanelEl = document.getElementById("logs-panel");
const logsTextEl = document.getElementById("logs-text");
const emptyEl = document.getElementById("empty-state");
const cardEl = document.getElementById("card");
const subjectEl = document.getElementById("mail-subject");
const metaEl = document.getElementById("mail-meta");
const statusPillEl = document.getElementById("status-pill");
const replyTextEl = document.getElementById("reply-text");
const reservationChangeEl = document.getElementById("reservation-change");
const mailPreviewEl = document.getElementById("mail-preview");
const positionLabelEl = document.getElementById("position-label");
const reasonInputEl = document.getElementById("decision-reason");

const prevBtn = document.getElementById("prev-btn");
const nextBtn = document.getElementById("next-btn");
const approveBtn = document.getElementById("approve-btn");
const rejectBtn = document.getElementById("reject-btn");

function fmt(value) {
  return value ?? "";
}

function formatChange(change) {
  if (!change) return "Geen reservatiewijzigingen voorgesteld.";

  const changedFields = Array.isArray(change.changed_fields)
    ? change.changed_fields.filter((field) => typeof field === "string" && field.trim())
    : [];

  if (!changedFields.length) {
    return "Geen gewijzigde velden.";
  }

  return changedFields.map((field) => `• ${field}`).join("\n");
}

function updateCounts() {
  const total = items.length;
  const pending = items.filter((item) => item.status === "pending").length;
  const approved = items.filter((item) => item.status === "approved").length;
  const rejected = items.filter((item) => item.status === "rejected").length;
  countsEl.textContent = `Totaal: ${total} | In behandeling: ${pending} | Goedgekeurd: ${approved} | Afgekeurd: ${rejected}`;
}

function render() {
  updateCounts();

  if (!items.length) {
    emptyEl.classList.remove("hidden");
    cardEl.classList.add("hidden");
    return;
  }

  emptyEl.classList.add("hidden");
  cardEl.classList.remove("hidden");

  const item = items[currentIndex];
  const mail = item.mail || {};
  const proposal = item.proposal || {};

  subjectEl.textContent = mail.subject || "(geen onderwerp)";
  metaEl.textContent = `${fmt(mail.from_email)} • ${fmt(mail.date)} • conversatie ${fmt(mail.thread_id)}`;
  statusPillEl.textContent = statusLabels[item.status] || item.status || "in behandeling";

  replyTextEl.textContent = proposal.reply_email_nl || "Geen voorgesteld antwoord.";
  reservationChangeEl.textContent = formatChange(proposal.reservation_change);
  mailPreviewEl.textContent = mail.body_preview || "Geen e-mailpreview beschikbaar.";

  positionLabelEl.textContent = `${currentIndex + 1} / ${items.length}`;
  prevBtn.disabled = currentIndex === 0;
  nextBtn.disabled = currentIndex === items.length - 1;
}

function go(delta) {
  const next = currentIndex + delta;
  if (next < 0 || next >= items.length) return;
  currentIndex = next;
  render();
}

async function decide(decision) {
  if (!items.length) return;
  const current = items[currentIndex];
  const payload = {
    decision,
    reason: reasonInputEl.value.trim(),
  };

  const response = await fetch(`/api/reviews/${encodeURIComponent(current.review_id)}/decision`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: "Onbekende fout" }));
    alert(error.error || "Opslaan van beslissing mislukt");
    return;
  }

  current.status = decision;
  current.decision_reason = payload.reason || null;
  reasonInputEl.value = "";
  render();
}

async function loadItems() {
  const response = await fetch("/api/reviews");
  if (!response.ok) {
    throw new Error("Failed to load reviews");
  }
  const payload = await response.json();
  items = payload.items || [];
  currentIndex = 0;
  render();
}

async function loadLogs() {
  const response = await fetch("/api/run-logs");
  if (!response.ok) {
    throw new Error("Failed to load logs");
  }
  const payload = await response.json();
  logsTextEl.textContent = payload.log || "Nog geen logs beschikbaar.";
}

function setRunStateText(status) {
  if (status.running) {
    runStateEl.textContent = "Bezig met verwerken...";
    return;
  }
  if (status.success === true) {
    runStateEl.textContent = "Laatste run: geslaagd";
    return;
  }
  if (status.success === false) {
    runStateEl.textContent = `Laatste run: mislukt (exit ${status.exit_code})`;
    return;
  }
  runStateEl.textContent = "Niet gestart";
}

async function loadRunStatus() {
  const response = await fetch("/api/run-status");
  if (!response.ok) {
    throw new Error("Failed to load run status");
  }
  const status = await response.json();

  runBtn.disabled = Boolean(status.running);
  setRunStateText(status);

  if (logsVisible && (status.running || status.has_log)) {
    await loadLogs();
  }

  if (
    status.success === true &&
    status.ended_at &&
    status.ended_at !== lastHandledRunEnd
  ) {
    lastHandledRunEnd = status.ended_at;
    await loadItems();
  }
}

async function startRun() {
  const response = await fetch("/api/run", { method: "POST" });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ error: "Onbekende fout" }));
    alert(payload.error || "Run starten mislukt");
    return;
  }
  await loadRunStatus();
}

function toggleLogs() {
  logsVisible = !logsVisible;
  logsPanelEl.classList.toggle("hidden", !logsVisible);
  logsToggleBtn.textContent = logsVisible ? "Logboek verbergen" : "Logboek";
  if (logsVisible) {
    loadLogs().catch((error) => {
      console.error(error);
      logsTextEl.textContent = "Laden van logs mislukt.";
    });
  }
}

function startStatusPolling() {
  if (statusPollTimer) return;
  statusPollTimer = setInterval(() => {
    loadRunStatus().catch((error) => {
      console.error(error);
      runStateEl.textContent = "Status laden mislukt";
    });
  }, 1500);
}

function setupSwipe() {
  cardEl.addEventListener("pointerdown", (event) => {
    dragStartX = event.clientX;
  });

  cardEl.addEventListener("pointerup", (event) => {
    if (dragStartX == null) return;
    const delta = event.clientX - dragStartX;
    dragStartX = null;

    if (Math.abs(delta) < 60) return;
    if (delta < 0) {
      go(1);
    } else {
      go(-1);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") go(-1);
    if (event.key === "ArrowRight") go(1);
  });
}

prevBtn.addEventListener("click", () => go(-1));
nextBtn.addEventListener("click", () => go(1));
approveBtn.addEventListener("click", () => decide("approved"));
rejectBtn.addEventListener("click", () => decide("rejected"));
runBtn.addEventListener("click", () => {
  startRun().catch((error) => {
    console.error(error);
    runStateEl.textContent = "Run starten mislukt";
  });
});
logsToggleBtn.addEventListener("click", toggleLogs);

setupSwipe();
loadItems().catch((error) => {
  console.error(error);
  countsEl.textContent = "Laden van review-queue mislukt.";
});
loadRunStatus().catch((error) => {
  console.error(error);
  runStateEl.textContent = "Status laden mislukt";
});
startStatusPolling();
