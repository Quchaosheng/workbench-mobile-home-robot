const state = {
  runs: [],
  currentRun: null,
  events: [],
  cursor: -1,
  filter: "all",
  playing: false,
  timer: null,
  runRequest: null,
  requestGeneration: 0,
  toastTimer: null,
};

const statusLabels = {
  confirmed: "已确认",
  insufficient_evidence: "证据不足",
  refuted: "未满足",
  running: "执行中",
};

const expressionLabels = {
  idle: "待命",
  thinking: "思考中",
  uncertain: "存疑",
  pleased: "确认完成",
};

const eventLabels = {
  action_request: "发出语义动作",
  action_result: "动作结果返回",
  observation: "工作区观测",
  task_accepted: "任务已接收",
  task_graph: "任务计划生成",
  task_terminal: "任务流程结束",
  verification: "验证器结论",
};

const eventIcons = {
  action_request: "send",
  action_result: "package-check",
  observation: "scan-eye",
  task_accepted: "inbox",
  task_graph: "list-tree",
  task_terminal: "circle-stop",
  verification: "badge-check",
};

const stepLabels = {
  action_request: "执行语义动作",
  action_result: "检查动作结果",
  observation: "观察工作区",
  task_accepted: "接收任务",
  task_graph: "生成任务计划",
  task_terminal: "任务结束",
  verification: "验证任务结果",
};

const entityLabels = {
  red_block: "红块",
  blue_cylinder: "蓝柱",
  green_gear: "绿齿轮",
  parcel_box: "纸箱快递",
  parcel_envelope: "信封快递",
  parcel_unreadable: "标签不可读",
  parcel_damaged: "破损快递",
};

const taskZones = {
  "task-place-red-block": [{ id: "tray", label: "托盘" }],
  "task-kit-three-parts": [{ id: "kit_tray", label: "齐套托盘" }],
  "task-inspect-workpieces": [{ id: "inspection_area", label: "检验区域" }],
  "task-clear-workspace": [
    { id: "tray", label: "目标托盘" },
    { id: "staging_bin", label: "障碍暂存" },
  ],
  "task-sort-parcels": [
    { id: "pickup_shelf", label: "取件架 · 完好件" },
    { id: "quarantine_bin", label: "异常隔离 · 破损件" },
  ],
};

const viewOrder = ["overview", "replay"];

const get = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function refreshIcons() {
  if (window.lucide) {
    window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
  }
}

function formatTime(value, withDate = false) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  const options = withDate
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
    : { hour: "2-digit", minute: "2-digit", second: "2-digit" };
  return new Intl.DateTimeFormat("zh-CN", options).format(date);
}

function describeEvent(event) {
  const payload = event.payload || {};
  switch (event.event_type) {
    case "task_accepted":
      return payload.goal || payload.task_id;
    case "task_graph":
      return `${payload.planner || "planner"} · ${(payload.actions || []).join(" → ")}`;
    case "observation":
      return `${payload.entity_id || "entity"} · 置信度 ${Math.round((payload.confidence || 0) * 100)}%`;
    case "action_request":
      return `${payload.action_type || "action"} · ${payload.target_id || "--"}`;
    case "action_result":
      return [
        payload.outcome || payload.status || "unknown",
        payload.dispatch_state ? `下发 ${payload.dispatch_state}` : "",
        payload.device_state ? `设备 ${payload.device_state}` : "",
        payload.entity_id,
        payload.resulting_location ? `声称 ${payload.resulting_location}` : "",
        payload.error_reason || payload.detail,
      ]
        .filter(Boolean)
        .join(" · ");
    case "verification":
      return `${statusLabels[payload.status] || payload.status} · ${payload.reason_code || "--"}`;
    case "task_terminal":
      return statusLabels[payload.status] || payload.status;
    default:
      return event.event_type;
  }
}

