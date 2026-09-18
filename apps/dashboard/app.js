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
  monitoring: null,
  monitoringRequest: null,
  monitoringTimer: null,
  monitoringGeneration: 0,
  monitoringBackoff: null,
  monitoringAlertKey: null,
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

const viewOrder = ["overview", "monitoring", "replay"];

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

const monitoringCards = [
  { id: "safety", label: "安全", matches: (domain) => domain === "safety" },
  { id: "power", label: "电源", matches: (domain) => domain === "power" },
  { id: "can", label: "CAN 通信", matches: (domain) => domain === "communication" },
  { id: "compute", label: "计算", matches: (domain, name) => domain === "compute" && !name.startsWith("compute.disk_") },
  { id: "storage", label: "存储", matches: (domain, name) => name.startsWith("compute.disk_") },
  { id: "nav", label: "定位", matches: (domain, name) => name.startsWith("nav.") },
  { id: "motion", label: "运动", matches: (domain, name) => name.startsWith("motion.") },
  { id: "perception", label: "感知", matches: (domain, name) => name.startsWith("perception.") },
  { id: "task", label: "任务", matches: (domain, name) => domain === "task" || name.startsWith("task.") },
];

const monitoringStatusLabels = {
  healthy: "正常",
  degraded: "降级",
  fault: "故障",
  unknown: "未知",
  unavailable: "不可用",
};

const monitoringSeverityLabels = { info: "提示", warning: "警告", critical: "严重" };

const monitoringAlertStateLabels = { active: "进行中", cleared: "已清除" };

const monitoringConditionLabels = {
  estop_unavailable: "急停通道不可用",
  estop_disagreement: "急停通道不一致",
  watchdog_loss: "看门狗失联",
  bms_fault: "电池管理故障",
  contactor_denied: "接触器未许可",
  can_bus_off: "CAN 总线关闭",
  can_link_loss: "CAN 链路中断",
  controller_fault: "控制器故障",
  stop_fault: "STOP 路径故障",
  localization_stale: "定位不可用",
  perception_stale: "感知数据陈旧",
  event_store_integrity: "事件库完整性失败",
  backend_unavailable: "后端不可用",
  disk_pressure: "磁盘空间不足",
  source_missing: "数据源未上报",
  source_stale: "数据源陈旧",
  source_fault: "数据源冲突",
};

// One bounded refresh interval. A failed refresh backs off instead of hammering
// an unavailable backend, and the interval never grows without a ceiling.
const MONITORING_REFRESH_MS = 5000;
const MONITORING_MAX_BACKOFF_MS = 60000;
const MONITORING_MAX_TREND_ROWS = 20;

// The most severe status wins. The order is a contract, not a preference: a
// fault outranks "we do not know", which outranks a degraded value.
const monitoringSeverityOrder = { fault: 0, unknown: 1, degraded: 2, healthy: 3 };
const monitoringAlertSeverityOrder = { critical: 0, warning: 1, info: 2 };

function monitoringWorst(statuses) {
  let worst = null;
  for (const status of statuses) {
    if (worst === null || (monitoringSeverityOrder[status] ?? 9) < (monitoringSeverityOrder[worst] ?? 9)) worst = status;
  }
  return worst ?? "unknown";
}

function monitoringMetricStatus(metric) {
  // A value the robot never reported is not a healthy `false` and not a zero.
  // Missing and stale inputs are "unknown" until a fresh source replaces them.
  if (!metric || typeof metric !== "object") return "unknown";
  if (metric.missing || metric.stale) return "unknown";
  if (metric.state === "conflict" || metric.state === "fault") return "fault";
  if (metric.state === "degraded") return "degraded";
  if (metric.value === null || metric.value === undefined) return "unknown";
  return "healthy";
}

function monitoringMetricText(metric) {
  if (!metric || typeof metric !== "object") return "未知";
  if (metric.missing) return "未上报";
  if (metric.state === "conflict") return "来源冲突";
  if (metric.stale) return "数据陈旧";
  const value = metric.value;
  if (value === null || value === undefined) return "未知";
  if (typeof value === "boolean") return value ? "正常" : "异常";
  if (typeof value !== "number") return String(value);
  const rendered = Number.isInteger(value) ? String(value) : value.toFixed(2);
  const unit = metric.unit && metric.unit !== "bool" ? ` ${metric.unit}` : "";
  return `${rendered}${unit}`;
}

function monitoringMetricFreshness(metric) {
  if (!metric || typeof metric !== "object") return "无时间戳";
  if (metric.missing) return "无观测时间";
  const age = metric.age_s;
  if (typeof age !== "number" || !Number.isFinite(age)) return "无时间戳";
  if (metric.stale) return `${age.toFixed(1)}s 前 · 已过期`;
  return `${age.toFixed(1)}s 前`;
}

