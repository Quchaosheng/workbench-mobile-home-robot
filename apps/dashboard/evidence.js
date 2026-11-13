/* Evidence-class view. Read-only, and it says so when it cannot read (#581).
 *
 * The whole point of this page is the thing the repository is built around: a run
 * that is a scripted fixture must not read as a released capability. Four rules
 * the rendering keeps, because a table is where an honest report most easily turns
 * into a progress bar:
 *
 *  1. `renders_as_success` comes from the server, not from this file. A template
 *     that decided for itself whether `release_eligible: false` looks green is the
 *     bug this page exists to prevent.
 *  2. `NOT_EXECUTED` and `BLOCKED` are red states, not empty cells. A scenario that
 *     never ran is not a scenario that passed.
 *  3. An axis is drawn from its own boolean. Nothing is inferred from a
 *     neighbouring axis: reaching `gazebo` does not light `physical`.
 *  4. If the report cannot be read, the page shows the unavailable panel and claims
 *     nothing. An empty table would read as "no scenario has an evidence class".
 */

const STATUS_CLASS = {
  SCRIPTED_FIXTURE: "is-not-executed",
  GAZEBO: "is-simulated",
  PHYSICAL: "is-hardware",
  NOT_EXECUTED: "is-not-executed",
  BLOCKED: "is-not-executed",
};

const AXIS_CLASS = "axis-chip";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function badge(text, className) {
  return el("span", `badge ${className || ""}`.trim(), text);
}

function showUnavailable(payload) {
  const line = document.getElementById("report-line");
  line.textContent = "就绪报告不可读，本页不展示任何证据等级";
  line.classList.add("scope-line-error");

  const panel = document.getElementById("unavailable-panel");
  panel.hidden = false;
  document.getElementById("unavailable-message").textContent =
    payload.message || "就绪报告不可用，无法判断证据等级。";
  document.getElementById("unavailable-reason").textContent = payload.reason
    ? `原因：${payload.reason}`
    : "原因未提供。";

  // Every panel that would otherwise show a table is hidden, so a reader cannot
  // mistake an absent row for a passing one.
  for (const id of ["metric-row", "scenario-panel"]) {
    const node = document.getElementById(id);
    if (node) node.hidden = true;
  }
  document.getElementById("vocab-list").replaceChildren();
  document.getElementById("source-note").textContent = "";
}

function renderVocabulary(vocabulary) {
  const list = document.getElementById("vocab-list");
  list.replaceChildren();
  for (const entry of vocabulary) {
    const item = el("li", "vocab-item");
    item.appendChild(el("span", "vocab-index", String(entry.index)));
    item.appendChild(el("span", "vocab-name", entry.label));
    item.appendChild(el("span", "cell-code", entry.name));
    list.appendChild(item);
  }
}

function renderScenarios(scenarios) {
  const body = document.getElementById("scenario-body");
  body.replaceChildren();
  for (const row of scenarios) {
    const tr = el("tr");
    // The server decides success styling. This only reads the decision.
    if (!row.renders_as_success) tr.classList.add("is-not-success");

    const identity = el("td", "cell-id", row.identity);
    identity.dataset.label = "场景";
    tr.appendChild(identity);

    const status = el("td");
    status.dataset.label = "执行状态";
    status.appendChild(badge(row.execution_status_label, STATUS_CLASS[row.execution_status]));
    status.appendChild(el("span", "cell-code cell-code-block", row.execution_status));
    tr.appendChild(status);

    const axes = el("td");
    axes.dataset.label = "到达的轴";
    for (const name of Object.keys(row.evidence_classes)) {
      const axis = row.evidence_classes[name];
      const chip = el("span", `${AXIS_CLASS} ${axis.reached ? "is-reached" : "is-absent"}`, axis.label);
      // An absent axis is labelled as absent rather than omitted: "did not reach
      // gazebo" is information, and a blank cell loses it.
      chip.title = axis.reached ? `到达 ${name}` : `未到达 ${name}`;
      axes.appendChild(chip);
    }
    tr.appendChild(axes);

    const eligible = el("td");
    eligible.dataset.label = "可发布";
    eligible.appendChild(
      badge(row.release_eligible ? "release_eligible: true" : "release_eligible: false", row.release_eligible ? "is-confirmed" : "is-not-executed"),
    );
    tr.appendChild(eligible);

    body.appendChild(tr);
  }
}

function renderSummary(payload) {
  const summary = payload.summary || {};
  document.getElementById("scenario-count").textContent = String(summary.scenarios ?? 0);
  document.getElementById("eligible-count").textContent = String(summary.release_eligible ?? 0);
  document.getElementById("eligible-foot").textContent =
    summary.release_eligible === 0 ? "没有任何场景可发布" : "release_eligible 为真";

  const gazebo = payload.scenarios.filter((row) => row.evidence_classes.gazebo?.reached).length;
  document.getElementById("gazebo-count").textContent = String(gazebo);

  const line = document.getElementById("report-line");
  line.textContent = payload.no_release_eligible_scenario
    ? `共 ${summary.scenarios} 个场景，其中 0 个可发布：全部停留在脚本夹具轴。`
    : `共 ${summary.scenarios} 个场景，其中 ${summary.release_eligible} 个可发布。`;

  const note = document.getElementById("source-note");
  const generated = payload.generated_at ? `生成于 ${payload.generated_at}` : "生成时间未知";
  const commit = payload.source_commit ? `源提交 ${payload.source_commit}` : "源提交未知";
  note.textContent = `${generated} · ${commit} · 版本 ${payload.report_version || "未知"} · ${payload.edit_policy || ""}`;
}

async function load() {
  let response;
  try {
    response = await fetch("/api/v1/readiness", { headers: { Accept: "application/json" } });
  } catch (error) {
    showUnavailable({ message: "无法连接后端，就绪报告不可读。", reason: String(error) });
    return;
  }
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    showUnavailable({ message: "后端返回的不是 JSON，就绪报告不可读。", reason: String(error) });
    return;
  }
  if (payload.available === false) {
    showUnavailable(payload);
    return;
  }
  renderSummary(payload);
  renderVocabulary(payload.vocabulary || []);
  renderScenarios(payload.scenarios || []);
}

load();