function deriveSummary(events, cursor = events.length - 1) {
  const visible = cursor < 0 ? [] : events.slice(0, cursor + 1);
  const accepted = events.find((event) => event.event_type === "task_accepted") || events[0];
  const current = visible.at(-1);
  const verifications = visible.filter((event) => event.event_type === "verification");
  const verification = verifications.at(-1);
  const status = verification?.payload?.status || "running";
  let expression = "idle";
  if (visible.some((event) => event.event_type === "task_accepted")) expression = "thinking";
  if (["refuted", "insufficient_evidence"].includes(status)) expression = "uncertain";
  if (status === "confirmed") expression = "pleased";
  const evidence = [];
  visible.forEach((event) => {
    (event.evidence_refs || []).forEach((reference) => {
      if (!evidence.includes(reference)) evidence.push(reference);
    });
  });
  return {
    run_id: events[0]?.run_id || "--",
    task_id: accepted?.payload?.task_id || "--",
    goal: accepted?.payload?.goal || "Place the red block in the tray",
    mode: accepted?.payload?.mode || "scripted",
    status,
    status_label: statusLabels[status] || "未知状态",
    expression,
    current_step: current ? stepLabels[current.event_type] || current.event_type : "等待任务",
    progress: events.length ? Math.round((visible.length / events.length) * 100) : 0,
    updated_at: current?.occurred_at,
    evidence_refs: evidence,
    missing_evidence: verification?.payload?.missing_evidence || [],
    recovery_count: verifications.filter((event) => event.payload?.status === "refuted").length,
  };
}