function monitoringDomainMap(payload) {
  const domains = payload?.current?.domains;
  return domains && typeof domains === "object" ? domains : {};
}

// The backend alert rules are authoritative: a metric can have a fresh value
// that still violates a configured threshold (a disk at 0 free bytes is fresh
// and alerting). A card therefore defers to an active alert naming one of its
// metrics, so the view can never show 正常 next to an active warning.
const monitoringAlertStatus = { critical: "fault", warning: "degraded", info: "degraded" };

function monitoringAlertStatusByMetric(payload) {
  const active = monitoringAlertsFor(payload);
  const statuses = new Map();
  for (const alert of active) {
    if (!alert || typeof alert.metric !== "string") continue;
    const status = monitoringAlertStatus[alert.severity] || "degraded";
    const current = statuses.get(alert.metric);
    if (current === undefined || (monitoringSeverityOrder[status] ?? 9) < (monitoringSeverityOrder[current] ?? 9)) {
      statuses.set(alert.metric, status);
    }
  }
  return statuses;
}

function monitoringCardsFor(payload) {
  const domains = monitoringDomainMap(payload);
  const alertStatus = monitoringAlertStatusByMetric(payload);
  const flat = [];
  for (const [domain, health] of Object.entries(domains)) {
    const metrics = Array.isArray(health?.metrics) ? health.metrics : [];
    for (const metric of metrics) {
      if (metric && typeof metric.name === "string") flat.push({ domain, metric });
    }
  }
  return monitoringCards.map((card) => {
    const matched = flat.filter((entry) => card.matches(entry.domain, entry.metric.name));
    const entryStatus = (metric) => {
      const declared = monitoringMetricStatus(metric);
      const alerted = alertStatus.get(metric.name);
      if (alerted === undefined) return declared;
      return monitoringWorst([declared, alerted]);
    };
    const statuses = matched.map((entry) => entryStatus(entry.metric));
    return {
      id: card.id,
      label: card.label,
      status: matched.length ? monitoringWorst(statuses) : "unknown",
      metrics: matched.map((entry) => ({
        name: entry.metric.name,
        domain: entry.domain,
        status: entryStatus(entry.metric),
        text: monitoringMetricText(entry.metric),
        source: entry.metric.source || entry.metric.expected_source || null,
        source_status: entry.metric.source_status || null,
        freshness: monitoringMetricFreshness(entry.metric),
      })),
    };
  });
}

function monitoringAlertsFor(payload) {
  const active = payload?.alerts?.active;
  const list = Array.isArray(active) ? active.filter((alert) => alert && typeof alert === "object") : [];
  return [...list].sort((left, right) => {
    const bySeverity =
      (monitoringAlertSeverityOrder[left.severity] ?? 9) - (monitoringAlertSeverityOrder[right.severity] ?? 9);
    if (bySeverity !== 0) return bySeverity;
    return (left.first_seen_at ?? 0) - (right.first_seen_at ?? 0);
  });
}

function monitoringAlertLabel(alert) {
  return monitoringConditionLabels[alert?.condition] || alert?.condition || "未知告警";
}

// A stable identity for the alert set, so an accessible live region is updated
// only when the alerts actually change rather than on every poll.
function monitoringAlertSignature(alerts) {
  return alerts
    .map((alert) => `${alert.alert_id || alert.condition}:${alert.severity}:${alert.count}`)
    .join("|");
}

function monitoringTrendRows(history) {
  const snapshots = history?.snapshots;
  const list = Array.isArray(snapshots) ? snapshots : [];
  return list.slice(-MONITORING_MAX_TREND_ROWS).map((snapshot) => ({
    collected_at: snapshot.collected_at,
    overall: monitoringStatusLabels[snapshot.overall] ? snapshot.overall : "unknown",
    domains: snapshot.domains && typeof snapshot.domains === "object" ? snapshot.domains : {},
  }));
}

// The view state is derived from the last payload *or* the last failure. A
// failure clears availability, so a stale green card can never remain on screen.
function monitoringViewState(payload, failure) {
  if (failure) {
    return { available: false, status: "unavailable", source: null, reason: failure, alerts: [], cards: [] };
  }
  if (!payload || typeof payload !== "object") {
    return { available: false, status: "unavailable", source: null, reason: "no_payload", alerts: [], cards: [] };
  }
  const status = monitoringStatusLabels[payload.status] ? payload.status : "unknown";
  return {
    available: true,
    status,
    source: typeof payload.source === "string" ? payload.source : null,
    readOnly: payload.read_only === true,
    reason: typeof payload.reason === "string" ? payload.reason : null,
    cards: monitoringCardsFor(payload),
    alerts: monitoringAlertsFor(payload),
  };
}