function renderRunList() {
  const runs = state.runs.filter((run) => {
    if (state.filter === "all") return true;
    if (state.filter === "attention") return ["refuted", "insufficient_evidence"].includes(run.status);
    return run.status === state.filter;
  });
  get("run-count").textContent = String(runs.length);
  get("run-list").innerHTML = runs
    .map(
      (run) => `
        <button class="run-item ${run.run_id === state.currentRun?.run_id ? "is-active" : ""}"
          type="button" data-run-id="${escapeHtml(run.run_id)}"
          aria-pressed="${run.run_id === state.currentRun?.run_id}">
          <div class="run-item-top">
            <strong>${escapeHtml(run.task_id)}</strong>
            <span class="status-pill status-${escapeHtml(run.status)}">${escapeHtml(run.status_label)}</span>
          </div>
          <p>${escapeHtml(run.goal)}</p>
          <div class="run-item-bottom">
            <code>${escapeHtml(run.run_id)}</code>
            <time>${escapeHtml(formatTime(run.updated_at, true))}</time>
          </div>
        </button>`,
    )
    .join("");
  document.querySelectorAll(".run-item").forEach((button) => {
    button.addEventListener("click", () => selectRun(button.dataset.runId));
  });
  document.querySelector(".run-item.is-active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function renderAttention(summary) {
  const banner = get("attention-banner");
  const needsAttention = summary.status === "insufficient_evidence";
  banner.hidden = !needsAttention;
  if (!needsAttention) return;
  const missing = summary.missing_evidence.map((item) => item.replaceAll("_", " ")).join("、");
  get("attention-copy").textContent = `缺少：${missing || "新鲜且可信的观测证据"}`;
}

function renderExpression(summary) {
  const panel = get("expression-panel");
  panel.className = `expression-panel expression-${summary.expression}`;
  get("expression-name").textContent = expressionLabels[summary.expression];
  get("expression-label").textContent = expressionLabels[summary.expression];
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function entityType(payload) {
  const supported = new Set(["block", "cylinder", "gear", "parcel", "envelope"]);
  return supported.has(payload?.entity_type) ? payload.entity_type : "object";
}

function posePosition(payload, index = 0) {
  const x = Number(payload?.pose?.position?.x || 0);
  const y = Number(payload?.pose?.position?.y || 0);
  return {
    left: clamp(40 + x * 140 + (index % 2) * 2, 18, 62),
    top: clamp(57 + y * 150 + (index % 3) * 2, 25, 82),
  };
}

function destinationPosition(location, index) {
  const slots = {
    "in:tray": [
      { left: 68, top: 30 },
      { left: 82, top: 30 },
      { left: 75, top: 48 },
    ],
    "in:kit_tray": [
      { left: 68, top: 30 },
      { left: 82, top: 30 },
      { left: 75, top: 48 },
    ],
    "in:staging_bin": [
      { left: 70, top: 80 },
      { left: 80, top: 80 },
    ],
    "in:pickup_shelf": [
      { left: 69, top: 28 },
      { left: 81, top: 28 },
    ],
    "in:quarantine_bin": [
      { left: 70, top: 79 },
      { left: 87, top: 79 },
    ],
  };
  const candidates = slots[location];
  return candidates ? candidates[index % candidates.length] : null;
}

function buildWorkbenchState(events, cursor) {
  const visible = cursor < 0 ? [] : events.slice(0, cursor + 1);
  const accepted = visible.find((event) => event.event_type === "task_accepted");
  const taskGraph = [...visible].reverse().find((event) => event.event_type === "task_graph");
  const actionTargets = new Map();
  const entities = new Map();
  const observations = [];
  const claims = [];
  visible.forEach((event) => {
    const payload = event.payload || {};
    if (event.event_type === "observation" && payload.entity_id) {
      const previous = entities.get(payload.entity_id) || {};
      const rawConfidence = payload.confidence;
      // An observation without a spatial claim is not evidence of absence: the
      // last observed location survives until a newer observation replaces it.
      const location = payload.location || previous.location || null;
      entities.set(payload.entity_id, {
        ...previous,
        entity_id: payload.entity_id,
        entity_type: entityType(payload),
        pose: payload.pose,
        location,
        attributes: payload.attributes && typeof payload.attributes === "object" ? { ...payload.attributes } : {},
        confidence: Number.isFinite(rawConfidence) ? clamp(rawConfidence, 0, 1) : null,
      });
      observations.push({ entity_id: payload.entity_id, sequence_no: event.sequence_no, location });
    }
    if (event.event_type === "action_request" && payload.action_id && payload.target_id) {
      actionTargets.set(payload.action_id, payload.target_id);
    }
    if (event.event_type === "action_result") {
      // An action result is execution evidence, never observed world truth. It
      // records what the robot attempted and whether the device confirmed it;
      // only an accepted observation may move an entity on the map.
      const entityId = payload.entity_id || actionTargets.get(payload.action_id);
      if (entityId) {
        claims.push({
          entity_id: entityId,
          action_id: payload.action_id || null,
          sequence_no: event.sequence_no,
          outcome: payload.outcome || payload.status || "unknown",
          dispatch_state: payload.dispatch_state || null,
          device_state: payload.device_state || null,
          claimed_location: payload.resulting_location || null,
          error_reason: payload.error_reason || payload.detail || null,
          occurred_at: event.occurred_at,
          evidence_refs: event.evidence_refs || [],
        });
      }
    }
  });
  const nextClaimSequence = (claim) =>
    claims.reduce(
      (earliest, candidate) =>
        candidate.entity_id === claim.entity_id &&
        candidate.sequence_no > claim.sequence_no &&
        (earliest === null || candidate.sequence_no < earliest)
          ? candidate.sequence_no
          : earliest,
      null,
    );
  const executionClaims = claims.map((claim) => {
    // A claim is judged only inside its own outcome window: observations after
    // this action and before the entity's next action result. A later placement
    // supersedes an earlier hold instead of contradicting it.
    const nextAction = nextClaimSequence(claim);
    const window = observations.filter(
      (entry) =>
        entry.entity_id === claim.entity_id &&
        entry.sequence_no > claim.sequence_no &&
        (nextAction === null || entry.sequence_no < nextAction),
    );
    const locating = window.find((entry) => entry.location);
    let verification = "awaiting_observation";
    if (!claim.claimed_location) verification = "no_spatial_claim";
    else if (locating) verification = locating.location === claim.claimed_location ? "supported" : "contradicted";
    // A window that already closed without a locating observation is "position
    // not observed"; only the newest claim of an entity may still await one.
    else if (nextAction !== null || window.length) verification = "unverified";
    return { ...claim, observed_location: locating ? locating.location : null, verification };
  });
  return {
    taskId: accepted?.payload?.task_id || "task-place-red-block",
    taskGraph: taskGraph?.payload || {},
    entities: [...entities.values()],
    executionClaims,
  };
}

const executionClaimLabels = {
  awaiting_observation: "等待观测",
  unverified: "位置未观测",
  no_spatial_claim: "无空间结论",
  supported: "观测支持",
  contradicted: "观测矛盾",
};

function executionClaimSummary(claim) {
  const outcome = claim.outcome || "unknown";
  const dispatch = claim.dispatch_state ? ` · 下发 ${claim.dispatch_state}` : "";
  const device = claim.device_state ? ` · 设备 ${claim.device_state}` : "";
  return `${outcome}${dispatch}${device}`;
}

function renderExecutionClaims(workbench) {
  const panel = get("execution-claims");
  const claims = workbench.executionClaims || [];
  if (!claims.length) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  panel.hidden = false;
  panel.innerHTML = `
    <div class="route-decision-head"><span>动作执行证据</span><small>执行结论不等于观测事实</small></div>
    <div class="execution-claim-table" role="table" aria-label="动作执行证据与观测核验">
      ${claims
        .map((claim) => {
          const label = entityLabels[claim.entity_id] || claim.entity_id;
          const verification = executionClaimLabels[claim.verification] || claim.verification;
          const claimText = claim.claimed_location ? `声称 ${escapeHtml(claim.claimed_location)}` : "无空间结论";
          const observedText = claim.observed_location ? `观测 ${escapeHtml(claim.observed_location)}` : "位置未观测";
          return `<div class="execution-claim-row execution-claim-${claim.verification}" role="row">
            <strong>${escapeHtml(label)}</strong>
            <span class="execution-claim-outcome">${escapeHtml(executionClaimSummary(claim))}</span>
            <span class="execution-claim-location">${claimText} · ${observedText}</span>
            <small>${escapeHtml(verification)}</small>
          </div>`;
        })
        .join("")}
    </div>`;
}

function entityVisual(entity, index, extraClass = "") {
  const position = destinationPosition(entity.location, index) || posePosition(entity, index);
  const opacity = entity.confidence == null ? 0.42 : Math.max(0.35, entity.confidence);
  const label = entityLabels[entity.entity_id] || entity.entity_id.replaceAll("_", " ");
  const locationText = entity.location ? ` · ${escapeHtml(entity.location)}` : " · 位置未观测";
  return `<span class="map-entity map-entity-${escapeHtml(entity.entity_type)} ${extraClass}"
    data-left="${position.left}" data-top="${position.top}" data-opacity="${opacity}"
    title="${escapeHtml(label)}${locationText}">${escapeHtml(label)}</span>`;
}

function parcelIdentityLabel(attributes) {
  const raw = String(attributes.tracking_id || attributes.barcode || attributes.parcel_uid || "").trim();
  if (!raw) return "无可读身份";
  return raw.length <= 6 ? `身份 ${raw}` : `身份 …${raw.slice(-6)}`;
}

function parcelDecision(entity, configuredPriorities = {}, manifestStatuses = {}) {
  const attributes = entity.attributes || {};
  const labelStatus = String(attributes.label_status || "missing").toLowerCase();
  const condition = String(attributes.condition || "missing").toLowerCase();
  const identity = parcelIdentityLabel(attributes);
  const manifestStatus = manifestStatuses[entity.entity_id] || "not_checked";
  const safe = labelStatus === "verified" && condition === "intact";
  const destination = safe ? "in:pickup_shelf" : "in:quarantine_bin";
  const labelText = {
    verified: "已核验",
    unreadable: "不可读",
    unverified: "未核验",
    mismatch: "不匹配",
    missing: "缺失",
  };
  const conditionText = {
    intact: "完好",
    damaged: "破损",
    opened: "已拆封",
    wet: "受潮",
    unknown: "未知",
    missing: "缺失",
  };
  const labelDisplay = labelText[labelStatus] || labelStatus;
  const conditionDisplay = conditionText[condition] || condition;
  const routeReason = safe
    ? "标签已核验 · 外观完好"
    : `隔离：${labelStatus !== "verified" ? `标签${labelDisplay}` : ""}${labelStatus !== "verified" && condition !== "intact" ? " · " : ""}${condition !== "intact" ? `外观${conditionDisplay}` : ""}`;
  const manifestText = {
    matched: "清单已匹配",
    mismatch: "清单不匹配",
    missing: "清单身份缺失",
    not_checked: "未接入清单",
  }[manifestStatus] || manifestStatus;
  const reason = `${manifestText} · ${identity} · ${routeReason}`;
  const actual = entity.location || "pending";
  const result = actual === destination ? "confirmed" : actual === "pending" ? "pending" : "refuted";
  const fallbackPriority =
    condition !== "intact"
      ? { label: "P0 状态异常", rank: 0 }
      : labelStatus !== "verified"
        ? { label: "P1 标签异常", rank: 1 }
        : { label: "P2 正常入架", rank: 2 };
  const configuredPriority = {
    condition_exception: { label: "P0 状态异常", rank: 0 },
    label_exception: { label: "P1 标签异常", rank: 1 },
    standard: { label: "P2 正常入架", rank: 2 },
  }[configuredPriorities[entity.entity_id]];
  const priority = configuredPriority || fallbackPriority;
  return {
    condition: conditionDisplay,
    destination,
    labelStatus: labelDisplay,
    manifestStatus,
    priority,
    reason,
    result,
  };
}

function renderParcelDecisions(workbench) {
  const panel = get("parcel-decisions");
  if (workbench.taskId !== "task-sort-parcels") {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  const decisions = workbench.entities
    .map((entity) => ({
      decision: parcelDecision(
        entity,
        workbench.taskGraph.routing_priorities,
        workbench.taskGraph.manifest_statuses,
      ),
      entity,
    }))
    .sort(
      (left, right) =>
        left.decision.priority.rank - right.decision.priority.rank ||
        String(left.entity.entity_id).localeCompare(String(right.entity.entity_id)),
    );
  const capacities = workbench.taskGraph.destination_capacities || {};
  const initialOccupancy = workbench.taskGraph.destination_occupancy || {};
  const capacitySummary = [
    ["取件", "pickup_shelf", "in:pickup_shelf", capacities.pickup_shelf],
    ["隔离", "quarantine_bin", "in:quarantine_bin", capacities.quarantine_bin],
  ]
    .filter(([, , , capacity]) => Number.isInteger(capacity))
    .map(([label, destination, location, capacity]) => {
      const placed = workbench.entities.filter((entity) => entity.location === location).length;
      return `${label} ${(Number(initialOccupancy[destination]) || 0) + placed}/${capacity}`;
    })
    .join(" · ");
  const auditSummary = [
    workbench.taskGraph.manifest_id ? `清单 ${workbench.taskGraph.manifest_id}` : "",
    capacitySummary,
  ]
    .filter(Boolean)
    .join(" · ");
  panel.hidden = false;
  panel.innerHTML = `
    <div class="route-decision-head"><span>逐件路由决策</span><small>${escapeHtml(auditSummary || "属性先于动作")}</small></div>
    <div class="route-decision-table" role="table" aria-label="快递逐件路由决策">
      ${decisions
        .map(({ decision, entity }) => {
          const destination = decision.destination === "in:pickup_shelf" ? "取件架" : "异常隔离";
          return `<div class="route-decision-row route-decision-${decision.result}" role="row">
            <strong>${escapeHtml(entityLabels[entity.entity_id] || entity.entity_id)}<b class="route-priority route-priority-${decision.priority.rank}">${escapeHtml(decision.priority.label)}</b></strong>
            <span>${escapeHtml(decision.labelStatus)} · ${escapeHtml(decision.condition)}</span>
            <span class="route-destination">${escapeHtml(destination)}</span>
            <small>${escapeHtml(decision.reason)}</small>
          </div>`;
        })
        .join("")}
    </div>`;
}

function applyEntityPositions(container) {
  container.querySelectorAll(".map-entity").forEach((entity) => {
    entity.style.left = `${entity.dataset.left}%`;
    entity.style.top = `${entity.dataset.top}%`;
    entity.style.opacity = entity.dataset.opacity;
  });
}

function renderWorkbench(events, cursor) {
  const workbench = buildWorkbenchState(events, cursor);
  const zones = taskZones[workbench.taskId] || taskZones["task-place-red-block"];
  get("map-zones").innerHTML = zones
    .map((zone) => `<span class="map-zone map-zone-${escapeHtml(zone.id)}">${escapeHtml(zone.label)}</span>`)
    .join("");
  get("map-entities").innerHTML = workbench.entities.length
    ? workbench.entities.map((entity, index) => entityVisual(entity, index)).join("")
    : '<span class="map-empty">等待实体观测</span>';
  applyEntityPositions(get("map-entities"));
  renderExecutionClaims(workbench);
  renderParcelDecisions(workbench);

  const confidences = workbench.entities
    .map((entity) => entity.confidence)
    .filter((confidence) => confidence != null);
  const minimumConfidence = confidences.length ? Math.min(...confidences) : null;
  const badge = get("confidence-badge");
  if (minimumConfidence == null) {
    badge.textContent = "未观测";
  } else if (confidences.length === 1) {
    badge.textContent = `置信度 ${Math.round(minimumConfidence * 100)}%`;
  } else {
    badge.textContent = `${confidences.length} 个实体 · 最低 ${Math.round(minimumConfidence * 100)}%`;
  }
  badge.classList.toggle("is-low", minimumConfidence != null && minimumConfidence < 0.8);
}

function evidenceKind(reference) {
  if (reference.startsWith("frame://")) return { icon: "image", label: "相机帧" };
  if (reference.startsWith("motion-log://")) return { icon: "file-chart-column", label: "动作日志" };
  return { icon: "paperclip", label: "证据引用" };
}

function renderEvidence(summary) {
  const references = summary.evidence_refs.slice(-4).reverse();
  get("evidence-list").innerHTML = references.length
    ? references
        .map((reference) => {
          const kind = evidenceKind(reference);
          return `
            <button class="evidence-button" type="button" data-evidence="${escapeHtml(reference)}">
              <span class="evidence-icon"><i data-lucide="${kind.icon}"></i></span>
              <span><strong>${kind.label}</strong><small>${escapeHtml(reference)}</small></span>
              <i data-lucide="chevron-right"></i>
            </button>`;
        })
        .join("")
    : '<div class="evidence-empty">尚无证据引用</div>';
  document.querySelectorAll(".evidence-button").forEach((button) => {
    button.addEventListener("click", () => openEvidence(button.dataset.evidence));
  });
  refreshIcons();
}

function timelineClasses(event, index, cursor) {
  const classes = ["timeline-item"];
  const status = event.payload?.status;
  if (event.event_type === "verification") classes.push("is-verification");
  if (status === "refuted" || status === "failed") classes.push("is-failure");
  if (status === "insufficient_evidence") classes.push("is-uncertain");
  if (index === cursor) classes.push("is-active");
  if (index > cursor) classes.push("is-future");
  return classes.join(" ");
}

function renderTimeline(target, events, cursor, replay = false) {
  target.innerHTML = events
    .map(
      (event, index) => `
        <li class="${timelineClasses(event, index, cursor)}">
          <span class="timeline-marker"></span>
          <button type="button" data-event-index="${index}">
            <strong>${escapeHtml(eventLabels[event.event_type] || event.event_type)}</strong>
            <p>${escapeHtml(describeEvent(event))}</p>
          </button>
          <time>${escapeHtml(formatTime(event.occurred_at))}</time>
        </li>`,
    )
    .join("");
  target.querySelectorAll("button[data-event-index]").forEach((button) => {
    button.addEventListener("click", () => {
      state.cursor = Number(button.dataset.eventIndex);
      if (!replay) setView("replay");
      renderCurrent();
    });
  });
}

function renderReplayEvent() {
  const event = state.cursor < 0 ? null : state.events[state.cursor];
  get("replay-position").textContent = `${Math.max(0, state.cursor + 1)} / ${state.events.length}`;
  const replayRange = get("replay-range");
  replayRange.max = String(Math.max(0, state.events.length - 1));
  replayRange.value = String(Math.max(0, state.cursor));
  replayRange.setAttribute(
    "aria-valuetext",
    event
      ? `${state.cursor + 1} / ${state.events.length}，${eventLabels[event.event_type] || event.event_type}`
      : `0 / ${state.events.length}，任务尚未开始`,
  );
  get("replay-event-title").textContent = event ? eventLabels[event.event_type] || event.event_type : "等待任务";
  get("replay-sequence").textContent = event ? `#${String(event.sequence_no).padStart(2, "0")}` : "#--";
  if (!event) {
    get("replay-event-body").innerHTML = `
      <div class="event-summary">
        <span class="event-summary-icon"><i data-lucide="pause"></i></span>
        <div><strong>任务尚未开始</strong><small>当前回放位置没有已应用事件。</small></div>
      </div>`;
    refreshIcons();
    return;
  }
  const payloadRows = Object.entries(event.payload || {})
    .map(([key, value]) => {
      const display = typeof value === "object" ? JSON.stringify(value) : String(value);
      return `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(display)}</dd></div>`;
    })
    .join("");
  get("replay-event-body").innerHTML = `
    <div class="event-summary">
      <span class="event-summary-icon"><i data-lucide="${eventIcons[event.event_type] || "circle"}"></i></span>
      <div><strong>${escapeHtml(describeEvent(event))}</strong><small>${escapeHtml(event.event_id)}<br>${escapeHtml(event.occurred_at)}</small></div>
    </div>
    <dl class="payload-table">${payloadRows}</dl>`;
  refreshIcons();
}

function renderCurrent() {
  if (!state.events.length) return;
  const liveSummary = deriveSummary(state.events);
  const replaySummary = deriveSummary(state.events, state.cursor);
  const summary = get("replay-view").hidden ? liveSummary : replaySummary;
  get("run-id").textContent = summary.run_id;
  get("run-mode").textContent = summary.mode;
  get("task-status").textContent = summary.status_label;
  get("evidence-count").textContent = String(summary.evidence_refs.length);
  get("recovery-count").textContent = String(summary.recovery_count);
  get("task-goal").textContent = summary.goal;
  get("current-step").textContent = summary.current_step;
  get("updated-at").textContent = formatTime(summary.updated_at, true);
  get("progress-value").textContent = `${summary.progress}%`;
  get("progress-bar").style.width = `${summary.progress}%`;
  get("event-count").textContent = `${state.events.length} 个事件`;
  renderAttention(summary);
  renderExpression(summary);
  renderWorkbench(state.events, get("replay-view").hidden ? state.events.length - 1 : state.cursor);
  renderEvidence(summary);
  renderTimeline(get("overview-timeline"), state.events, state.events.length - 1);
  renderTimeline(get("replay-timeline"), state.events, state.cursor, true);
  renderReplayEvent();
  refreshIcons();
}

function openEvidence(reference) {
  const kind = evidenceKind(reference);
  get("evidence-title").textContent = kind.label;
  const visual = get("evidence-visual");
  const sourceIndex = state.events.findIndex((event) => (event.evidence_refs || []).includes(reference));
  const sourceEvent = sourceIndex >= 0 ? state.events[sourceIndex] : null;
  if (reference.startsWith("frame://")) {
    const snapshot = buildWorkbenchState(state.events, sourceIndex);
    visual.className = "evidence-visual camera-frame";
    visual.innerHTML = `${snapshot.entities
      .map((entity, index) => entityVisual(entity, index, "evidence-frame-entity"))
      .join("")}<span class="evidence-frame-label">${escapeHtml(reference)} · SCRIPTED FIXTURE</span>`;
    applyEntityPositions(visual);
  } else {
    visual.className = "evidence-visual motion-log";
    const payloadLines = Object.entries(sourceEvent?.payload || {})
      .map(([key, value]) => `${key.padEnd(20)} ${typeof value === "object" ? JSON.stringify(value) : value}`)
      .join("\n");
    visual.textContent = `${sourceEvent?.occurred_at || "--"}  ${sourceEvent?.event_type || "evidence"}\n${payloadLines || reference}`;
  }
  get("evidence-meta").innerHTML = `
    <dt>Reference</dt><dd>${escapeHtml(reference)}</dd>
    <dt>Run ID</dt><dd>${escapeHtml(state.currentRun?.run_id)}</dd>
    <dt>Source</dt><dd>scripted fixture</dd>
    <dt>Integrity</dt><dd>event stream reference</dd>`;
  const dialog = get("evidence-dialog");
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function setView(view, focusTab = false) {
  const replay = view === "replay";
  get("overview-view").hidden = replay;
  get("replay-view").hidden = !replay;
  get("overview-view").setAttribute("aria-hidden", String(replay));
  get("replay-view").setAttribute("aria-hidden", String(!replay));
  document.querySelectorAll(".view-tab").forEach((tab) => {
    const selected = tab.dataset.view === view;
    tab.classList.toggle("is-active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected && focusTab) tab.focus();
  });
  if (replay && state.cursor < 0) state.cursor = state.events.length - 1;
  renderCurrent();
}

function stopPlayback() {
  state.playing = false;
  clearTimeout(state.timer);
  state.timer = null;
  get("replay-play").innerHTML = '<i data-lucide="play"></i><span>播放</span>';
  get("replay-play").setAttribute("aria-pressed", "false");
  refreshIcons();
}

function playbackTick() {
  if (!state.playing) return;
  if (state.cursor >= state.events.length - 1) {
    stopPlayback();
    return;
  }
  state.cursor += 1;
  renderCurrent();
  state.timer = setTimeout(playbackTick, Number(get("replay-speed").value));
}

function togglePlayback() {
  if (state.playing) {
    stopPlayback();
    return;
  }
  if (state.cursor >= state.events.length - 1) state.cursor = -1;
  state.playing = true;
  get("replay-play").innerHTML = '<i data-lucide="pause"></i><span>暂停</span>';
  get("replay-play").setAttribute("aria-pressed", "true");
  refreshIcons();
  playbackTick();
}

async function selectRun(runId) {
  stopPlayback();
  state.runRequest?.abort();
  const controller = new AbortController();
  const generation = ++state.requestGeneration;
  state.runRequest = controller;
  get("run-list").setAttribute("aria-busy", "true");
  document.querySelector(".workspace").setAttribute("aria-busy", "true");
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/events`, { signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (generation !== state.requestGeneration) return;
    state.currentRun = payload.run;
    state.events = payload.events;
    state.cursor = state.events.length - 1;
    renderRunList();
    renderCurrent();
  } catch (error) {
    if (error.name === "AbortError" || generation !== state.requestGeneration) return;
    showToast(`无法读取运行记录：${error.message}`);
  } finally {
    if (generation === state.requestGeneration) {
      state.runRequest = null;
      get("run-list").removeAttribute("aria-busy");
      document.querySelector(".workspace").removeAttribute("aria-busy");
    }
  }
}

function showToast(message) {
  const toast = get("toast");
  toast.textContent = message;
  toast.classList.add("is-visible");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 3200);
}

function bindControls() {
  document.querySelectorAll(".filter-button").forEach((button) => {
    button.addEventListener("click", () => {
      state.filter = button.dataset.filter;
      document.querySelectorAll(".filter-button").forEach((item) => {
        const selected = item === button;
        item.classList.toggle("is-active", selected);
        item.setAttribute("aria-pressed", String(selected));
      });
      renderRunList();
    });
  });
  document.querySelectorAll(".view-tab").forEach((tab) => {
    tab.addEventListener("click", () => setView(tab.dataset.view));
    tab.addEventListener("keydown", (event) => {
      const currentIndex = viewOrder.indexOf(tab.dataset.view);
      let targetIndex = null;
      if (["ArrowLeft", "ArrowUp"].includes(event.key)) targetIndex = (currentIndex - 1 + viewOrder.length) % viewOrder.length;
      if (["ArrowRight", "ArrowDown"].includes(event.key)) targetIndex = (currentIndex + 1) % viewOrder.length;
      if (event.key === "Home") targetIndex = 0;
      if (event.key === "End") targetIndex = viewOrder.length - 1;
      if (targetIndex === null) return;
      event.preventDefault();
      setView(viewOrder[targetIndex], true);
    });
  });
  get("replay-start").addEventListener("click", () => {
    stopPlayback();
    state.cursor = -1;
    renderCurrent();
  });
  get("replay-prev").addEventListener("click", () => {
    stopPlayback();
    state.cursor = Math.max(-1, state.cursor - 1);
    renderCurrent();
  });
  get("replay-next").addEventListener("click", () => {
    stopPlayback();
    state.cursor = Math.min(state.events.length - 1, state.cursor + 1);
    renderCurrent();
  });
  get("replay-end").addEventListener("click", () => {
    stopPlayback();
    state.cursor = state.events.length - 1;
    renderCurrent();
  });
  get("replay-play").addEventListener("click", togglePlayback);
  get("replay-range").addEventListener("input", (event) => {
    stopPlayback();
    state.cursor = Number(event.target.value);
    renderCurrent();
  });
  get("evidence-close").addEventListener("click", () => get("evidence-dialog").close());
}

async function initialize() {
  bindControls();
  refreshIcons();
  try {
    const response = await fetch("/api/runs");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    state.runs = payload.runs;
    renderRunList();
    const requestedRun = new URLSearchParams(window.location.search).get("run");
    const initial =
      state.runs.find((run) => run.run_id === requestedRun) ||
      state.runs.find((run) => run.status === "insufficient_evidence") ||
      state.runs[0];
    if (initial) await selectRun(initial.run_id);
  } catch (error) {
    showToast(`服务未就绪：${error.message}`);
  }
}

initialize();