// This repository ships a read-only fixture health document. The badge states
// that plainly so a viewer cannot mistake a scripted value for a physical sensor.
function monitoringSourceLabel(view) {
  if (!view.available) return "监控数据不可用";
  const source = view.source ? `数据源：${view.source}` : "数据源未知";
  return `只读 · ${source} · 仿真夹具，未连接物理传感器`;
}

function monitoringStatusText(status) {
  return monitoringStatusLabels[status] || "未知";
}

function renderMonitoringCards(view) {
  const grid = get("monitoring-cards");
  if (!view.available) {
    grid.innerHTML = `
      <div class="monitoring-unavailable" role="status">
        <i data-lucide="plug-zap"></i>
        <div>
          <strong>监控数据不可用</strong>
          <p>无法读取 /api/v1/health。此处不显示缓存的健康状态，避免把过期结果当成当前状态。</p>
        </div>
      </div>`;
    return;
  }
  grid.innerHTML = view.cards
    .map(
      (card) => `
      <section class="monitoring-card monitoring-${card.status}" aria-label="${escapeHtml(card.label)}">
        <div class="monitoring-card-head">
          <h3>${escapeHtml(card.label)}</h3>
          <span class="monitoring-status">${escapeHtml(monitoringStatusText(card.status))}</span>
        </div>
        ${
          card.metrics.length
            ? `<dl class="monitoring-metrics">${card.metrics
                .map(
                  (metric) => `
            <div class="monitoring-metric monitoring-${metric.status}">
              <dt>${escapeHtml(metric.name)}</dt>
              <dd>
                <strong>${escapeHtml(metric.text)}</strong>
                <small>${escapeHtml(metric.source || "来源未知")} · ${escapeHtml(metric.freshness)}</small>
              </dd>
            </div>`,
                )
                .join("")}</dl>`
            : `<p class="monitoring-empty">未上报该域的任何指标。</p>`
        }
      </section>`,
    )
    .join("");
}

function renderMonitoringAlerts(view) {
  const panel = get("monitoring-alerts");
  const alerts = view.alerts || [];
  const live = get("monitoring-live");
  // The live region speaks only when the alert set actually changes, so a
  // five-second poll does not repeat the same announcement to a screen reader.
  const signature = monitoringAlertSignature(alerts);
  if (signature !== state.monitoringAlertKey) {
    live.textContent = alerts.length
      ? `当前有 ${alerts.length} 条活动告警，最高等级 ${monitoringSeverityLabels[alerts[0].severity] || alerts[0].severity}：${monitoringAlertLabel(alerts[0])}`
      : "当前没有活动告警";
    state.monitoringAlertKey = signature;
  }
  if (!view.available) {
    panel.innerHTML = "";
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  if (!alerts.length) {
    panel.innerHTML = `<p class="monitoring-empty">当前没有活动告警。</p>`;
    return;
  }
  panel.innerHTML = `
    <ul class="monitoring-alert-table" aria-label="活动告警">
      ${alerts
        .map(
          (alert) => `
        <li class="monitoring-alert monitoring-alert-${escapeHtml(alert.severity)}">
          <span class="monitoring-alert-severity">${escapeHtml(monitoringSeverityLabels[alert.severity] || alert.severity)}</span>
          <strong>${escapeHtml(monitoringAlertLabel(alert))}</strong>
          <span class="monitoring-alert-metric">${escapeHtml(alert.metric || "--")}</span>
          <span class="monitoring-alert-count">×${escapeHtml(String(alert.count ?? 0))}</span>
          <small>${escapeHtml(alert.summary || "")} · ${escapeHtml(alert.evidence_ref || "")}</small>
        </li>`,
        )
        .join("")}
    </div>`;
}

function renderMonitoringTrend(view) {
  const panel = get("monitoring-trend");
  const rows = view.trend || [];
  if (!view.available || !rows.length) {
    panel.innerHTML = "";
    panel.hidden = true;
    return;
  }
  const domains = [...new Set(rows.flatMap((row) => Object.keys(row.domains)))].sort();
  panel.hidden = false;
  panel.innerHTML = `
    <table class="monitoring-trend-table">
      <caption>最近 ${rows.length} 个快照（<code>collected_at</code>，秒）</caption>
      <thead><tr><th scope="col">时间</th><th scope="col">总体</th>${domains
        .map((domain) => `<th scope="col">${escapeHtml(domain)}</th>`)
        .join("")}</tr></thead>
      <tbody>
        ${rows
          .map(
            (row) => `<tr>
          <td>${escapeHtml(String(row.collected_at))}</td>
          <td class="monitoring-${escapeHtml(row.overall)}">${escapeHtml(monitoringStatusText(row.overall))}</td>
          ${domains
            .map((domain) => {
              const value = row.domains[domain] || "unknown";
              return `<td class="monitoring-${escapeHtml(value)}">${escapeHtml(monitoringStatusText(value))}</td>`;
            })
            .join("")}
        </tr>`,
          )
          .join("")}
      </tbody>
    </table>`;
}

function renderMonitoring() {
  // `view` is the derived projection; `state.monitoring*` keeps the raw payload
  // so a later render can reuse it without re-fetching.
  const view = monitoringViewState(state.monitoring, state.monitoringFailure);
  view.trend = state.monitoring?.trend || [];
  const overall = get("monitoring-overall");
  overall.className = `monitoring-overall monitoring-${view.status}`;
  overall.textContent = monitoringStatusText(view.status);
  get("monitoring-source").textContent = monitoringSourceLabel(view);
  renderMonitoringCards(view);
  renderMonitoringAlerts(view);
  renderMonitoringTrend(view);
  refreshIcons();
}

function scheduleMonitoring(delay) {
  clearTimeout(state.monitoringTimer);
  state.monitoringTimer = setTimeout(refreshMonitoring, delay);
}

async function refreshMonitoring() {
  state.monitoringRequest?.abort();
  const controller = new AbortController();
  const generation = ++state.monitoringGeneration;
  state.monitoringRequest = controller;
  try {
    const [health, history] = await Promise.all([
      fetch("/api/v1/health", { signal: controller.signal }),
      fetch("/api/v1/health/history", { signal: controller.signal }),
    ]);
    if (!health.ok) throw new Error(`HTTP ${health.status}`);
    const payload = await health.json();
    if (generation !== state.monitoringGeneration) return;
    if (history.ok) {
      const historyPayload = await history.json();
      payload.trend = monitoringTrendRows(historyPayload);
    }
    state.monitoring = payload;
    state.monitoringFailure = null;
    state.monitoringBackoff = null;
    renderMonitoring();
    if (!get("monitoring-view").hidden) scheduleMonitoring(MONITORING_REFRESH_MS);
  } catch (error) {
    if (error.name === "AbortError" || generation !== state.monitoringGeneration) return;
    // A failed refresh clears the cards rather than leaving a stale green
    // status on screen, and the next attempt backs off.
    state.monitoring = null;
    state.monitoringFailure = error.message || "unavailable";
    renderMonitoring();
    state.monitoringBackoff = Math.min(
      (state.monitoringBackoff || MONITORING_REFRESH_MS) * 2,
      MONITORING_MAX_BACKOFF_MS,
    );
    if (!get("monitoring-view").hidden) scheduleMonitoring(state.monitoringBackoff);
  } finally {
    if (generation === state.monitoringGeneration) state.monitoringRequest = null;
  }
}

// The retry decision is explicit and testable: after a failure the next attempt
// is scheduled from the backoff, regardless of whether a payload was retained.
function monitoringRetryDelay(view, monitoring, backoff) {
  if (view.available) return null;
  if (!monitoring && !backoff) return MONITORING_REFRESH_MS;
  return backoff || MONITORING_REFRESH_MS;
}

function startMonitoring() {
  const view = monitoringViewState(state.monitoring, state.monitoringFailure);
  if (state.monitoring || state.monitoringFailure) {
    renderMonitoring();
    const delay = monitoringRetryDelay(view, state.monitoring, state.monitoringBackoff);
    if (delay !== null) scheduleMonitoring(delay);
    return;
  }
  refreshMonitoring();
}

function stopMonitoring() {
  clearTimeout(state.monitoringTimer);
  state.monitoringTimer = null;
  state.monitoringRequest?.abort();
  state.monitoringRequest = null;
}

function setView(view, focusTab = false) {
  const overview = view === "overview";
  const monitoring = view === "monitoring";
  const replay = view === "replay";
  get("overview-view").hidden = !overview;
  get("monitoring-view").hidden = !monitoring;
  get("replay-view").hidden = !replay;
  get("overview-view").setAttribute("aria-hidden", String(!overview));
  get("monitoring-view").setAttribute("aria-hidden", String(!monitoring));
  get("replay-view").setAttribute("aria-hidden", String(!replay));
  document.querySelectorAll(".view-tab").forEach((tab) => {
    const selected = tab.dataset.view === view;
    tab.classList.toggle("is-active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected && focusTab) tab.focus();
  });
  if (replay && state.cursor < 0) state.cursor = state.events.length - 1;
  // Monitoring polls only while it is the visible view and the tab is visible,
  // so a hidden page or another tab costs no requests.
  if (monitoring) startMonitoring();
  else stopMonitoring();
  if (!monitoring) renderCurrent();
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
  document.addEventListener("visibilitychange", () => {
    const monitoringVisible = !get("monitoring-view").hidden;
    if (document.hidden) stopMonitoring();
    else if (monitoringVisible) startMonitoring();
  });
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
